#!/usr/bin/env python3
"""Durable SQLite inbox/outbox for GitHub webhook deliveries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing


LEASE_SECONDS = 30
MAX_RETRY_SECONDS = 300
WORKER_STALL_SECONDS = 600
ADAPTER_PATH = pathlib.Path(__file__).with_name("github_docs_impact_compose_adapter.py")
NEXT_JOB_KIND = {"intake": "dispatch", "dispatch": "harvest", "harvest": "project", "project": None}
PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
GIT_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}")


class InputValidationError(ValueError):
    """A terminal delivery error that cannot succeed when retried."""


class WorkerHealth:
    """Thread-safe worker liveness and bounded progress history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._started_at: int | None = None
        self._last_progress_at: int | None = None
        self._attempt_started_at: int | None = None
        self._failure_started_at: int | None = None
        self._last_failure_at: int | None = None
        self._consecutive_failures = 0
        self._last_error: str | None = None

    def started(self, now: int) -> None:
        with self._lock:
            self._running = True
            self._started_at = now
            self._last_progress_at = now
            self._attempt_started_at = None
            self._failure_started_at = None
            self._last_failure_at = None
            self._consecutive_failures = 0
            self._last_error = None

    def progressed(self, now: int) -> None:
        with self._lock:
            self._last_progress_at = now
            self._attempt_started_at = None
            self._failure_started_at = None
            self._last_failure_at = None
            self._consecutive_failures = 0
            self._last_error = None

    def attempt_started(self, now: int) -> None:
        with self._lock:
            self._attempt_started_at = now

    def failed(self, now: int, error: str) -> None:
        with self._lock:
            self._attempt_started_at = None
            if self._failure_started_at is None:
                self._failure_started_at = now
            self._last_failure_at = now
            self._consecutive_failures += 1
            self._last_error = error

    def stopped(self, now: int, error: str | None = None) -> None:
        with self._lock:
            self._running = False
            self._attempt_started_at = None
            self._last_failure_at = now
            if error:
                self._last_error = error

    def snapshot(self, now: int, stall_seconds: int) -> dict[str, object]:
        with self._lock:
            stalled = (
                self._running
                and (
                    (self._attempt_started_at is not None and now - self._attempt_started_at >= stall_seconds)
                    or (self._failure_started_at is not None and now - self._failure_started_at >= stall_seconds)
                )
            )
            return {
                "running": self._running,
                "stalled": stalled,
                "started_at": self._started_at,
                "last_progress_at": self._last_progress_at,
                "attempt_started_at": self._attempt_started_at,
                "last_failure_at": self._last_failure_at,
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
            }


@dataclass(frozen=True)
class Job:
    id: int
    delivery_id: str
    event: str
    payload: bytes
    kind: str
    attempts: int
    lease_until: int
    lease_token: int


def validate_pull_request_payload(payload: bytes) -> dict[str, object]:
    """Return a complete immutable PR delivery or raise a terminal error."""
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputValidationError("pull_request webhook is not valid JSON") from exc
    if not isinstance(document, dict):
        raise InputValidationError("pull_request webhook must be an object")
    if document.get("action") not in PULL_REQUEST_ACTIONS:
        raise InputValidationError("pull_request webhook action is unsupported")

    installation = document.get("installation")
    repository = document.get("repository")
    pull_request = document.get("pull_request")
    base = pull_request.get("base") if isinstance(pull_request, dict) else None
    head = pull_request.get("head") if isinstance(pull_request, dict) else None
    base_repository = base.get("repo") if isinstance(base, dict) else None
    head_repository = head.get("repo") if isinstance(head, dict) else None

    installation_id = installation.get("id") if isinstance(installation, dict) else None
    repository_id = repository.get("id") if isinstance(repository, dict) else None
    repository_name = repository.get("full_name") if isinstance(repository, dict) else None
    number = pull_request.get("number") if isinstance(pull_request, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    head_ref = head.get("ref") if isinstance(head, dict) else None
    base_sha = base.get("sha") if isinstance(base, dict) else None
    base_ref = base.get("ref") if isinstance(base, dict) else None
    head_repository_id = head_repository.get("id") if isinstance(head_repository, dict) else None
    head_repository_name = head_repository.get("full_name") if isinstance(head_repository, dict) else None
    base_repository_id = base_repository.get("id") if isinstance(base_repository, dict) else None
    base_repository_name = base_repository.get("full_name") if isinstance(base_repository, dict) else None

    def positive_integer(value: object) -> bool:
        return type(value) is int and value > 0

    def nonempty_string(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def repository_full_name(value: object) -> bool:
        return nonempty_string(value) and value.count("/") == 1 and all(part.strip() for part in value.split("/"))

    if not positive_integer(installation_id):
        raise InputValidationError("pull_request webhook has an invalid installation id")
    if (
        not positive_integer(repository_id)
        or not repository_full_name(repository_name)
        or not positive_integer(number)
        or not isinstance(head_sha, str)
        or GIT_COMMIT_SHA.fullmatch(head_sha) is None
        or not nonempty_string(head_ref)
        or not isinstance(base_sha, str)
        or GIT_COMMIT_SHA.fullmatch(base_sha) is None
        or not nonempty_string(base_ref)
        or not positive_integer(head_repository_id)
        or not repository_full_name(head_repository_name)
        or not positive_integer(base_repository_id)
        or not repository_full_name(base_repository_name)
    ):
        raise InputValidationError("pull_request webhook lacks immutable identity")
    if base_repository_id != repository_id or base_repository_name != repository_name:
        raise InputValidationError("pull_request base repository does not match webhook repository")
    return document


class GatewayStore:
    """Persist deliveries and lease their idempotent lifecycle jobs."""

    def __init__(self, state_root: str | pathlib.Path | None = None) -> None:
        self.state_root = pathlib.Path(state_root or os.environ.get("GC_SERVICE_STATE_ROOT", "/var/lib/github-intake"))
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.path = self.state_root / "gateway.sqlite"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    received_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at INTEGER NOT NULL,
                    lease_until INTEGER,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    completed_at INTEGER,
                    UNIQUE(delivery_id, kind)
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "lease_token" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN lease_token INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS jobs_runnable ON jobs(status, available_at, lease_until)")

    def enqueue_delivery(self, delivery_id: str, event: str, payload: bytes, now: int) -> bool:
        """Store a verified delivery and its initial job exactly once."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO deliveries(delivery_id, event, payload, received_at) VALUES (?, ?, ?, ?)",
                    (delivery_id, event, payload, now),
                ).rowcount == 1
                if inserted:
                    connection.execute(
                        "INSERT INTO jobs(delivery_id, kind, available_at) VALUES (?, 'intake', ?)",
                        (delivery_id, now),
                    )
                connection.execute("COMMIT")
                return inserted
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def advance(self, job_id: int, lease_token: int, next_kind: str | None, now: int) -> bool:
        """Complete a leased job and durably schedule its successor."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT delivery_id FROM jobs WHERE id = ? AND status = 'leased' AND lease_token = ?",
                    (job_id, lease_token),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    "UPDATE jobs SET status = 'complete', lease_until = NULL, completed_at = ? WHERE id = ?",
                    (now, job_id),
                )
                if next_kind is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO jobs(delivery_id, kind, available_at) VALUES (?, ?, ?)",
                        (row[0], next_kind, now),
                    )
                connection.execute("COMMIT")
                return True
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def claim(self, now: int) -> Job | None:
        """Lease one runnable job, reclaiming any lease that has expired."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT jobs.id, jobs.delivery_id, deliveries.event, deliveries.payload,
                           jobs.kind, jobs.attempts, jobs.lease_token
                    FROM jobs JOIN deliveries USING (delivery_id)
                    WHERE jobs.status IN ('pending', 'leased')
                      AND jobs.available_at <= ?
                      AND (jobs.lease_until IS NULL OR jobs.lease_until <= ?)
                    ORDER BY jobs.available_at, jobs.id
                    LIMIT 1
                    """,
                    (now, now),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                lease_until = now + LEASE_SECONDS
                connection.execute(
                    "UPDATE jobs SET status = 'leased', lease_until = ?, lease_token = lease_token + 1 WHERE id = ?",
                    (lease_until, row[0]),
                )
                connection.execute("COMMIT")
                return Job(*row[:-1], lease_until, int(row[-1]) + 1)
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def fail_input(self, job_id: int, lease_token: int, error: str, now: int) -> bool:
        """Terminalize malformed persisted input so it cannot be retried."""
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', lease_until = NULL, last_error = ?, completed_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (error, now, job_id, lease_token),
            ).rowcount == 1

    def queue_status(self, now: int) -> dict[str, object]:
        """Report runnable depth and the oldest runnable job."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            predicate = """
                status IN ('pending', 'leased')
                  AND available_at <= ?
                  AND (lease_until IS NULL OR lease_until <= ?)
            """
            count = connection.execute(
                f"SELECT COUNT(*) FROM jobs WHERE {predicate}",
                (now, now),
            ).fetchone()[0]
            oldest = connection.execute(
                """
                SELECT id, delivery_id, kind, available_at
                FROM jobs
                WHERE status IN ('pending', 'leased')
                  AND available_at <= ?
                  AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY available_at, id
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            connection.execute("COMMIT")
        return {
            "runnable_jobs": count,
            "oldest_runnable_job": (
                {
                    "id": oldest[0],
                    "delivery_id": oldest[1],
                    "kind": oldest[2],
                    "available_at": oldest[3],
                }
                if oldest is not None
                else None
            ),
        }

    def retry(self, job_id: int, lease_token: int, error: str, now: int) -> bool:
        """Release a failed job after bounded exponential backoff."""
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is not None:
                    attempts = int(row[0]) + 1
                    delay = min(2**attempts, MAX_RETRY_SECONDS)
                    changed = connection.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending', attempts = ?, available_at = ?, lease_until = NULL, last_error = ?
                        WHERE id = ? AND status = 'leased' AND lease_token = ?
                        """,
                        (attempts, now + delay, error, job_id, lease_token),
                    ).rowcount == 1
                else:
                    changed = False
                connection.execute("COMMIT")
                return changed
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def complete(self, job_id: int, lease_token: int) -> bool:
        """Mark successfully processed work terminal so it cannot be reclaimed."""
        with closing(self._connect()) as connection:
            return connection.execute(
                """
                UPDATE jobs
                SET status = 'complete', lease_until = NULL,
                    completed_at = CAST(strftime('%s', 'now') AS INTEGER)
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (job_id, lease_token),
            ).rowcount == 1


def _installation_token(payload: bytes) -> str:
    pack_scripts = os.environ.get("GC_GITHUB_PACK_SCRIPTS", "/opt/gascity-packs/github/scripts")
    if pack_scripts not in sys.path:
        sys.path.insert(0, pack_scripts)
    import github_intake_common as common

    document = validate_pull_request_payload(payload)
    installation = (document.get("installation") or {}).get("id")
    app = common.load_effective_config().get("app")
    if not isinstance(app, dict):
        raise ValueError("GitHub App config is unavailable")
    return common.create_installation_token(app, str(installation))


def _run_adapter(job: Job) -> dict[str, object]:
    """Run one existing Compose adapter boundary for persisted gateway work."""
    environment = dict(os.environ)
    command = [sys.executable, str(ADAPTER_PATH)]
    payload_path: pathlib.Path | None = None
    if job.kind == "intake":
        environment["GH_TOKEN"] = _installation_token(job.payload)
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as handle:
            handle.write(job.payload)
            handle.flush()
            payload_path = pathlib.Path(handle.name)
        command.extend(("intake", "--once", "--payload-file", str(payload_path)))
    elif job.kind in {"dispatch", "harvest", "project"}:
        command.extend(("reconcile", "--once"))
    else:
        raise ValueError(f"unknown gateway job kind: {job.kind}")
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=environment)
    finally:
        if payload_path is not None:
            payload_path.unlink(missing_ok=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"docs-impact {job.kind} failed")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"docs-impact {job.kind} returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise ValueError(f"docs-impact {job.kind} returned a non-object response")
    return response


def _source_key(payload: bytes) -> str:
    document = validate_pull_request_payload(payload)
    repository = document["repository"]
    pull_request = document["pull_request"]
    head = pull_request["head"]
    repository_id = str(repository["id"])
    number = pull_request["number"]
    head_sha = str(head["sha"]).lower()
    return f"github-pr:{repository_id}:{number}:{head_sha}"


def _matching_marker(directory: pathlib.Path, source_key: str) -> bool:
    for path in directory.glob("*.json"):
        try:
            marker = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(marker, dict) and marker.get("source_key") == source_key and marker.get("dispatched") is True:
            return True
    return False


def _has_candidate(candidate_root: pathlib.Path, source_key: str) -> bool:
    for path in candidate_root.glob("*.json"):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
            identity = artifact.get("identity") if isinstance(artifact, dict) else None
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(identity, dict) and identity.get("source_key") == source_key:
            return True
    return False


def _projected(review_root: pathlib.Path, source_key: str) -> bool:
    path = review_root / "runs" / f"{hashlib.sha256(source_key.encode()).hexdigest()}.json"
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(run, dict)
        and run.get("identity") == source_key
        and run.get("state") in {"terminal", "stale", "journey-pending"}
        and run.get("pending_actions") == []
    )


def _stage_completed(job: Job) -> bool:
    """Require durable evidence rather than treating one poll as completion."""
    if job.kind == "intake":
        return True
    source_key = _source_key(job.payload)
    assignment_root = pathlib.Path(os.environ["GC_GITHUB_DOCS_ASSIGNMENT_DIR"])
    if job.kind == "dispatch":
        return _matching_marker(assignment_root.parent / "dispatch", source_key)
    if job.kind == "harvest":
        return _has_candidate(pathlib.Path(os.environ["GC_GITHUB_DOCS_CANDIDATE_DIR"]), source_key)
    if job.kind == "project":
        return _projected(pathlib.Path(os.environ["GC_GITHUB_DOCS_REVIEW_RUNS_DIR"]), source_key)
    raise ValueError(f"unknown gateway job kind: {job.kind}")


def process_one(
    store: GatewayStore,
    now: int,
    *,
    clock: Callable[[], float] = time.time,
    health: WorkerHealth | None = None,
) -> bool:
    """Claim and process one job, leaving failures retryable with backoff."""
    job = store.claim(now)
    if job is None:
        return False
    try:
        if job.event != "pull_request":
            raise InputValidationError("unsupported persisted GitHub event")
        validate_pull_request_payload(job.payload)
    except InputValidationError as exc:
        terminal_at = int(clock())
        terminalized = store.fail_input(job.id, job.lease_token, str(exc), terminal_at)
        if terminalized and health is not None:
            health.progressed(terminal_at)
        return False
    try:
        if health is not None:
            health.attempt_started(now)
        _run_adapter(job)
        if not _stage_completed(job):
            raise ValueError(f"durable {job.kind} predicate is not met")
    except Exception as exc:
        failed_at = int(clock())
        retried = store.retry(job.id, job.lease_token, str(exc), failed_at)
        if retried and health is not None:
            health.failed(failed_at, str(exc))
        return False
    advanced = store.advance(job.id, job.lease_token, NEXT_JOB_KIND[job.kind], now)
    if advanced and health is not None:
        health.progressed(now)
    return advanced


def worker_loop(
    store: GatewayStore | None = None,
    health: WorkerHealth | None = None,
    *,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Continuously consume persisted jobs; sleep only when none are runnable."""
    gateway = store or GatewayStore()
    worker_health = health or WorkerHealth()
    interval = max(0.1, float(os.environ.get("GC_GITHUB_GATEWAY_POLL_SECONDS", "1")))
    worker_health.started(int(clock()))
    failure: str | None = None
    try:
        while True:
            now = int(clock())
            if not process_one(gateway, now, clock=clock, health=worker_health):
                sleeper(interval)
    except BaseException as exc:
        failure = str(exc) or type(exc).__name__
        raise
    finally:
        worker_health.stopped(int(clock()), failure)
