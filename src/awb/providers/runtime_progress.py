from __future__ import annotations

from threading import Lock
from typing import Any


_LOCK = Lock()
_PROGRESS: dict[str, dict[str, Any]] = {}


def set_progress(model: str, payload: dict[str, Any]) -> None:
    with _LOCK:
        _PROGRESS[str(model)] = dict(payload)


def clear_progress(model: str) -> None:
    with _LOCK:
        _PROGRESS.pop(str(model), None)


def get_progress(model: str | None = None) -> dict[str, Any] | None:
    with _LOCK:
        if model is not None:
            value = _PROGRESS.get(str(model))
            return dict(value) if value else None
        if not _PROGRESS:
            return None
        # Local small-device routing is sequential, so the newest active entry is
        # enough for the dashboard. Keep a defensive copy for thread safety.
        value = next(reversed(_PROGRESS.values()))
        return dict(value)
