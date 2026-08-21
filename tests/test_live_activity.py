import tempfile
import unittest
from pathlib import Path

from awb.core.models import Task
from awb.core.storage import Ledger
from awb.web.live_app import _human_event


class LiveActivityTests(unittest.TestCase):
    def test_model_call_event_is_human_readable(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "ledger.sqlite3")
            task = Task(id="TASK-1", title="Build CSV parser", description="x")
            ledger.upsert_task(task)
            state, title, detail = _human_event(
                {
                    "kind": "model_call_started",
                    "task_id": "TASK-1",
                    "payload": {"role": "worker", "provider": "OllamaProvider"},
                },
                ledger,
            )
            self.assertEqual(state, "active")
            self.assertIn("Worker", title)
            self.assertIn("Build CSV parser", detail)

    def test_review_event_reports_objections(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Ledger(Path(td) / "ledger.sqlite3")
            state, title, detail = _human_event(
                {
                    "kind": "review",
                    "task_id": None,
                    "payload": {"approved": False, "critical_objections": ["a", "b"]},
                },
                ledger,
            )
            self.assertEqual(state, "warn")
            self.assertIn("challenged", title.lower())
            self.assertIn("2 critical objection", detail)


if __name__ == "__main__":
    unittest.main()
