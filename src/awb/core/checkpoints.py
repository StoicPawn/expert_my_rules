from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awb.core.models import TaskStatus
from awb.core.resume_trace import load_stream_trace
from awb.core.storage import Ledger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checkpoint_dir(root: Path) -> Path:
    path = root / 'artifacts' / 'checkpoints'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_record(task) -> dict[str, Any]:
    meta = dict(task.metadata or {})
    return {
        'id': task.id,
        'title': task.title,
        'description': task.description,
        'status': task.status.value,
        'priority': task.priority,
        'created_by': task.created_by,
        'scientific_attempts': int(meta.get('scientific_attempts', meta.get('attempts', 0)) or 0),
        'technical_failures': int(meta.get('technical_failures', 0) or 0),
        'lifecycle_phase': meta.get('lifecycle_phase', ''),
        'focus_chain_id': meta.get('focus_chain_id', ''),
        'focus_chain_active': bool(meta.get('focus_chain_active', False)),
        'critical_objections': list(meta.get('critical_objections') or []),
        'review_recommendations': list(meta.get('last_review_recommendations') or []),
        'next_strategy': meta.get('next_strategy', ''),
        'last_verification_detail': meta.get('last_verification_detail', ''),
        'artifact': meta.get('artifact', ''),
        'patch_artifact': meta.get('patch_artifact', ''),
        'resolution': meta.get('resolution', ''),
        'rejection_reason': meta.get('rejection_reason', ''),
        'waiting_on_recovery_tasks': list(meta.get('waiting_on_recovery_tasks') or []),
        'dependency_outcomes': list(meta.get('dependency_outcomes') or []),
        'interrupted_resume': dict(meta.get('interrupted_resume') or {}),
        'latest_stream_snapshot': dict(meta.get('latest_stream_snapshot') or {}),
    }


def build_project_state(root: Path) -> dict[str, Any]:
    ledger = Ledger(root / 'ledger.sqlite3')
    tasks = ledger.list_tasks()
    latest_job = ledger.latest_job()
    task_rows = [_task_record(t) for t in tasks]

    established = [t for t in task_rows if t['status'] == TaskStatus.DONE.value]
    negative = [t for t in task_rows if t['status'] in {TaskStatus.BLOCKED.value, TaskStatus.REJECTED.value}]
    active = [t for t in task_rows if t['status'] in {TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value, TaskStatus.ERROR.value}]

    def resume_rank(item: dict[str, Any]):
        focused = bool(item.get('focus_chain_active') or item.get('focus_chain_id') or item.get('lifecycle_phase'))
        in_progress = item['status'] == TaskStatus.IN_PROGRESS.value
        return (0 if focused else 1, 0 if in_progress else 1, -float(item.get('priority') or 0.0))

    resumable = sorted(active + [t for t in negative if t['status'] == TaskStatus.BLOCKED.value], key=resume_rank)
    next_action = None
    if resumable:
        t = resumable[0]
        next_action = {
            'task_id': t['id'],
            'title': t['title'],
            'status': t['status'],
            'phase': t.get('lifecycle_phase') or ('REWORK' if t['status'] == TaskStatus.BLOCKED.value else ''),
            'strategy': t.get('next_strategy', ''),
            'critical_objections': t.get('critical_objections', [])[:5],
            'resume_artifact': (t.get('interrupted_resume') or {}).get('artifact', ''),
        }

    live_trace = load_stream_trace(root, (latest_job or {}).get('id')) if latest_job else None
    live_summary = None
    if live_trace:
        live_summary = {
            'job_id': live_trace.get('job_id'),
            'role': live_trace.get('role'),
            'model': live_trace.get('model'),
            'state': live_trace.get('state'),
            'elapsed_seconds': live_trace.get('elapsed_seconds'),
            'chunks': live_trace.get('chunks'),
            'prompt_tokens': live_trace.get('prompt_tokens'),
            'output_tokens': live_trace.get('output_tokens'),
            'output_chars': live_trace.get('output_chars'),
            'visible_output_tail': str(live_trace.get('visible_output') or '')[-16000:],
        }

    return {
        'schema_version': 2,
        'generated_at': _now(),
        'project': root.name,
        'latest_job': latest_job,
        'gates': ledger.gate_state(),
        'summary': {
            'total_tasks': len(task_rows),
            'done': sum(1 for t in task_rows if t['status'] == TaskStatus.DONE.value),
            'blocked': sum(1 for t in task_rows if t['status'] == TaskStatus.BLOCKED.value),
            'rejected': sum(1 for t in task_rows if t['status'] == TaskStatus.REJECTED.value),
            'open': sum(1 for t in task_rows if t['status'] == TaskStatus.OPEN.value),
            'in_progress': sum(1 for t in task_rows if t['status'] == TaskStatus.IN_PROGRESS.value),
            'error': sum(1 for t in task_rows if t['status'] == TaskStatus.ERROR.value),
        },
        'established_results': established,
        'negative_or_blocked_findings': negative,
        'active_work': active,
        'next_best_action': next_action,
        'live_stream': live_summary,
        'tasks': task_rows,
        'recent_events': ledger.recent_events(100),
    }


def write_checkpoint(root: Path, *, reason: str, manual: bool = False) -> dict[str, Any]:
    state = build_project_state(root)
    checkpoint_id = 'CP-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6].upper()
    state['checkpoint_id'] = checkpoint_id
    state['reason'] = reason
    state['manual'] = bool(manual)

    cp_dir = _checkpoint_dir(root)
    cp_path = cp_dir / f'{checkpoint_id}.json'
    latest_path = root / 'project_state.json'
    payload = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True)
    cp_path.write_text(payload, encoding='utf-8')
    latest_path.write_text(payload, encoding='utf-8')

    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.event('checkpoint_created', {
        'checkpoint_id': checkpoint_id,
        'reason': reason,
        'manual': bool(manual),
        'path': str(cp_path.relative_to(root)),
        'summary': state['summary'],
        'next_best_action': state['next_best_action'],
        'live_stream': state.get('live_stream'),
    })
    return state


def latest_checkpoint(root: Path) -> dict[str, Any] | None:
    path = root / 'project_state.json'
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
