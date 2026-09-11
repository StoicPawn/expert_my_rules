from __future__ import annotations

import json

from awb.core.cloud_orchestrator import CloudAwareOrchestrator
from awb.core.models import Task, TaskStatus


class FocusedCloudAwareOrchestrator(CloudAwareOrchestrator):
    """Keep one scientific focus chain active until review objections are resolved.

    Reviewer rejection is rework, not permission to wander to unrelated tasks. The
    only work allowed ahead of the current task is an explicit prerequisite created
    to unblock it. Interrupted visible model work is also fed back into the next
    Worker prompt so a long local generation is not intellectually discarded.
    """

    _FOCUS_PHASES = {'REWORK', 'PREREQUISITE', 'REFRAMED_REPLACEMENT'}
    _TERMINAL = {TaskStatus.DONE, TaskStatus.REJECTED}

    @staticmethod
    def _focus_root(task: Task) -> str:
        return str(task.metadata.get('focus_chain_id') or task.metadata.get('parent_task_id') or task.id)

    def _mark_focus(self, task: Task, phase: str, *, root_id: str | None = None) -> Task:
        task.metadata['focus_chain_active'] = True
        task.metadata['focus_chain_id'] = root_id or self._focus_root(task)
        task.metadata['lifecycle_phase'] = phase
        self.ledger.upsert_task(task)
        return task

    def _clear_focus(self, task: Task, reason: str) -> None:
        if task.metadata.get('focus_chain_active'):
            task.metadata['focus_chain_active'] = False
            task.metadata['focus_chain_closed_reason'] = reason
            self.ledger.upsert_task(task)
            self.ledger.event('focus_chain_closed', {'reason': reason}, task.id)

    def _spawn_recovery_task(self, parent: Task, title, description, priority, kind):
        child = super()._spawn_recovery_task(parent, title, description, priority, kind)
        if child is None:
            return None
        root_id = str(parent.metadata.get('focus_chain_id') or parent.id)
        phase = 'REFRAMED_REPLACEMENT' if kind == 'reframe' else 'PREREQUISITE'
        self._mark_focus(child, phase, root_id=root_id)
        parent.metadata['focus_chain_active'] = True
        parent.metadata['focus_chain_id'] = root_id
        self.ledger.upsert_task(parent)
        self.ledger.event(
            'focus_recovery_child_created',
            {'parent_task_id': parent.id, 'child_task_id': child.id, 'phase': phase},
            child.id,
        )
        return child

    def _plan_blocked_recovery(self, task, review, verification_detail):
        plan = super()._plan_blocked_recovery(task, review, verification_detail)
        action = str(task.metadata.get('last_recovery_action') or plan.get('action') or 'retry').lower()
        root_id = str(task.metadata.get('focus_chain_id') or task.id)

        if action == 'retry':
            task.status = TaskStatus.BLOCKED
            task.metadata['focus_chain_active'] = True
            task.metadata['focus_chain_id'] = root_id
            task.metadata['lifecycle_phase'] = 'REWORK'
            self.ledger.upsert_task(task)
            self.ledger.event(
                'task_rework_queued',
                {
                    'strategy': task.metadata.get('next_strategy', ''),
                    'critical_objections': task.metadata.get('critical_objections', [])[:8],
                },
                task.id,
            )
        elif action == 'decompose':
            task.metadata['focus_chain_active'] = True
            task.metadata['focus_chain_id'] = root_id
            task.metadata['lifecycle_phase'] = 'WAITING_ON_DEPENDENCY'
            self.ledger.upsert_task(task)
            self.ledger.event(
                'task_waiting_on_dependencies',
                {'task_ids': task.metadata.get('waiting_on_recovery_tasks', [])},
                task.id,
            )
        elif action == 'reframe':
            self._clear_focus(task, 'reframed into an explicit replacement')
        elif action == 'reject':
            self._clear_focus(task, 'evidence-based rejection')
        return plan

    def _adopt_legacy_recovery_state(self) -> None:
        for parent in self.ledger.list_tasks([TaskStatus.BLOCKED]):
            waiting = list(parent.metadata.get('waiting_on_recovery_tasks') or [])
            root_id = str(parent.metadata.get('focus_chain_id') or parent.id)
            if waiting:
                changed = False
                if parent.metadata.get('lifecycle_phase') != 'WAITING_ON_DEPENDENCY':
                    parent.metadata['lifecycle_phase'] = 'WAITING_ON_DEPENDENCY'
                    changed = True
                if not parent.metadata.get('focus_chain_active'):
                    parent.metadata['focus_chain_active'] = True
                    parent.metadata['focus_chain_id'] = root_id
                    changed = True
                if changed:
                    self.ledger.upsert_task(parent)
                for child_id in waiting:
                    child = self.ledger.get_task(str(child_id))
                    if child and child.status not in self._TERMINAL:
                        phase = str(child.metadata.get('lifecycle_phase') or 'PREREQUISITE')
                        self._mark_focus(child, phase, root_id=root_id)
                continue

            if parent.metadata.get('next_strategy') or parent.metadata.get('last_recovery_action') == 'retry':
                parent.status = TaskStatus.OPEN
                self._mark_focus(parent, 'REWORK', root_id=root_id)
                self.ledger.event(
                    'blocked_task_reopened_as_focused_rework',
                    {'strategy': parent.metadata.get('next_strategy', '')},
                    parent.id,
                )

    def _release_ready_parents(self) -> None:
        for parent in self.ledger.list_tasks([TaskStatus.BLOCKED]):
            waiting = [str(x) for x in (parent.metadata.get('waiting_on_recovery_tasks') or [])]
            if not waiting:
                continue
            children = [self.ledger.get_task(child_id) for child_id in waiting]
            if any(child is None for child in children):
                continue
            pending = [child for child in children if child.status not in self._TERMINAL]
            if pending:
                continue

            parent.status = TaskStatus.OPEN
            parent.metadata['lifecycle_phase'] = 'REWORK'
            parent.metadata['focus_chain_active'] = True
            parent.metadata['focus_chain_id'] = str(parent.metadata.get('focus_chain_id') or parent.id)
            parent.metadata['dependency_outcomes'] = [
                {
                    'task_id': child.id,
                    'title': child.title,
                    'status': child.status.value,
                    'artifact': child.metadata.get('artifact', ''),
                    'resolution': child.metadata.get('resolution', ''),
                }
                for child in children
            ]
            parent.metadata.pop('waiting_on_recovery_tasks', None)
            self.ledger.upsert_task(parent)
            self.ledger.event(
                'task_dependencies_resolved',
                {'children': parent.metadata['dependency_outcomes'], 'action': 'resume_parent_rework'},
                parent.id,
            )

    def _recovery_context(self, task: Task):
        base = super()._recovery_context(task)
        outcomes = task.metadata.get('dependency_outcomes') or []
        if outcomes:
            base += '\n\nRECOVERY DEPENDENCY OUTCOMES:\n' + json.dumps(outcomes, ensure_ascii=False, indent=2)

        interrupted = task.metadata.get('interrupted_resume') or {}
        if interrupted:
            base += (
                '\n\nINTERRUPTED LOCAL GENERATION — PRESERVE USEFUL WORK:\n'
                + json.dumps({
                    'artifact': interrupted.get('artifact', ''),
                    'role': interrupted.get('role', ''),
                    'model': interrupted.get('model', ''),
                    'prompt_tokens': interrupted.get('prompt_tokens'),
                    'output_tokens': interrupted.get('output_tokens'),
                    'partial_visible_output': str(interrupted.get('visible_output_tail') or '')[-16000:],
                    'instruction': (
                        'Treat this as unfinished scratch work from the same task. Validate it, reuse correct parts, '
                        'and continue rather than mechanically restarting from zero.'
                    ),
                }, ensure_ascii=False, indent=2)
            )
        return base

    def _focused_open(self) -> list[Task]:
        return [
            task for task in self.ledger.list_tasks([TaskStatus.OPEN])
            if task.metadata.get('focus_chain_active') or task.metadata.get('lifecycle_phase') in self._FOCUS_PHASES
        ]

    def _focused_errors(self) -> list[Task]:
        return [
            task for task in self.ledger.list_tasks([TaskStatus.ERROR])
            if task.metadata.get('focus_chain_active')
        ]

    def choose_next_task(self):
        self._adopt_legacy_recovery_state()
        self._release_ready_parents()

        focused = self._focused_open()
        if focused:
            task = focused[0]
            self.ledger.event(
                'focus_task_selected',
                {
                    'phase': task.metadata.get('lifecycle_phase', 'REWORK'),
                    'focus_chain_id': task.metadata.get('focus_chain_id', task.id),
                    'reason': 'finish reviewer-worker recovery before unrelated work',
                },
                task.id,
            )
            return task

        focused_errors = self._focused_errors()
        if focused_errors:
            task = focused_errors[0]
            task.status = TaskStatus.OPEN
            task.metadata['lifecycle_phase'] = task.metadata.get('lifecycle_phase') or 'REWORK'
            self.ledger.upsert_task(task)
            self.ledger.event(
                'focused_task_reopened_after_technical_error',
                {'technical_failures': task.metadata.get('technical_failures', 0)},
                task.id,
            )
            return task

        return super().choose_next_task()
