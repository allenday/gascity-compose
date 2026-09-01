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


def dispatch_pending() -> list[str]:
    """Sling each pending request once, and acknowledge only on success."""
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    target = os.environ.get("GC_CITY_DOCS_REVIEW_TARGET", "").strip()
    if not review_dir or not city or not target:
        raise ValueError("GC_CITY_DOCS_REVIEW_DIR, CITY_PATH, and GC_CITY_DOCS_REVIEW_TARGET are required")
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
        result = subprocess.run(["gc", "--city", city, "--rig", "gascity", "bd", "create", "Review GitHub documentation impact", "--type", "task", "--priority", "2", "--labels", "github-docs-impact", "--metadata", metadata, "--description", description, "--json"], capture_output=True, text=True, check=False, timeout=45)
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
    compact = "".join(output.splitlines())
    decoder = json.JSONDecoder()
    for index, char in enumerate(compact):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(compact[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("kind") == "github-pr-docs-impact-review":
            return value
    return None


def export_transcripts() -> list[str]:
    review_dir = pathlib.Path(os.environ.get("GC_CITY_DOCS_REVIEW_DIR", "").strip())
    city = os.environ.get("CITY_PATH", "").strip()
    exported: list[str] = []
    for marker_path in sorted((review_dir / "dispatch").glob("*.json")):
        marker = _pending_marker(marker_path)
        if marker is not None:
            continue
        try:
            marker_json = json.loads(marker_path.read_text())
            if marker_json.get("dispatched") is not True: continue
            transcript_path = review_dir / "transcripts" / marker_path.name
            if transcript_path.exists(): continue
            bead = subprocess.run(["gc", "--city", city, "--rig", "gascity", "bd", "show", marker_json["bead_id"], "--json"], capture_output=True, text=True, check=False, timeout=20)
            if bead.returncode: continue
            bead_json = json.loads(bead.stdout)
            if isinstance(bead_json, list): bead_json = bead_json[0]
            session_id = bead_json.get("assignee") if isinstance(bead_json, dict) else None
            if not isinstance(session_id, str) or not session_id: continue
            result = subprocess.run(["gc", "--city", city, "session", "peek", session_id, "--lines", "400", "--json"], capture_output=True, text=True, check=False, timeout=20)
            if result.returncode: continue
            review = _review_from_peek(json.loads(result.stdout))
            if review is None: continue
            transcript = {"entries": [{"role": "assistant", "text": json.dumps(review, sort_keys=True, separators=(",", ":"))}]}
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
            print(json.dumps({"created": create_pending(), "dispatched": dispatch_pending(), "transcripts": export_transcripts()}, sort_keys=True), flush=True)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
