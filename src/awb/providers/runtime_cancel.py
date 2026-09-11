from __future__ import annotations

from contextvars import ContextVar, Token
from threading import Event, Lock


class ModelGenerationCancelled(RuntimeError):
    """Raised when a running model call is cancelled by its owning job."""


_CURRENT_JOB: ContextVar[str | None] = ContextVar('awb_current_job', default=None)
_LOCK = Lock()
_EVENTS: dict[str, Event] = {}


def bind_job(job_id: str) -> Token:
    """Bind model calls in the current worker thread to one durable job id."""
    with _LOCK:
        event = _EVENTS.setdefault(str(job_id), Event())
        event.clear()
    return _CURRENT_JOB.set(str(job_id))


def unbind_job(token: Token) -> None:
    _CURRENT_JOB.reset(token)


def current_job_id() -> str | None:
    return _CURRENT_JOB.get()


def event_for_current_job() -> Event | None:
    job_id = current_job_id()
    if not job_id:
        return None
    with _LOCK:
        return _EVENTS.setdefault(job_id, Event())


def request_cancel(job_id: str) -> None:
    with _LOCK:
        _EVENTS.setdefault(str(job_id), Event()).set()


def is_cancel_requested(job_id: str | None = None) -> bool:
    target = str(job_id) if job_id else current_job_id()
    if not target:
        return False
    with _LOCK:
        event = _EVENTS.get(target)
        return bool(event and event.is_set())


def clear_job(job_id: str) -> None:
    with _LOCK:
        _EVENTS.pop(str(job_id), None)
