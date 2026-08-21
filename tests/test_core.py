import tempfile
import unittest
from pathlib import Path

from awb.core.models import JobStatus, TaskStatus
from awb.core.orchestrator import Orchestrator
from awb.core.planner import propose_manifest
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.core.tools import ToolRunner
from awb.providers.providers import MockProvider
from awb.providers.base import ModelProvider
from awb.templates.templates import custom_manifest, software_manifest


class ToolCallingProvider(ModelProvider):
    def __init__(self):
        self.worker_calls = 0

    def generate(self, system: str, user: str) -> str:
        if "REVIEW_JSON" in system or "adversarial Reviewer" in system:
            return '{"approved": true, "critical_objections": [], "recommendations": []}'
        if "DIRECTOR_JSON" in system or "Director of an autonomous" in system:
            return '{"title":"Next","description":"Continue","priority":1}'
        if "AVAILABLE TOOLS" in system:
            self.worker_calls += 1
            if self.worker_calls == 1:
                return '{"tool":"write","arguments":{"path":"result.txt","content":"created by tool"}}'
            return "Implemented the requested artifact and verified that it was written."
        return "done"


class WorkbenchTests(unittest.TestCase):
    def test_workspace_and_loop_persists_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Ship a verified demo"))
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

    def test_worker_can_call_declared_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            manifest = custom_manifest("demo", "Create a result")
            manifest["tools"] = [
                {"id":"write","type":"write_file","description":"write","writable":True}
            ]
            for agent in manifest["agents"]:
                if agent["role"] == "worker":
                    agent["tools"] = ["write"]
            write_workspace(root, manifest)
            ws = load_workspace(root)
            ledger = Ledger(root / "ledger.sqlite3")
            from awb.core.models import Task
            ledger.upsert_task(Task(id="USER-0001", title="Create", description="Create result.txt", priority=10, created_by="user"))
            provider = ToolCallingProvider()
            Orchestrator(ws, provider).step()
            self.assertEqual((root / "result.txt").read_text(), "created by tool")
            self.assertTrue(any(e["kind"] == "tool_call" for e in ledger.recent_events(30)))

    def test_tool_layer_is_workspace_sandboxed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            manifest = custom_manifest("demo", "Use tools")
            manifest["tools"] = [
                {"id":"read","type":"read_file","description":"read"},
                {"id":"write","type":"write_file","description":"write","writable":True},
            ]
            write_workspace(root, manifest)
            runner = ToolRunner(load_workspace(root))
            result = runner.execute("write", {"path": "notes/result.md", "content": "evidence"})
            self.assertTrue(result["ok"])
            self.assertEqual(runner.execute("read", {"path": "notes/result.md"})["content"], "evidence")
            with self.assertRaises(Exception):
                runner.execute("read", {"path": "../escape.txt"})

    def test_persistent_job_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Run later"))
            ledger = Ledger(root / "ledger.sqlite3")
            job_id = ledger.create_job(60, 100)
            self.assertEqual(ledger.get_job(job_id)["status"], JobStatus.QUEUED.value)
            ledger.update_job(job_id, status=JobStatus.PAUSED, steps_done=4, detail="paused")
            reopened = Ledger(root / "ledger.sqlite3").get_job(job_id)
            self.assertEqual(reopened["status"], JobStatus.PAUSED.value)
            self.assertEqual(reopened["steps_done"], 4)

    def test_goal_first_planner_has_safe_local_defaults(self):
        manifest = propose_manifest(
            "Obtain a rigorous result ready for an Annals of Probability paper",
            name="paper_goal",
            use_local_ai=False,
        )
        self.assertEqual(manifest["type"], "research")
        self.assertEqual(manifest["runtime"]["default_provider"]["kind"], "ollama")
        self.assertFalse(manifest["runtime"]["escalation"]["enabled"])
        self.assertEqual(manifest["runtime"]["escalation"]["daily_budget_eur"], 0.0)
        self.assertGreaterEqual(len(manifest["gates"]), 4)

    def test_continuous_jobs_are_recoverable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "demo"
            write_workspace(root, custom_manifest("demo", "Keep working until done"))
            ledger = Ledger(root / "ledger.sqlite3")
            job_id = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(job_id, status=JobStatus.RUNNING, detail="active")
            recovered = Ledger(root / "ledger.sqlite3").recoverable_jobs()
            self.assertEqual([j["id"] for j in recovered], [job_id])


if __name__ == "__main__":
    unittest.main()
