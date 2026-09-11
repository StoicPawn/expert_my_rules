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


def _job_dir() -> Path | None:
    base = _shared_path()
    if base is None:
        return None
    return base.parent / (base.stem + '-jobs')


def _job_path(job_id: str | None) -> Path | None:
    if not job_id:
        return _shared_path()
    directory = _job_dir()
    if directory is None:
        return None
    safe = ''.join(ch for ch in str(job_id) if ch.isalnum() or ch in {'-', '_'})
    return directory / f'{safe}.json'


def _write_doc(path: Path, model: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {'model_key': str(model), 'written_at': time.time(), 'payload': dict(payload)}
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def _write_shared(model: str, payload: dict[str, Any]) -> None:
    path = _job_path(str(payload.get('job_id') or '') or None)
    if path is None:
        return
    try:
        _write_doc(path, model, payload)
        # Keep the legacy single-file snapshot as a best-effort "latest activity"
        # view for old clients. Job-aware readers always use the isolated file.
        base = _shared_path()
        if base is not None and base != path:
            _write_doc(base, model, payload)
    except Exception:
        # Telemetry must never be able to fail an inference.
        pass


def set_progress(model: str, payload: dict[str, Any]) -> None:
    key = f"{payload.get('job_id') or 'global'}::{model}"
    with _LOCK:
        _PROGRESS[key] = dict(payload)
    _write_shared(model, payload)


def clear_progress(model: str) -> None:
    with _LOCK:
        stale = [key for key, value in _PROGRESS.items() if key.endswith(f'::{model}')]
        for key in stale:
            _PROGRESS.pop(key, None)
    # Do not delete job-scoped files by model name: another process can be using
    # the same resident model. Job lifecycle cleanup owns those files.


def clear_progress_for_job(job_id: str) -> None:
    target = str(job_id)
    with _LOCK:
        stale = [key for key, value in _PROGRESS.items() if str(value.get('job_id') or '') == target]
        for key in stale:
            _PROGRESS.pop(key, None)
    path = _job_path(target)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    base = _shared_path()
    if base is None:
        return
    try:
        if base.exists():
            doc = json.loads(base.read_text(encoding='utf-8'))
            payload = doc.get('payload') or {}
            if str(payload.get('job_id') or '') == target:
                base.unlink(missing_ok=True)
    except Exception:
        pass


def _read_path(path: Path | None, model: str | None = None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        if model is not None and str(doc.get('model_key')) != str(model):
            return None
        if time.time() - float(doc.get('written_at') or 0) > 90:
            return None
        payload = doc.get('payload')
        return dict(payload) if isinstance(payload, dict) else None
    except Exception:
        return None


def _read_shared(model: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
    if job_id:
        return _read_path(_job_path(job_id), model)
    base = _shared_path()
    candidates: list[Path] = []
    if base is not None and base.exists():
        candidates.append(base)
    directory = _job_dir()
    if directory is not None and directory.exists():
        try:
            candidates.extend(directory.glob('*.json'))
        except Exception:
            pass
    newest: tuple[float, dict[str, Any]] | None = None
    for path in candidates:
        value = _read_path(path, model)
        if value is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except Exception:
            mtime = 0.0
        if newest is None or mtime > newest[0]:
            newest = (mtime, value)
    return newest[1] if newest else None


def get_progress(model: str | None = None, job_id: str | None = None) -> dict[str, Any] | None:
    with _LOCK:
        values = list(_PROGRESS.values())
        for value in reversed(values):
            if model is not None and str(value.get('model') or '') != str(model):
                continue
            if job_id is not None and str(value.get('job_id') or '') != str(job_id):
                continue
            return dict(value)
    value = _read_shared(model, job_id)
    if value and job_id is not None and str(value.get('job_id') or '') != str(job_id):
        return None
    return value
