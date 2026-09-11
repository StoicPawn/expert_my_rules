from __future__ import annotations

import json
import uuid
from typing import Any

from .cloud_budget import load_control
from .cloud_orchestrator import CloudAwareOrchestrator
from .meta_orchestrator import MetaOrchestrator
from .models import IterationResult, Review, Task, TaskStatus, WorkflowStageSpec
from .project_memory import ProjectMemory
from .task_graph import TaskGraph, VerificationContract, normalize_contract, strategy_fingerprint, task_graph_snapshot


PLAN_SYSTEM = """You are the Planner in a general-purpose deep iterative workbench. Plan exactly ONE small, independently verifiable micro-task. Do not solve the whole project and do not specialize to a benchmark that is not the actual goal. Return JSON only with: title, description, priority, depends_on (existing task ids only), strategy, verification_contract. verification_contract must contain criteria, validators, evidence_required, reviewer_required. Prefer a task that reduces uncertainty, attacks a blocker, establishes a prerequisite, or produces deterministic evidence. If software/math/data/parser tools can verify something, require them instead of asking an LLM to guess."""

CONTRACT_SYSTEM = """Define how this micro-task will be verified BEFORE it is executed. Return JSON only with criteria (list), validators (list of available validator ids), evidence_required (list), reviewer_required (bool). Use deterministic validators/tools whenever applicable. Never invent a validator id."""

REPAIR_SYSTEM = """You are the Re-planner in a general-purpose autonomous engine. A micro-task failed review or verification. Do not repeat the same strategy. Return JSON only with action (retry|decompose|reframe|reject), strategy, rationale, prerequisite (object or null), replacement (object or null). A prerequisite/replacement object has title, description, priority, verification_contract. Reject only for evidence-based false/ill-posed/superseded/not-required conclusions, never because work is slow."""

VERIFY_SYSTEM = """You are the Verifier. Judge whether the candidate satisfies the micro-task verification contract using only the supplied evidence. Deterministic validator failure is authoritative and cannot be waived. Return JSON only with passed (bool), detail (str), missing_evidence (list). Be conservative."""

WORKER_SUFFIX = """\nENGINE RULES:\n- Work on this micro-task only.\n- Prefer deterministic tools, code, parsers, tests, numerical or symbolic checks over prose reasoning whenever possible.\n- Never claim a tool result you did not obtain.\n- Preserve useful prior evidence and explicitly distinguish new evidence from assumptions.\n- The task is not complete until its predeclared verification contract can be checked.\n"""


class DeepIterativeEngine(CloudAwareOrchestrator):
    """General-purpose micro-task engine optimized for eventual verified quality.

    Logical roles are sequential; the physical model may be the same resident local
    model for every role. The authoritative state is the graph + ledger + external
    memory, not one growing model context.
    """

    def __init__(self, workspace, provider=None):
        super().__init__(workspace, provider)
        self.graph = TaskGraph(self.ledger)
        self.memory = ProjectMemory(workspace.root / 'ledger.sqlite3')
        self.meta = MetaOrchestrator(workspace, self.ledger, self._call_model)

    def _cloud_important(self, role, task):
        # Paid inference is manual-only. Merely configuring a key/budget is never
        # permission to spend money.
        control = load_control(self.workspace.root)
        if not control.enabled or control.mode != 'force':
            return False
        return super()._cloud_important(role, task)

    def _compact_context(self, query: str) -> dict[str, Any]:
        return {
            'meta_plan': self.meta.ensure(),
            'relevant_memory': self.memory.relevant(query, limit=12),
            'task_graph': task_graph_snapshot(self.ledger)[-60:],
            'project_gates': self.ledger.gate_state(),
        }

    def _ensure_contract(self, task: Task) -> VerificationContract:
        existing = VerificationContract.from_task(task)
        if existing.valid():
            return existing
        available = list(self.workspace.manifest.validators)
        prompt = json.dumps({
            'north_star': self.workspace.manifest.goal,
            'micro_task': {'title': task.title, 'description': task.description},
            'available_validators': available,
            'available_tools': [t.id for t in self.workspace.manifest.tools if t.enabled],
            'meta_plan': self.meta.ensure(),
        }, ensure_ascii=False, indent=2)
        raw: Any = {}
        try:
            raw = json.loads(self._call_model('director', CONTRACT_SYSTEM, prompt, task))
        except Exception as exc:
            self.ledger.event('verification_contract_fallback', {'error': f'{type(exc).__name__}: {exc}'}, task.id)
        contract = normalize_contract(raw, fallback_criterion=f'Produce inspectable evidence that resolves: {task.title}')
        contract['validators'] = [x for x in contract['validators'] if x in available]
        task.metadata['verification_contract'] = contract
        self.ledger.upsert_task(task)
        self.ledger.event('verification_contract_defined', contract, task.id)
        return VerificationContract.from_task(task)

    def _new_task_from_plan(self, plan: dict[str, Any], *, created_by: str = 'planner', focus_root: str | None = None) -> Task:
        title = str(plan.get('title') or 'Resolve next project gap').strip()
        description = str(plan.get('description') or 'Produce the next smallest verifiable piece of evidence.').strip()
        task = Task(
            id=f'TASK-{uuid.uuid4().hex[:8].upper()}',
            title=title,
            description=description,
            priority=float(plan.get('priority') or 1.0),
            created_by=created_by,
            metadata={
                'depends_on': [str(x) for x in (plan.get('depends_on') or [])],
                'verification_contract': normalize_contract(
                    plan.get('verification_contract'),
                    fallback_criterion=f'Produce inspectable evidence that resolves: {title}',
                ),
                'next_strategy': str(plan.get('strategy') or f'Address {title} directly with evidence.').strip(),
                'focus_chain_active': True,
                'focus_chain_id': focus_root or '',
                'lifecycle_phase': 'PLAN',
            },
        )
        if not task.metadata['focus_chain_id']:
            task.metadata['focus_chain_id'] = task.id
        known = {t.id for t in self.ledger.list_tasks()}
        task.metadata['depends_on'] = [x for x in task.metadata['depends_on'] if x in known and x != task.id]
        self.ledger.upsert_task(task)
        self.ledger.event('micro_task_created', {
            'title': task.title,
            'depends_on': task.metadata['depends_on'],
            'verification_contract': task.metadata['verification_contract'],
            'strategy': task.metadata['next_strategy'],
        }, task.id)
        return task

    def _plan_micro_task(self) -> Task:
        context = self._compact_context(self.workspace.manifest.goal)
        prompt = json.dumps({
            'north_star': self.workspace.manifest.goal,
            'state': context,
            'instruction': 'Choose the highest-information graph-ready micro-task. Existing unresolved tasks should be respected rather than duplicated.',
        }, ensure_ascii=False, indent=2)
        plan: dict[str, Any]
        try:
            parsed = json.loads(self._call_model('director', PLAN_SYSTEM, prompt, None))
            plan = parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            self.ledger.event('micro_task_planner_fallback', {'error': f'{type(exc).__name__}: {exc}'})
            plan = {}
        if not plan:
            plan = {
                'title': 'Close the highest-value unresolved evidence gap',
                'description': 'Inspect persisted project state, select one unresolved dependency or completion criterion, and produce the smallest checkable evidence that advances it.',
                'priority': 1.0,
                'depends_on': [],
                'strategy': 'Use persisted evidence and deterministic checks first; make one falsifiable advancement.',
                'verification_contract': {
                    'criteria': ['The selected evidence gap is explicitly identified and materially reduced.'],
                    'validators': [],
                    'evidence_required': ['An inspectable artifact or tool result supporting the claimed advancement.'],
                    'reviewer_required': True,
                },
            }
        return self._new_task_from_plan(plan)

    def _focused_unresolved(self) -> list[Task]:
        return [
            t for t in self.ledger.list_tasks()
            if t.metadata.get('focus_chain_active') and t.status not in {TaskStatus.DONE, TaskStatus.REJECTED}
        ]

    def _repair_blocked(self, task: Task) -> Task | None:
        used = list(task.metadata.get('strategy_fingerprints') or [])
        prompt = json.dumps({
            'north_star': self.workspace.manifest.goal,
            'blocked_task': task.model_dump(mode='json'),
            'critical_objections': task.metadata.get('critical_objections', []),
            'last_verification': task.metadata.get('last_verification_detail', ''),
            'failed_strategy_fingerprints': used[-12:],
            'recent_memory': self.memory.relevant(task.title + ' ' + task.description, limit=10),
            'rule': 'The next strategy must be materially different from previously failed strategies.',
        }, ensure_ascii=False, indent=2)
        plan: dict[str, Any] = {}
        try:
            parsed = json.loads(self._call_model('director', REPAIR_SYSTEM, prompt, task))
            if isinstance(parsed, dict):
                plan = parsed
        except Exception as exc:
            self.ledger.event('replanner_fallback', {'error': f'{type(exc).__name__}: {exc}'}, task.id)
        action = str(plan.get('action') or 'retry').lower()
        strategy = str(plan.get('strategy') or '').strip()
        if not strategy:
            strongest = str((task.metadata.get('critical_objections') or ['unresolved verification gap'])[0])
            strategy = f'Falsify or resolve the strongest remaining objection with new deterministic evidence: {strongest}'
        fp = strategy_fingerprint(strategy)
        if fp in used:
            # Deterministic anti-loop fallback. It deliberately changes the move from
            # construction to falsification/evidence isolation.
            strategy = f'Independent counter-check #{len(used)+1}: isolate the strongest objection and test it with a different tool, representation or minimal example before rebuilding the candidate.'
            fp = strategy_fingerprint(strategy)
            self.ledger.event('repeated_strategy_rewritten', {'new_strategy': strategy, 'fingerprint': fp}, task.id)

        if action == 'decompose':
            raw = plan.get('prerequisite') if isinstance(plan.get('prerequisite'), dict) else {}
            raw = dict(raw)
            raw.setdefault('title', f'Prerequisite for {task.title}')
            raw.setdefault('description', strategy)
            raw.setdefault('priority', task.priority + 0.1)
            raw.setdefault('strategy', strategy)
            child = self._new_task_from_plan(raw, created_by='re-planner', focus_root=str(task.metadata.get('focus_chain_id') or task.id))
            deps = list(task.metadata.get('depends_on') or [])
            if child.id not in deps:
                deps.append(child.id)
            task.metadata['depends_on'] = deps
            task.metadata['lifecycle_phase'] = 'WAITING_ON_DEPENDENCY'
            task.metadata['next_strategy'] = strategy
            task.status = TaskStatus.BLOCKED
            self.ledger.upsert_task(task)
            self.ledger.event('micro_task_decomposed', {'prerequisite': child.id, 'strategy': strategy}, task.id)
            return child

        if action == 'reframe':
            raw = plan.get('replacement') if isinstance(plan.get('replacement'), dict) else {}
            raw = dict(raw)
            raw.setdefault('title', f'Reframe: {task.title}')
            raw.setdefault('description', strategy)
            raw.setdefault('priority', task.priority)
            raw.setdefault('strategy', strategy)
            task.status = TaskStatus.REJECTED
            task.metadata['resolution'] = 'reframed'
            task.metadata['focus_chain_active'] = False
            self.ledger.upsert_task(task)
            self.memory.remember('reframed_task', f'{task.title} was explicitly reframed. {strategy}', task_id=task.id)
            return self._new_task_from_plan(raw, created_by='re-planner')

        if action == 'reject':
            task.status = TaskStatus.REJECTED
            task.metadata['resolution'] = 'evidence_rejected'
            task.metadata['rejection_reason'] = str(plan.get('rationale') or strategy)
            task.metadata['focus_chain_active'] = False
            self.ledger.upsert_task(task)
            self.memory.remember('negative_result', f'{task.title}: {task.metadata["rejection_reason"]}', task_id=task.id)
            return None

        task.status = TaskStatus.OPEN
        task.metadata['next_strategy'] = strategy
        task.metadata['lifecycle_phase'] = 'REWORK'
        task.metadata['focus_chain_active'] = True
        self.ledger.upsert_task(task)
        self.ledger.event('micro_task_rework_planned', {'strategy': strategy, 'fingerprint': fp}, task.id)
        return task

    def choose_next_task(self):
        self.meta.ensure()
        focused = self._focused_unresolved()
        # Resolve dependencies within the focus chain first.
        for task in focused:
            if task.status == TaskStatus.ERROR and self.graph.ready(task):
                task.status = TaskStatus.OPEN
                task.metadata['lifecycle_phase'] = 'REWORK'
                self.ledger.upsert_task(task)
                return task
        for task in focused:
            if task.status == TaskStatus.BLOCKED and self.graph.ready(task):
                repaired = self._repair_blocked(task)
                if repaired is not None:
                    return repaired
        ready_focus = [t for t in focused if t.status == TaskStatus.OPEN and self.graph.ready(t)]
        if ready_focus:
            return sorted(ready_focus, key=lambda t: -t.priority)[0]
        # A focused parent may be waiting; select one of its unresolved dependencies.
        focus_ids = {t.id for t in focused}
        for parent in focused:
            for dep_id in self.graph.unresolved_dependencies(parent):
                dep = self.ledger.get_task(dep_id)
                if dep and dep.status == TaskStatus.OPEN and self.graph.ready(dep):
                    dep.metadata['focus_chain_active'] = True
                    dep.metadata['focus_chain_id'] = str(parent.metadata.get('focus_chain_id') or parent.id)
                    self.ledger.upsert_task(dep)
                    return dep
        ready = self.graph.ready_open()
        if ready:
            task = sorted(ready, key=lambda t: -t.priority)[0]
            task.metadata['focus_chain_active'] = True
            task.metadata['focus_chain_id'] = str(task.metadata.get('focus_chain_id') or task.id)
            task.metadata['lifecycle_phase'] = str(task.metadata.get('lifecycle_phase') or 'PLAN')
            self.ledger.upsert_task(task)
            return task
        return self._plan_micro_task()

    def _verifier(self, task: Task, candidate: str, deterministic_ok: bool, deterministic_detail: str, review: Review) -> tuple[bool, str]:
        contract = VerificationContract.from_task(task)
        prompt = json.dumps({
            'north_star': self.workspace.manifest.goal,
            'micro_task': {'title': task.title, 'description': task.description},
            'verification_contract': contract.to_dict(),
            'deterministic_validation': {'passed': deterministic_ok, 'detail': deterministic_detail},
            'review': review.model_dump(mode='json'),
            'candidate': candidate[-50000:],
        }, ensure_ascii=False, indent=2)
        try:
            data = json.loads(self._call_model('verifier', VERIFY_SYSTEM, prompt, task))
            passed = bool(data.get('passed', False)) and deterministic_ok
            detail = str(data.get('detail') or '')
            missing = data.get('missing_evidence') or []
            if missing:
                detail += '\nMissing evidence: ' + json.dumps(missing, ensure_ascii=False)
            return passed, detail
        except Exception as exc:
            return False, f'Verifier returned no valid decision: {type(exc).__name__}: {exc}'

    def _worker_prompt(self, task: Task) -> str:
        deps = self.graph.dependency_state(task)
        memory = self.memory.relevant(task.title + ' ' + task.description, limit=12)
        interrupted = task.metadata.get('interrupted_resume') or {}
        return json.dumps({
            'north_star': self.workspace.manifest.goal,
            'meta_plan': self.meta.ensure(),
            'micro_task': {'id': task.id, 'title': task.title, 'description': task.description},
            'verification_contract': VerificationContract.from_task(task).to_dict(),
            'strategy': task.metadata.get('next_strategy', ''),
            'dependencies': deps,
            'relevant_external_memory': memory,
            'unresolved_objections': task.metadata.get('critical_objections', []),
            'interrupted_visible_work': {
                'artifact': interrupted.get('artifact', ''),
                'visible_output_tail': str(interrupted.get('visible_output_tail') or '')[-16000:],
            } if interrupted else None,
        }, ensure_ascii=False, indent=2)

    def step(self):
        task = self.choose_next_task()
        contract = self._ensure_contract(task)
        execution_attempt = int(task.metadata.get('execution_attempts', 0)) + 1
        task.metadata['execution_attempts'] = execution_attempt
        task.metadata['lifecycle_phase'] = 'EXECUTE'
        task.status = TaskStatus.IN_PROGRESS
        strategy = str(task.metadata.get('next_strategy') or task.title)
        fp = strategy_fingerprint(strategy)
        fingerprints = list(task.metadata.get('strategy_fingerprints') or [])
        fingerprints.append(fp)
        task.metadata['strategy_fingerprints'] = fingerprints[-32:]
        self.ledger.upsert_task(task)
        self.ledger.event('deep_cycle_started', {
            'phase': 'EXECUTE',
            'strategy': strategy,
            'strategy_fingerprint': fp,
            'verification_contract': contract.to_dict(),
        }, task.id)
        attempt_id = self.ledger.start_attempt(task.id, execution_attempt)
        self._attempt_routes = []
        task_workspace = None
        try:
            task_workspace = self.git.prepare(task.id) if self.git.enabled else None
            execution_root = task_workspace.path if task_workspace else self.workspace.root
            worker_stage = WorkflowStageSpec(id='deep-execute', kind='execute', role='worker')
            prompt = self._worker_prompt(task)
            candidate = self._execute_with_tools(task, worker_stage, prompt + WORKER_SUFFIX, execution_root)
            self.ledger.event('work_output', {'stage': 'deep-execute', 'text': candidate}, task.id)

            task.metadata['lifecycle_phase'] = 'VERIFY'
            self.ledger.upsert_task(task)
            if contract.validators:
                deterministic_ok, deterministic_detail = self.verify(task, list(contract.validators), cwd=execution_root)
            else:
                deterministic_ok, deterministic_detail = True, 'No deterministic validator required by this micro-task contract.'
            self.ledger.event('verification', {
                'stage': 'deep-deterministic', 'passed': deterministic_ok, 'detail': deterministic_detail,
            }, task.id)

            task.metadata['lifecycle_phase'] = 'CRITIQUE'
            self.ledger.upsert_task(task)
            patch = self.git.patch(task.id) if self.git.enabled else ''
            review_stage = WorkflowStageSpec(id='deep-review', kind='review', role='reviewer')
            review = self._review_stage(task, review_stage, candidate, patch)
            self.ledger.event('review', {'stage': 'deep-review', 'role': 'reviewer', **review.model_dump(mode='json')}, task.id)
            verifier_ok, verifier_detail = self._verifier(task, candidate, deterministic_ok, deterministic_detail, review)
            self.ledger.event('verification', {
                'stage': 'deep-verifier', 'passed': verifier_ok, 'detail': verifier_detail,
            }, task.id)

            scientific = int(task.metadata.get('scientific_attempts', task.metadata.get('attempts', 0))) + 1
            task.metadata['scientific_attempts'] = scientific
            task.metadata['attempts'] = scientific
            task.metadata['last_verification_detail'] = deterministic_detail + '\n' + verifier_detail
            task.metadata['last_completed_strategy'] = strategy
            patch_summary = self.git.diff_summary(task.id) if self.git.enabled else ''
            patch_artifact = self._save_patch(task, patch)

            accepted = deterministic_ok and verifier_ok and (review.approved or not contract.reviewer_required)
            if accepted:
                task.status = TaskStatus.DONE
                task.metadata['lifecycle_phase'] = 'DONE'
                task.metadata['focus_chain_active'] = False
                task.metadata.pop('critical_objections', None)
                self.memory.remember(
                    'accepted_evidence',
                    f'{task.title}: accepted after deterministic verification/review. {verifier_detail}',
                    task_id=task.id,
                    payload={'strategy_fingerprint': fp, 'verification': verifier_detail},
                )
            else:
                task.status = TaskStatus.BLOCKED
                task.metadata['lifecycle_phase'] = 'REWORK'
                task.metadata['focus_chain_active'] = True
                objections = list(review.critical_objections)
                if not deterministic_ok:
                    objections.append(deterministic_detail)
                if not verifier_ok:
                    objections.append(verifier_detail)
                task.metadata['critical_objections'] = objections[:12]
                self.memory.remember(
                    'failed_strategy',
                    f'{task.title}: strategy {fp} did not pass. ' + ' | '.join(objections[:4]),
                    task_id=task.id,
                    payload={'strategy': strategy, 'fingerprint': fp, 'objections': objections[:12]},
                )

            artifact = self._save_artifact(
                task, candidate, review, {'deep-review': review.model_dump(mode='json')},
                task.metadata['last_verification_detail'], patch_artifact, patch_summary,
            )
            task.metadata['artifact'] = str(artifact.relative_to(self.workspace.root))
            if patch_artifact:
                task.metadata['patch_artifact'] = patch_artifact

            if self.git.enabled and task.status == TaskStatus.DONE:
                merge_result = self.git.merge(task.id, f'AWB: {task.title}')
                task.metadata['git_merge'] = merge_result
                self.ledger.event('git_candidate_merged', merge_result, task.id)
            elif self.git.enabled:
                self.ledger.event('git_candidate_preserved', {'reason': 'micro-task requires rework'}, task.id)

            self.ledger.upsert_task(task)
            self.evaluate_gates()
            self.ledger.finish_attempt(
                attempt_id,
                status=task.status.value,
                route={'calls': self._attempt_routes},
                review=review.model_dump(mode='json'),
                verification={'passed': accepted, 'detail': task.metadata['last_verification_detail']},
                artifact=task.metadata['artifact'],
            )
            self.ledger.event('deep_cycle_finished', {
                'status': task.status.value,
                'phase': task.metadata.get('lifecycle_phase'),
                'scientific_attempts': scientific,
            }, task.id)
            next_task = None if self.is_complete() else self.choose_next_task()
            return IterationResult(
                task=task,
                work_output=candidate,
                review=review,
                verification_passed=accepted,
                verification_detail=task.metadata['last_verification_detail'],
                next_task=next_task,
            )
        except Exception as exc:
            task.status = TaskStatus.ERROR
            task.metadata['lifecycle_phase'] = 'REWORK'
            task.metadata['focus_chain_active'] = True
            task.metadata['technical_failures'] = int(task.metadata.get('technical_failures', 0)) + 1
            task.metadata['last_error'] = f'{type(exc).__name__}: {exc}'
            self.ledger.upsert_task(task)
            self.memory.remember('technical_failure', f'{task.title}: {task.metadata["last_error"]}', task_id=task.id)
            self.ledger.event('task_technical_error', {
                'error': task.metadata['last_error'],
                'technical_failures': task.metadata['technical_failures'],
            }, task.id)
            self.ledger.finish_attempt(
                attempt_id, status='TECHNICAL_ERROR', route={'calls': self._attempt_routes}, error=task.metadata['last_error']
            )
            raise
