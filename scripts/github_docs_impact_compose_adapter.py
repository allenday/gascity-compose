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
from collections.abc import Callable
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


def _harvest_city_candidates() -> list[str]:
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


def _journey_request(candidate: dict[str, Any], assignment: dict[str, Any], installation_id: str) -> dict[str, Any]:
    """Normalize one exact blocking review into the docs-journey contract.

    The journey snapshot is the reviewed pull-request SHA: it is the only
    immutable revision the review artifact authorizes.  ``default_branch`` is
    retained as the eventual follow-up PR base, while the controller validates
    the admitted child against this reviewed snapshot.
    """
    artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
    identity = artifact.get("identity") if isinstance(artifact, dict) else None
    evidence_bundle = assignment.get("evidence_bundle") if isinstance(assignment, dict) else None
    proposal_identity = evidence_bundle.get("proposal_identity") if isinstance(evidence_bundle, dict) else None
    if not isinstance(identity, dict) or not isinstance(proposal_identity, dict):
        raise ValueError("docs-change candidate lacks immutable review identity")
    repository_id = str(identity.get("repository_id") or "")
    repository = str(identity.get("repository") or "")
    pr_number = identity.get("pr_number")
    head_sha = str(identity.get("head_sha") or "").lower()
    source_key = str(identity.get("source_key") or "")
    followup_base = str(evidence_bundle.get("source_head_ref") or "") if isinstance(evidence_bundle, dict) else ""
    if not repository_id or not repository or type(pr_number) is not int or not head_sha or not source_key or not followup_base:
        raise ValueError("docs-change candidate lacks immutable review identity")
    return {
        "repository_id": repository_id,
        "repository": repository,
        "installation_id": installation_id,
        # This is the reviewed source branch, not the PR's base branch.  The
        # worker's docs PR must merge into the contributor's revision so its
        # merge creates a new source SHA and re-runs the original Check.
        "default_branch": followup_base,
        "default_branch_sha": head_sha,
        "source": {
            "kind": "github-pull-request",
            "key": source_key,
            "url": f"https://github.com/{repository}/pull/{pr_number}",
            "projection_capabilities": [],
        },
        "docs_impact_source_key": source_key,
        "documentation_index": "README.md",
        "domain": "techdocs",
        "role": "developer",
        "job": "use the changed developer-facing interface",
        "starting_context": "a clone of the repository at the reviewed pull-request revision",
        "success_condition": "the developer can complete the changed workflow from repository documentation",
        "backfill_policy": "blocking-only",
        "budgets": {
            "max_depth": 2,
            "max_children": 1,
            "max_docs_prs": 1,
            "max_debt_issues": 4,
            "max_elapsed_seconds": 24 * 60 * 60,
            "max_non_progress": 3,
        },
    }


def _journey_marker_path(candidate: dict[str, Any]) -> pathlib.Path:
    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _path_env("GC_GITHUB_DOCS_ASSIGNMENT_DIR").parent / "journey-dispatch" / f"{hashlib.sha256(canonical).hexdigest()}.json"


def _admit_docs_journey(candidate: dict[str, Any], on_admitted: Callable[[], None] | None = None) -> dict[str, Any]:
    """Persist and project a blocking journey, then request one City sling.

    This deliberately stops at dispatch.  Worker-result harvesting and
    check terminalization are a separate, source-bound lifecycle phase.
    """
    artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
    if not isinstance(artifact, dict) or artifact.get("verdict") != "docs-change-required":
        raise ValueError("only docs-change-required candidates may enter docs-journey")
    marker_path = _journey_marker_path(candidate)
    if marker_path.exists():
        return json.loads(marker_path.read_text(encoding="utf-8"))
    installation_id = _installation_id_from_config()
    assignment_identity = artifact.get("identity")
    if not isinstance(assignment_identity, dict):
        raise ValueError("docs-change candidate has no assignment identity")
    review_store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    source_key = str(assignment_identity.get("source_key") or "")
    review_run = review_store.load(source_key)
    if review_run is None or not isinstance(review_run.get("assignment"), dict):
        raise ValueError("docs-change candidate has no durable assignment")
    request = _journey_request(candidate, review_run["assignment"], installation_id)
    payload = {"request": request, "decision": {"artifact": artifact, "journey_disposition": "blocking"}}
    command = [
        sys.executable, f"{PACK_SCRIPTS}/github_intake_docs_journey_commands.py", "start-or-admit", "--once",
        "--store", str(_path_env("GC_GITHUB_DOCS_ASSIGNMENT_DIR").parent / "journeys"),
        "--input", json.dumps(payload, sort_keys=True, separators=(",", ":")),
    ]
    started = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if started.returncode:
        raise ValueError(started.stderr.strip() or "docs journey admission failed")
    result = json.loads(started.stdout)
    journey = result.get("journey") if isinstance(result, dict) else None
    journey_identity = journey.get("identity") if isinstance(journey, dict) else None
    if not isinstance(journey_identity, str) or not journey_identity:
        raise ValueError("docs journey admission returned no identity")
    if on_admitted is not None:
        on_admitted()
    projected = subprocess.run(command[:2] + ["project-until-settled", "--once", "--store", command[5], "--identity", journey_identity], capture_output=True, text=True, check=False, timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    if projected.returncode:
        raise ValueError(projected.stderr.strip() or "docs journey projection failed")
    settled = json.loads(projected.stdout)
    settled_journey = settled.get("journey") if isinstance(settled, dict) else None
    ready = settled.get("worker_ready_children") if isinstance(settled, dict) else None
    if not isinstance(settled_journey, dict) or not isinstance(ready, list) or len(ready) != 1:
        raise ValueError("docs journey did not produce exactly one dispatchable child")
    child = next((item for item in settled_journey.get("children", []) if isinstance(item, dict) and item.get("key") == ready[0]), None)
    bead_action = next((item for item in settled_journey.get("actions", []) if isinstance(item, dict) and item.get("kind") == "create_bead" and item.get("child_key") == ready[0] and item.get("state") == "completed"), None)
    bead_id = ((bead_action or {}).get("resource") or {}).get("id") if isinstance(bead_action, dict) else None
    if not isinstance(child, dict) or not isinstance(bead_id, str) or not bead_id:
        raise ValueError("docs journey did not persist dispatchable child evidence")
    marker = {"bead_id": bead_id, "child_key": ready[0], "journey_identity": journey_identity, "admitted_child": child, "dispatched": False}
    _atomic_write(marker_path, _assignment_bytes(marker))
    return marker


def _installation_id_from_config() -> str:
    value = os.environ.get("GITHUB_INSTALLATION_ID", "")
    if not str(value).strip():
        config = common.load_effective_config()
        app = config.get("app") if isinstance(config, dict) else None
        value = (app or {}).get("installation_id", "")
    if not str(value).strip():
        raise ValueError("GitHub App installation id is unavailable for docs journey")
    return str(value)


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


def candidates(now: float) -> list[dict[str, Any]]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    result: list[dict[str, Any]] = []
    for path in sorted(_path_env("GC_GITHUB_DOCS_CANDIDATE_DIR").glob("*.json")):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            artifact = candidate.get("artifact") if isinstance(candidate, dict) else None
            identity = artifact.get("identity") if isinstance(artifact, dict) else None
            source_key = identity.get("source_key") if isinstance(identity, dict) else None
            review_run = store.load(source_key) if isinstance(source_key, str) and source_key else None
            if not isinstance(review_run, dict) or review_run.get("state") not in {"dispatched", "journey-pending"}:
                # Candidate artifacts are retained as immutable evidence.  A
                # stale or terminal run must not be re-admitted on every poll
                # and block later revisions behind an obsolete journey.
                result.append({"accepted": False, "reason": "stale review run"})
                continue
            if isinstance(artifact, dict) and artifact.get("verdict") == "docs-change-required":
                # Do not terminalize the visible Check before the exact
                # blocking decision has become a durable City journey.  The
                # later worker-result bridge owns candidate acceptance and the
                # check's terminal transition.
                accepted: dict[str, Any] | None = None
                def mark_journey_pending() -> None:
                    nonlocal accepted
                    accepted = runtime.accept_candidate(store, candidate, adapter, now=now)
                journey = _admit_docs_journey(candidate, mark_journey_pending)
                # The generic runtime owns the visible Check's durable
                # ``journey-pending`` transition. Admission is deliberately
                # first: a crash cannot leave a pending Check with no journey.
                result.append({"journey": journey, "review": accepted})
            else:
                result.append(runtime.accept_candidate(store, candidate, adapter, now=now))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result.append({"accepted": False, "reason": f"invalid candidate {path.name}: {exc}"})
    return result


def reconcile(now: float) -> dict[str, Any]:
    store = runtime.FileDocsReviewStore(_path_env("GC_GITHUB_DOCS_REVIEW_RUNS_DIR"))
    adapter = ComposeAdapter(store, _gateway())
    harvested = _harvest_city_candidates()
    return {"harvested": harvested, "candidates": candidates(now), "runs": runtime.reconcile_pending(store, adapter, now=now)}


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
