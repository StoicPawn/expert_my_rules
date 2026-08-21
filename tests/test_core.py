import tempfile
import unittest
from pathlib import Path

from awb.core.models import TaskStatus
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.providers import MockProvider
from awb.templates.templates import custom_manifest, software_manifest


class WorkbenchTests(unittest.TestCase):
    def test_workspace_and_loop_persists_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            manifest = custom_manifest("demo", "Ship a verified demo")
            write_workspace(root, manifest)
            ws = load_workspace(root)
            orch = Orchestrator(ws, MockProvider())
            result = orch.step()
            self.assertTrue(result.review.approved)
            self.assertTrue(result.verification_passed)
            self.assertEqual(result.task.status, TaskStatus.DONE)
            self.assertTrue((root / result.task.metadata["artifact"]).exists())
            self.assertFalse(orch.is_complete())

    def test_validator_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            manifest = software_manifest("demo", "Pass gate")
            manifest["validators"] = {"tests": "python -c \"print('ok')\""}
            manifest["gates"] = [{"id": "tests_pass", "description": "tests", "required": True, "validator": "tests"}]
            write_workspace(root, manifest)
            orch = Orchestrator(load_workspace(root), MockProvider())
            self.assertTrue(orch.is_complete())

    def test_manual_gate_controls_completion(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Finish goal"))
            ws = load_workspace(root)
            orch = Orchestrator(ws, MockProvider())
            self.assertFalse(orch.is_complete())
            Ledger(root / "ledger.sqlite3").set_gate("goal_verified", True, "human accepted")
            self.assertTrue(orch.is_complete())

    def test_bounded_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Keep improving"))
            results = Orchestrator(load_workspace(root), MockProvider()).run(max_steps=2, max_minutes=1)
            self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
