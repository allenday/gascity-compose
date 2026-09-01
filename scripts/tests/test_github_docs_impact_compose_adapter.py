from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


PACK_SCRIPTS = pathlib.Path("/root/src/gascity-packs/github/scripts")
os.environ["GC_GITHUB_PACK_SCRIPTS"] = str(PACK_SCRIPTS)
spec = importlib.util.spec_from_file_location(
    "github_docs_impact_compose_adapter",
    pathlib.Path(__file__).resolve().parents[1] / "github_docs_impact_compose_adapter.py",
)
assert spec and spec.loader
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)

dispatcher_spec = importlib.util.spec_from_file_location(
    "github_docs_impact_city_dispatcher",
    pathlib.Path(__file__).resolve().parents[1] / "github_docs_impact_city_dispatcher.py",
)
assert dispatcher_spec and dispatcher_spec.loader
dispatcher = importlib.util.module_from_spec(dispatcher_spec)
sys.modules[dispatcher_spec.name] = dispatcher
dispatcher_spec.loader.exec_module(dispatcher)


SHA = "a" * 40


def assignment() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "github-pr-docs-impact-assignment",
        "identity": {"repository_id": "17", "repository": "example/docs", "pr_number": 9, "head_sha": SHA, "source_key": f"github-pr:17:9:{SHA}"},
        "agent_skill": "developer-experience-techdocs",
        "evidence_bundle": {"head_sha": SHA, "proposal_identity": {"repository_id": "17", "repository": "example/docs", "pr_number": 9, "base_sha": "b" * 40, "head_sha": SHA, "head_repository_id": "17", "head_repository": "example/docs", "base_ref": "main"}, "files": [{"path": "docs/guide.md", "reference": f"github://example/docs/blob/{SHA}/docs/guide.md", "patch": "@@ -1 +1 @@\n-old\n+new\n"}]},
    }


def review() -> dict[str, object]:
    source = assignment()["identity"]
    return {"schema_version": 1, "kind": "github-pr-docs-impact-review", "identity": source, "agent_skill": "developer-experience-techdocs", "verdict": "no-impact", "rationale": "The documentation is sufficient.", "evidence": [{"path": "docs/guide.md", "evidence": f"github://example/docs/blob/{SHA}/docs/guide.md"}], "confidence": 0.9, "proposal": None}


class CandidateBridgeTests(unittest.TestCase):
    def test_final_assistant_document_requires_exact_json(self) -> None:
        self.assertIsNone(adapter._final_assistant_document({"entries": [{"role": "assistant", "text": "Here is the result: {}"}]}))
        self.assertEqual(adapter._final_assistant_document({"entries": [{"role": "assistant", "text": json.dumps(review())}]}), review())

    def test_candidate_is_bound_to_the_exact_assignment_bytes(self) -> None:
        raw = json.dumps(assignment(), sort_keys=True, separators=(",", ":")).encode()
        transcript = {"entries": [{"role": "assistant", "text": json.dumps(review())}]}
        candidate = adapter._candidate_from_transcript(raw, transcript)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["snapshot_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(candidate["artifact"]["verdict"], "no-impact")

    def test_nonfinal_or_invalid_assistant_output_is_not_a_candidate(self) -> None:
        raw = json.dumps(assignment(), sort_keys=True, separators=(",", ":")).encode()
        self.assertIsNone(adapter._candidate_from_transcript(raw, {"entries": [{"role": "assistant", "text": "working"}]}))
        bad = review()
        bad["identity"] = {**bad["identity"], "head_sha": "c" * 40}
        self.assertIsNone(adapter._candidate_from_transcript(raw, {"entries": [{"role": "assistant", "text": json.dumps(bad)}]}))

    def test_dispatch_places_the_immutable_assignment_in_the_city_task(self) -> None:
        source = assignment()
        raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            run = {"assignment_bytes": __import__("base64").b64encode(raw).decode(), "assignment": source}
            with mock.patch.dict(os.environ, {"GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(root / "assignments"), "GC_GITHUB_DOCS_CANDIDATE_DIR": str(root / "candidates"), "GC_CITY_ROOT": "/city"}, clear=False), mock.patch.object(adapter.subprocess, "run") as command:
                adapter._dispatch_city(run)
            request = json.loads((root / "requests" / f"{digest}.json").read_text())
            description = request["description"]
            self.assertIn(raw.decode(), description)
            self.assertNotIn(f"assignments/{digest}.json", description)
            self.assertEqual(command.call_count, 0)
            self.assertEqual(request["source_key"], source["identity"]["source_key"])

    def test_city_dispatcher_slings_only_pending_durable_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            marker = root / "dispatch" / ("d" * 64 + ".json")
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "bead-1", "source_key": "github-pr:17:9:" + SHA, "dispatched": False}))
            with mock.patch.dict(os.environ, {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "gascity/github-docs-impact.docs-impact-reviewer"}, clear=False), mock.patch.object(dispatcher.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="{}", stderr="")) as command:
                self.assertEqual(dispatcher.dispatch_pending(), [marker.stem])
            self.assertEqual(command.call_args.args[0][:5], ["gc", "--city", "/city", "sling", "gascity/github-docs-impact.docs-impact-reviewer"])
            self.assertEqual(json.loads(marker.read_text()), {"bead_id": "bead-1", "source_key": "github-pr:17:9:" + SHA, "dispatched": True})

    def test_city_dispatcher_creates_review_work_in_the_target_rig(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            request = root / "requests" / ("d" * 64 + ".json")
            request.parent.mkdir()
            request.write_text(json.dumps({"source_key": "github-pr:17:9:" + SHA, "description": "review", "metadata": "{}"}))
            result = mock.Mock(returncode=0, stdout=json.dumps({"id": "mp-1"}), stderr="")
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", return_value=result) as command:
                self.assertEqual(dispatcher.create_pending(), [request.stem])
            self.assertEqual(command.call_args.args[0][:6], ["gc", "--city", "/city", "--rig", "my-project", "bd"])

    def test_city_peek_export_recovers_only_the_canonical_review_object(self) -> None:
        rendered = "intro\n• " + json.dumps(review()) + "\n\n› Implement {feature}"
        self.assertEqual(dispatcher._review_from_peek({"output": rendered}), review())
        self.assertIsNone(dispatcher._review_from_peek({"output": "• {not json}"}))


if __name__ == "__main__":
    unittest.main()
