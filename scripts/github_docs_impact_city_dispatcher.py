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
import tempfile
import time


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
            ["gc", "--city", city, "--rig", rig, "bd", "close", bead_id, "--reason", "Superseded by a newer GitHub pull-request revision", "--json"],
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
            if review is None: continue
            identity = review.get("identity")
            if not isinstance(identity, dict) or identity.get("source_key") != marker_json["source_key"]:
                continue
            transcript = {"entries": [{"role": "assistant", "text": json.dumps(review, sort_keys=True, separators=(",", ":"))}]}
            if replace_stale:
                _quarantine_stale_transcript(transcript_path)
            _atomic_write(transcript_path, json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode())
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
            print(json.dumps({"retired": retire_superseded(), "created": create_pending(), "dispatched": dispatch_pending(), "transcripts": export_transcripts()}, sort_keys=True), flush=True)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
