from __future__ import annotations

import json
import subprocess
import uuid

from awb.providers.base import ModelProvider
from .models import IterationResult, Review, Task, TaskStatus, Workspace
from .storage import Ledger

DIRECTOR_SYSTEM = """You are the Director of an autonomous project workbench.
Choose exactly ONE next task that maximally advances the stated north-star goal.
Prefer falsification, blockers, failed gates and high-information work over cosmetic improvements.
Return DIRECTOR_JSON only as JSON with: title, description, priority.
"""
WORKER_SYSTEM = """You are the Worker. Execute the assigned task rigorously.
Separate evidence, assumptions, uncertainty and conclusions. Do not claim completion without evidence.
"""
REVIEW_SYSTEM = """You are an adversarial Reviewer. Try to reject the candidate result.
Look for logical gaps, untested assumptions, missing cases, non-reproducibility and unsupported novelty.
Return REVIEW_JSON only as JSON: approved (bool), critical_objections (list[str]), recommendations (list[str]).
"""

class Orchestrator:
    def __init__(self, workspace: Workspace, provider: ModelProvider):
        self.workspace = workspace
        self.provider = provider
        self.ledger = Ledger(workspace.root / "ledger.sqlite3")
        for gate in workspace.manifest.gates:
            if gate.id not in self.ledger.gate_state(): self.ledger.set_gate(gate.id, False, "not evaluated")

    def snapshot(self) -> str:
        tasks = [t.model_dump(mode="json") for t in self.ledger.list_tasks()]
        return json.dumps({"goal": self.workspace.manifest.goal, "tasks": tasks[-25:], "gates": self.ledger.gate_state()}, indent=2)

    def choose_next_task(self) -> Task:
        existing = self.ledger.list_tasks([TaskStatus.OPEN, TaskStatus.BLOCKED])
        if existing: return existing[0]
        raw = self.provider.generate(DIRECTOR_SYSTEM, self.snapshot())
        try: d = json.loads(raw)
        except json.JSONDecodeError: d = {"title":"Resolve next project gap","description":raw,"priority":1.0}
        task = Task(id=f"TASK-{uuid.uuid4().hex[:8].upper()}", title=str(d.get("title","Next task")), description=str(d.get("description","Advance the project goal.")), priority=float(d.get("priority",1.0)), created_by="director")
        self.ledger.upsert_task(task); self.ledger.event("task_created", task.model_dump(mode="json"), task.id)
        return task

    def verify(self, task: Task, work_output: str) -> tuple[bool,str]:
        validators = self.workspace.manifest.validators
        if not validators: return True, "No external validator configured for this workspace."
        failures=[]
        for name, command in validators.items():
            proc=subprocess.run(command,cwd=self.workspace.root,shell=True,text=True,capture_output=True)
            detail=(proc.stdout+"\n"+proc.stderr).strip()
            if proc.returncode != 0: failures.append(f"{name}: FAILED\n{detail[-3000:]}")
            else: self.ledger.event("validator_passed", {"name":name,"detail":detail[-1000:]}, task.id)
        return (False,"\n\n".join(failures)) if failures else (True,"All configured validators passed.")

    def evaluate_gates(self) -> dict[str,dict]:
        for gate in self.workspace.manifest.gates:
            if gate.validator and gate.validator in self.workspace.manifest.validators:
                proc=subprocess.run(self.workspace.manifest.validators[gate.validator],cwd=self.workspace.root,shell=True,text=True,capture_output=True)
                self.ledger.set_gate(gate.id, proc.returncode==0, (proc.stdout+"\n"+proc.stderr).strip()[-2000:])
        return self.ledger.gate_state()

    def is_complete(self) -> bool:
        state=self.evaluate_gates(); required=[g for g in self.workspace.manifest.gates if g.required]
        return bool(required) and all(state.get(g.id,{}).get("passed",False) for g in required)

    def step(self) -> IterationResult:
        task=self.choose_next_task(); task.status=TaskStatus.IN_PROGRESS; self.ledger.upsert_task(task); self.ledger.event("task_started",{},task.id)
        work_output=self.provider.generate(WORKER_SYSTEM, f"NORTH STAR:\n{self.workspace.manifest.goal}\n\nTASK:\n{task.title}\n{task.description}\n\nSTATE:\n{self.snapshot()}")
        self.ledger.event("work_output",{"text":work_output},task.id)
        review_raw=self.provider.generate(REVIEW_SYSTEM,f"TASK:\n{task.model_dump_json()}\n\nCANDIDATE RESULT:\n{work_output}")
        try: review=Review.model_validate(json.loads(review_raw))
        except Exception: review=Review(approved=False,critical_objections=["Reviewer returned invalid structured output."],recommendations=[review_raw])
        self.ledger.event("review",review.model_dump(mode="json"),task.id)
        verified,detail=self.verify(task,work_output); self.ledger.event("verification",{"passed":verified,"detail":detail},task.id)
        if review.approved and verified: task.status=TaskStatus.DONE
        elif review.critical_objections: task.status=TaskStatus.BLOCKED; task.metadata["critical_objections"]=review.critical_objections
        else: task.status=TaskStatus.OPEN
        self.ledger.upsert_task(task)
        next_task=None if self.is_complete() else self.choose_next_task()
        return IterationResult(task=task,work_output=work_output,review=review,verification_passed=verified,verification_detail=detail,next_task=next_task)

    def run(self,max_steps:int)->list[IterationResult]:
        results=[]
        for _ in range(max_steps):
            if self.is_complete(): break
            results.append(self.step())
        return results
