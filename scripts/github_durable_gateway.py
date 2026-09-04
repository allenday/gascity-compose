#!/usr/bin/env python3
"""Durable SQLite inbox/outbox for GitHub webhook deliveries."""

from __future__ import annotations

from dataclasses import dataclass
import os
import pathlib
import sqlite3


LEASE_SECONDS = 30
MAX_RETRY_SECONDS = 300


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
