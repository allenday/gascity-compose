from __future__ import annotations

import importlib.util
import pathlib
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[2]
PROXY_PATH = ROOT / "scripts" / "github_docs_model_egress_proxy.py"


def load_proxy_module():
    spec = importlib.util.spec_from_file_location("github_docs_model_egress_proxy", PROXY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load model egress proxy")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpstreamHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, bytes]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).requests.append((self.path, self.headers.get("Host", ""), body))
        response = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def raw_request(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)


class ModelEgressProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.proxy = load_proxy_module()

    def setUp(self) -> None:
        UpstreamHandler.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.upstream_thread.start()
        endpoint = f"http://127.0.0.1:{self.upstream.server_port}/v1"
        self.gateway = self.proxy.create_server(endpoint, "127.0.0.1", 0)
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.gateway_thread.start()

    def tearDown(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def test_origin_form_request_always_reaches_only_configured_model_authority(self) -> None:
        body = b'{"model":"terra"}'
        response = raw_request(
            self.gateway.server_address[1],
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: api.github.com\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body,
        )

        self.assertIn(b"200 OK", response)
        self.assertEqual(UpstreamHandler.requests, [
            ("/v1/chat/completions", f"127.0.0.1:{self.upstream.server_port}", body)
        ])

    def test_absolute_form_and_connect_cannot_turn_gateway_into_open_proxy(self) -> None:
        attempts = (
            b"GET http://api.github.com/repos HTTP/1.1\r\nHost: api.github.com\r\nConnection: close\r\n\r\n",
            b"CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\nConnection: close\r\n\r\n",
        )
        for request in attempts:
            with self.subTest(request=request.split(b" ", 1)[0]):
                response = raw_request(self.gateway.server_address[1], request)
                self.assertTrue(response.startswith(b"HTTP/1.1 403 "), response)
        self.assertEqual(UpstreamHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
