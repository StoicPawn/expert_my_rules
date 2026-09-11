from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any


_LOCK = Lock()
_PROGRESS: dict[str, dict[str, Any]] = {}


def _shared_path() -> Path | None:
    raw = os.getenv('AWB_RUNTIME_PROGRESS_FILE', '').strip()
    return Path(raw) if raw else None


def _write_shared(model: str, payload: dict[str, Any]) -> None:
    path = _shared_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {'model_key': str(model), 'written_at': time.time(), 'payload': dict(payload)}
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except Exception:
        # Telemetry must never be able to fail an inference.
        pass


def set_progress(model: str, payload: dict[str, Any]) -> None:
    with _LOCK:
        _PROGRESS[str(model)] = dict(payload)
    _write_shared(model, payload)


def clear_progress(model: str) -> None:
    with _LOCK:
        _PROGRESS.pop(str(model), None)
    path = _shared_path()
    if path is None:
        return
    try:
        if path.exists():
            doc = json.loads(path.read_text(encoding='utf-8'))
            if str(doc.get('model_key')) == str(model):
                path.unlink(missing_ok=True)
    except Exception:
        pass


def clear_progress_for_job(job_id: str) -> None:
    """Remove telemetry owned by one completed/cancelled job."""
    target = str(job_id)
    with _LOCK:
        stale = [key for key, value in _PROGRESS.items() if str(value.get('job_id') or '') == target]
        for key in stale:
            _PROGRESS.pop(key, None)
    path = _shared_path()
    if path is None:
        return
    try:
        if path.exists():
            doc = json.loads(path.read_text(encoding='utf-8'))
            payload = doc.get('payload') or {}
            if str(payload.get('job_id') or '') == target:
                path.unlink(missing_ok=True)
    except Exception:
        pass


def _read_shared(model: str | None = None) -> dict[str, Any] | None:
    path = _shared_path()
    if path is None or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        if model is not None and str(doc.get('model_key')) != str(model):
            return None
        # The writer refreshes frequently while a model is active. A short TTL
        # prevents a dead/finished run from looking like it is still generating.
        if time.time() - float(doc.get('written_at') or 0) > 30:
            return None
        payload = doc.get('payload')
        return dict(payload) if isinstance(payload, dict) else None
    except Exception:
        return None


def get_progress(model: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
    with _LOCK:
        if model is not None:
            value = _PROGRESS.get(str(model))
            if value and (job_id is None or str(value.get('job_id') or '') == str(job_id)):
                return dict(value)
        elif _PROGRESS:
            for value in reversed(list(_PROGRESS.values())):
                if job_id is None or str(value.get('job_id') or '') == str(job_id):
                    return dict(value)
    value = _read_shared(model)
    if value and job_id is not None and str(value.get('job_id') or '') != str(job_id):
        return None
    return value
