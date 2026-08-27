#!/usr/bin/env python3
"""Private bridge-only proxy for current collaborator permission reads."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, NamedTuple


CANONICAL_ATOM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
PERMISSION_PATH = re.compile(r"^/v1/repos/([^/]+)/([^/]+)/collaborators/([^/]+)/permission$")
KNOWN_PERMISSIONS = frozenset({"read", "triage", "write", "maintain", "admin", "owner"})


class Binding(NamedTuple):
    gitea_token: str
    bearer_token: str
    repository_scopes: frozenset[str]


def canonical_atom(value: str, field: str) -> str:
    atom = value.strip()
    if not CANONICAL_ATOM.fullmatch(atom):
        raise ValueError(f"{field} must be a canonical tracker atom")
    return atom


def canonical_login(value: str) -> str:
    return canonical_atom(value, "login")


def parse_repository_scopes(raw: str) -> frozenset[str]:
    scopes: set[str] = set()
    for entry in raw.split(","):
        scope = entry.strip()
        if not scope:
            continue
        owner, separator, repository = scope.partition("/")
        if not separator:
            raise ValueError("repository scope must be owner/repo")
        scopes.add(f"{canonical_atom(owner, 'owner')}/{canonical_atom(repository, 'repository')}")
    return frozenset(scopes)


def load_binding(secret_file: str, repository_scopes: str = "") -> Binding:
    values: dict[str, str] = {}
    with open(secret_file, encoding="utf-8") as handle:
        for raw_line in handle:
            key, separator, value = raw_line.rstrip("\n").partition("=")
            if separator:
                values[key] = value
    required = {
        "GITEA_INTAKE_PERMISSION_READER_TOKEN",
        "GITEA_INTAKE_PERMISSION_READER_BEARER_TOKEN",
    }
    missing = sorted(key for key in required if not values.get(key) or values[key] == "bootstrap-required")
    if missing:
        raise ValueError("missing permission reader credentials: " + ", ".join(missing))
    scopes = parse_repository_scopes(repository_scopes)
    if not scopes:
        raise ValueError("missing declared intake repository scopes")
    return Binding(
        gitea_token=values["GITEA_INTAKE_PERMISSION_READER_TOKEN"],
        bearer_token=values["GITEA_INTAKE_PERMISSION_READER_BEARER_TOKEN"],
        repository_scopes=scopes,
    )


def normalize_permission(value: str) -> str:
    permission = value.strip().lower()
    if permission not in KNOWN_PERMISSIONS:
        raise ValueError(f"unexpected Gitea permission {value!r}")
    return permission


def allowed_repository(binding: Binding, owner: str, repo: str) -> str:
    repository = f"{canonical_atom(owner, 'owner')}/{canonical_atom(repo, 'repository')}"
    if repository not in binding.repository_scopes:
        raise PermissionError("repository is not declared for intake")
    return repository


def gitea_api_url(base_url: str, *parts: str) -> str:
    path = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", "api/v1/" + path)


def read_repo_owner(gitea_url: str, token: str, owner: str, repo: str, timeout: float) -> str | None:
    request = urllib.request.Request(
        gitea_api_url(gitea_url, "repos", owner, repo),
        headers={"Authorization": "token " + token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    owner_payload = payload.get("owner")
    if not isinstance(owner_payload, dict) or not isinstance(owner_payload.get("login"), str):
        raise ValueError("repository owner payload is invalid")
    return canonical_atom(owner_payload["login"], "owner")


def fetch_permission(binding: Binding, gitea_url: str, owner: str, repo: str, login: str, timeout: float) -> str | None:
    request = urllib.request.Request(
        gitea_api_url(gitea_url, "repos", owner, repo, "collaborators", login, "permission"),
        headers={"Authorization": "token " + binding.gitea_token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            owner_login = read_repo_owner(gitea_url, binding.gitea_token, owner, repo, timeout)
            if owner_login is not None and owner_login.lower() == login.lower():
                return "owner"
            return None
        if error.code == 401:
            raise PermissionError("Gitea rejected the configured reader token") from error
        if error.code == 403:
            raise PermissionError("Gitea denied collaborator permission lookup for the configured reader token") from error
        raise
    if not isinstance(payload, dict) or not isinstance(payload.get("permission"), str):
        raise ValueError("Gitea permission payload is invalid")
    return normalize_permission(payload["permission"])


class ReaderHandler(http.server.BaseHTTPRequestHandler):
    binding: Binding
    gitea_url: str
    upstream_timeout: float

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health/liveness":
            self._send_json(200, {"status": "ok"})
            return
        match = PERMISSION_PATH.fullmatch(self.path)
        if not match:
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer " + self.binding.bearer_token:
            self.send_error(401, "bridge bearer token is required")
            return
        try:
            owner = canonical_atom(match.group(1), "owner")
            repo = canonical_atom(match.group(2), "repository")
            login = canonical_login(match.group(3))
            allowed_repository(self.binding, owner, repo)
            permission = fetch_permission(self.binding, self.gitea_url, owner, repo, login, self.upstream_timeout)
        except ValueError as error:
            self.send_error(400, str(error))
            return
        except PermissionError as error:
            code = 403 if "repository is not declared" in str(error) else 502
            self.send_error(code, str(error))
            return
        except urllib.error.HTTPError as error:
            self.send_error(502, f"Gitea upstream returned HTTP {error.code}")
            return
        except (OSError, urllib.error.URLError) as error:
            self.send_error(502, f"Gitea upstream unavailable: {error}")
            return
        if permission is None:
            self.send_error(404, "collaborator permission not found")
            return
        self._send_json(200, {"permission": permission})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


class ReaderServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default=os.environ.get("GITEA_INTAKE_PERMISSION_READER_ADDR", "0.0.0.0:8080"))
    parser.add_argument("--gitea-url", default=os.environ.get("GITEA_URL", "http://gitea:3000"))
    parser.add_argument(
        "--secret-file",
        default=os.environ.get("GITEA_INTAKE_PERMISSION_READER_SECRET_FILE", "/run/secrets/city-mail/permission-reader.env"),
    )
    parser.add_argument("--upstream-timeout", type=float, default=10.0)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.gitea_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        parser.error("--gitea-url must be an absolute HTTP(S) URL without query or fragment")
    host, separator, port_text = args.listen.rpartition(":")
    if not separator or not host:
        parser.error("--listen must be HOST:PORT")
    binding = load_binding(args.secret_file, os.environ.get("INTAKE_REPOSITORY_SCOPES", ""))
    ReaderHandler.binding = binding
    ReaderHandler.gitea_url = args.gitea_url
    ReaderHandler.upstream_timeout = args.upstream_timeout
    server = ReaderServer((host, int(port_text)), ReaderHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
