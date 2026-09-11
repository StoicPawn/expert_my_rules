from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from awb.core.checkpoints import build_project_state, write_checkpoint
from awb.core.models import JobStatus
from awb.core.resume_trace import load_stream_trace
from awb.core.storage import Ledger


_LOCK = threading.Lock()


@dataclass
class OutputPolicy:
    auto_every_minutes: int = 30
    max_snapshots_shown: int = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _policy_path(root: Path) -> Path:
    return root / '.awb' / 'output-policy.json'


def _output_dir(root: Path) -> Path:
    path = root / 'artifacts' / 'outputs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_output_policy(root: Path) -> OutputPolicy:
    path = _policy_path(root)
    if not path.exists():
        return OutputPolicy()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return OutputPolicy(
            auto_every_minutes=max(0, min(int(raw.get('auto_every_minutes', 30)), 1440)),
            max_snapshots_shown=max(3, min(int(raw.get('max_snapshots_shown', 12)), 100)),
        )
    except Exception:
        return OutputPolicy()


def save_output_policy(root: Path, policy: OutputPolicy) -> OutputPolicy:
    policy.auto_every_minutes = max(0, min(int(policy.auto_every_minutes), 1440))
    policy.max_snapshots_shown = max(3, min(int(policy.max_snapshots_shown), 100))
    path = _policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(asdict(policy), indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    return policy


def _generated_files(root: Path, limit: int = 250) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    roots = [root / 'artifacts', root / 'sources', root / 'project_state.json']
    output_root = (_output_dir(root)).resolve()
    for source in roots:
        if not source.exists():
            continue
        paths = [source] if source.is_file() else list(source.rglob('*'))
        for path in paths:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if output_root == resolved or output_root in resolved.parents:
                continue
            try:
                stat = path.stat()
                rows.append({
                    'path': str(path.relative_to(root)),
                    'size_bytes': int(stat.st_size),
                    'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    'suffix': path.suffix.lower(),
                })
            except Exception:
                continue
    rows.sort(key=lambda item: item['modified_at'], reverse=True)
    return rows[:limit]


def _token_totals(events: list[dict[str, Any]]) -> dict[str, int]:
    prompt = 0
    output = 0
    calls = 0
    for event in events:
        payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        p = payload.get('prompt_tokens', usage.get('prompt_tokens', usage.get('input_tokens', 0)))
        o = payload.get('output_tokens', usage.get('output_tokens', usage.get('completion_tokens', 0)))
        try:
            p = int(p or 0)
            o = int(o or 0)
        except Exception:
            continue
        if p or o or str(event.get('kind') or '') in {'model_call', 'model_call_completed'}:
            calls += 1
        prompt += max(0, p)
        output += max(0, o)
    return {'model_calls': calls, 'prompt_tokens': prompt, 'output_tokens': output}


def _summary_markdown(snapshot: dict[str, Any]) -> str:
    state = snapshot['state']
    summary = state.get('summary') or {}
    next_action = state.get('next_best_action') or {}
    live = snapshot.get('live_stream') or {}
    tokens = snapshot.get('token_totals') or {}
    files = snapshot.get('files') or []
    lines = [
        f"# Project output · {snapshot['output_id']}",
        '',
        f"Generated: {snapshot['generated_at']}",
        f"Reason: {snapshot['reason']}",
        '',
        '## State',
        f"- Done: {summary.get('done', 0)}",
        f"- Open: {summary.get('open', 0)}",
        f"- In progress: {summary.get('in_progress', 0)}",
        f"- Blocked: {summary.get('blocked', 0)}",
        f"- Rejected: {summary.get('rejected', 0)}",
        f"- Errors: {summary.get('error', 0)}",
    ]
    if next_action:
        lines += [
            '',
            '## Next action',
            f"- Task: {next_action.get('title', '')}",
            f"- Phase: {next_action.get('phase') or next_action.get('status') or ''}",
            f"- Strategy: {next_action.get('strategy', '')}",
        ]
    lines += [
        '',
        '## Model / token trace',
        f"- Role: {live.get('role', '')}",
        f"- Model: {live.get('model', '')}",
        f"- State: {live.get('state', '')}",
        f"- Current chunks: {live.get('chunks', 0) or 0}",
        f"- Current prompt tokens: {live.get('prompt_tokens', 0) or 0}",
        f"- Current output tokens: {live.get('output_tokens', 0) or 0}",
        f"- Recorded calls: {tokens.get('model_calls', 0)}",
        f"- Recorded prompt tokens: {tokens.get('prompt_tokens', 0)}",
        f"- Recorded output tokens: {tokens.get('output_tokens', 0)}",
        '',
        '## Generated files',
    ]
    for item in files[:100]:
        lines.append(f"- `{item['path']}` · {item['size_bytes']} bytes · {item['modified_at']}")
    if not files:
        lines.append('- none yet')
    tail = str(live.get('visible_output_tail') or '').strip()
    if tail:
        lines += ['', '## Visible model output tail', '', '```text', tail[-12000:], '```']
    return '\n'.join(lines) + '\n'


def write_output_snapshot(root: Path, *, reason: str, manual: bool = False) -> dict[str, Any]:
    with _LOCK:
        ledger = Ledger(root / 'ledger.sqlite3')
        job = ledger.latest_job()
        try:
            checkpoint = write_checkpoint(root, reason=f'output:{reason}', manual=manual)
        except Exception:
            checkpoint = build_project_state(root)
        events = ledger.recent_events(500)
        live = load_stream_trace(root, (job or {}).get('id')) if job else None
        if not live:
            live = checkpoint.get('live_stream') or {}
        output_id = 'OUT-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6].upper()
        snapshot = {
            'schema_version': 1,
            'output_id': output_id,
            'generated_at': _now(),
            'reason': reason,
            'manual': bool(manual),
            'project': root.name,
            'job': job,
            'state': checkpoint,
            'live_stream': live or {},
            'token_totals': _token_totals(events),
            'files': _generated_files(root),
        }
        folder = _output_dir(root) / output_id
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / 'manifest.json'
        manifest_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        summary_path = folder / 'summary.md'
        summary_path.write_text(_summary_markdown(snapshot), encoding='utf-8')
        tail = str((live or {}).get('visible_output_tail') or (live or {}).get('visible_output') or '')
        if tail:
            (folder / 'live-output.txt').write_text(tail, encoding='utf-8')
        latest = _output_dir(root) / 'latest.json'
        latest.write_text(json.dumps({
            'output_id': output_id,
            'generated_at': snapshot['generated_at'],
            'reason': reason,
            'manual': bool(manual),
            'manifest': str(manifest_path.relative_to(root)),
            'summary': str(summary_path.relative_to(root)),
        }, indent=2, ensure_ascii=False), encoding='utf-8')
        ledger.event('output_snapshot_created', {
            'output_id': output_id,
            'reason': reason,
            'manual': bool(manual),
            'manifest': str(manifest_path.relative_to(root)),
            'summary': str(summary_path.relative_to(root)),
            'files_indexed': len(snapshot['files']),
            'token_totals': snapshot['token_totals'],
        })
        return snapshot


def list_output_snapshots(root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    policy = load_output_policy(root)
    cap = int(limit or policy.max_snapshots_shown)
    rows: list[dict[str, Any]] = []
    out = _output_dir(root)
    for folder in sorted((p for p in out.iterdir() if p.is_dir() and p.name.startswith('OUT-')), reverse=True):
        manifest = folder / 'manifest.json'
        if not manifest.exists():
            continue
        try:
            raw = json.loads(manifest.read_text(encoding='utf-8'))
            rows.append({
                'output_id': str(raw.get('output_id') or folder.name),
                'generated_at': str(raw.get('generated_at') or ''),
                'reason': str(raw.get('reason') or ''),
                'manual': bool(raw.get('manual', False)),
                'token_totals': dict(raw.get('token_totals') or {}),
                'files_count': len(raw.get('files') or []),
                'summary_path': str((folder / 'summary.md').relative_to(root)),
                'manifest_path': str(manifest.relative_to(root)),
                'live_output_path': str((folder / 'live-output.txt').relative_to(root)) if (folder / 'live-output.txt').exists() else '',
            })
        except Exception:
            continue
        if len(rows) >= cap:
            break
    return rows


def latest_generated_files(root: Path, limit: int = 40) -> list[dict[str, Any]]:
    return _generated_files(root, limit=limit)


def maybe_auto_output_snapshot(root: Path) -> dict[str, Any] | None:
    policy = load_output_policy(root)
    if policy.auto_every_minutes <= 0:
        return None
    ledger = Ledger(root / 'ledger.sqlite3')
    job = ledger.latest_job()
    if not job or str(job.get('status') or '') != JobStatus.RUNNING.value:
        return None
    latest = _output_dir(root) / 'latest.json'
    if latest.exists():
        try:
            raw = json.loads(latest.read_text(encoding='utf-8'))
            stamp = str(raw.get('generated_at') or '')
            if stamp:
                previous = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if (datetime.now(timezone.utc) - previous).total_seconds() < policy.auto_every_minutes * 60:
                    return None
        except Exception:
            pass
    return write_output_snapshot(root, reason=f'auto-{policy.auto_every_minutes}m', manual=False)
