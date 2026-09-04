from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib import error, request


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


def _request_json(path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, server: object | None = None) -> tuple[int, dict[str, object]]:
    httpd = server or webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
    assert isinstance(httpd, webhook.ThreadingHTTPServer)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{httpd.server_port}{path}"
        message = request.Request(endpoint, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
        try:
            with request.urlopen(message) as response:
                return response.status, json.loads(response.read())
        except error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


def _post_webhook(body: bytes, headers: dict[str, str], *, server: object | None = None) -> tuple[int, dict[str, object]]:
    return _request_json("/v0/github/webhook", body=body, headers=headers, server=server)


def _valid_payload(*, action: str = "opened", sha: str = "a" * 40) -> bytes:
    return json.dumps(
        {
            "action": action,
            "installation": {"id": 23},
            "repository": {"id": 17, "full_name": "example/docs"},
            "pull_request": {
                "number": 9,
                "base": {"ref": "main", "sha": "b" * 40, "repo": {"id": 17, "full_name": "example/docs"}},
                "head": {"ref": "feature/docs", "sha": sha, "repo": {"id": 17, "full_name": "example/docs"}},
            },
        },
        sort_keys=True,
    ).encode()


class GatewayStoreTests(unittest.TestCase):
    def test_webhook_publisher_loop_uses_only_the_app_side_outbox_boundary(self) -> None:
        calls: list[bool] = []
        publisher = type(sys)("github_docs_impact_compose_adapter")
        publisher.publish_direct_child_results = lambda: calls.append(True)

        with mock.patch.dict(sys.modules, {"github_docs_impact_compose_adapter": publisher}), mock.patch.object(webhook.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                webhook._direct_result_publisher_loop()

        self.assertEqual(calls, [True])

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

    def test_terminal_input_failure_is_excluded_from_claims_and_queue_status(self) -> None:
        """Catches a legacy poison delivery remaining runnable forever."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            store.enqueue_delivery("delivery-poison", "pull_request", b'{"action":"opened"}', 100)

            self.assertFalse(gateway.process_one(store, 100, clock=lambda: 101))
            self.assertIsNone(store.claim(1_000))
            self.assertEqual(store.queue_status(1_000), {"runnable_jobs": 0, "oldest_runnable_job": None})
            with sqlite3.connect(root / "gateway.sqlite") as connection:
                self.assertEqual(
                    connection.execute("SELECT status, attempts, completed_at FROM jobs").fetchone(),
                    ("failed", 0, 101),
                )

    def test_legacy_delivery_with_non_scalar_installation_id_is_terminal(self) -> None:
        """Catches legacy malformed installation IDs being retried by a worker."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            document = json.loads(_valid_payload())
            document["installation"]["id"] = [23]
            store.enqueue_delivery("delivery-legacy-installation-type", "pull_request", json.dumps(document).encode(), 100)

            with mock.patch.object(gateway, "_run_adapter") as run_adapter:
                self.assertFalse(gateway.process_one(store, 100, clock=lambda: 101))
            run_adapter.assert_not_called()
            with sqlite3.connect(root / "gateway.sqlite") as connection:
                self.assertEqual(
                    connection.execute("SELECT status, attempts, completed_at FROM jobs").fetchone(),
                    ("failed", 0, 101),
                )

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
            payload = _valid_payload()
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

    def test_signed_malformed_delivery_is_rejected_without_persistence_on_replay(self) -> None:
        """Catches an authenticated malformed payload entering an infinite retry loop."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            payload = b'{"action":"opened"}'
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-malformed-123",
                "X-Hub-Signature-256": "sha256=verified",
            }
            with mock.patch.object(webhook, "GatewayStore", return_value=store), mock.patch.object(webhook, "_app", return_value={"webhook_secret": "secret"}), mock.patch.object(webhook.common, "verify_github_signature", return_value=True):
                first = _post_webhook(payload, headers)
                replay = _post_webhook(payload, headers)

            self.assertEqual(first, (400, {"error": "invalid_payload"}))
            self.assertEqual(replay, first)
            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_signed_delivery_with_non_scalar_installation_id_is_rejected_without_persistence_on_replay(self) -> None:
        """Catches a signed list installation ID being coerced into retryable work."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            document = json.loads(_valid_payload())
            document["installation"]["id"] = [23]
            payload = json.dumps(document, sort_keys=True).encode()
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-installation-type-123",
                "X-Hub-Signature-256": "sha256=verified",
            }
            with mock.patch.object(webhook, "GatewayStore", return_value=store), mock.patch.object(webhook, "_app", return_value={"webhook_secret": "secret"}), mock.patch.object(webhook.common, "verify_github_signature", return_value=True):
                first = _post_webhook(payload, headers)
                replay = _post_webhook(payload, headers)

            self.assertEqual(first, (400, {"error": "invalid_payload"}))
            self.assertEqual(replay, first)
            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_health_exposes_queue_and_turns_unhealthy_after_bounded_failed_progress(self) -> None:
        """Catches a live HTTP process hiding a persistently stuck worker."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            store.enqueue_delivery("delivery-health", "pull_request", _valid_payload(), 100)
            worker_health = gateway.WorkerHealth()
            worker_health.started(100)
            server = webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
            server.gateway_store = store
            server.worker_health = worker_health
            server.clock = lambda: 101
            with mock.patch.dict(os.environ, {"GC_GITHUB_GATEWAY_STALL_SECONDS": "30"}, clear=False):
                healthy = _request_json("/healthz", server=server)

                worker_health.failed(102, "City unavailable")
                stalled_server = webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
                stalled_server.gateway_store = store
                stalled_server.worker_health = worker_health
                stalled_server.clock = lambda: 132
                unhealthy = _request_json("/healthz", server=stalled_server)

            self.assertEqual(healthy[0], 200)
            self.assertEqual(healthy[1]["runnable_jobs"], 1)
            self.assertEqual(
                healthy[1]["oldest_runnable_job"],
                {"available_at": 100, "delivery_id": "delivery-health", "id": 1, "kind": "intake"},
            )
            self.assertEqual(healthy[1]["worker"]["running"], True)
            self.assertEqual(unhealthy[0], 503)
            self.assertEqual(unhealthy[1]["status"], "unhealthy")
            self.assertEqual(unhealthy[1]["worker"]["stalled"], True)

    def test_health_is_unhealthy_when_worker_has_exited(self) -> None:
        """Catches a dead daemon worker leaving the ingress health check green."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            worker_health = gateway.WorkerHealth()
            worker_health.started(100)
            worker_health.stopped(101, "database unavailable")
            server = webhook.ThreadingHTTPServer(("127.0.0.1", 0), webhook.Handler)
            server.gateway_store = store
            server.worker_health = worker_health
            server.clock = lambda: 102

            status, response = _request_json("/healthz", server=server)

            self.assertEqual(status, 503)
            self.assertEqual(response["worker"]["running"], False)

    def test_worker_health_marks_one_overlong_attempt_stalled(self) -> None:
        """Catches a hung adapter attempt remaining healthy forever."""
        worker_health = gateway.WorkerHealth()
        worker_health.started(100)
        worker_health.attempt_started(101)

        status = worker_health.snapshot(131, 30)

        self.assertEqual(status["stalled"], True)
        self.assertEqual(status["attempt_started_at"], 101)

    def test_uncaught_worker_error_records_that_the_worker_exited(self) -> None:
        """Catches a worker-loop crash leaving its liveness state running."""
        class BrokenStore:
            def claim(self, _now: int) -> None:
                raise OSError("database unavailable")

        worker_health = gateway.WorkerHealth()
        with self.assertRaisesRegex(OSError, "database unavailable"):
            gateway.worker_loop(BrokenStore(), worker_health, clock=lambda: 100, sleeper=lambda _interval: None)

        status = worker_health.snapshot(101, 30)
        self.assertEqual(status["running"], False)
        self.assertEqual(status["last_error"], "database unavailable")

    def test_intake_exception_releases_the_existing_delivery_for_retry(self) -> None:
        """Catches worker failures losing work or inserting a duplicate delivery."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            payload = _valid_payload()
            self.assertTrue(store.enqueue_delivery("delivery-worker-123", "pull_request", payload, 100))

            with mock.patch.object(gateway, "_run_adapter", side_effect=ValueError("GitHub unavailable")):
                self.assertFalse(gateway.process_one(store, 100, clock=lambda: 150))

            self.assertFalse(store.enqueue_delivery("delivery-worker-123", "pull_request", payload, 101))
            with sqlite3.connect(root / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT status, attempts, available_at, lease_until FROM jobs").fetchone(), ("pending", 1, 152, None))

    def test_adapter_process_has_a_bounded_timeout(self) -> None:
        """A hung GitHub reconciliation cannot monopolize the sole gateway worker."""
        job = gateway.Job(1, "delivery-timeout", "pull_request", _valid_payload(), "dispatch", 0, 0, 1)
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(gateway.subprocess, "run", return_value=completed) as command:
            gateway._run_adapter(job)

        self.assertGreater(command.call_args.kwargs["timeout"], 0)
        self.assertEqual(
            command.call_args.args[0][-2:],
            ["--source-key", "github-pr:17:9:" + "a" * 40],
        )

    def test_empty_reconciliation_keeps_dispatch_retryable(self) -> None:
        """Catches a zero-exit polling pass completing City dispatch before it happens."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            payload = _valid_payload()
            store.enqueue_delivery("delivery-dispatch-123", "pull_request", payload, 100)
            review_root = root / "docs-review"
            environment = {
                "GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(review_root / "assignments"),
                "GC_GITHUB_DOCS_CANDIDATE_DIR": str(review_root / "candidates"),
                "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(review_root),
            }

            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(gateway, "_run_adapter", return_value={}):
                self.assertTrue(gateway.process_one(store, 100))
                self.assertFalse(gateway.process_one(store, 101))

            with sqlite3.connect(root / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT status, attempts, lease_until FROM jobs WHERE kind = 'dispatch'").fetchone(), ("pending", 1, None))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'harvest'").fetchone()[0], 0)

    def test_newer_pr_revision_preempts_old_reconciliation_stages(self) -> None:
        """A fresh PR revision must not sit behind stale harvest or dispatch work."""
        with tempfile.TemporaryDirectory() as temp:
            store = gateway.GatewayStore(pathlib.Path(temp))
            older = _valid_payload(sha="a" * 40)
            newer = _valid_payload(sha="b" * 40)
            store.enqueue_delivery("delivery-older", "pull_request", older, 100)
            first = store.claim(100)
            assert first is not None
            self.assertTrue(store.advance(first.id, first.lease_token, "dispatch", 100))
            dispatch = store.claim(100)
            assert dispatch is not None
            self.assertTrue(store.advance(dispatch.id, dispatch.lease_token, "harvest", 100))
            store.enqueue_delivery("delivery-newer", "pull_request", newer, 101)

            claimed = store.claim(102)

            assert claimed is not None
            self.assertEqual((claimed.delivery_id, claimed.kind), ("delivery-newer", "intake"))
            self.assertTrue(store.advance(claimed.id, claimed.lease_token, "dispatch", 102))
            claimed = store.claim(103)
            assert claimed is not None
            self.assertEqual((claimed.delivery_id, claimed.kind), ("delivery-newer", "dispatch"))

    def test_empty_reconciliation_keeps_harvest_and_project_retryable(self) -> None:
        """Catches later polling stages completing before their durable records exist."""
        for stage in ("harvest", "project"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                store = gateway.GatewayStore(root)
                sha = "a" * 40
                source_key = f"github-pr:17:9:{sha}"
                payload = _valid_payload(sha=sha)
                store.enqueue_delivery(f"delivery-{stage}-123", "pull_request", payload, 100)
                review_root = root / "docs-review"
                environment = {
                    "GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(review_root / "assignments"),
                    "GC_GITHUB_DOCS_CANDIDATE_DIR": str(review_root / "candidates"),
                    "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(review_root),
                }
                intake = store.claim(100)
                assert intake is not None
                self.assertTrue(store.advance(intake.id, intake.lease_token, "dispatch", 100))
                marker = review_root / "dispatch" / "assignment.json"
                marker.parent.mkdir(parents=True)
                marker.write_text(json.dumps({"bead_id": "bead-1", "source_key": source_key, "dispatched": True}))
                dispatch = store.claim(100)
                assert dispatch is not None
                self.assertTrue(store.advance(dispatch.id, dispatch.lease_token, "harvest", 100))
                if stage == "project":
                    candidate = review_root / "candidates" / "assignment.json"
                    candidate.parent.mkdir(parents=True)
                    candidate.write_text(json.dumps({"artifact": {"identity": {"source_key": source_key}}}))
                    harvest = store.claim(100)
                    assert harvest is not None
                    self.assertTrue(store.advance(harvest.id, harvest.lease_token, "project", 100))

                with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(gateway, "_run_adapter", return_value={}):
                    self.assertFalse(gateway.process_one(store, 101))

                with sqlite3.connect(root / "gateway.sqlite") as connection:
                    self.assertEqual(connection.execute("SELECT status, attempts, lease_until FROM jobs WHERE kind = ?", (stage,)).fetchone(), ("pending", 1, None))

    def test_successful_worker_advances_one_persisted_delivery_through_reconciliation(self) -> None:
        """Catches lifecycle stages being skipped or detached from their delivery bytes."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = gateway.GatewayStore(root)
            sha = "a" * 40
            source_key = f"github-pr:17:9:{sha}"
            payload = _valid_payload(sha=sha)
            store.enqueue_delivery("delivery-stages-123", "pull_request", payload, 100)
            seen: list[tuple[str, bytes]] = []
            review_root = root / "docs-review"
            environment = {
                "GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(review_root / "assignments"),
                "GC_GITHUB_DOCS_CANDIDATE_DIR": str(review_root / "candidates"),
                "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(review_root),
            }

            def run_adapter(job: gateway.Job) -> dict[str, object]:
                seen.append((job.kind, job.payload))
                if job.kind == "dispatch":
                    marker = review_root / "dispatch" / "assignment.json"
                    marker.parent.mkdir(parents=True)
                    marker.write_text(json.dumps({"bead_id": "bead-1", "source_key": source_key, "dispatched": True}))
                elif job.kind == "harvest":
                    candidate = review_root / "candidates" / "assignment.json"
                    candidate.parent.mkdir(parents=True)
                    candidate.write_text(json.dumps({"artifact": {"identity": {"source_key": source_key}}}))
                elif job.kind == "project":
                    run = review_root / "runs" / f"{hashlib.sha256(source_key.encode()).hexdigest()}.json"
                    run.parent.mkdir(parents=True)
                    run.write_text(json.dumps({"identity": source_key, "state": "terminal", "pending_actions": []}))
                return {}

            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(gateway, "_run_adapter", side_effect=run_adapter):
                for now in range(100, 104):
                    self.assertTrue(gateway.process_one(store, now))

            self.assertEqual(seen, [("intake", payload), ("dispatch", payload), ("harvest", payload), ("project", payload)])
            with sqlite3.connect(pathlib.Path(temp) / "gateway.sqlite") as connection:
                self.assertEqual(connection.execute("SELECT kind, status FROM jobs ORDER BY id").fetchall(), [("intake", "complete"), ("dispatch", "complete"), ("harvest", "complete"), ("project", "complete")])


if __name__ == "__main__":
    unittest.main()
