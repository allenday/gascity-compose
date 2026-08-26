#!/usr/bin/env python3
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import city_mail_launcher as launcher


MESSAGE = {
    "type": "gc.intake.start-authorized.v1",
    "thread_id": "gc.tracker.example",
    "payload": {
        "id": "auth-1",
        "issue": {"repository": "admin/gascity-mail-fixture", "number": 42},
        "plan": {"id": "plan-1"},
        "pinned_base": "a" * 40,
    },
}


class LauncherProtocolTest(unittest.TestCase):
    def test_accepts_exact_authorization_and_builds_binding(self):
        self.assertEqual("auth-1", launcher.validate_authorization(MESSAGE)["payload"]["id"])
        self.assertEqual("bead-9", launcher.binding_for(MESSAGE, "bead-9")["payload"]["run_id"])
        self.assertEqual("gc-binding-5548710825af9134ac625b7befad29fe", launcher.binding_topic("auth-1"))

    def test_builds_city_local_request(self):
        request = launcher.request_for(MESSAGE)
        self.assertEqual("auth-1", request["authorization_id"])
        self.assertEqual("superpowers-build", request["formula"])

    def test_city_launch_command_supplies_configured_rig_and_artifact_root(self):
        command = launcher.city_launch_command(launcher.request_for(MESSAGE), "/opt/gascity/my-city", "my-project")
        self.assertEqual(["--rig", "my-project"], command[3:5])
        self.assertEqual(["--var", "artifact_root=/opt/gascity/my-city"], command[-9:-7])
        self.assertEqual(["--scope-kind", "rig", "--scope-ref", "my-project", "--json"], command[-5:])

    def test_city_service_receives_local_worker_rig(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        city_service = compose.split("  city:\n", 1)[1].split("\n  gitea:", 1)[0]
        self.assertIn("CITY_MAIL_LAUNCHER_RIG:", city_service)

    def test_launcher_uses_city_host_mapping_for_private_queue_compatibility(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        launcher_service = compose.split("  city-mail-launcher:\n", 1)[1].split("\n  gascity-mcp:", 1)[0]
        self.assertIn('user: "${HOST_UID:-1000}:${HOST_GID:-1000}"', launcher_service)

    def test_inbox_request_includes_message_bodies(self):
        self.assertEqual(
            {
                "project_key": "project",
                "agent_name": "gas-city-launcher",
                "registration_token": "registration",
                "limit": 100,
                "include_bodies": True,
            },
            launcher.inbox_arguments("project", "gas-city-launcher", "registration"),
        )

    def test_rejects_missing_immutable_base(self):
        with self.assertRaisesRegex(ValueError, "pinned_base"):
            invalid = {**MESSAGE, "payload": {key: value for key, value in MESSAGE["payload"].items() if key != "pinned_base"}}
            launcher.validate_authorization(invalid)

    def test_duplicate_record_reuses_existing_run_without_second_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "ledger.json"
            calls = []
            self.assertEqual(("bead-1", True), launcher.record_before_ack(path, "auth-1", "bead-1", lambda: calls.append("ack")))
            self.assertEqual(("bead-1", False), launcher.record_before_ack(path, "auth-1", "bead-2", lambda: calls.append("ack")))
            self.assertEqual(["ack", "ack"], calls)

    def test_failed_persistence_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "ledger.json"
            acknowledged = []
            with mock.patch.object(launcher.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    launcher.record_before_ack(path, "auth-1", "bead-1", lambda: acknowledged.append(True))
            self.assertEqual([], acknowledged)


if __name__ == "__main__":
    unittest.main()
