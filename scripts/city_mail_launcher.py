#!/usr/bin/env python3
"""Pure protocol helpers for the private City Mail launcher."""

from __future__ import annotations

import json
import os
import re
import tempfile
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable


PINNED_BASE = re.compile(r"^[0-9a-f]{40,64}$")
AUTHORIZATION_TYPE = "gc.intake.start-authorized.v1"


def validate_authorization(message: dict[str, object]) -> dict[str, object]:
    if message.get("type") != AUTHORIZATION_TYPE:
        raise ValueError("authorization type")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("authorization payload")
    if not isinstance(payload.get("id"), str) or not payload["id"]:
        raise ValueError("authorization id")
    issue = payload.get("issue")
    if not isinstance(issue, dict) or not isinstance(issue.get("repository"), str) or not isinstance(issue.get("number"), int):
        raise ValueError("authorization issue")
    plan = payload.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("id"), str) or not plan["id"]:
        raise ValueError("authorization plan")
    pinned_base = payload.get("pinned_base")
    if not isinstance(pinned_base, str) or not PINNED_BASE.fullmatch(pinned_base):
        raise ValueError("pinned_base")
    return message


def binding_for(message: dict[str, object], run_id: str) -> dict[str, object]:
    validate_authorization(message)
    if not run_id:
        raise ValueError("run_id")
    payload = message["payload"]
    assert isinstance(payload, dict)
    authorization_id = payload["id"]
    return {
        "event_id": f"gc.run.binding.{authorization_id}",
        "type": "gc.run.binding.v1",
        "issue": payload["issue"],
        "thread_id": message.get("thread_id"),
        "payload": {
            "issue": payload["issue"],
            "plan": payload["plan"],
            "authorization_id": authorization_id,
            "pinned_base": payload["pinned_base"],
            "run_id": run_id,
        },
    }


def request_for(message: dict[str, object]) -> dict[str, object]:
    validate_authorization(message)
    payload = message["payload"]
    assert isinstance(payload, dict)
    return {"authorization_id": payload["id"], "formula": "superpowers-build", "target": "mayor", "message": message}


def record_before_ack(ledger_path: str | Path, authorization_id: str, run_id: str, acknowledge: Callable[[], None]) -> tuple[str, bool]:
    path = Path(ledger_path)
    try:
        ledger = json.loads(path.read_text()) if path.exists() and path.stat().st_size else {}
    except json.JSONDecodeError as error:
        raise ValueError("launcher ledger is invalid") from error
    completed = ledger.setdefault("completed", {})
    if not isinstance(completed, dict):
        raise ValueError("launcher ledger completed map is invalid")
    existing = completed.get(authorization_id)
    created = existing is None
    resolved_run = run_id if created else existing
    if not isinstance(resolved_run, str) or not resolved_run:
        raise ValueError("launcher ledger run id is invalid")
    if created:
        completed[authorization_id] = resolved_run
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(ledger, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    acknowledge()
    return resolved_run, created


def _secrets(path: str) -> dict[str, str]:
    values = dict(line.rstrip("\n").split("=", 1) for line in Path(path).read_text().splitlines() if "=" in line)
    required = {"MCP_AGENT_MAIL_BEARER_TOKEN", "MCP_AGENT_MAIL_REGISTRATION_TOKEN", "MCP_AGENT_MAIL_PROJECT_KEY", "MCP_AGENT_MAIL_AGENT_NAME"}
    missing = sorted(key for key in required if not values.get(key) or values[key] == "bootstrap-required")
    if missing:
        raise ValueError("missing launcher Mail credentials: " + ", ".join(missing))
    return values


def _rpc(url: str, bearer: str, tool: str, arguments: dict[str, object]) -> object:
    request = urllib.request.Request(url, data=json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":tool,"arguments":arguments}}, separators=(",", ":")).encode(), headers={"Authorization": "Bearer " + bearer, "Content-Type":"application/json", "Accept":"application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read())
    if payload.get("error") or payload.get("result", {}).get("isError"):
        raise ValueError(f"Mail {tool} failed")
    result = payload["result"]
    if result.get("structuredContent") is not None:
        return result["structuredContent"].get("result", result["structuredContent"])
    content = result.get("content", [])
    return json.loads(content[0]["text"]) if content and content[0].get("text") else result


def _existing_run(path: str, authorization_id: str) -> str | None:
    candidate = Path(path)
    if not candidate.exists() or not candidate.stat().st_size:
        return None
    completed = json.loads(candidate.read_text()).get("completed", {})
    value = completed.get(authorization_id)
    return value if isinstance(value, str) and value else None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def city_worker() -> None:
    queue = Path(os.environ["CITY_MAIL_LAUNCHER_QUEUE"])
    while True:
        for request_path in sorted((queue / "requests").glob("*.json")):
            response_path = queue / "responses" / request_path.name
            if response_path.exists():
                continue
            try:
                request = json.loads(request_path.read_text())
                if request.get("formula") != "superpowers-build" or request.get("target") != "mayor":
                    raise ValueError("unsupported City launch request")
                message = validate_authorization(request["message"])
                payload = message["payload"]
                assert isinstance(payload, dict)
                issue = payload["issue"]
                assert isinstance(issue, dict)
                launched = subprocess.run(["gc", "--city", os.environ["CITY_PATH"], "sling", "mayor", f"Gitea intake authorization {payload['id']} for {issue['repository']}#{issue['number']} at {payload['pinned_base']}", "--on", "superpowers-build", "--json"], check=True, text=True, capture_output=True, timeout=120)
                _write_json(response_path, {"run_id": json.loads(launched.stdout)["bead_id"]})
            except (KeyError, ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
                continue
        time.sleep(1)


def serve() -> None:
    secret = _secrets(os.environ["CITY_MAIL_LAUNCHER_SECRET_FILE"])
    state = os.environ["CITY_MAIL_LAUNCHER_STATE"]
    bridge = os.environ["CITY_MAIL_LAUNCHER_BRIDGE_IDENTITY"]
    url = os.environ["CITY_MAIL_LAUNCHER_MAIL_URL"]
    while True:
        try:
            inbox = _rpc(url, secret["MCP_AGENT_MAIL_BEARER_TOKEN"], "fetch_inbox", {"project_key":secret["MCP_AGENT_MAIL_PROJECT_KEY"], "agent_name":secret["MCP_AGENT_MAIL_AGENT_NAME"], "registration_token":secret["MCP_AGENT_MAIL_REGISTRATION_TOKEN"], "limit":100})
            for envelope in inbox if isinstance(inbox, list) else []:
                if not str(envelope.get("subject", "")).startswith("gc.intake.start-authorized."):
                    continue
                try:
                    message = validate_authorization(json.loads(envelope["body_md"]))
                    payload = message["payload"]
                    assert isinstance(payload, dict)
                    auth_id = str(payload["id"])
                    run_id = _existing_run(state, auth_id)
                    if not run_id:
                        queue = Path(os.environ["CITY_MAIL_LAUNCHER_QUEUE"])
                        request_path = queue / "requests" / f"{auth_id}.json"
                        response_path = queue / "responses" / f"{auth_id}.json"
                        if not request_path.exists():
                            _write_json(request_path, request_for(message))
                        if not response_path.exists():
                            continue
                        run_id = json.loads(response_path.read_text())["run_id"]
                    binding = binding_for(message, run_id)
                    _rpc(url, secret["MCP_AGENT_MAIL_BEARER_TOKEN"], "send_message", {"project_key":secret["MCP_AGENT_MAIL_PROJECT_KEY"], "sender_name":secret["MCP_AGENT_MAIL_AGENT_NAME"], "sender_token":secret["MCP_AGENT_MAIL_REGISTRATION_TOKEN"], "to":[bridge], "subject":f"gc.run.binding.{auth_id}", "body_md":json.dumps(binding, separators=(",", ":")), "thread_id":message.get("thread_id"), "topic":f"gc-binding-{auth_id}", "ack_required":True})
                    record_before_ack(state, auth_id, run_id, lambda: _rpc(url, secret["MCP_AGENT_MAIL_BEARER_TOKEN"], "acknowledge_message", {"project_key":secret["MCP_AGENT_MAIL_PROJECT_KEY"], "agent_name":secret["MCP_AGENT_MAIL_AGENT_NAME"], "registration_token":secret["MCP_AGENT_MAIL_REGISTRATION_TOKEN"], "message_id":envelope["id"]}))
                except (KeyError, ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError):
                    continue
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(2)


if __name__ == "__main__":
    city_worker() if os.environ.get("CITY_MAIL_LOCAL_LAUNCHER_WORKER") == "true" else serve()
