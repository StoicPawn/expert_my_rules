import tempfile
import unittest
from pathlib import Path

from awb.core.models import Task
from awb.core.storage import Ledger
from awb.web.live_app import INJECTION, _candidate_review_html, _human_event


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

    def test_candidate_inspector_reads_full_persisted_worker_output_without_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(id="SEED-001", title="Audit theorem", description="x")
            ledger.upsert_task(task)
            full = "candidate-start\n" + ("proof-detail " * 100) + "\ncandidate-end"
            ledger.event("work_output", {"stage": "execute", "text": full}, task.id)

            rendered = _candidate_review_html(root, ledger, task.id)

            self.assertIn("candidate-start", rendered)
            self.assertIn("candidate-end", rendered)
            self.assertIn("review pending / not yet persisted", rendered)
            self.assertIn("Read-only view", rendered)

    def test_candidate_inspector_shows_latest_reviewer_objections_and_recommendations(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / "ledger.sqlite3")
            task = Task(id="SEED-001", title="Audit theorem", description="x")
            ledger.upsert_task(task)
            ledger.event("work_output", {"stage": "execute", "text": "full theorem candidate"}, task.id)
            ledger.event(
                "review",
                {
                    "stage": "review",
                    "approved": False,
                    "critical_objections": ["prove the disjoint-union step"],
                    "recommendations": ["pin the exact strong-Markov theorem"],
                },
                task.id,
            )

            rendered = _candidate_review_html(root, ledger, task.id)

            self.assertIn("challenged", rendered)
            self.assertIn("prove the disjoint-union step", rendered)
            self.assertIn("pin the exact strong-Markov theorem", rendered)

    def test_live_polling_does_not_replace_unchanged_dom_and_preserves_scroll(self):
        self.assertIn("if(next===lastHtml) return;", INJECTION)
        self.assertIn("const oldTop=oldFeed ? oldFeed.scrollTop : 0;", INJECTION)
        self.assertIn("newFeed.scrollTop=Math.min(oldTop", INJECTION)
        self.assertIn("const pinned=", INJECTION)
        self.assertIn("let refreshing=false;", INJECTION)
        self.assertIn("if(refreshing) return;", INJECTION)
        self.assertIn("-webkit-overflow-scrolling:touch", INJECTION)
        self.assertIn("candidate-review-inspector", INJECTION)
        self.assertIn("/inspection", INJECTION)
        self.assertIn("setInterval(refreshInspection,15000)", INJECTION)


if __name__ == "__main__":
    unittest.main()
