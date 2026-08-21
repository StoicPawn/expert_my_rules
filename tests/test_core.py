import tempfile
import unittest
from pathlib import Path

from awb.core.orchestrator import Orchestrator
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.providers import MockProvider
from awb.templates.templates import software_manifest


class WorkbenchTests(unittest.TestCase):
    def test_workspace_and_loop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            manifest = software_manifest("demo", "Ship a verified demo")
            manifest["validators"] = {}
            manifest["gates"] = [{"id": "manual", "description": "manual semantic gate", "required": True}]
            write_workspace(root, manifest)
            ws = load_workspace(root)
            orch = Orchestrator(ws, MockProvider())
            result = orch.step()
            self.assertTrue(result.review.approved)
            self.assertTrue(result.verification_passed)
            self.assertEqual(result.task.status.value, "DONE")
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


if __name__ == "__main__":
    unittest.main()
