#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "gitea-intake-permission-reader.py"


def load_reader():
    spec = importlib.util.spec_from_file_location("gitea_intake_permission_reader", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PermissionReaderTest(unittest.TestCase):
    def setUp(self):
        self.reader = load_reader()

    def test_load_binding_requires_pat_and_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "permission-reader.env"
            path.write_text("GITEA_INTAKE_PERMISSION_READER_TOKEN=\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing permission reader credentials"):
                self.reader.load_binding(str(path))

    def test_normalize_permission_enforces_known_role_names(self):
        self.assertEqual("write", self.reader.normalize_permission(" Write "))
        with self.assertRaisesRegex(ValueError, "unexpected Gitea permission"):
            self.reader.normalize_permission("superadmin")

    def test_permission_request_requires_declared_repository_and_canonical_login(self):
        binding = self.reader.Binding(gitea_token="token", bearer_token="bearer", repository_scopes=frozenset({"owner/repo"}))
        self.assertEqual("owner/repo", self.reader.allowed_repository(binding, "owner", "repo"))
        with self.assertRaisesRegex(PermissionError, "repository is not declared"):
            self.reader.allowed_repository(binding, "other", "repo")
        with self.assertRaisesRegex(ValueError, "login"):
            self.reader.canonical_login("../bad")

    def test_http_proxy_requires_bearer_and_returns_normalized_permission(self):
        class Upstream(self.reader.http.server.BaseHTTPRequestHandler):
            auth = None
            path_seen = None

            def do_GET(self):  # noqa: N802
                self.__class__.auth = self.headers.get("Authorization")
                self.__class__.path_seen = self.path
                encoded = b'{"permission":"Write"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                return

        try:
            upstream = self.reader.http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        except PermissionError:
            self.skipTest("sandbox forbids loopback listeners")
        self.reader.ReaderHandler.binding = self.reader.Binding(
            gitea_token="admin-read-token",
            bearer_token="bridge-bearer",
            repository_scopes=frozenset({"owner/repo"}),
        )
        self.reader.ReaderHandler.gitea_url = f"http://127.0.0.1:{upstream.server_port}"
        self.reader.ReaderHandler.upstream_timeout = 2
        server = self.reader.ReaderServer(("127.0.0.1", 0), self.reader.ReaderHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        upstream_thread.start()
        server_thread.start()
        try:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/v1/repos/owner/repo/collaborators/human/permission",
                    headers={"Authorization": "Bearer bridge-bearer"},
                ),
                timeout=3,
            )
            self.assertEqual({"permission": "write"}, json.loads(response.read()))
            self.assertEqual("token admin-read-token", Upstream.auth)
            self.assertEqual("/api/v1/repos/owner/repo/collaborators/human/permission", Upstream.path_seen)
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_http_proxy_rejects_unauthenticated_requests(self):
        self.reader.ReaderHandler.binding = self.reader.Binding(
            gitea_token="admin-read-token",
            bearer_token="bridge-bearer",
            repository_scopes=frozenset({"owner/repo"}),
        )
        self.reader.ReaderHandler.gitea_url = "http://127.0.0.1:1"
        self.reader.ReaderHandler.upstream_timeout = 1
        try:
            server = self.reader.ReaderServer(("127.0.0.1", 0), self.reader.ReaderHandler)
        except PermissionError:
            self.skipTest("sandbox forbids loopback listeners")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/v1/repos/owner/repo/collaborators/human/permission"
                    ),
                    timeout=3,
                )
            self.assertEqual(401, error.exception.code)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
