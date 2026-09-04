from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "github_durable_gateway.py"
SPEC = importlib.util.spec_from_file_location("github_durable_gateway", MODULE_PATH)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class GatewayStoreTests(unittest.TestCase):
    def test_duplicate_delivery_creates_one_delivery_and_one_intake_job(self) -> None:
        """Catches a replay creating duplicate durable intake work."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))

            self.assertTrue(store.enqueue_delivery("delivery-123", "pull_request", b'{"action":"opened"}', 100))
            self.assertFalse(store.enqueue_delivery("delivery-123", "pull_request", b'{"action":"opened"}', 101))

            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'intake'").fetchone()[0], 1)

    def test_expired_lease_is_reclaimable_but_active_lease_is_not(self) -> None:
        """Catches workers concurrently consuming a job before its lease expires."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            store.enqueue_delivery("delivery-456", "pull_request", b"{}", 100)

            first = store.claim(100)
            self.assertIsNotNone(first)
            self.assertIsNone(store.claim(101))
            assert first is not None
            reclaimed = store.claim(first.lease_until)

            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed.id, first.id)

    def test_retry_releases_job_after_bounded_backoff(self) -> None:
        """Catches retry failures either becoming hot loops or permanently stranded."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            store.enqueue_delivery("delivery-789", "pull_request", b"{}", 100)
            now = 100

            for attempt in range(10):
                job = store.claim(now)
                self.assertIsNotNone(job)
                assert job is not None
                store.retry(job.id, "city unavailable", now)
                delay = min(2 ** (attempt + 1), gateway.MAX_RETRY_SECONDS)
                self.assertIsNone(store.claim(now + delay - 1))
                now += delay

            retried = store.claim(now)
            self.assertIsNotNone(retried)
            assert retried is not None
            self.assertEqual(retried.attempts, 10)

    def test_completed_job_is_not_claimed_again(self) -> None:
        """Catches successful work being repeated after a worker releases its lease."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            store.enqueue_delivery("delivery-complete", "pull_request", b"{}", 100)
            job = store.claim(100)

            self.assertIsNotNone(job)
            assert job is not None
            store.complete(job.id)
            self.assertIsNone(store.claim(job.lease_until))


if __name__ == "__main__":
    unittest.main()
