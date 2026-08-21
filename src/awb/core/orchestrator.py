from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from awb.providers.base import ModelProvider
from awb.providers.providers import make_provider
from .models import IterationResult, Review, Task, TaskStatus, Workspace
from .storage import Ledger
from .tools import ToolRunner, ToolError, parse_tool_message


DIRECTOR_SYSTEM = """You are the Director of an autonomous project workbench.
Choose exactly ONE next task that maximally advances the stated north-star goal.
Prefer falsification, blockers, failed gates and high-information work over cosmetic improvements.
Do not repeat a failed task unchanged: formulate a remediation, counterexample search, decomposition, or evidence-gathering task.
Return DIRECTOR_JSON only as JSON with: title, description, priority.
"""

WORKER_SYSTEM = """You are the Worker in an autonomous project workbench.
Execute the assigned task rigorously. Produce a concrete result that can be inspected by another expert.
Separate evidence, assumptions, uncertainty and conclusions. Do not claim completion without evidence.
When tools are available, you may call them by returning ONLY JSON of the form {"tool":"tool_id","arguments":{...}}.
After a tool result is returned, continue the task. When finished, return the final candidate result as normal text.
Never invent tool results.
"""

REVIEW_SYSTEM = """You are an adversarial Reviewer. Try to reject the candidate result.
Look for logical gaps, untested assumptions, missing cases, non-reproducibility, unsafe changes, circular reasoning and unsupported novelty.
Approve only if the assigned task has actually been addressed at the level claimed.
Return REVIEW_JSON only as JSON: approved (bool), critical_objections (list[str]), recommendations (list[str]).
"""


class Orchestrator:
    def __init__(self, workspace: Workspace, provider: ModelProvider | None = None):
        self.workspace = workspace
        self.provider_override = provider
        self.ledger = Ledger(workspace.root / "ledger.sqlite3")
        self.artifacts = workspace.root / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for gate in workspace.manifest.gates:
            if gate.id not in self.ledger.gate_state():
                self.ledger.set_gate(gate.id, False, "not evaluated")

    def _agent(self, role: str):
        return next((a for a in self.workspace.manifest.agents if a.role == role), None)

    def _provider(self, role: str) -> ModelProvider:
        if self.provider_override is not None:
            return self.provider_override
        agent = self._agent(role)
        spec = agent.provider if agent and agent.provider else self.workspace.manifest.runtime.default_provider
        return make_provider(spec.kind, spec.model)

    def _instructions(self, role: str) -> str:
        agent = self._agent(role)
        return agent.instructions if agent else ""

    def snapshot(self) -> str:
        tasks = [t.model_dump(mode="json") for t in self.ledger.list_tasks()]
        gates = self.ledger.gate_state()
        return json.dumps({
            "goal": self.workspace.manifest.goal,
            "description": self.workspace.manifest.description,
            "tasks": tasks[-40:],
            "gates": gates,
            "recent_events": self.ledger.recent_events(20),
        }, indent=2)

    def choose_next_task(self) -> Task:
        existing = self.ledger.list_tasks([TaskStatus.OPEN])
        if existing:
            return existing[0]

        blocked = self.ledger.list_tasks([TaskStatus.BLOCKED])
        context = self.snapshot()
        if blocked:
            context += "\n\nBLOCKED TASKS REQUIRE REMEDIATION:\n" + json.dumps(
                [t.model_dump(mode="json") for t in blocked[:10]], indent=2
            )
        provider = self._provider("director")
        system = DIRECTOR_SYSTEM + "\nROLE-SPECIFIC RULES:\n" + self._instructions("director")
        raw = provider.generate(system, context)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = {"title": "Resolve next project gap", "description": raw, "priority": 1.0}
        task = Task(
            id=f"TASK-{uuid.uuid4().hex[:8].upper()}",
            title=str(d.get("title", "Next task")),
            description=str(d.get("description", "Advance the project goal.")),
            priority=float(d.get("priority", 1.0)),
            created_by="director",
        )
        self.ledger.upsert_task(task)
        self.ledger.event("task_created", task.model_dump(mode="json"), task.id)
        return task

    def _worker_with_tools(self, task: Task, worker_system: str, worker_prompt: str) -> str:
        agent = self._agent("worker")
        allowed = agent.tools if agent else []
        runner = ToolRunner(self.workspace)
        tool_desc = runner.describe(allowed)
        if not tool_desc:
            return self._provider("worker").generate(worker_system, worker_prompt)

        system = worker_system + "\nAVAILABLE TOOLS:\n" + json.dumps(tool_desc, indent=2)
        conversation = worker_prompt
        provider = self._provider("worker")
        max_calls = self.workspace.manifest.runtime.max_tool_calls_per_task
        for call_index in range(max_calls + 1):
            raw = provider.generate(system, conversation)
            parsed = parse_tool_message(raw)
            if not parsed:
                return raw
            tool_id, arguments = parsed
            if tool_id not in allowed:
                result = {"ok": False, "error": f"Tool not allowed for worker: {tool_id}"}
            else:
                try:
                    result = runner.execute(tool_id, arguments)
                except (ToolError, subprocess.TimeoutExpired, OSError) as exc:
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.ledger.event("tool_call", {"tool": tool_id, "arguments": arguments, "result": result}, task.id)
            conversation += (
                "\n\nTOOL CALL #" + str(call_index + 1) + ": " + json.dumps({"tool": tool_id, "arguments": arguments}) +
                "\nTOOL RESULT:\n" + json.dumps(result, indent=2) +
                "\nContinue. Call another tool if needed, otherwise return the final candidate result."
            )
        return "Tool-call budget exhausted before a final answer was produced."

    def verify(self, task: Task, work_output: str) -> tuple[bool, str]:
        validators = self.workspace.manifest.validators
        if not validators:
            return True, "No external validator configured for this workspace."
        failures = []
        passed = []
        for name, command in validators.items():
            proc = subprocess.run(command, cwd=self.workspace.root, shell=True, text=True, capture_output=True)
            detail = (proc.stdout + "\n" + proc.stderr).strip()
            if proc.returncode != 0:
                failures.append(f"{name}: FAILED\n{detail[-4000:]}")
            else:
                passed.append(name)
                self.ledger.event("validator_passed", {"name": name, "detail": detail[-1500:]}, task.id)
        if failures:
            return False, "\n\n".join(failures)
        return True, "Validators passed: " + ", ".join(passed)

    def evaluate_gates(self) -> dict[str, dict]:
        for gate in self.workspace.manifest.gates:
            if gate.validator and gate.validator in self.workspace.manifest.validators:
                command = self.workspace.manifest.validators[gate.validator]
                proc = subprocess.run(command, cwd=self.workspace.root, shell=True, text=True, capture_output=True)
                detail = (proc.stdout + "\n" + proc.stderr).strip()[-3000:]
                self.ledger.set_gate(gate.id, proc.returncode == 0, detail)
        return self.ledger.gate_state()

    def is_complete(self) -> bool:
        state = self.evaluate_gates()
        required = [g for g in self.workspace.manifest.gates if g.required]
        return bool(required) and all(state.get(g.id, {}).get("passed", False) for g in required)

    def _save_artifact(self, task: Task, output: str, review: Review, verification: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.artifacts / f"{ts}_{task.id}.md"
        path.write_text(
            f"# {task.title}\n\n"
            f"**Task:** {task.description}\n\n"
            f"## Candidate result\n\n{output}\n\n"
            f"## Adversarial review\n\n"
            f"Approved: **{review.approved}**\n\n"
            f"Critical objections: {json.dumps(review.critical_objections, indent=2)}\n\n"
            f"Recommendations: {json.dumps(review.recommendations, indent=2)}\n\n"
            f"## External verification\n\n{verification}\n"
        )
        return path

    def step(self) -> IterationResult:
        task = self.choose_next_task()
        attempts = int(task.metadata.get("attempts", 0)) + 1
        task.metadata["attempts"] = attempts
        task.status = TaskStatus.IN_PROGRESS
        self.ledger.upsert_task(task)
        self.ledger.event("task_started", {"attempt": attempts}, task.id)

        worker_prompt = (
            f"NORTH STAR:\n{self.workspace.manifest.goal}\n\n"
            f"TASK:\n{task.title}\n{task.description}\n\nSTATE:\n{self.snapshot()}"
        )
        worker_system = WORKER_SYSTEM + "\nROLE-SPECIFIC RULES:\n" + self._instructions("worker")
        work_output = self._worker_with_tools(task, worker_system, worker_prompt)
        self.ledger.event("work_output", {"text": work_output}, task.id)

        review_prompt = f"NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.model_dump_json()}\n\nCANDIDATE RESULT:\n{work_output}"
        review_system = REVIEW_SYSTEM + "\nROLE-SPECIFIC RULES:\n" + self._instructions("reviewer")
        review_raw = self._provider("reviewer").generate(review_system, review_prompt)
        try:
            review = Review.model_validate(json.loads(review_raw))
        except Exception:
            review = Review(
                approved=False,
                critical_objections=["Reviewer returned invalid structured output."],
                recommendations=[review_raw],
            )
        self.ledger.event("review", review.model_dump(mode="json"), task.id)

        verified, detail = self.verify(task, work_output)
        self.ledger.event("verification", {"passed": verified, "detail": detail}, task.id)

        if review.approved and verified:
            task.status = TaskStatus.DONE
            task.metadata.pop("critical_objections", None)
        elif attempts >= self.workspace.manifest.runtime.max_task_attempts:
            task.status = TaskStatus.REJECTED
            task.metadata["critical_objections"] = review.critical_objections
            self.ledger.event("task_rejected", {"attempts": attempts, "objections": review.critical_objections}, task.id)
        else:
            task.status = TaskStatus.BLOCKED
            task.metadata["critical_objections"] = review.critical_objections or [detail]
        artifact = self._save_artifact(task, work_output, review, detail)
        task.metadata["artifact"] = str(artifact.relative_to(self.workspace.root))
        self.ledger.upsert_task(task)

        next_task = None if self.is_complete() else self.choose_next_task()
        return IterationResult(
            task=task,
            work_output=work_output,
            review=review,
            verification_passed=verified,
            verification_detail=detail,
            next_task=next_task,
        )

    def run(
        self,
        max_steps: int | None = None,
        max_minutes: float | None = None,
        control=None,
        on_step=None,
    ) -> list[IterationResult]:
        max_steps = max_steps or self.workspace.manifest.runtime.max_steps_per_run
        max_minutes = max_minutes if max_minutes is not None else self.workspace.manifest.runtime.max_minutes_per_run
        deadline = time.monotonic() + max_minutes * 60 if max_minutes and max_minutes > 0 else None
        results: list[IterationResult] = []
        self.ledger.event("run_started", {"max_steps": max_steps, "max_minutes": max_minutes})
        stop_reason = None
        for _ in range(max_steps):
            if control is not None:
                action = control()
                while action == "pause":
                    time.sleep(0.5)
                    action = control()
                if action == "cancel":
                    stop_reason = "cancelled"
                    break
            if self.is_complete():
                stop_reason = "complete"
                break
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "time_budget"
                break
            result = self.step()
            results.append(result)
            if on_step is not None:
                on_step(len(results), result)
            if self.workspace.manifest.runtime.pause_seconds > 0:
                time.sleep(self.workspace.manifest.runtime.pause_seconds)
        self.ledger.event("run_finished", {"steps": len(results), "complete": self.is_complete(), "reason": stop_reason or "step_budget"})
        return results
