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
