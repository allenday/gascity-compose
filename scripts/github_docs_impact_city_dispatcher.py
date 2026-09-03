#!/usr/bin/env python3
"""Dispatch durable docs-review tasks from the live City supervisor context.

The GitHub-facing runtime may create an immutable reviewer task, but it never
invokes ``gc sling``: doing that from its network namespace makes the CLI try
to recover a second local controller.  This small City-local process owns only
the handoff.  It has Codex access through City, but no GitHub credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time


PACK_SCRIPTS = os.environ.get("GC_GITHUB_PACK_SCRIPTS", "/opt/gascity-packs/github/scripts")
if PACK_SCRIPTS not in sys.path:
    sys.path.insert(0, PACK_SCRIPTS)

import github_intake_common as common
import github_intake_docs_impact as impact
import github_intake_docs_review_runtime as review_runtime


def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = pathlib.Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def _pending_marker(path: pathlib.Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"bead_id", "source_key", "dispatched"}:
        return None
    if value.get("dispatched") is not False:
        return None
    bead_id, source_key = value.get("bead_id"), value.get("source_key")
    if not isinstance(bead_id, str) or not bead_id or not isinstance(source_key, str) or not source_key:
        return None
    return {"bead_id": bead_id, "source_key": source_key}


def _reviewer_target() -> tuple[str, str]:
    target = os.environ.get("GC_CITY_DOCS_REVIEW_TARGET", "").strip()
    rig, separator, agent = target.partition("/")
    if not rig or not separator or not agent:
        raise ValueError("GC_CITY_DOCS_REVIEW_TARGET must be a qualified <rig>/<agent> name")
    return rig, target


def _source_scope(source_key: str) -> tuple[str, str, str] | None:
    """Return the immutable PR lineage portion of a GitHub source key."""
    parts = source_key.split(":")
    if len(parts) != 4 or parts[0] != "github-pr" or not all(parts):
        return None
    return tuple(parts[:3])


def _latest_sources(review_dir: pathlib.Path) -> dict[tuple[str, str, str], str]:
    """Pick the last durable request for each PR lineage.

    The webhook persists requests locally in delivery order.  Only the latest
    SHA can be reviewed; older revisions consume a finite reviewer pool and
    would otherwise starve the current revision after a force-push storm.
    """
    selected: dict[tuple[str, str, str], tuple[int, str, str]] = {}
    for request_path in sorted((review_dir / "requests").glob("*.json")):
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            source_key = request.get("source_key") if isinstance(request, dict) else None
            scope = _source_scope(source_key) if isinstance(source_key, str) else None
            if scope is None:
                continue
            candidate = (request_path.stat().st_mtime_ns, request_path.stem, source_key)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if scope not in selected or candidate[:2] > selected[scope][:2]:
            selected[scope] = candidate
    return {scope: value[2] for scope, value in selected.items()}


def retire_superseded() -> list[str]:
    """Close City Beads for older SHA deliveries before they occupy a worker."""
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    if not review_dir or not city:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR and CITY_PATH are required")
    rig, _ = _reviewer_target()
    latest = _latest_sources(review_dir)
    retired: list[str] = []
    for marker_path in sorted((review_dir / "dispatch").glob("*.json")):
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            source_key = marker.get("source_key") if isinstance(marker, dict) else None
            scope = _source_scope(source_key) if isinstance(source_key, str) else None
            bead_id = marker.get("bead_id") if isinstance(marker, dict) else None
            if marker.get("dispatched") is not True or scope is None or not isinstance(bead_id, str):
                continue
            if latest.get(scope) == source_key:
                continue
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        result = subprocess.run(
            ["gc", "--city", city, "--rig", rig, "bd", "close", bead_id, "--force", "--reason", "Superseded by a newer GitHub pull-request revision", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if result.returncode:
            continue
        _atomic_write(marker_path, json.dumps({**marker, "dispatched": "retired"}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        retired.append(marker_path.stem)
    return retired


def dispatch_pending() -> list[str]:
    """Sling each pending request once, and acknowledge only on success."""
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    if not review_dir or not city:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR, CITY_PATH, and GC_CITY_DOCS_REVIEW_TARGET are required")
    _, target = _reviewer_target()
    dispatched: list[str] = []
    for marker_path in sorted((review_dir / "dispatch").glob("*.json")):
        marker = _pending_marker(marker_path)
        if marker is None:
            continue
        result = subprocess.run(
            ["gc", "--city", city, "sling", target, marker["bead_id"], "--force", "--no-convoy", "--no-formula", "--nudge", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if result.returncode:
            continue
        _atomic_write(marker_path, json.dumps({**marker, "dispatched": True}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        dispatched.append(marker_path.stem)
    return dispatched


def _pending_journey_marker(path: pathlib.Path) -> dict[str, str] | None:
    """Validate one controller-projected journey child dispatch request."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    required = {"bead_id", "child_key", "journey_identity", "admitted_child", "dispatched"}
    if not isinstance(value, dict) or set(value) != required or value.get("dispatched") is not False:
        return None
    result = {key: value.get(key) for key in ("bead_id", "child_key", "journey_identity")}
    if not all(isinstance(item, str) and item for item in result.values()):
        return None
    if not result["journey_identity"].startswith("github-docs-journey:"):
        return None
    if not isinstance(value.get("admitted_child"), dict):
        return None
    return {**result, "admitted_child": value["admitted_child"]}  # type: ignore[return-value]


def _journey_target() -> str:
    target = os.environ.get("GC_CITY_DOCS_JOURNEY_TARGET", "").strip()
    rig, separator, agent = target.partition("/")
    if not rig or not separator or not agent:
        raise ValueError("GC_CITY_DOCS_JOURNEY_TARGET must be a qualified <rig>/<agent> name")
    return target


def dispatch_journey_pending() -> list[str]:
    """Sling only children already projected by the durable journey controller."""
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    if not review_dir or not city:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR and CITY_PATH are required")
    target = _journey_target()
    dispatched: list[str] = []
    for marker_path in sorted((review_dir / "journey-dispatch").glob("*.json")):
        marker = _pending_journey_marker(marker_path)
        if marker is None:
            continue
        result = subprocess.run(
            ["gc", "--city", city, "sling", target, marker["bead_id"], "--force", "--no-convoy", "--no-formula", "--nudge", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
        if result.returncode:
            continue
        _atomic_write(marker_path, json.dumps({**marker, "dispatched": True}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        dispatched.append(marker_path.stem)
    return dispatched


def _completed_journey_marker(path: pathlib.Path) -> dict[str, str] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    required = {"bead_id", "child_key", "journey_identity", "admitted_child", "dispatched"}
    if not isinstance(value, dict) or set(value) != required or value.get("dispatched") is not True:
        return None
    result = {key: value.get(key) for key in ("bead_id", "child_key", "journey_identity")}
    if not all(isinstance(item, str) and item for item in result.values()):
        return None
    if not isinstance(value.get("admitted_child"), dict):
        return None
    return {**result, "admitted_child": value["admitted_child"]}  # type: ignore[return-value]


def _matches_admitted_child(expected: dict[str, object], update: dict[str, object]) -> bool:
    admitted = update.get("admitted_child")
    if not isinstance(admitted, dict):
        return False
    fields = (
        "journey_identity", "snapshot_sha", "decision_identity", "decision_digest",
        "source_key", "source_url", "documentation_entry_point", "parent_issue_url", "evidence_paths",
    )
    return all(admitted.get(field) == expected.get(field) for field in fields)


def _original_check_outcome(journey: dict[str, object]) -> str:
    """Return whether a settled journey can terminalize its originating Check.

    A docs-followup PR is evidence of work prepared, not evidence that the
    contributor's PR is documented. Its merge produces a new source SHA and a
    new normal docs-impact review, which alone may pass the required Check.
    """
    state = journey.get("state")
    if state != "baseline-complete":
        return "action_required" if state in {
            "owner-review-required", "blocked-on-product-decision", "budget-exhausted", "cancelled",
        } else "pending"
    actions = journey.get("actions")
    docs_pr = any(
        isinstance(action, dict) and action.get("kind") == "create_docs_pr" and action.get("state") == "completed"
        for action in actions
    ) if isinstance(actions, list) else False
    return "pending" if docs_pr else "action_required"


def _terminalize_original_check(marker: dict[str, object], journey: dict[str, object]) -> bool:
    """Persist and project only a genuine journey failure to the source Check."""
    if _original_check_outcome(journey) != "action_required":
        return False
    admitted = marker.get("admitted_child")
    source_key = admitted.get("source_key") if isinstance(admitted, dict) else None
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    if not isinstance(source_key, str) or not source_key or not review_dir:
        raise ValueError("journey marker lacks original review source")
    config = common.load_effective_config()
    app = config.get("app") if isinstance(config, dict) else None
    installation_id = os.environ.get("GITHUB_INSTALLATION_ID", "") or (app or {}).get("installation_id", "")
    if not isinstance(app, dict) or not str(installation_id).strip():
        raise ValueError("GitHub App configuration is unavailable for journey terminal status")
    store = review_runtime.FileDocsReviewStore(review_dir)
    with store.lock(source_key):
        record = store.load(source_key)
        if record is None:
            raise ValueError("journey source review run was not found")
        if record.get("state") == "terminal" and record.get("conclusion") == "action_required":
            return True
        if record.get("state") != "journey-pending":
            return False
        record["state"] = "terminal"
        record["conclusion"] = "action_required"
        record["pending_actions"] = ["ensure_terminal_check"]
        record["journey"] = {"identity": marker.get("journey_identity"), "state": journey.get("state")}
        store.save(record)
        gateway = impact.GitHubAppProjectionGateway(app, str(installation_id))
        projection = impact.AppProjection(store, gateway)
        projection.perform("ensure_terminal_check", record)
        record["pending_actions"] = []
        store.save(record)
    return True


def _journey_update_from_peek(peek: dict[str, object]) -> dict[str, object] | None:
    """Recover one final worker update without trusting surrounding prose."""
    output = peek.get("output")
    if not isinstance(output, str):
        return None
    compact = "".join(line.strip() for line in output.splitlines())
    decoder = json.JSONDecoder()
    update: dict[str, object] | None = None
    for index, char in enumerate(compact):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(compact[index:])
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict)
                and value.get("schema_version") == 1
                and value.get("kind") == "github-docs-journey-child-update"
                and value.get("state") in {"complete", "blocked", "failed", "cancelled"}
                and isinstance(value.get("admitted_child"), dict)):
            update = value
    return update


def _journey_update_from_bead(bead: dict[str, object]) -> dict[str, object] | None:
    """Read the worker's durable terminal update, never a dead session."""
    metadata = bead.get("metadata")
    value = metadata.get("docs-journey.result") if isinstance(metadata, dict) else None
    try:
        update = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return None
    return update if isinstance(update, dict) and update.get("kind") == "github-docs-journey-child-update" else None


def harvest_journey_updates() -> list[str]:
    """Record exactly one closed worker result, then re-project its intents.

    The controller remains the only authority that may create the follow-up
    PR.  This adapter merely moves its final worker evidence across the
    durable command boundary; it does not touch the original docs-impact
    Check.
    """
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    if not review_dir or not city:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR and CITY_PATH are required")
    target = _journey_target()
    rig = target.partition("/")[0]
    pack_scripts = os.environ.get("GC_GITHUB_PACK_SCRIPTS", "/opt/gascity-packs/github/scripts")
    command_script = f"{pack_scripts}/github_intake_docs_journey_commands.py"
    store = str(review_dir / "journeys")
    harvested: list[str] = []
    for marker_path in sorted((review_dir / "journey-dispatch").glob("*.json")):
        marker = _completed_journey_marker(marker_path)
        if marker is None:
            continue
        try:
            bead = subprocess.run(
                ["gc", "--city", city, "--rig", rig, "bd", "show", marker["bead_id"], "--json"],
                capture_output=True, text=True, check=False, timeout=20,
            )
            if bead.returncode:
                continue
            bead_json = json.loads(bead.stdout)
            if isinstance(bead_json, list):
                bead_json = bead_json[0]
            if not isinstance(bead_json, dict) or bead_json.get("status") != "closed":
                continue
            update = _journey_update_from_bead(bead_json)
            if update is None or not _matches_admitted_child(marker["admitted_child"], update):
                continue
            record = subprocess.run(
                [sys.executable, command_script, "record-child-update", "--once", "--store", store,
                 "--input", json.dumps({"identity": marker["journey_identity"], "update": update}, sort_keys=True, separators=(",", ":"))],
                capture_output=True, text=True, check=False, timeout=60,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if record.returncode:
                continue
            recorded = json.loads(record.stdout)
            journey = recorded.get("journey") if isinstance(recorded, dict) else None
            child = next((item for item in journey.get("children", []) if isinstance(item, dict) and item.get("key") == marker["child_key"]), None) if isinstance(journey, dict) else None
            if not isinstance(child, dict) or child.get("state") == "admitted":
                continue
            projected = subprocess.run(
                [sys.executable, command_script, "project-until-settled", "--once", "--store", store,
                 "--identity", marker["journey_identity"]],
                capture_output=True, text=True, check=False, timeout=120,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if projected.returncode:
                continue
            projected_result = json.loads(projected.stdout)
            projected_journey = projected_result.get("journey") if isinstance(projected_result, dict) else None
            if not isinstance(projected_journey, dict):
                continue
            _terminalize_original_check(marker, projected_journey)
            _atomic_write(marker_path, json.dumps({**marker, "dispatched": "recorded"}, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            harvested.append(marker_path.stem)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
            continue
    return harvested


def create_pending() -> list[str]:
    """Create reviewer Beads through City's managed store, never a sidecar."""
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    if not review_dir or not city:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR and CITY_PATH are required")
    rig, _ = _reviewer_target()
    created: list[str] = []
    for request_path in sorted((review_dir / "requests").glob("*.json")):
        digest = request_path.stem
        dispatch_path = review_dir / "dispatch" / f"{digest}.json"
        if dispatch_path.exists():
            continue
        try:
            request = json.loads(request_path.read_text(encoding="utf-8"))
            source_key, description, metadata = request["source_key"], request["description"], request["metadata"]
            if not all(isinstance(value, str) and value for value in (source_key, description, metadata)):
                continue
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        result = subprocess.run(["gc", "--city", city, "--rig", rig, "bd", "create", "Review GitHub documentation impact", "--type", "task", "--priority", "2", "--labels", "github-docs-impact", "--metadata", metadata, "--description", description, "--json"], capture_output=True, text=True, check=False, timeout=45)
        if result.returncode:
            continue
        try:
            response = json.loads(result.stdout)
            if isinstance(response, list): response = response[0]
            bead_id = response["id"]
            if not isinstance(bead_id, str) or not bead_id: continue
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
            continue
        _atomic_write(dispatch_path, json.dumps({"bead_id": bead_id, "source_key": source_key, "dispatched": False}, sort_keys=True, separators=(",", ":")).encode())
        created.append(digest)
    return created


def _review_from_peek(peek: dict[str, object]) -> dict[str, object] | None:
    """Recover the one-line canonical review from Codex's wrapped terminal view."""
    output = peek.get("output")
    if not isinstance(output, str):
        return None
    compact = "".join(line.strip() for line in output.splitlines())
    decoder = json.JSONDecoder()
    review: dict[str, object] | None = None
    for index, char in enumerate(compact):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(compact[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("kind") == "github-pr-docs-impact-review":
            if value.get("verdict") in {"no-impact", "docs-sufficient", "docs-change-required", "proposal-ready", "inconclusive"}:
                review = value
    return review


def _stored_transcript_matches_source(path: pathlib.Path, source_key: str) -> bool:
    """Accept a persisted transcript only when it is bound to this exact SHA."""
    try:
        transcript = json.loads(path.read_text(encoding="utf-8"))
        entries = transcript.get("entries") if isinstance(transcript, dict) else None
        if not isinstance(entries, list):
            return False
        for entry in reversed(entries):
            if not isinstance(entry, dict) or entry.get("role") != "assistant":
                continue
            review = json.loads(entry.get("text")) if isinstance(entry.get("text"), str) else None
            identity = review.get("identity") if isinstance(review, dict) else None
            return isinstance(identity, dict) and identity.get("source_key") == source_key
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return False


def _quarantine_stale_transcript(path: pathlib.Path) -> None:
    """Retain stale output for diagnosis without letting it block a fresh SHA."""
    rejected = path.parent / "rejected"
    rejected.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.replace(rejected / f"{path.stem}.{time.time_ns()}{path.suffix}")


def _write_invalid_final(path: pathlib.Path, *, replace_stale: bool) -> None:
    """Record only the terminal failure marker; never persist raw agent prose."""
    transcript = {"entries": [{"role": "system", "text": "invalid-final"}]}
    if replace_stale:
        _quarantine_stale_transcript(path)
    _atomic_write(path, json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode())


def _completion_path(review_dir: pathlib.Path, marker_path: pathlib.Path) -> pathlib.Path:
    return review_dir / "completions" / marker_path.name


def _transcript_session_id(path: pathlib.Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    session_id = value.get("session_id") if isinstance(value, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _close_persisted_review(review_dir: pathlib.Path, marker_path: pathlib.Path, marker: dict[str, object], session_id: str, city: str, rig: str) -> None:
    """Close only the session that produced an already-durable review.

    The intent is written before the guarded status transition, so a restart
    retries transport failures.  The Beads update guards make a reaper's new
    assignee a harmless lost race rather than a task we close by mistake.
    """
    path = _completion_path(review_dir, marker_path)
    expected = {"bead_id": marker["bead_id"], "source_key": marker["source_key"], "session_id": session_id, "state": "close-pending"}
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(value.get(key) != expected[key] for key in ("bead_id", "source_key", "session_id")):
            raise ValueError("invalid review completion marker")
        if value.get("state") in {"closed", "ownership-changed"}:
            return
    else:
        _atomic_write(path, json.dumps(expected, sort_keys=True, separators=(",", ":")).encode())
    closed = subprocess.run(
        ["gc", "--city", city, "--rig", rig, "bd", "update", str(marker["bead_id"]), "--status", "closed", "--if-assignee", session_id, "--if-status", "in_progress", "--session", session_id, "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if closed.returncode == 0:
        _atomic_write(path, json.dumps({**expected, "state": "closed"}, sort_keys=True, separators=(",", ":")).encode())
    elif closed.returncode == 13:
        _atomic_write(path, json.dumps({**expected, "state": "ownership-changed"}, sort_keys=True, separators=(",", ":")).encode())


def export_transcripts() -> list[str]:
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    rig, _ = _reviewer_target()
    exported: list[str] = []
    for marker_path in sorted((review_dir / "dispatch").glob("*.json")):
        marker = _pending_marker(marker_path)
        if marker is not None:
            continue
        try:
            marker_json = json.loads(marker_path.read_text())
            if marker_json.get("dispatched") is not True: continue
            transcript_path = review_dir / "transcripts" / marker_path.name
            replace_stale = False
            if transcript_path.exists():
                if _stored_transcript_matches_source(transcript_path, marker_json["source_key"]):
                    completion = _completion_path(review_dir, marker_path)
                    if completion.exists():
                        value = json.loads(completion.read_text(encoding="utf-8"))
                        session_id = value.get("session_id") if isinstance(value, dict) else None
                        if isinstance(session_id, str) and session_id:
                            _close_persisted_review(review_dir, marker_path, marker_json, session_id, city, rig)
                    else:
                        session_id = _transcript_session_id(transcript_path)
                        if session_id is not None:
                            _close_persisted_review(review_dir, marker_path, marker_json, session_id, city, rig)
                    continue
                replace_stale = True
            bead = subprocess.run(["gc", "--city", city, "--rig", rig, "bd", "show", marker_json["bead_id"], "--json"], capture_output=True, text=True, check=False, timeout=20)
            if bead.returncode: continue
            bead_json = json.loads(bead.stdout)
            if isinstance(bead_json, list): bead_json = bead_json[0]
            session_id = bead_json.get("assignee") if isinstance(bead_json, dict) else None
            if not isinstance(session_id, str) or not session_id: continue
            # `session logs` can include the prompt's illustrative JSON schema.
            # The rendered session view is the authoritative final agent output.
            result = subprocess.run(["gc", "--city", city, "session", "peek", session_id, "--lines", "400", "--json"], capture_output=True, text=True, check=False, timeout=20)
            if result.returncode: continue
            review = _review_from_peek(json.loads(result.stdout))
            if review is None:
                # A closed reviewer has no further output to await. Persist a
                # trusted completion sentinel so the adapter can terminalize
                # the Check as inconclusive rather than wait for its deadline.
                if bead_json.get("status") != "closed":
                    continue
                _write_invalid_final(transcript_path, replace_stale=replace_stale)
                exported.append(marker_path.stem)
                continue
            identity = review.get("identity")
            if not isinstance(identity, dict) or identity.get("source_key") != marker_json["source_key"]:
                if bead_json.get("status") == "closed":
                    _write_invalid_final(transcript_path, replace_stale=replace_stale)
                    exported.append(marker_path.stem)
                continue
            transcript = {"entries": [{"role": "assistant", "text": json.dumps(review, sort_keys=True, separators=(",", ":"))}], "session_id": session_id}
            if replace_stale:
                _quarantine_stale_transcript(transcript_path)
            _atomic_write(transcript_path, json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode())
            _close_persisted_review(review_dir, marker_path, marker_json, session_id, city, rig)
            exported.append(marker_path.stem)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            continue
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    interval = max(1.0, float(os.environ.get("GC_CITY_DOCS_DISPATCH_SECONDS", "5")))
    while True:
        try:
            print(json.dumps({"retired": retire_superseded(), "created": create_pending(), "dispatched": dispatch_pending(), "journeys": dispatch_journey_pending(), "journey_updates": harvest_journey_updates(), "transcripts": export_transcripts()}, sort_keys=True), flush=True)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
