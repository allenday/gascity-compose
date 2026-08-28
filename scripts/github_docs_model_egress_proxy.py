#!/usr/bin/env python3
"""Forward reviewer HTTP calls to one configured model endpoint authority."""

from __future__ import annotations

import argparse
import http.client
import os
import socketserver
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class EndpointPolicy:
    """Canonical model authority and base path accepted by the gateway."""

    def __init__(self, endpoint: str) -> None:
        parsed = urllib.parse.urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("model endpoint must be an http(s) URL with one authority and no credentials or query")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("model endpoint port is invalid") from exc
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = port or (443 if parsed.scheme == "https" else 80)
        self.base_path = (parsed.path or "/").rstrip("/") or "/"
        default_port = 443 if self.scheme == "https" else 80
        self.host_header = self.host if self.port == default_port else f"{self.host}:{self.port}"

    def allows_path(self, target: str) -> bool:
        if not target.startswith("/") or target.startswith("//"):
            return False
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return False
        return (
            self.base_path == "/"
            or parsed.path == self.base_path
            or parsed.path.startswith(self.base_path + "/")
        )


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ModelEgressHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    policy: EndpointPolicy

    def do_CONNECT(self) -> None:  # noqa: N802
        self.send_error(403, "CONNECT is not permitted")

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def _request_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("streamed request bodies are not supported")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("request Content-Length is invalid") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds the gateway limit")
        return self.rfile.read(length)

    def _forward(self) -> None:
        if not self.policy.allows_path(self.path):
            self.send_error(403, "request is outside the configured model endpoint")
            return
        try:
            body = self._request_body()
        except ValueError as exc:
            self.send_error(413, str(exc))
            return
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() not in {"host", "content-length"}
        }
        headers["Host"] = self.policy.host_header
        headers["Content-Length"] = str(len(body))
        connection_type: type[http.client.HTTPConnection]
        connection_type = http.client.HTTPSConnection if self.policy.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self.policy.host, self.policy.port, timeout=60)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            upstream = connection.getresponse()
            response = upstream.read(MAX_RESPONSE_BYTES + 1)
            if len(response) > MAX_RESPONSE_BYTES:
                raise ValueError("model response exceeds the gateway limit")
            self.send_response(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length":
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)
        except (OSError, http.client.HTTPException, ValueError):
            self.send_error(502, "configured model endpoint is unavailable")
        finally:
            connection.close()
            self.close_connection = True

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def create_server(endpoint: str, listen_host: str, listen_port: int) -> ThreadingHTTPServer:
    policy = EndpointPolicy(endpoint)
    handler = type("ConfiguredModelEgressHandler", (ModelEgressHandler,), {"policy": policy})
    return ThreadingHTTPServer((listen_host, listen_port), handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("GC_TECHDOCS_MODEL_UPSTREAM_ENDPOINT", ""))
    parser.add_argument("--listen-host", default=os.environ.get("GC_TECHDOCS_MODEL_EGRESS_HOST", "0.0.0.0"))
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("GC_TECHDOCS_MODEL_EGRESS_PORT", "3128")),
    )
    args = parser.parse_args()
    if not args.endpoint:
        parser.error("--endpoint or GC_TECHDOCS_MODEL_UPSTREAM_ENDPOINT is required")
    with create_server(args.endpoint, args.listen_host, args.listen_port) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
