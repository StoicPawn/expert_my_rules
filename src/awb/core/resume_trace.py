from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awb.core.models import TaskStatus
from awb.core.storage import Ledger


def _trace_root(workspace_root: Path) -> Path:
    configured = os.getenv('AWB_STREAM_TRACE_DIR', '').strip()
    if configured:
        return Path(configured)
    return workspace_root.parent / '.awb-streams'


def trace_path(workspace_root: Path, job_id: str) -> Path:
    return _trace_root(workspace_root) / f'{job_id}.json'


def load_stream_trace(workspace_root: Path, job_id: str | None) -> dict[str, Any] | None:
    if not job_id:
        return None
    path = trace_path(workspace_root, str(job_id))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def capture_stream_trace(
    workspace_root: Path,
    job_id: str | None,
    *,
    reason: str,
    interrupted: bool,
) -> dict[str, Any] | None:
    """Copy the current local-model stream journal into the project artifacts.

    The journal contains model inputs and visible generated output, but never hidden
    reasoning text. When interrupted=True the current task receives a compact resume
    pointer so the next Worker can continue from the partial work instead of blindly
    starting from zero.
    """
    trace = load_stream_trace(workspace_root, job_id)
    if trace is None:
        return None

    ledger = Ledger(workspace_root / 'ledger.sqlite3')
    in_progress = ledger.list_tasks([TaskStatus.IN_PROGRESS])
    task = in_progress[0] if in_progress else None

    ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    label = str(job_id or 'NOJOB').replace('/', '_')
    out_dir = workspace_root / 'artifacts' / 'checkpoints'
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{ts}_stream_{label}.json'
    payload = dict(trace)
    payload['captured_at'] = datetime.now(timezone.utc).isoformat()
    payload['capture_reason'] = reason
    payload['interrupted'] = bool(interrupted)
    tmp = out.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(out)
    rel = str(out.relative_to(workspace_root))

    summary = {
        'artifact': rel,
        'job_id': str(job_id or ''),
        'role': trace.get('role'),
        'model': trace.get('model'),
        'state': trace.get('state'),
        'prompt_tokens': trace.get('prompt_tokens'),
        'output_tokens': trace.get('output_tokens'),
        'output_chars': trace.get('output_chars'),
        'visible_output_tail': str(trace.get('visible_output') or '')[-16000:],
        'reason': reason,
    }
    if task is not None:
        key = 'interrupted_resume' if interrupted else 'latest_stream_snapshot'
        task.metadata[key] = summary
        ledger.upsert_task(task)
    ledger.event(
        'stream_resume_checkpointed' if interrupted else 'stream_snapshot_checkpointed',
        {**summary, 'task_id': task.id if task else None},
        task.id if task else None,
    )
    return summary


def clear_stream_trace(workspace_root: Path, job_id: str | None) -> None:
    if not job_id:
        return
    try:
        trace_path(workspace_root, str(job_id)).unlink(missing_ok=True)
    except Exception:
        pass
