from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import request


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "github_durable_gateway.py"
SPEC = importlib.util.spec_from_file_location("github_durable_gateway", MODULE_PATH)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


WEBHOOK_PATH = pathlib.Path(__file__).resolve().parents[1] / "github_docs_impact_webhook.py"
common = type(sys)("github_intake_common")
common.load_effective_config = lambda: {}
common.verify_github_signature = lambda _secret, _payload, _signature: False
sys.modules[common.__name__] = common
webhook_spec = importlib.util.spec_from_file_location("github_docs_impact_webhook", WEBHOOK_PATH)
assert webhook_spec and webhook_spec.loader
webhook = importlib.util.module_from_spec(webhook_spec)
sys.modules[webhook_spec.name] = webhook
webhook_spec.loader.exec_module(webhook)


def _post_webhook(body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    server = webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v0/github/webhook"
        message = request.Request(endpoint, data=body, headers=headers, method="POST")
        with request.urlopen(message) as response:
            return response.status, json.loads(response.read())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


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
                self.assertTrue(store.retry(job.id, job.lease_token, "city unavailable", now))
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
            self.assertTrue(store.complete(job.id, job.lease_token))
            self.assertIsNone(store.claim(job.lease_until))

    def test_stale_worker_cannot_mutate_reclaimed_lease(self) -> None:
        """Catches a late worker clearing the lease owned by a newer claim."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            store.enqueue_delivery("delivery-fenced", "pull_request", b"{}", 100)
            stale = store.claim(100)
            self.assertIsNotNone(stale)
            assert stale is not None
            current = store.claim(stale.lease_until)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertNotEqual(stale.lease_token, current.lease_token)

            self.assertFalse(store.retry(stale.id, stale.lease_token, "late worker", current.lease_until))
            self.assertFalse(store.complete(stale.id, stale.lease_token))
            self.assertIsNone(store.claim(current.lease_until - 1))


class GatewayIngressWorkerTests(unittest.TestCase):
    def test_verified_delivery_is_durable_before_the_handler_returns_accepted(self) -> None:
        """Catches a 202 acknowledgement emitted before the inbox transaction commits."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            payload = b'{"action":"opened"}'
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-http-123",
                "X-Hub-Signature-256": "sha256=verified",
            }
            with mock.patch.object(webhook, "GatewayStore", return_value=store), mock.patch.object(webhook, "_app", return_value={"webhook_secret": "secret"}), mock.patch.object(webhook.common, "verify_github_signature", return_value=True):
                status, response = _post_webhook(payload, headers)

            self.assertEqual(status, 202)
            self.assertEqual(response, {"accepted": True})
            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT delivery_id, payload FROM deliveries").fetchall(), [("delivery-http-123", payload)])

    def test_intake_exception_releases_the_existing_delivery_for_retry(self) -> None:
        """Catches worker failures losing work or inserting a duplicate delivery."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            self.assertTrue(store.enqueue_delivery("delivery-worker-123", "pull_request", b'{"action":"opened"}', 100))

            with mock.patch.object(gateway, "_run_adapter", side_effect=ValueError("GitHub unavailable")):
                self.assertFalse(gateway.process_one(store, 100))

            self.assertFalse(store.enqueue_delivery("delivery-worker-123", "pull_request", b'{"action":"opened"}', 101))
            with sqlite3.connect(root / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT status, attempts, lease_until FROM jobs").fetchone(), ("pending", 1, None))

    def test_successful_worker_advances_one_persisted_delivery_through_reconciliation(self) -> None:
        """Catches lifecycle stages being skipped or detached from their delivery bytes."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            payload = b'{"installation":{"id":17},"action":"opened"}'
            store.enqueue_delivery("delivery-stages-123", "pull_request", payload, 100)
            seen: list[tuple[str, bytes]] = []

            def run_adapter(job: gateway.Job) -> None:
                seen.append((job.kind, job.payload))

            with mock.patch.object(gateway, "_run_adapter", side_effect=run_adapter):
                for now in range(100, 104):
                    self.assertTrue(gateway.process_one(store, now))

            self.assertEqual(seen, [("intake", payload), ("dispatch", payload), ("harvest", payload), ("project", payload)])
            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT kind, status FROM jobs ORDER BY id").fetchall(), [("intake", "complete"), ("dispatch", "complete"), ("harvest", "complete"), ("project", "complete")])


if __name__ == "__main__":
    unittest.main()
