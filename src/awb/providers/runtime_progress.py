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


def _read_shared(model: str | None = None) -> dict[str, Any] | None:
    path = _shared_path()
    if path is None or not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        if model is not None and str(doc.get('model_key')) != str(model):
            return None
        # A progress writer refreshes at least once per liveness interval. After
        # ten minutes without an update, treat the file as stale rather than
        # claiming a dead process is still generating.
        if time.time() - float(doc.get('written_at') or 0) > 600:
            return None
        payload = doc.get('payload')
        return dict(payload) if isinstance(payload, dict) else None
    except Exception:
        return None


def get_progress(model: str | None = None) -> dict[str, Any] | None:
    with _LOCK:
        if model is not None:
            value = _PROGRESS.get(str(model))
            if value:
                return dict(value)
        elif _PROGRESS:
            return dict(next(reversed(_PROGRESS.values())))
    return _read_shared(model)
