from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock


# The adapter's lifecycle dependencies are supplied by the mounted GitHub
# pack at runtime. These unit tests exercise only Compose-owned dispatch and
# candidate helpers, so replace that external boundary with empty modules.
# This keeps CI independent of a sibling checkout that GitHub Actions lacks.
for module_name in (
    "github_intake_common",
    "github_intake_docs_impact",
    "github_intake_docs_review_runtime",
):
    sys.modules[module_name] = types.ModuleType(module_name)


def _validate_final_candidate(raw_assignment: bytes, envelope: dict[str, object]) -> dict[str, object]:
    """Minimal boundary double; pack tests own candidate-schema validation."""
    assignment = json.loads(raw_assignment)
    artifact = envelope.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("identity") != assignment.get("identity"):
        raise ValueError("candidate identity must match its immutable assignment")
    if artifact.get("verdict") == "proposal-ready" and artifact.get("proposal") is None:
        raise ValueError("proposal-ready requires a complete proposal")
    return envelope


worker = types.ModuleType("github_intake_docs_patch_worker")
worker.validate_final_candidate = _validate_final_candidate
sys.modules[worker.__name__] = worker

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
        "evidence_bundle": {"head_sha": SHA, "source_head_ref": "feature/docs-change", "proposal_identity": {"repository_id": "17", "repository": "example/docs", "pr_number": 9, "base_sha": "b" * 40, "head_sha": SHA, "head_repository_id": "17", "head_repository": "example/docs", "base_ref": "main"}, "files": [{"path": "docs/guide.md", "reference": f"github://example/docs/blob/{SHA}/docs/guide.md", "patch": "@@ -1 +1 @@\n-old\n+new\n"}]},
    }


def review() -> dict[str, object]:
    source = assignment()["identity"]
    return {"schema_version": 1, "kind": "github-pr-docs-impact-review", "identity": source, "agent_skill": "developer-experience-techdocs", "verdict": "no-impact", "rationale": "The documentation is sufficient.", "evidence": [{"path": "docs/guide.md", "evidence": f"github://example/docs/blob/{SHA}/docs/guide.md"}], "confidence": 0.9, "proposal": None}


class CandidateBridgeTests(unittest.TestCase):
    def test_stale_blocking_candidate_is_ignored_before_direct_child_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            candidate = {"artifact": {**review(), "verdict": "docs-change-required"}}
            (root / "candidates").mkdir()
            (root / "candidates" / "candidate.json").write_text(json.dumps(candidate))
            store = mock.Mock()
            store.load.return_value = {"state": "stale"}
            environment = {"GC_GITHUB_DOCS_CANDIDATE_DIR": str(root / "candidates"), "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(root / "runs")}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(adapter.runtime, "FileDocsReviewStore", return_value=store, create=True), mock.patch.object(adapter, "ComposeAdapter"), mock.patch.object(adapter, "_gateway"), mock.patch.object(adapter, "_persist_direct_child") as persist:
                self.assertEqual(adapter.candidates(100), [{"accepted": False, "reason": "stale review run"}])
            persist.assert_not_called()

    def test_docs_change_candidate_becomes_a_source_bound_direct_child_request(self) -> None:
        candidate = {"artifact": {**review(), "verdict": "docs-change-required"}}
        source_assignment = assignment()
        source_assignment["evidence_bundle"]["source_head_ref"] = "release/docs-revision"
        candidate["coverage_cells"] = [
            {"identity": "developer:use-interface:how-to", "classification": "unmet", "evidence_paths": ["docs/guide.md"]},
            {"identity": "developer:use-interface:reference", "classification": "unmet", "evidence_paths": ["docs/reference.md"]},
        ]

        request = adapter._direct_child_request(candidate, source_assignment)

        self.assertEqual(request["repository"], "example/docs")
        self.assertEqual(request["source_key"], source_assignment["identity"]["source_key"])
        self.assertEqual(request["snapshot_sha"], SHA)
        self.assertEqual(request["coverage_cells"], candidate["coverage_cells"])
        self.assertEqual(request["execution_budgets"]["max_children"], 1)
        self.assertEqual(request["execution_budgets"]["max_docs_prs"], 1)

    def test_docs_change_candidate_persists_one_direct_child_without_controller_handoff(self) -> None:
        candidate = {"artifact": {**review(), "verdict": "docs-change-required"}}
        candidate["coverage_cells"] = [{"identity": "developer:use-interface:how-to", "classification": "unmet", "evidence_paths": ["docs/guide.md"]}]
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root))
        run = {"assignment": assignment()}
        store = mock.Mock()
        store.load.return_value = run
        environment = {
            "GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(root / "assignments"),
            "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(root / "runs"),
        }
        admission = {"schema_version": 1, "kind": "github-docs-recursion-direct-admission", "recursion_identity": "github-docs-recursion:17:test", "admitted_child": {"identity": "child", "key": "child"}}
        with mock.patch.dict(os.environ, {**environment, "GITHUB_INSTALLATION_ID": "91"}, clear=False), mock.patch.object(adapter.runtime, "FileDocsReviewStore", return_value=store, create=True), mock.patch.object(adapter.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(admission), stderr="")) as command:
            marker = adapter._persist_direct_child(candidate)

        self.assertEqual(marker["kind"], "github-pr-docs-direct-child")
        self.assertEqual(marker["source_key"], assignment()["identity"]["source_key"])
        self.assertEqual(marker["snapshot_sha"], SHA)
        self.assertEqual(marker["coverage_cells"], candidate["coverage_cells"])
        self.assertEqual(marker["execution_budgets"]["max_children"], 1)
        self.assertFalse(marker["dispatched"])
        self.assertIn("github_intake_docs_direct_child_admit.py", command.call_args.args[0][1])
        self.assertEqual(len(list((root / "direct-child-dispatch").glob("*.json"))), 1)
        self.assertFalse((root / "journey-dispatch").exists())

    def test_replaying_candidate_reuses_its_direct_child_without_duplicate_followup_intent(self) -> None:
        candidate = {"artifact": {**review(), "verdict": "docs-change-required"}}
        candidate["coverage_cells"] = [{"identity": "developer:use-interface:how-to", "classification": "unmet", "evidence_paths": ["docs/guide.md"]}]
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            store = mock.Mock()
            store.load.return_value = {"assignment": assignment()}
            environment = {"GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(root / "assignments"), "GC_GITHUB_DOCS_REVIEW_RUNS_DIR": str(root / "runs")}
            admission = {"schema_version": 1, "kind": "github-docs-recursion-direct-admission", "recursion_identity": "github-docs-recursion:17:test", "admitted_child": {"identity": "child", "key": "child"}}
            with mock.patch.dict(os.environ, {**environment, "GITHUB_INSTALLATION_ID": "91"}, clear=False), mock.patch.object(adapter.runtime, "FileDocsReviewStore", return_value=store, create=True), mock.patch.object(adapter.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(admission), stderr="")):
                first = adapter._persist_direct_child(candidate)
                replay = adapter._persist_direct_child(candidate)

            self.assertEqual(replay, first)
            self.assertEqual(len(list((root / "direct-child-dispatch").glob("*.json"))), 1)
            self.assertNotIn("followup", first)

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

    def test_invalid_final_decision_becomes_inconclusive(self) -> None:
        """A completed malformed answer must finish the visible Check safely."""
        raw = json.dumps(assignment(), sort_keys=True, separators=(",", ":")).encode()
        malformed = review()
        malformed["verdict"] = "proposal-ready"
        transcript = {"entries": [{"role": "assistant", "text": json.dumps(malformed)}]}

        candidate = adapter._inconclusive_from_invalid_final(raw, transcript)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["artifact"]["verdict"], "inconclusive")
        self.assertIsNone(candidate["artifact"]["proposal"])
        self.assertEqual(candidate["artifact"]["identity"], assignment()["identity"])

    def test_invalid_completed_transcript_becomes_inconclusive(self) -> None:
        raw = json.dumps(assignment(), sort_keys=True, separators=(",", ":")).encode()
        transcript = {"entries": [{"role": "system", "text": "invalid-final"}]}

        candidate = adapter._inconclusive_from_invalid_final(raw, transcript)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["artifact"]["verdict"], "inconclusive")

    def test_invalid_completion_sentinel_harvests_an_inconclusive_candidate(self) -> None:
        source = assignment()
        raw = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "assignments").mkdir()
            (root / "dispatch").mkdir()
            (root / "transcripts").mkdir()
            (root / "assignments" / f"{digest}.json").write_bytes(raw)
            (root / "dispatch" / f"{digest}.json").write_text(json.dumps({"bead_id": "mp-1", "source_key": source["identity"]["source_key"], "dispatched": True}))
            (root / "transcripts" / f"{digest}.json").write_text(json.dumps({"entries": [{"role": "system", "text": "invalid-final"}]}))
            with mock.patch.dict(os.environ, {"GC_GITHUB_DOCS_ASSIGNMENT_DIR": str(root / "assignments"), "GC_GITHUB_DOCS_CANDIDATE_DIR": str(root / "candidates")}, clear=False):
                self.assertEqual(adapter._harvest_city_candidates(), [digest])
            candidate = json.loads((root / "candidates" / f"{digest}.json").read_text())
            self.assertEqual(candidate["artifact"]["verdict"], "inconclusive")

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

    def test_city_dispatcher_slings_only_persisted_direct_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            marker = root / "direct-child-dispatch" / ("d" * 64 + ".json")
            marker.parent.mkdir()
            direct_child = {"key": "github-pr-docs-child:" + "d" * 64, "source_key": "github-pr:17:9:" + SHA, "snapshot_sha": SHA, "coverage_cells": [{"identity": "developer:use-interface:how-to", "classification": "unmet", "evidence_paths": ["docs/guide.md"]}], "execution_budgets": {"max_depth": 1, "max_children": 1, "max_docs_prs": 1, "max_elapsed_seconds": 86400, "max_non_progress": 3}}
            admission = {"schema_version": 1, "kind": "github-docs-recursion-direct-admission", "recursion_identity": "github-docs-recursion:17:test", "admitted_child": direct_child}
            marker.write_text(json.dumps({"schema_version": 1, "kind": "github-pr-docs-direct-child", "candidate_digest": "d" * 64, "repository_id": "17", "repository": "example/docs", "pr_number": 9, "source_key": "github-pr:17:9:" + SHA, "snapshot_sha": SHA, "source_branch": "feature/docs", "candidate_identity": assignment()["identity"], "coverage_cells": direct_child["coverage_cells"], "execution_budgets": direct_child["execution_budgets"], "admission": admission, "direct_child": direct_child, "bead_id": None, "dispatched": False}))
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_DIRECT_CHILD_TARGET": "my-project/github-docs-impact.docs-journey"}
            commands = [mock.Mock(returncode=0, stdout=json.dumps([{"id": "bead-direct", "metadata": {"github.docs_direct_child": direct_child}}]), stderr=""), mock.Mock(returncode=0, stdout="{}", stderr="")]
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=commands) as command:
                self.assertEqual(dispatcher.dispatch_direct_child_pending(), [marker.stem])
            self.assertEqual(command.call_args_list[1].args[0][:5], ["gc", "--city", "/city", "sling", "my-project/github-docs-impact.docs-journey"])
            self.assertNotIn("create", command.call_args_list[0].args[0])
            self.assertTrue(json.loads(marker.read_text())["dispatched"])

    def test_direct_child_adoption_failure_is_not_treated_as_permission_to_create(self) -> None:
        """Catches a transient City lookup duplicating a child after a crash."""
        marker = {"direct_child": {"key": "github-pr-docs-child:deadbeef"}}
        failed = mock.Mock(returncode=1, stdout="", stderr="City unavailable")
        with mock.patch.object(dispatcher.subprocess, "run", return_value=failed) as command:
            self.assertEqual(dispatcher._adopt_direct_child_bead("/city", "my-project", marker), (False, None))
        self.assertIn("--all", command.call_args.args[0])
        self.assertIn("--limit", command.call_args.args[0])

    def test_city_dispatcher_records_one_completed_journey_worker_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            marker = root / "journey-dispatch" / ("d" * 64 + ".json")
            marker.parent.mkdir()
            admitted = {"journey_identity": "github-docs-journey:17:key:" + SHA, "snapshot_sha": SHA, "decision_identity": {"source_key": "github-pr:17:9:" + SHA}, "decision_digest": "digest", "source_key": "github-pr:17:9:" + SHA, "source_url": "https://github.com/example/docs/pull/9", "documentation_entry_point": "README.md", "parent_issue_url": "https://github.com/example/docs/pull/9", "evidence_paths": ["docs/guide.md"]}
            marker.write_text(json.dumps({"bead_id": "bead-journey", "child_key": "child-1", "journey_identity": "github-docs-journey:17:key:" + SHA, "admitted_child": admitted, "dispatched": True}))
            update = {"schema_version": 1, "kind": "github-docs-journey-child-update", "admitted_child": admitted, "state": "complete"}
            commands = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "closed", "metadata": {"docs-journey.result": json.dumps(update)}}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"journey": {"children": [{"key": "child-1", "state": "complete"}]}}), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"journey": {"state": "baseline-complete", "actions": [{"kind": "create_docs_pr", "state": "completed"}]}}), stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_DIRECT_CHILD_TARGET": "my-project/github-docs-impact.docs-journey", "GC_GITHUB_PACK_SCRIPTS": "/pack/scripts"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=commands) as command:
                self.assertEqual(dispatcher.harvest_journey_updates(), [marker.stem])
            self.assertEqual(command.call_count, 3)
            self.assertIn("record-child-update", command.call_args_list[1].args[0])
            self.assertIn("project-until-settled", command.call_args_list[2].args[0])
            self.assertEqual(json.loads(marker.read_text())["dispatched"], "recorded")

    def test_journey_worker_update_must_echo_its_admitted_child(self) -> None:
        child = {"journey_identity": "journey", "snapshot_sha": SHA, "decision_identity": {"source_key": "source"}, "decision_digest": "digest", "source_key": "source", "source_url": "url", "documentation_entry_point": "README.md", "parent_issue_url": "url", "evidence_paths": ["docs/guide.md"]}
        update = {"admitted_child": {**child, "decision_digest": "other"}}

        self.assertFalse(dispatcher._matches_admitted_child(child, update))

    def test_followup_pr_keeps_originating_check_pending_until_current_sha_is_reviewed(self) -> None:
        self.assertEqual(dispatcher._original_check_outcome({"state": "baseline-complete", "actions": [{"kind": "create_docs_pr", "state": "completed"}]}), "pending")
        self.assertEqual(dispatcher._original_check_outcome({"state": "baseline-complete", "actions": []}), "action_required")
        self.assertEqual(dispatcher._original_check_outcome({"state": "budget-exhausted", "actions": []}), "action_required")

    def test_dispatcher_normal_loop_reconciles_persisted_legacy_journeys(self) -> None:
        """Catches an upgrade silently stranding a pre-existing journey marker."""
        with mock.patch.object(dispatcher, "retire_superseded", return_value=[]), mock.patch.object(dispatcher, "create_pending", return_value=[]), mock.patch.object(dispatcher, "dispatch_pending", return_value=[]), mock.patch.object(dispatcher, "dispatch_direct_child_pending", return_value=[]), mock.patch.object(dispatcher, "harvest_direct_child_updates", return_value=[]), mock.patch.object(dispatcher, "dispatch_journey_pending", return_value=["legacy"] ) as dispatch_legacy, mock.patch.object(dispatcher, "harvest_journey_updates", return_value=["legacy"]) as harvest_legacy, mock.patch.object(dispatcher, "export_transcripts", return_value=[]), mock.patch.object(sys, "argv", ["dispatcher", "--once"]):
            self.assertEqual(dispatcher.main(), 0)
        dispatch_legacy.assert_called_once_with()
        harvest_legacy.assert_called_once_with()

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
            invocation = command.call_args.args[0]
            self.assertIn("--body-file", invocation)
            self.assertNotIn("--description", invocation)

    def test_city_dispatcher_retires_superseded_pr_work_before_dispatching_new_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            older, newer = "d" * 64, "e" * 64
            source_prefix = "github-pr:17:9:"
            for digest, sha in ((older, "a" * 40), (newer, "b" * 40)):
                request = root / "requests" / f"{digest}.json"
                request.parent.mkdir(exist_ok=True)
                request.write_text(json.dumps({"source_key": source_prefix + sha, "description": "review", "metadata": "{}"}))
            marker = root / "dispatch" / f"{older}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-old", "source_key": source_prefix + "a" * 40, "dispatched": True}))
            result = mock.Mock(returncode=0, stdout="{}", stderr="")
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", return_value=result) as command:
                self.assertEqual(dispatcher.retire_superseded(), [older])
            self.assertEqual(command.call_args.args[0], ["gc", "--city", "/city", "--rig", "my-project", "bd", "close", "mp-old", "--force", "--reason", "Superseded by a newer GitHub pull-request revision", "--json"])
            self.assertEqual(json.loads(marker.read_text())["dispatched"], "retired")

    def test_city_peek_export_recovers_only_the_canonical_review_object(self) -> None:
        rendered = "intro\n• " + json.dumps(review()) + "\n\n› Implement {feature}"
        self.assertEqual(dispatcher._review_from_peek({"output": rendered}), review())
        self.assertIsNone(dispatcher._review_from_peek({"output": "• {not json}"}))

    def test_city_export_closes_an_open_reviewer_bead_after_persisting_valid_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "in_progress"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": "• " + json.dumps(review())}), stderr=""),
                mock.Mock(returncode=0, stdout="{}", stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result) as command:
                self.assertEqual(dispatcher.export_transcripts(), [digest])
            self.assertEqual(
                command.call_args_list[2].args[0],
                ["gc", "--city", "/city", "--rig", "my-project", "bd", "update", "mp-1", "--status", "closed", "--if-assignee", "mc-1", "--if-status", "in_progress", "--session", "mc-1", "--json"],
            )

    def test_city_export_retries_a_durable_close_intent_after_a_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "in_progress"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": "• " + json.dumps(review())}), stderr=""),
                mock.Mock(returncode=1, stdout="", stderr="temporarily unavailable"),
                mock.Mock(returncode=0, stdout="{}", stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result) as command:
                self.assertEqual(dispatcher.export_transcripts(), [digest])
                self.assertEqual(dispatcher.export_transcripts(), [])
            self.assertEqual(command.call_count, 4)
            self.assertEqual(json.loads((root / "completions" / f"{digest}.json").read_text())["state"], "closed")

    def test_city_export_recovers_close_intent_from_a_persisted_transcript_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            transcript = root / "transcripts" / f"{digest}.json"
            transcript.parent.mkdir()
            transcript.write_text(json.dumps({"entries": [{"role": "assistant", "text": json.dumps(review())}], "session_id": "mc-1"}))
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="{}", stderr="")) as command:
                self.assertEqual(dispatcher.export_transcripts(), [])
            self.assertEqual(command.call_args.args[0][6:12], ["update", "mp-1", "--status", "closed", "--if-assignee", "mc-1"])
            self.assertEqual(json.loads((root / "completions" / f"{digest}.json").read_text())["state"], "closed")

    def test_city_export_does_not_close_a_reaper_reassigned_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "in_progress"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": "• " + json.dumps(review())}), stderr=""),
                mock.Mock(returncode=13, stdout="", stderr="assignee guard failed"),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result) as command:
                self.assertEqual(dispatcher.export_transcripts(), [digest])
                self.assertEqual(dispatcher.export_transcripts(), [])
            self.assertEqual(command.call_count, 3)
            self.assertEqual(json.loads((root / "completions" / f"{digest}.json").read_text())["state"], "ownership-changed")

    def test_city_export_marks_closed_non_json_output_as_invalid_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "closed"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": "review could not be completed"}), stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result):
                self.assertEqual(dispatcher.export_transcripts(), [digest])
            transcript = json.loads((root / "transcripts" / f"{digest}.json").read_text())
            self.assertEqual(transcript, {"entries": [{"role": "system", "text": "invalid-final"}]})

    def test_city_export_marks_closed_wrong_identity_as_invalid_final(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            wrong = review()
            wrong["identity"] = {**wrong["identity"], "source_key": "github-pr:17:9:" + "b" * 40}
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1", "status": "closed"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": json.dumps(wrong)}), stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result):
                self.assertEqual(dispatcher.export_transcripts(), [digest])
            transcript = json.loads((root / "transcripts" / f"{digest}.json").read_text())
            self.assertEqual(transcript, {"entries": [{"role": "system", "text": "invalid-final"}]})

    def test_city_export_replaces_a_stale_transcript_for_the_same_assignment(self) -> None:
        """An older session result must never freeze a newer SHA's marker."""
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            digest = "d" * 64
            source_key = f"github-pr:17:9:{SHA}"
            marker = root / "dispatch" / f"{digest}.json"
            marker.parent.mkdir()
            marker.write_text(json.dumps({"bead_id": "mp-1", "source_key": source_key, "dispatched": True}))
            transcript = root / "transcripts" / f"{digest}.json"
            transcript.parent.mkdir()
            stale = review()
            stale["identity"] = {**stale["identity"], "source_key": "github-pr:17:9:" + "c" * 40}
            transcript.write_text(json.dumps({"entries": [{"role": "assistant", "text": json.dumps(stale)}]}))
            result = [
                mock.Mock(returncode=0, stdout=json.dumps([{"assignee": "mc-1"}]), stderr=""),
                mock.Mock(returncode=0, stdout=json.dumps({"output": "• " + json.dumps(review())}), stderr=""),
                mock.Mock(returncode=0, stdout="{}", stderr=""),
            ]
            environment = {"GC_CITY_DOCS_REVIEW_DIR": str(root), "CITY_PATH": "/city", "GC_CITY_DOCS_REVIEW_TARGET": "my-project/github-docs-impact.docs-impact-reviewer"}
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(dispatcher.subprocess, "run", side_effect=result):
                self.assertEqual(dispatcher.export_transcripts(), [digest])
            exported = json.loads(transcript.read_text())
            self.assertEqual(json.loads(exported["entries"][-1]["text"])["identity"]["source_key"], source_key)
            self.assertEqual(len(list((root / "transcripts" / "rejected").glob(f"{digest}.*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
