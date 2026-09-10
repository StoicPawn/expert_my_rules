import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.models import Task, TaskStatus
from awb.core.storage import Ledger
from awb.web import research_console


class ResearchConsoleTests(unittest.TestCase):
    def test_mechanical_delta_keeps_literal_formula_sign_changes(self):
        before = "The candidate theorem uses x + y.\nBound: a <= b."
        after = "The candidate theorem uses x - y.\nBound: a <= b.\nNew counterexample: z < 0."
        diff = research_console._mechanical_delta(before, after)
        self.assertIn("-The candidate theorem uses x + y.", diff)
        self.assertIn("+The candidate theorem uses x - y.", diff)
        self.assertIn("+New counterexample: z < 0.", diff)

    def test_secret_like_fields_are_redacted_in_raw_machine_view(self):
        rendered = research_console._json({
            "tool": "example",
            "arguments": {"query": "safe", "api_key": "do-not-render", "password": "hidden"},
        })
        self.assertIn("safe", rendered)
        self.assertNotIn("do-not-render", rendered)
        self.assertNotIn("hidden", rendered)
        self.assertIn("***redacted***", rendered)

    def test_events_fragment_shows_exact_candidates_review_and_raw_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(
                id="SEED-0005",
                title="Model size trade off analysis",
                description="x",
                status=TaskStatus.IN_PROGRESS,
            )
            ledger.upsert_task(task)
            ledger.event("work_output", {"stage": "execute", "text": "First theorem: x + y."}, task.id)
            ledger.event("review", {
                "approved": False,
                "critical_objections": ["Sign is not justified."],
                "recommendations": ["Check the x term."],
            }, task.id)
            ledger.event("work_output", {"stage": "execute", "text": "Revised theorem: x - y."}, task.id)
            rendered = research_console._events_fragment(root)
            self.assertIn("Exact Worker candidate", rendered)
            self.assertIn("Revised theorem: x - y.", rendered)
            self.assertIn("Mechanical delta vs previous candidate", rendered)
            self.assertIn("Reviewer result", rendered)
            self.assertIn("Sign is not justified.", rendered)
            self.assertIn("Raw ledger event", rendered)

    def test_live_fragment_includes_current_task_machine_and_progress_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(
                id="SEED-0005",
                title="Model size trade off analysis",
                description="x",
                status=TaskStatus.IN_PROGRESS,
            )
            ledger.upsert_task(task)
            progress = {
                "model": "qwen3:4b",
                "state": "generating",
                "elapsed_seconds": 125,
                "chunks": 9,
                "output_chars": 420,
            }
            with patch.object(research_console, "get_progress", return_value=progress), \
                 patch.object(research_console, "_machine_summary", return_value="RAM 6/8 · CPU 95%"):
                rendered = research_console._live_fragment(root)
            self.assertIn("SEED-0005", rendered)
            self.assertIn("qwen3:4b", rendered)
            self.assertIn("2m 05s", rendered)
            self.assertIn("CPU 95%", rendered)
            self.assertIn("No visible content chunk", rendered)


if __name__ == "__main__":
    unittest.main()
