from __future__ import annotations

from contextvars import ContextVar, Token
from threading import Event, Lock


class ModelGenerationCancelled(RuntimeError):
    """Raised when a running model call is cancelled by its owning job."""


_CURRENT_JOB: ContextVar[str | None] = ContextVar('awb_current_job', default=None)
_LOCK = Lock()
_EVENTS: dict[str, Event] = {}
_GLOBAL_STOP = Event()
_ACTIVE_GENERATIONS = 0


def bind_job(job_id: str) -> Token:
    with _LOCK:
        event = _EVENTS.setdefault(str(job_id), Event())
        event.clear()
    return _CURRENT_JOB.set(str(job_id))


def unbind_job(token: Token) -> None:
    _CURRENT_JOB.reset(token)


def current_job_id() -> str | None:
    return _CURRENT_JOB.get()


def event_for_current_job() -> Event:
    job_id = current_job_id()
    if not job_id:
        return _GLOBAL_STOP
    with _LOCK:
        return _EVENTS.setdefault(job_id, Event())


def request_cancel(job_id: str | None = None) -> None:
    """Stop the requested job and, as a safety barrier, any in-flight local generation."""
    _GLOBAL_STOP.set()
    if job_id:
        with _LOCK:
            _EVENTS.setdefault(str(job_id), Event()).set()


def clear_cancel_barrier(job_id: str | None = None) -> None:
    """Allow new generations only after all previous ones have actually stopped."""
    with _LOCK:
        if _ACTIVE_GENERATIONS:
            raise RuntimeError('Cannot clear model stop barrier while a generation is still active')
        if job_id:
            _EVENTS.pop(str(job_id), None)
        _GLOBAL_STOP.clear()


def is_cancel_requested(job_id: str | None = None) -> bool:
    if _GLOBAL_STOP.is_set():
        return True
    target = str(job_id) if job_id else current_job_id()
    if not target:
        return False
    with _LOCK:
        event = _EVENTS.get(target)
        return bool(event and event.is_set())


def generation_started() -> None:
    global _ACTIVE_GENERATIONS
    with _LOCK:
        _ACTIVE_GENERATIONS += 1


def generation_finished() -> None:
    global _ACTIVE_GENERATIONS
    with _LOCK:
        _ACTIVE_GENERATIONS = max(0, _ACTIVE_GENERATIONS - 1)


def active_generations() -> int:
    with _LOCK:
        return _ACTIVE_GENERATIONS


def clear_job(job_id: str) -> None:
    with _LOCK:
        _EVENTS.pop(str(job_id), None)
