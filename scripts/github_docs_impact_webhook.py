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
from github_durable_gateway import GatewayStore, worker_loop


MAX_PAYLOAD_BYTES = 1_048_576
PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


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
            self._reply(HTTPStatus.OK, {"status": "ok"})
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
            event = self.headers.get("X-GitHub-Event", "")
            document = json.loads(payload)
            if event != "pull_request" or not isinstance(document, dict) or document.get("action") not in PULL_REQUEST_ACTIONS:
                self._reply(HTTPStatus.ACCEPTED, {"accepted": False, "reason": "ignored"})
                return
            delivery_id = self.headers.get("X-GitHub-Delivery", "").strip()
            if not delivery_id:
                raise ValueError("GitHub delivery id is required")
            GatewayStore().enqueue_delivery(delivery_id, event, payload, int(time.time()))
            self._reply(HTTPStatus.ACCEPTED, {"accepted": True})
        except (OSError, ValueError, json.JSONDecodeError):
            # Return a retryable failure without leaking configuration or GitHub
            # response details to the public endpoint.
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "intake_failed"})


def main() -> None:
    host = os.environ.get("GC_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("GC_SERVICE_PORT", "8080"))
    threading.Thread(target=worker_loop, name="github-durable-gateway", daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
