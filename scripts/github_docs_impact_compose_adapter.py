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


PACK_SCRIPTS = os.environ.get("GC_GITHUB_PACK_SCRIPTS", "/opt/gascity-packs/github/scripts")
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


def _final_assistant_document(transcript: dict[str, Any]) -> dict[str, Any] | None:
    """Return only an exact final assistant JSON object, never scraped prose."""
    entries = transcript.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("role") != "assistant":
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            continue
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return None
        return document if isinstance(document, dict) else None
    return None


def _candidate_from_transcript(raw_assignment: bytes, transcript: dict[str, Any]) -> dict[str, Any] | None:
    """Bind one exact City final response to the immutable assignment."""
    artifact = _final_assistant_document(transcript)
    if artifact is None:
        return None
    envelope = {
        "schema_version": 1,
        "snapshot_sha256": hashlib.sha256(raw_assignment).hexdigest(),
        "artifact": artifact,
    }
    try:
        import github_intake_docs_patch_worker as worker
        return worker.validate_final_candidate(raw_assignment, envelope)
    except ValueError:
        return None


def _inconclusive_from_invalid_final(raw_assignment: bytes, transcript: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a completed malformed City response into a safe terminal decision.

    The transcript exporter writes only after the assigned City session has
    completed.  Waiting for the run deadline after that session returns invalid
    JSON leaves GitHub's required Check stuck despite there being no more work
    to await.  This adapter therefore records a credential-free, assignment-
    bound ``inconclusive`` decision.  It never preserves the malformed verdict
    or proposal, so it cannot create a follow-up branch or make a Check pass.
    """
    entries = transcript.get("entries")
    invalid_completion = isinstance(entries, list) and any(
        isinstance(entry, dict)
        and entry.get("role") == "system"
        and entry.get("text") == "invalid-final"
        for entry in entries
    )
    if _final_assistant_document(transcript) is None and not invalid_completion:
        return None
    try:
        assignment = json.loads(raw_assignment)
        identity = assignment["identity"]
        skill = assignment["agent_skill"]
        files = assignment["evidence_bundle"]["files"]
        evidence = [
            {"path": item["path"], "evidence": item["reference"]}
            for item in files
        ]
        artifact = {
            "schema_version": 1,
            "kind": "github-pr-docs-impact-review",
            "identity": identity,
            "agent_skill": skill,
            "verdict": "inconclusive",
            "rationale": "The City reviewer completed but returned an invalid final decision; human review is required.",
            "evidence": evidence,
            "confidence": 0.0,
            "proposal": None,
        }
        envelope = {
            "schema_version": 1,
            "snapshot_sha256": hashlib.sha256(raw_assignment).hexdigest(),
            "artifact": artifact,
        }
        import github_intake_docs_patch_worker as worker
        return worker.validate_final_candidate(raw_assignment, envelope)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _harvest_city_candidates(source_key: str | None = None) -> list[str]:
    """Bridge a completed City transcript into the trusted candidate outbox.

    The City only emits its credential-free review decision. This trusted
    adapter reads its own task transcript, validates it against the exact
    persisted assignment, then atomically creates the candidate envelope.
    """
    review_root = _path_env("GC_GITHUB_DOCS_ASSIGNMENT_DIR")
    candidate_root = _path_env("GC_GITHUB_DOCS_CANDIDATE_DIR")
    harvested: list[str] = []
    for marker_path in sorted((review_root.parent / "dispatch").glob("*.json")):
        digest = marker_path.stem
        assignment_path = review_root / f"{digest}.json"
        candidate_path = candidate_root / f"{digest}.json"
        if candidate_path.exists() or not assignment_path.is_file():
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if not isinstance(marker, dict) or set(marker) != {"bead_id", "source_key", "dispatched"} or marker.get("dispatched") is not True:
                continue
            if source_key is not None and marker.get("source_key") != source_key:
                continue
            raw_assignment = assignment_path.read_bytes()
            if hashlib.sha256(raw_assignment).hexdigest() != digest:
                continue
            transcript_path = review_root.parent / "transcripts" / f"{digest}.json"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8")) if transcript_path.is_file() else {}
            candidate = _candidate_from_transcript(raw_assignment, transcript)
            if candidate is None:
                candidate = _inconclusive_from_invalid_final(raw_assignment, transcript)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if candidate is None:
            continue
        _atomic_write(candidate_path, _assignment_bytes(candidate))
        harvested.append(digest)
    return harvested


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
        "head_ref": str(head.get("ref", "")),
        "base_sha": str(base.get("sha", "")).lower(),
        "base_ref": str(base.get("ref", "")),
        "head_repository_id": str(head_repo.get("id", "")),
        "head_repository": str(head_repo.get("full_name", "")),
    }
    if not all(result[key] for key in ("repository_id", "repository", "head_sha", "head_ref", "base_sha", "base_ref", "head_repository_id", "head_repository")) or type(result["pr_number"]) is not int:
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


def _direct_child_request(candidate: dict[str, Any], assignment: dict[str, Any]) -> dict[str, Any]:
    """Normalize one blocking review into the one-child PR recursion contract."""
    artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
    identity = artifact.get("identity") if isinstance(artifact, dict) else None
    evidence_bundle = assignment.get("evidence_bundle") if isinstance(assignment, dict) else None
    coverage_cells = candidate.get("coverage_cells") if isinstance(candidate, dict) else None
    if not isinstance(identity, dict) or not isinstance(evidence_bundle, dict):
        raise ValueError("docs-change candidate lacks immutable review identity")
    repository_id = str(identity.get("repository_id") or "")
    repository = str(identity.get("repository") or "")
    pr_number = identity.get("pr_number")
    head_sha = str(identity.get("head_sha") or "").lower()
    source_key = str(identity.get("source_key") or "")
    followup_base = str(evidence_bundle.get("source_head_ref") or "")
    if (not repository_id or not repository or type(pr_number) is not int or not head_sha
            or not source_key or not followup_base or not isinstance(coverage_cells, list)
            or not coverage_cells or not all(isinstance(cell, dict) for cell in coverage_cells)):
        raise ValueError("docs-change candidate lacks immutable review identity")
    budgets = {
        "max_depth": 1,
        "max_children": 1,
        "max_docs_prs": 1,
        "max_elapsed_seconds": 24 * 60 * 60,
        "max_non_progress": 3,
    }
    candidate_digest = hashlib.sha256(_assignment_bytes(candidate)).hexdigest()
    return {
        "schema_version": 1,
        "kind": "github-pr-docs-direct-child",
        "candidate_digest": candidate_digest,
        "repository_id": repository_id,
        "repository": repository,
        "pr_number": pr_number,
        "source_key": source_key,
        "snapshot_sha": head_sha,
        "source_branch": followup_base,
        "candidate_identity": identity,
        "coverage_cells": coverage_cells,
        "execution_budgets": budgets,
        "direct_child": {
            "key": f"github-pr-docs-child:{candidate_digest}",
            "source_key": source_key,
            "snapshot_sha": head_sha,
            "coverage_cells": coverage_cells,
            "execution_budgets": budgets,
        },
        "bead_id": None,
        "dispatched": False,
    }


def _persist_direct_child(candidate: dict[str, Any]) -> dict[str, Any]:
    """Persist one SHA-bound child before the generic review run is advanced."""
    artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
    if not isinstance(artifact, dict) or artifact.get("verdict") != "docs-change-required":
        raise ValueError("only docs-change-required candidates may enter direct child dispatch")
    canonical = _assignment_bytes(candidate)
    marker_path = _path_env("GC_GITHUB_DOCS_ASSIGNMENT_DIR").parent / "direct-child-dispatch" / f"{hashlib.sha256(canonical).hexdigest()}.json"
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("candidate_digest") != hashlib.sha256(canonical).hexdigest():
            raise ValueError("invalid durable direct child marker")
        return marker
    assignment_identity = artifact.get("identity")
    if not isinstance(assignment_identity, dict):
        raise ValueError("docs-change candidate has no assignment identity")
    review_store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    source_key = str(assignment_identity.get("source_key") or "")
    review_run = review_store.load(source_key)
    if review_run is None or not isinstance(review_run.get("assignment"), dict):
        raise ValueError("docs-change candidate has no durable assignment")
    marker = _direct_child_request(candidate, review_run["assignment"])
    _atomic_write(marker_path, _assignment_bytes(marker))
    return marker


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
    """Persist an immutable task; the City-local dispatcher performs the sling."""
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

    identity = run["assignment"]["identity"]
    candidate_path = _path_env("GC_GITHUB_DOCS_CANDIDATE_DIR") / f"{digest}.json"
    description = "\n".join((
        "Review the immutable GitHub pull-request documentation assignment.",
        "The JSON below is the complete, SHA-bound record. Do not fetch other state.", "",
        assignment_bytes.decode("utf-8"), "", f"Source key: {identity['source_key']}",
        "Return one canonical github-pr-docs-impact-review JSON decision bound to the assignment.",
        "Do not use GitHub credentials, network access, or mutate GitHub.",
    ))
    metadata = json.dumps({"github.docs_review.assignment_sha256": digest, "github.docs_review.candidate_path": str(candidate_path)}, sort_keys=True)
    request_path = review_root.parent / "requests" / f"{digest}.json"
    request = {"source_key": identity["source_key"], "description": description, "metadata": metadata}
    if request_path.exists() and json.loads(request_path.read_text(encoding="utf-8")) != request:
        raise ValueError("immutable City review request collision")
    if not request_path.exists():
        _atomic_write(request_path, _assignment_bytes(request))
    return

    dispatch_marker = review_root.parent / "dispatch" / f"{digest}.json"
    identity = run["assignment"]["identity"]
    city = os.environ.get("GC_CITY_ROOT", "").strip()
    target = os.environ.get("GC_CITY_DOCS_REVIEW_TARGET", "gascity/github-docs-impact.docs-impact-reviewer").strip()
    if not city or not target:
        raise ValueError("City docs reviewer target is not configured")
    candidate_path = _path_env("GC_GITHUB_DOCS_CANDIDATE_DIR") / f"{digest}.json"
    marker: dict[str, Any] | None = None
    if dispatch_marker.exists():
        try:
            marker = json.loads(dispatch_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid durable City dispatch marker") from exc
        if not isinstance(marker, dict) or marker.get("source_key") != identity["source_key"] or not isinstance(marker.get("bead_id"), str):
            raise ValueError("invalid durable City dispatch marker")
        if marker.get("dispatched") is True:
            return
    description = "\n".join((
        "Review the immutable GitHub pull-request documentation assignment.",
        "The JSON below is the complete, SHA-bound record. Do not fetch other state.",
        "",
        assignment_bytes.decode("utf-8"),
        "",
        f"Source key: {identity['source_key']}",
        "Return one canonical github-pr-docs-impact-review JSON decision bound to the assignment.",
        "Do not use GitHub credentials, network access, or mutate GitHub.",
    ))
    metadata = json.dumps({"github.docs_review.assignment_sha256": digest, "github.docs_review.candidate_path": str(candidate_path)}, sort_keys=True)
    # This sidecar shares City's managed Dolt namespace. Use its pinned Beads
    # client directly for the one task creation so a second `gc` controller
    # lookup cannot block behind the supervisor's own store coordination.
    if marker is None:
        direct_bd = os.environ.get("GC_GITHUB_INTAKE_DIRECT_BD", "") == "1"
        rig_dir = os.environ.get("GC_CITY_DOCS_REVIEW_RIG_DIR", "").strip()
        create_command = (["bd", "-C", rig_dir] if direct_bd and rig_dir else ["gc", "--city", city, "bd"])
        create_command.extend(["create", f"Review docs impact for PR #{identity['pr_number']}", "--type", "task", "--priority", "2", "--labels", "github-docs-impact", "--metadata", metadata, "--description", description, "--json"])
        try:
            create_env = dict(os.environ)
            if direct_bd and rig_dir:
                # BEADS_DIR points to the City's ledger for the webhook
                # service. The reviewer task belongs in the reviewer rig.
                create_env.pop("BEADS_DIR", None)
            created = subprocess.run(create_command, capture_output=True, text=True, check=False, timeout=45, env=create_env)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("City timed out creating docs review task") from exc
        if created.returncode:
            raise ValueError(created.stderr.strip() or "City could not create docs review task")
        response = json.loads(created.stdout)
        if isinstance(response, list) and len(response) == 1:
            response = response[0]
        bead_id = str((response or {}).get("id", "")) if isinstance(response, dict) else ""
        if not bead_id:
            raise ValueError("City returned no docs review task id")
        marker = {"bead_id": bead_id, "source_key": identity["source_key"], "dispatched": False}
        _atomic_write(dispatch_marker, _assignment_bytes(marker))
    else:
        bead_id = marker["bead_id"]
    # The request remains durable and pending until the process inside the
    # actual City supervisor marks it dispatched. Retrying this action will
    # reuse its bead rather than creating a second review.


def intake(payload: dict[str, Any], token: str, now: float) -> dict[str, Any]:
    delivery = _delivery(payload)
    path = f"/repos/{delivery['repository']}/pulls/{delivery['pr_number']}/files"
    files = common.github_api_paginated_list_request("GET", path, bearer_token=token)
    assignment = runtime.assignment_from_paginated_evidence(delivery, [files])
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    return runtime.intake_delivery(store, assignment, ComposeAdapter(store, _gateway(payload)), now=now)


def candidates(now: float, source_key: str | None = None) -> list[dict[str, Any]]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    result: list[dict[str, Any]] = []
    for path in sorted(_path_env("GC_GITHUB_DOCS_CANDIDATE_DIR").glob("*.json")):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
            identity = artifact.get("identity") if isinstance(artifact, dict) else None
            candidate_source_key = identity.get("source_key") if isinstance(identity, dict) else None
            if source_key is not None and candidate_source_key != source_key:
                continue
            review_run = store.load(candidate_source_key) if isinstance(candidate_source_key, str) and candidate_source_key else None
            if not isinstance(review_run, dict) or review_run.get("state") not in {"dispatched", "journey-pending"}:
                # Candidate artifacts are retained as immutable evidence.  A
                # stale or terminal run must not be re-admitted on every poll
                # and block later revisions behind an obsolete journey.
                result.append({"accepted": False, "reason": "stale review run"})
                continue
            if isinstance(artifact, dict) and artifact.get("verdict") == "docs-change-required":
                # The direct child is the sole executable PR continuation.
                # Persist it before advancing the generic review record so a
                # crash cannot leave a pending Check without bounded work.
                direct_child = _persist_direct_child(candidate)
                accepted = runtime.accept_candidate(store, candidate, adapter, now=now)
                result.append({"direct_child": direct_child, "review": accepted})
            else:
                result.append(runtime.accept_candidate(store, candidate, adapter, now=now))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result.append({"accepted": False, "reason": f"invalid candidate {path.name}: {exc}"})
    return result


class _ScopedDocsReviewStore:
    """Expose one immutable run to the generic reconciler.

    Gateway lifecycle jobs are source-key scoped.  Letting one job rescan all
    historical reviews turns a single GitHub API stall into head-of-line
    blocking for every newer revision.
    """

    def __init__(self, store: runtime.FileDocsReviewStore, source_key: str) -> None:
        self._store = store
        self._source_key = source_key

    def list_runs(self) -> list[dict[str, Any]]:
        run = self._store.load(self._source_key)
        return [run] if isinstance(run, dict) else []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def reconcile(now: float, source_key: str | None = None) -> dict[str, Any]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    harvested = _harvest_city_candidates(source_key)
    accepted = candidates(now, source_key)
    reconciler_store: Any = _ScopedDocsReviewStore(store, source_key) if source_key else store
    return {"harvested": harvested, "candidates": accepted, "runs": runtime.reconcile_pending(reconciler_store, adapter, now=now)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("intake", "reconcile"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--payload-file", default=os.environ.get("GC_GITHUB_EVENT_PAYLOAD_FILE", ""))
    parser.add_argument("--token-env", default="GH_TOKEN")
    parser.add_argument("--source-key", default="")
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
        result = intake(payload, token, now) if args.operation == "intake" else reconcile(now, args.source_key or None)
        print(json.dumps(result, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
