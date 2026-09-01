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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    interval = max(1.0, float(os.environ.get("GC_CITY_DOCS_DISPATCH_SECONDS", "5")))
    while True:
        try:
            print(json.dumps({"dispatched": dispatch_pending()}, sort_keys=True), flush=True)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(json.dumps({"error": str(exc)}, sort_keys=True), flush=True)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
