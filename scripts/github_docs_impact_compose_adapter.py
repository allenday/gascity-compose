#!/usr/bin/env python3
"""Local Compose adapter for the deployment-neutral docs-impact reviewer.

The GitHub pack deliberately supplies lifecycle and projection primitives, not
an opinionated scheduler.  This adapter is the narrow dogfood binding: the
signed webhook creates a complete immutable assignment and durable run; only
then can it dispatch a City bead.  A separate loop consumes exact candidate
envelopes and reconciles every interrupted run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any


PACK_SCRIPTS = "/opt/gascity-packs/github/scripts"
if PACK_SCRIPTS not in sys.path:
    sys.path.insert(0, PACK_SCRIPTS)

import github_intake_common as common
import github_intake_docs_impact as impact
import github_intake_docs_review_runtime as runtime


def _path_env(name: str) -> pathlib.Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return pathlib.Path(value)


def _atomic_write(path: pathlib.Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = pathlib.Path(handle.name)
    temporary.chmod(mode)
    temporary.replace(path)


def _assignment_bytes(assignment: dict[str, Any]) -> bytes:
    return json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _installation_id(payload: dict[str, Any]) -> str:
    value = (payload.get("installation") or {}).get("id")
    if value is None or not str(value).strip():
        raise ValueError("pull_request webhook has no installation id")
    return str(value)


def _delivery(payload: dict[str, Any]) -> dict[str, Any]:
    repository = payload.get("repository") or {}
    pull = payload.get("pull_request") or {}
    base, head = pull.get("base") or {}, pull.get("head") or {}
    base_repo, head_repo = base.get("repo") or {}, head.get("repo") or {}
    result = {
        "repository_id": str(repository.get("id", "")),
        "repository": str(repository.get("full_name", "")),
        "pr_number": pull.get("number"),
        "head_sha": str(head.get("sha", "")).lower(),
        "base_sha": str(base.get("sha", "")).lower(),
        "base_ref": str(base.get("ref", "")),
        "head_repository_id": str(head_repo.get("id", "")),
        "head_repository": str(head_repo.get("full_name", "")),
    }
    if not all(result[key] for key in ("repository_id", "repository", "head_sha", "base_sha", "base_ref", "head_repository_id", "head_repository")) or type(result["pr_number"]) is not int:
        raise ValueError("pull_request webhook lacks immutable identity")
    if str(base_repo.get("id", "")) != result["repository_id"] or str(base_repo.get("full_name", "")) != result["repository"]:
        raise ValueError("pull_request base repository does not match webhook repository")
    return result


def _gateway(payload: dict[str, Any] | None = None) -> impact.GitHubAppProjectionGateway:
    config = common.load_effective_config()
    app = config.get("app")
    if not isinstance(app, dict):
        raise ValueError("GitHub App config is unavailable")
    installation_id = _installation_id(payload) if payload is not None else str(
        os.environ.get("GITHUB_INSTALLATION_ID", "") or app.get("installation_id", "")
    ).strip()
    if not installation_id:
        raise ValueError("GitHub App installation id is unavailable for reconciliation")
    return impact.GitHubAppProjectionGateway(app, installation_id)


class ComposeAdapter:
    """Combine generic lifecycle actions with the two Compose-owned actions."""

    def __init__(self, store: runtime.FileDocsReviewStore, gateway: impact.GitHubAppProjectionGateway) -> None:
        self.projection = impact.AppProjection(store, gateway)

    def head_is_current(self, run: dict[str, Any]) -> bool:
        return self.projection.head_is_current(run)

    def perform(self, action: str, run: dict[str, Any]) -> None:
        if action == "dispatch":
            _dispatch_city(run)
            return
        self.projection.perform(action, run)


def _dispatch_city(run: dict[str, Any]) -> None:
    """Persist the exact assignment before creating a City task and sling."""
    raw = run.get("assignment_bytes")
    if not isinstance(raw, str):
        raise ValueError("durable run lacks assignment bytes")
    import base64
    assignment_bytes = base64.b64decode(raw, validate=True)
    digest = hashlib.sha256(assignment_bytes).hexdigest()
    review_root = _path_env("GC_GITHUB_DOCS_ASSIGNMENT_DIR")
    assignment_path = review_root / f"{digest}.json"
    if assignment_path.exists() and assignment_path.read_bytes() != assignment_bytes:
        raise ValueError("immutable assignment digest collision")
    if not assignment_path.exists():
        _atomic_write(assignment_path, assignment_bytes, mode=0o400)

    dispatch_marker = review_root.parent / "dispatch" / f"{digest}.json"
    if dispatch_marker.exists():
        return
    identity = run["assignment"]["identity"]
    city = os.environ.get("GC_CITY_ROOT", "").strip()
    target = os.environ.get("GC_CITY_DOCS_REVIEW_TARGET", "github-docs-impact.docs-impact-reviewer").strip()
    if not city or not target:
        raise ValueError("City docs reviewer target is not configured")
    candidate_path = _path_env("GC_GITHUB_DOCS_CANDIDATE_DIR") / f"{digest}.json"
    description = "\n".join((
        "Review the immutable GitHub pull-request documentation assignment.",
        f"Assignment: /var/lib/github-docs-impact/review/assignments/{digest}.json",
        f"Candidate outbox: /var/lib/github-docs-impact/review/candidates/{digest}.json",
        f"Source key: {identity['source_key']}",
        "Return one canonical github-pr-docs-impact-review JSON candidate bound to the assignment.",
        "Do not use GitHub credentials or mutate GitHub.",
    ))
    metadata = json.dumps({"github.docs_review.assignment_sha256": digest, "github.docs_review.candidate_path": str(candidate_path)}, sort_keys=True)
    created = subprocess.run(
        ["gc", "--city", city, "bd", "create", f"Review docs impact for PR #{identity['pr_number']}", "--type", "task", "--priority", "2", "--labels", "github-docs-impact", "--metadata", metadata, "--description", description, "--json"],
        capture_output=True, text=True, check=False,
    )
    if created.returncode:
        raise ValueError(created.stderr.strip() or "City could not create docs review task")
    response = json.loads(created.stdout)
    if isinstance(response, list) and len(response) == 1:
        response = response[0]
    bead_id = str((response or {}).get("id", "")) if isinstance(response, dict) else ""
    if not bead_id:
        raise ValueError("City returned no docs review task id")
    sling = subprocess.run(["gc", "--city", city, "sling", target, bead_id, "--no-convoy", "--no-formula", "--nudge", "--json"], capture_output=True, text=True, check=False)
    if sling.returncode:
        raise ValueError(sling.stderr.strip() or "City could not dispatch docs review task")
    _atomic_write(dispatch_marker, json.dumps({"bead_id": bead_id, "source_key": identity["source_key"]}, sort_keys=True).encode("utf-8"))


def intake(payload: dict[str, Any], token: str, now: float) -> dict[str, Any]:
    delivery = _delivery(payload)
    path = f"/repos/{delivery['repository']}/pulls/{delivery['pr_number']}/files"
    files = common.github_api_paginated_list_request("GET", path, bearer_token=token)
    assignment = runtime.assignment_from_paginated_evidence(delivery, [files])
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    return runtime.intake_delivery(store, assignment, ComposeAdapter(store, _gateway(payload)), now=now)


def candidates(now: float) -> list[dict[str, Any]]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    result: list[dict[str, Any]] = []
    for path in sorted(_path_env("GC_GITHUB_DOCS_CANDIDATE_DIR").glob("*.json")):
        try:
            result.append(runtime.accept_candidate(store, json.loads(path.read_text(encoding="utf-8")), adapter, now=now))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result.append({"accepted": False, "reason": f"invalid candidate {path.name}: {exc}"})
    return result


def reconcile(now: float) -> dict[str, Any]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    return {"candidates": candidates(now), "runs": runtime.reconcile_pending(store, adapter, now=now)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("intake", "reconcile"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--payload-file", default=os.environ.get("GC_GITHUB_EVENT_PAYLOAD_FILE", ""))
    parser.add_argument("--token-env", default="GH_TOKEN")
    args = parser.parse_args()
    if args.once == args.loop:
        parser.error("choose exactly one of --once or --loop")
    payload_file = pathlib.Path(args.payload_file) if args.payload_file else None
    if args.operation == "intake" and (payload_file is None or not payload_file.is_file()):
        parser.error("--payload-file is required for intake")
    payload = json.loads(payload_file.read_text(encoding="utf-8")) if payload_file is not None and payload_file.is_file() else None
    token = os.environ.get(args.token_env, "")
    if args.operation == "intake" and not token:
        parser.error(f"{args.token_env} is required for paginated pull-request evidence")
    interval = max(1.0, float(os.environ.get("GC_GITHUB_DOCS_RECONCILE_SECONDS", "15")))
    while True:
        now = time.time()
        result = intake(payload, token, now) if args.operation == "intake" else reconcile(now)
        print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
