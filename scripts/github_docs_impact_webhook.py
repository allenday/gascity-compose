#!/usr/bin/env python3
"""Minimal signed GitHub webhook ingress for the Compose docs-review binding."""

from __future__ import annotations

import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys

PACK_SCRIPTS = os.environ.get("GC_GITHUB_PACK_SCRIPTS", "/opt/gascity-packs/github/scripts")
if PACK_SCRIPTS not in sys.path:
    sys.path.insert(0, PACK_SCRIPTS)

import github_intake_common as common
from github_durable_gateway import (
    GatewayStore,
    InputValidationError,
    PULL_REQUEST_ACTIONS,
    WORKER_STALL_SECONDS,
    WorkerHealth,
    validate_pull_request_payload,
    worker_loop,
)


MAX_PAYLOAD_BYTES = 1_048_576


def _direct_result_publisher_loop() -> None:
    """Publish City outbox records only from the credentialed App service."""
    from github_docs_impact_compose_adapter import publish_direct_child_results

    interval = max(1.0, float(os.environ.get("GC_GITHUB_DOCS_RECONCILE_SECONDS", "15")))
    while True:
        try:
            publish_direct_child_results()
        except (OSError, ValueError):
            # The durable outbox remains for the next App-side attempt.
            pass
        time.sleep(interval)


def _app() -> dict[str, object]:
    value = common.load_effective_config().get("app")
    if not isinstance(value, dict):
        raise ValueError("GitHub App config is unavailable")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "GasCityDocsWebhook/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _reply(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            try:
                now = int(getattr(self.server, "clock", time.time)())
                store = getattr(self.server, "gateway_store", None) or GatewayStore()
                worker_health = getattr(self.server, "worker_health", None)
                if worker_health is None:
                    worker_health = WorkerHealth()
                try:
                    stall_seconds = max(1, int(float(os.environ.get("GC_GITHUB_GATEWAY_STALL_SECONDS", str(WORKER_STALL_SECONDS)))))
                except ValueError:
                    stall_seconds = WORKER_STALL_SECONDS
                worker = worker_health.snapshot(now, stall_seconds)
                payload = {
                    "status": "ok" if worker["running"] and not worker["stalled"] else "unhealthy",
                    **store.queue_status(now),
                    "worker": worker,
                }
                status = HTTPStatus.OK if payload["status"] == "ok" else HTTPStatus.SERVICE_UNAVAILABLE
                self._reply(status, payload)
            except Exception:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "unhealthy", "error": "status_unavailable"})
            return
        self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v0/github/webhook":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_PAYLOAD_BYTES:
            self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "payload_too_large"})
            return
        payload = self.rfile.read(length)
        try:
            app = _app()
            secret = str(app.get("webhook_secret", ""))
            signature = self.headers.get("X-Hub-Signature-256", "")
            if not secret or not common.verify_github_signature(secret, payload, signature):
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "invalid_signature"})
                return
        except (OSError, ValueError):
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "intake_failed"})
            return
        try:
            event = self.headers.get("X-GitHub-Event", "")
            document = json.loads(payload)
            if event != "pull_request" or not isinstance(document, dict) or document.get("action") not in PULL_REQUEST_ACTIONS:
                self._reply(HTTPStatus.ACCEPTED, {"accepted": False, "reason": "ignored"})
                return
            delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
            if not delivery_id:
                raise InputValidationError("GitHub delivery id is required")
            validate_pull_request_payload(payload)
        except (InputValidationError, UnicodeDecodeError, json.JSONDecodeError):
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid_payload"})
            return
        try:
            store = getattr(self.server, "gateway_store", None) or GatewayStore()
            clock = getattr(self.server, "clock", time.time)
            store.enqueue_delivery(delivery_id, event, payload, int(clock()))
            self._reply(HTTPStatus.ACCEPTED, {"accepted": True})
        except Exception:
            # Return a retryable failure without leaking configuration or GitHub
            # response details to the public endpoint.
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "intake_failed"})


def main() -> None:
    host = os.environ.get("GC_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("GC_SERVICE_PORT", "8080"))
    store = GatewayStore()
    worker_health = WorkerHealth()
    server = ThreadingHTTPServer((host, port), Handler)
    server.gateway_store = store
    server.worker_health = worker_health
    server.clock = time.time
    threading.Thread(target=worker_loop, args=(store, worker_health), name="github-durable-gateway", daemon=True).start()
    threading.Thread(target=_direct_result_publisher_loop, name="github-direct-result-publisher", daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
