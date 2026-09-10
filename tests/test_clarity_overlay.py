import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.models import Task, TaskStatus
from awb.core.storage import Ledger
from awb.web import clarity_overlay


class ClarityOverlayTests(unittest.TestCase):
    def test_long_model_duration_is_human_readable(self):
        self.assertEqual(clarity_overlay._format_duration(46177), "12h 49m 37s")

    def test_work_output_explains_candidate_and_next_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(id="SEED-0005", title="Model size trade off analysis", description="x", status=TaskStatus.IN_PROGRESS)
            ledger.upsert_task(task)
            ledger.event("work_output", {"stage": "execute", "text": "candidate"}, task.id)
            rows = ledger.conn.execute(
                "SELECT seq,ts,kind,payload_json FROM events WHERE task_id=? ORDER BY seq DESC",
                (task.id,),
            ).fetchall()
            phase, explanation = clarity_overlay._phase_from_events(rows, task.title)
            self.assertIn("candidate has been saved", phase.lower())
            self.assertIn("adversarial review", explanation.lower())
            self.assertIn(task.title, explanation)

    def test_clarity_panel_separates_current_task_from_previous_model_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "project.yaml").write_text("name: test\n", encoding="utf-8")
            ledger = Ledger(root / "ledger.sqlite3")
            old = Task(id="SEED-0001", title="Reconstruct and audit the central theorem dependency graph", description="x", status=TaskStatus.BLOCKED)
            current = Task(id="SEED-0005", title="Model size trade off analysis", description="x", status=TaskStatus.IN_PROGRESS)
            ledger.upsert_task(old)
            ledger.upsert_task(current)
            ledger.event("work_output", {"stage": "execute", "text": "candidate"}, current.id)
            ledger.record_model_call(
                task_id=old.id,
                role="worker",
                node="local-ollama",
                kind="ollama",
                model="qwen3:4b",
                source="route",
                success=True,
                seconds=46177,
                chars=100,
            )
            with patch.object(clarity_overlay, "_machine_summary", return_value="RAM 6/8 · CPU 95%"):
                rendered = clarity_overlay._clarity_html(root)
            self.assertIn("SEED-0005", rendered)
            self.assertIn("Model size trade off analysis", rendered)
            self.assertIn("Reconstruct and audit the central theorem dependency graph", rendered)
            self.assertIn("12h 49m 37s", rendered)
            self.assertIn("history across tasks", rendered)

    def test_overlay_moves_resource_and_clarity_cards_next_to_live_activity(self):
        injection = clarity_overlay.CLARITY_INJECTION
        self.assertIn("machine-resource-card", injection)
        self.assertIn("history across all tasks", injection)
        self.assertIn("setInterval(refreshClarity,2000)", injection)


if __name__ == "__main__":
    unittest.main()
