import tempfile
import unittest
from pathlib import Path

from awb.core.models import Task, TaskStatus
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.providers import MockProvider
from awb.templates.templates import custom_manifest


class RecoveryTests(unittest.TestCase):
    def test_orchestrator_reopens_interrupted_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Finish safely"))
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(id="TASK-STUCK", title="Stuck", description="Resume me", status=TaskStatus.IN_PROGRESS, priority=5, created_by="director")
            ledger.upsert_task(task)

            orch = Orchestrator(load_workspace(root), MockProvider())
            recovered = Ledger(root / "ledger.sqlite3").get_task("TASK-STUCK")
            self.assertEqual(recovered.status, TaskStatus.OPEN)
            self.assertEqual(orch.choose_next_task().id, "TASK-STUCK")

    def test_model_call_events_are_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Trace model calls"))
            orch = Orchestrator(load_workspace(root), MockProvider())
            orch.step()
            kinds = {event["kind"] for event in Ledger(root / "ledger.sqlite3").recent_events(100)}
            self.assertIn("model_call_started", kinds)
            self.assertIn("model_call_finished", kinds)


if __name__ == "__main__":
    unittest.main()
