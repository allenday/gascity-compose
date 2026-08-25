import importlib.util
import http.server
import json
import pathlib
import threading
import unittest
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "city-mail-mcp-proxy.py"


def load_proxy():
    spec = importlib.util.spec_from_file_location("city_mail_mcp_proxy", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RewriteRequestTest(unittest.TestCase):
    def setUp(self):
        self.proxy = load_proxy()
        self.binding = self.proxy.Binding(
            project_key="gascity-project",
            agent_name="gas-city-mayor",
            registration_token="mayor-secret",
        )

    def test_fetch_inbox_injects_bound_identity_without_exposing_token_to_codex(self):
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "fetch_inbox", "arguments": {"limit": 20}},
        }

        rewritten = self.proxy.rewrite_request(request, self.binding)

        self.assertEqual("gascity-project", rewritten["params"]["arguments"]["project_key"])
        self.assertEqual("gas-city-mayor", rewritten["params"]["arguments"]["agent_name"])
        self.assertEqual("mayor-secret", rewritten["params"]["arguments"]["registration_token"])
        self.assertNotIn("registration_token", request["params"]["arguments"])

    def test_send_message_injects_sender_token(self):
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "send_message",
                "arguments": {"to": ["gitea-mail-bridge"], "subject": "s", "body_md": "b"},
            },
        }

        rewritten = self.proxy.rewrite_request(request, self.binding)

        self.assertEqual("gas-city-mayor", rewritten["params"]["arguments"]["sender_name"])
        self.assertEqual("mayor-secret", rewritten["params"]["arguments"]["sender_token"])

    def test_rejects_cross_identity_and_unapproved_tools(self):
        wrong_identity = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "fetch_inbox",
                "arguments": {"agent_name": "gas-city-launcher"},
            },
        }
        with self.assertRaisesRegex(ValueError, "bound Mayor identity"):
            self.proxy.rewrite_request(wrong_identity, self.binding)

        register = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "register_agent", "arguments": {}},
        }
        with self.assertRaisesRegex(ValueError, "not available"):
            self.proxy.rewrite_request(register, self.binding)

    def test_rejects_every_non_protocol_and_non_tool_call_method(self):
        for request_id, method in enumerate(
            (
                "resources/list",
                "resources/read",
                "prompts/list",
                "prompts/get",
                "completion/complete",
                "sampling/createMessage",
                "logging/setLevel",
                "roots/list",
                "not/a-real-method",
            ),
            start=20,
        ):
            with self.subTest(method=method):
                request = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": {},
                }
                with self.assertRaisesRegex(ValueError, "not available to Mayor"):
                    self.proxy.rewrite_request(request, self.binding)

    def test_allows_only_required_mcp_session_and_tool_discovery_methods(self):
        requests = (
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            {"jsonrpc": "2.0", "id": 31, "method": "tools/list", "params": {}},
        )

        for request in requests:
            with self.subTest(method=request["method"]):
                self.assertEqual(request, self.proxy.rewrite_request(request, self.binding))

    def test_tools_list_hides_every_non_mayor_operation(self):
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {"name": "fetch_inbox"},
                    {"name": "send_message"},
                    {"name": "register_agent"},
                    {"name": "ensure_project"},
                ]
            },
        }

        filtered = self.proxy.filter_response(response)

        self.assertEqual(["fetch_inbox", "send_message"], [tool["name"] for tool in filtered["result"]["tools"]])

        initialized = self.proxy.filter_response(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}}},
            }
        )
        self.assertEqual({"tools": {"listChanged": False}}, initialized["result"]["capabilities"])

    def test_http_proxy_forwards_bearer_and_injects_identity(self):
        class Upstream(http.server.BaseHTTPRequestHandler):
            request = None
            authorization = None

            def do_POST(self):  # noqa: N802
                length = int(self.headers["Content-Length"])
                self.__class__.request = json.loads(self.rfile.read(length))
                self.__class__.authorization = self.headers.get("Authorization")
                encoded = b'{"jsonrpc":"2.0","id":11,"result":{"ok":true}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format, *args):
                return

        try:
            upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        except PermissionError:
            self.skipTest("sandbox forbids loopback listeners")
        self.proxy.ProxyHandler.binding = self.binding
        self.proxy.ProxyHandler.bearer_token = "server-bearer"
        self.proxy.ProxyHandler.upstream_url = f"http://127.0.0.1:{upstream.server_port}/mcp"
        self.proxy.ProxyHandler.upstream_timeout = 2
        proxy = self.proxy.ProxyServer(("127.0.0.1", 0), self.proxy.ProxyHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        upstream_thread.start()
        proxy_thread.start()
        request = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "acknowledge_message", "arguments": {"message_id": 42}},
        }
        try:
            response = urllib.request.urlopen(
                urllib.request.Request(
                    f"http://127.0.0.1:{proxy.server_port}/mcp",
                    data=json.dumps(request).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=3,
            )
            self.assertEqual({"ok": True}, json.loads(response.read())["result"])
            self.assertEqual("Bearer server-bearer", Upstream.authorization)
            arguments = Upstream.request["params"]["arguments"]
            self.assertEqual("gascity-project", arguments["project_key"])
            self.assertEqual("gas-city-mayor", arguments["agent_name"])
            self.assertEqual("mayor-secret", arguments["registration_token"])
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

if __name__ == "__main__":
    unittest.main()
