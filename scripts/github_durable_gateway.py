#!/usr/bin/env python3
"""Durable SQLite inbox/outbox for GitHub webhook deliveries."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time


LEASE_SECONDS = 30
MAX_RETRY_SECONDS = 300
ADAPTER_PATH = pathlib.Path(__file__).with_name("github_docs_impact_compose_adapter.py")
NEXT_JOB_KIND = {"intake": "dispatch", "dispatch": "harvest", "harvest": "project", "project": None}


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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT jobs.id, jobs.delivery_id, deliveries.event, deliveries.payload,
                           jobs.kind, jobs.attempts, jobs.lease_token
                    FROM jobs JOIN deliveries USING (delivery_id)
                    WHERE jobs.status != 'complete'
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

    def retry(self, job_id: int, lease_token: int, error: str, now: int) -> bool:
        """Release a failed job after bounded exponential backoff."""
        with self._connect() as connection:
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
        with self._connect() as connection:
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

    document = json.loads(payload)
    installation = (document.get("installation") or {}).get("id") if isinstance(document, dict) else None
    if installation is None:
        raise ValueError("pull_request webhook has no installation id")
    app = common.load_effective_config().get("app")
    if not isinstance(app, dict):
        raise ValueError("GitHub App config is unavailable")
    return common.create_installation_token(app, str(installation))


def _run_adapter(job: Job) -> None:
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


def process_one(store: GatewayStore, now: int) -> bool:
    """Claim and process one job, leaving failures retryable with backoff."""
    job = store.claim(now)
    if job is None:
        return False
    try:
        _run_adapter(job)
    except Exception as exc:
        store.retry(job.id, job.lease_token, str(exc), now)
        return False
    return store.advance(job.id, job.lease_token, NEXT_JOB_KIND[job.kind], now)


def worker_loop(store: GatewayStore | None = None) -> None:
    """Continuously consume persisted jobs; sleep only when none are runnable."""
    gateway = store or GatewayStore()
    interval = max(0.1, float(os.environ.get("GC_GITHUB_GATEWAY_POLL_SECONDS", "1")))
    while True:
        if not process_one(gateway, int(time.time())):
            time.sleep(interval)
