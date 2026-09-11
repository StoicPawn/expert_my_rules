from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import Task, TaskStatus
from .storage import Ledger


TERMINAL = {TaskStatus.DONE, TaskStatus.REJECTED}


@dataclass(frozen=True)
class VerificationContract:
    criteria: tuple[str, ...]
    validators: tuple[str, ...]
    evidence_required: tuple[str, ...]
    reviewer_required: bool = True

    @classmethod
    def from_task(cls, task: Task) -> 'VerificationContract':
        raw = task.metadata.get('verification_contract') or {}
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            criteria=tuple(str(x) for x in (raw.get('criteria') or []) if str(x).strip()),
            validators=tuple(str(x) for x in (raw.get('validators') or []) if str(x).strip()),
            evidence_required=tuple(str(x) for x in (raw.get('evidence_required') or []) if str(x).strip()),
            reviewer_required=bool(raw.get('reviewer_required', True)),
        )

    def valid(self) -> bool:
        return bool(self.criteria or self.validators or self.evidence_required)

    def to_dict(self) -> dict[str, Any]:
        return {
            'criteria': list(self.criteria),
            'validators': list(self.validators),
            'evidence_required': list(self.evidence_required),
            'reviewer_required': self.reviewer_required,
        }


class TaskGraph:
    """First-class view of durable micro-task dependencies stored in task metadata.

    Metadata remains the persistence format for backwards compatibility, while all
    graph semantics live here rather than being scattered through prompts.
    """

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def dependencies(task: Task) -> list[str]:
        raw = task.metadata.get('depends_on') or []
        return [str(x) for x in raw if str(x).strip()]

    def dependency_state(self, task: Task) -> list[dict[str, Any]]:
        out = []
        for task_id in self.dependencies(task):
            dep = self.ledger.get_task(task_id)
            out.append({
                'task_id': task_id,
                'exists': dep is not None,
                'status': dep.status.value if dep else 'MISSING',
                'title': dep.title if dep else '',
                'artifact': dep.metadata.get('artifact', '') if dep else '',
                'resolution': dep.metadata.get('resolution', '') if dep else '',
            })
        return out

    def ready(self, task: Task) -> bool:
        if task.status not in {TaskStatus.OPEN, TaskStatus.ERROR, TaskStatus.BLOCKED}:
            return False
        for task_id in self.dependencies(task):
            dep = self.ledger.get_task(task_id)
            if dep is None or dep.status not in TERMINAL:
                return False
        return True

    def ready_open(self) -> list[Task]:
        return [t for t in self.ledger.list_tasks([TaskStatus.OPEN]) if self.ready(t)]

    def unresolved_dependencies(self, task: Task) -> list[str]:
        return [x['task_id'] for x in self.dependency_state(task) if x['status'] not in {'DONE', 'REJECTED'}]

    def add_dependencies(self, task: Task, dependencies: list[str]) -> Task:
        existing = self.dependencies(task)
        known = {t.id for t in self.ledger.list_tasks()}
        for dep in dependencies:
            dep = str(dep)
            if dep and dep != task.id and dep in known and dep not in existing:
                existing.append(dep)
        task.metadata['depends_on'] = existing
        self.ledger.upsert_task(task)
        return task


def normalize_contract(raw: Any, *, fallback_criterion: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    criteria = [str(x).strip() for x in (raw.get('criteria') or []) if str(x).strip()]
    validators = [str(x).strip() for x in (raw.get('validators') or []) if str(x).strip()]
    evidence = [str(x).strip() for x in (raw.get('evidence_required') or []) if str(x).strip()]
    if not criteria and not validators and not evidence:
        criteria = [fallback_criterion]
        evidence = ['An inspectable candidate result addressing the micro-task.']
    return {
        'criteria': criteria[:12],
        'validators': validators[:12],
        'evidence_required': evidence[:12],
        'reviewer_required': bool(raw.get('reviewer_required', True)),
    }


def strategy_fingerprint(text: str) -> str:
    normalized = ' '.join(str(text or '').lower().split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def task_graph_snapshot(ledger: Ledger) -> list[dict[str, Any]]:
    graph = TaskGraph(ledger)
    out = []
    for task in ledger.list_tasks():
        out.append({
            'id': task.id,
            'title': task.title,
            'status': task.status.value,
            'priority': task.priority,
            'depends_on': graph.dependencies(task),
            'dependencies': graph.dependency_state(task),
            'verification_contract': VerificationContract.from_task(task).to_dict(),
            'lifecycle_phase': task.metadata.get('lifecycle_phase', ''),
            'focus_chain_id': task.metadata.get('focus_chain_id', ''),
            'focus_chain_active': bool(task.metadata.get('focus_chain_active')),
            'strategy_fingerprints': list(task.metadata.get('strategy_fingerprints') or [])[-8:],
        })
    return out
