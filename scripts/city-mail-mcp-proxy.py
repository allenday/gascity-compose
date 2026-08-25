#!/usr/bin/env python3
"""Mayor-only Agent Mail MCP proxy with server-side identity injection."""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, NamedTuple


ALLOWED_TOOLS = frozenset(
    {
        "fetch_inbox",
        "fetch_topic",
        "mark_message_read",
        "acknowledge_message",
        "send_message",
        "reply_message",
    }
)
AGENT_TOOLS = frozenset(
    {"fetch_inbox", "fetch_topic", "mark_message_read", "acknowledge_message"}
)
SENDER_TOOLS = frozenset({"send_message", "reply_message"})
COPIED_RESPONSE_HEADERS = frozenset(
    {"content-type", "mcp-session-id", "cache-control", "retry-after"}
)


class Binding(NamedTuple):
    project_key: str
    agent_name: str
    registration_token: str


def rewrite_request(request: Any, binding: Binding) -> Any:
    """Constrain a tools/call to Mayor and inject its credential server-side."""
    if isinstance(request, list):
        return [rewrite_request(item, binding) for item in request]
    if not isinstance(request, dict):
        raise ValueError("JSON-RPC request must be an object or batch")
    rewritten = copy.deepcopy(request)
    if rewritten.get("method") != "tools/call":
        return rewritten
    params = rewritten.get("params")
    if not isinstance(params, dict):
        raise ValueError("tools/call params must be an object")
    tool = params.get("name")
    if tool not in ALLOWED_TOOLS:
        raise ValueError(f"Agent Mail tool {tool!r} is not available to Mayor")
    arguments = params.setdefault("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tools/call arguments must be an object")

    supplied_project = arguments.get("project_key")
    if supplied_project not in (None, binding.project_key):
        raise ValueError("request does not match the bound Mayor project")
    arguments["project_key"] = binding.project_key

    if tool in AGENT_TOOLS:
        supplied_agent = arguments.get("agent_name")
        if supplied_agent not in (None, binding.agent_name):
            raise ValueError("request does not match the bound Mayor identity")
        arguments["agent_name"] = binding.agent_name
        arguments["registration_token"] = binding.registration_token
    elif tool in SENDER_TOOLS:
        supplied_sender = arguments.get("sender_name")
        if supplied_sender not in (None, binding.agent_name):
            raise ValueError("request does not match the bound Mayor identity")
        arguments["sender_name"] = binding.agent_name
        arguments["sender_token"] = binding.registration_token
    return rewritten


def filter_response(response: Any) -> Any:
    """Hide Agent Mail administration and cross-identity tools from Mayor."""
    if isinstance(response, list):
        return [filter_response(item) for item in response]
    if not isinstance(response, dict):
        return response
    filtered = copy.deepcopy(response)
    result = filtered.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        result["tools"] = [
            tool
            for tool in result["tools"]
            if isinstance(tool, dict) and tool.get("name") in ALLOWED_TOOLS
        ]
    if isinstance(result, dict) and isinstance(result.get("capabilities"), dict):
        tools = result["capabilities"].get("tools", {})
        result["capabilities"] = {"tools": tools}
    return filtered


def transform_response_body(body: bytes, content_type: str) -> bytes:
    if not body:
        return body
    if "text/event-stream" in content_type:
        transformed: list[bytes] = []
        for line in body.splitlines(keepends=True):
            if line.startswith(b"data:"):
                prefix, encoded = line.split(b":", 1)
                newline = b"\n" if line.endswith(b"\n") else b""
                try:
                    payload = json.loads(encoded.strip())
                    line = prefix + b": " + json.dumps(filter_response(payload), separators=(",", ":")).encode() + newline
                except (json.JSONDecodeError, TypeError):
                    pass
            transformed.append(line)
        return b"".join(transformed)
    try:
        return json.dumps(filter_response(json.loads(body)), separators=(",", ":")).encode()
    except (json.JSONDecodeError, TypeError):
        return body


def load_binding(secret_file: str) -> tuple[Binding, str]:
    values: dict[str, str] = {}
    with open(secret_file, encoding="utf-8") as handle:
        for raw_line in handle:
            key, separator, value = raw_line.rstrip("\n").partition("=")
            if separator:
                values[key] = value
    required = {
        "MCP_AGENT_MAIL_BEARER_TOKEN",
        "MCP_AGENT_MAIL_REGISTRATION_TOKEN",
        "MCP_AGENT_MAIL_PROJECT_KEY",
        "MCP_AGENT_MAIL_AGENT_NAME",
    }
    missing = sorted(key for key in required if not values.get(key) or values[key] == "bootstrap-required")
    if missing:
        raise ValueError("missing Mayor Mail credentials: " + ", ".join(missing))
    return (
        Binding(
            project_key=values["MCP_AGENT_MAIL_PROJECT_KEY"],
            agent_name=values["MCP_AGENT_MAIL_AGENT_NAME"],
            registration_token=values["MCP_AGENT_MAIL_REGISTRATION_TOKEN"],
        ),
        values["MCP_AGENT_MAIL_BEARER_TOKEN"],
    )


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    binding: Binding
    bearer_token: str
    upstream_url: str
    upstream_timeout: float

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health/liveness":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_error(405, "MCP event streams are not required by this bounded proxy")

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward(None, "DELETE")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            encoded = json.dumps(rewrite_request(request, self.binding), separators=(",", ":")).encode()
        except (ValueError, json.JSONDecodeError) as error:
            request_id = request.get("id") if isinstance(locals().get("request"), dict) else None
            payload = json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(error)}},
                separators=(",", ":"),
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._forward(encoded, "POST")

    def _forward(self, body: bytes | None, method: str) -> None:
        headers = {
            "Authorization": "Bearer " + self.bearer_token,
            "Accept": self.headers.get("Accept", "application/json, text/event-stream"),
            "Content-Type": self.headers.get("Content-Type", "application/json"),
        }
        for name in ("Mcp-Session-Id", "MCP-Protocol-Version", "Last-Event-ID"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        upstream_request = urllib.request.Request(
            self.upstream_url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(upstream_request, timeout=self.upstream_timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            self.send_error(502, f"Agent Mail upstream unavailable: {error}")
            return
        with response:
            response_body = response.read()
            content_type = response.headers.get("Content-Type", "application/json")
            response_body = transform_response_body(response_body, content_type)
            self.send_response(response.status)
            for name, value in response.headers.items():
                if name.lower() in COPIED_RESPONSE_HEADERS:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

    def log_message(self, format: str, *args: Any) -> None:
        # The proxy shares Mayor's terminal; access logs would corrupt the
        # interactive Codex stream. MCP errors are returned in-band instead.
        return


class ProxyServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="127.0.0.1:8767")
    parser.add_argument("--upstream", default=os.environ.get("CITY_MAIL_MCP_UPSTREAM", "http://mcp-agent-mail:8765/mcp"))
    parser.add_argument("--secret-file", default=os.environ.get("CITY_MAIL_MAYOR_SECRET_FILE", "/run/secrets/city-mail/mayor.env"))
    parser.add_argument("--upstream-timeout", type=float, default=60.0)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.upstream)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        parser.error("--upstream must be an absolute HTTP(S) URL without query or fragment")
    host, separator, port_text = args.listen.rpartition(":")
    if not separator or host != "127.0.0.1":
        parser.error("--listen must be a 127.0.0.1:PORT address")
    binding, bearer = load_binding(args.secret_file)
    ProxyHandler.binding = binding
    ProxyHandler.bearer_token = bearer
    ProxyHandler.upstream_url = args.upstream
    ProxyHandler.upstream_timeout = args.upstream_timeout
    server = ProxyServer((host, int(port_text)), ProxyHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
