from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any

import httpx

from .base import ModelProvider
from .runtime_progress import clear_progress, set_progress


class OllamaLivenessError(RuntimeError):
    """Raised only when a stalled stream is accompanied by repeated health failure."""


_END = object()
_ROLE_TOKEN_DEFAULTS = {
    'director': 1200,
    'worker': 8192,
    'reviewer': 2200,
    'verifier': 1200,
}


def _env_float(name: str, default: float, low: float) -> float:
    try:
        return max(low, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, low: int) -> int:
    try:
        return max(low, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_optional_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


class OllamaProvider(ModelProvider):
    """Ollama chat provider optimized for very slow local hardware.

    Generation has no wall-clock deadline. Ollama is asked to stream JSON chunks and
    a watchdog observes liveness independently from speed. A slow generation may run
    for hours as long as the stream makes progress or Ollama remains healthy.

    Role-specific output/reasoning budgets prevent orchestration JSON from consuming
    the same CPU budget as theorem construction. The Worker remains the high-reasoning
    path; Director defaults to think=false on Qwen-family models.
    """

    def __init__(self, model: str, base_url: str | None = None):
        self.model = model
        self.base_url = (base_url or os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')).rstrip('/')
        self.stall_check_seconds = _env_float('AWB_OLLAMA_STALL_CHECK_SECONDS', 60.0, 0.05)
        self.health_timeout_seconds = _env_float('AWB_OLLAMA_HEALTH_TIMEOUT_SECONDS', 5.0, 0.05)
        self.health_failure_limit = _env_int('AWB_OLLAMA_HEALTH_FAILURES', 15, 1)
        self.progress_event_seconds = _env_float('AWB_OLLAMA_PROGRESS_EVENT_SECONDS', 60.0, 0.05)
        self.max_output_tokens = _env_int('AWB_OLLAMA_MAX_OUTPUT_TOKENS', 8192, 0)
        self.think: bool | str | None = None
        self.role: str | None = None

        self.legacy_read_timeout_seconds = os.getenv('AWB_OLLAMA_READ_TIMEOUT_SECONDS')
        self.timeout = httpx.Timeout(connect=30.0, read=None, write=60.0, pool=60.0)
        self.health_timeout = httpx.Timeout(
            connect=self.health_timeout_seconds,
            read=self.health_timeout_seconds,
            write=self.health_timeout_seconds,
            pool=self.health_timeout_seconds,
        )

    def configure_role(self, role: str) -> None:
        """Apply a cheap orchestration budget without weakening Worker reasoning."""
        self.role = role
        default_tokens = _ROLE_TOKEN_DEFAULTS.get(role, self.max_output_tokens)
        self.max_output_tokens = _env_int(
            f'AWB_OLLAMA_{role.upper()}_MAX_OUTPUT_TOKENS', default_tokens, 0
        )
        explicit = _env_optional_bool(f'AWB_OLLAMA_{role.upper()}_THINK')
        if explicit is not None:
            self.think = explicit
        elif role == 'director' and self.model.lower().startswith('qwen3'):
            self.think = False
        elif role == 'worker' and self.model.lower().startswith('qwen3'):
            self.think = True
        else:
            self.think = None

    def _healthy(self) -> bool:
        try:
            response = httpx.get(f'{self.base_url}/api/ps', timeout=self.health_timeout)
            response.raise_for_status()
            return True
        except Exception:
            try:
                response = httpx.get(f'{self.base_url}/api/version', timeout=self.health_timeout)
                response.raise_for_status()
                return True
            except Exception:
                return False

    def generate(self, system: str, user: str) -> str:
        started = time.monotonic()
        last_stream_activity = started
        last_progress_event = 0.0
        chunks_seen = 0
        output_chars = 0
        thinking_chars = 0
        health_failures = 0
        pieces: list[str] = []
        q: queue.Queue[Any] = queue.Queue()
        response_holder: dict[str, Any] = {}
        stop = threading.Event()

        payload: dict[str, Any] = {
            'model': self.model,
            'stream': True,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
        }
        if self.max_output_tokens > 0:
            payload['options'] = {'num_predict': self.max_output_tokens}
        if self.think is not None:
            payload['think'] = self.think

        def reader() -> None:
            try:
                with httpx.stream(
                    'POST', f'{self.base_url}/api/chat', json=payload, timeout=self.timeout,
                ) as response:
                    response_holder['response'] = response
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if stop.is_set():
                            break
                        if not line:
                            continue
                        try:
                            q.put(json.loads(line))
                        except json.JSONDecodeError as exc:
                            q.put(exc)
                            return
            except Exception as exc:
                q.put(exc)
            finally:
                q.put(_END)

        thread = threading.Thread(target=reader, name=f'ollama-stream-{self.model}', daemon=True)
        thread.start()

        def emit(state: str, now: float, **extra: Any) -> None:
            nonlocal last_progress_event
            # Only the model's public content stream is exposed. The separate
            # reasoning/thinking field is deliberately represented by a char count.
            visible_tail = ''.join(pieces)[-6000:]
            payload_event = {
                'state': state,
                'model': self.model,
                'role': self.role,
                'elapsed_seconds': round(now - started, 3),
                'chunks': chunks_seen,
                'output_chars': output_chars,
                'thinking_chars': thinking_chars,
                'visible_tail': visible_tail,
                'max_output_tokens': self.max_output_tokens,
                'thinking_enabled': self.think,
                'last_stream_activity_seconds': round(now - last_stream_activity, 3),
                'health_failures': health_failures,
                **extra,
            }
            set_progress(self.model, payload_event)
            self._emit_progress(payload_event)
            last_progress_event = now

        set_progress(self.model, {
            'state': 'starting', 'model': self.model, 'role': self.role,
            'elapsed_seconds': 0.0, 'chunks': 0, 'output_chars': 0,
            'thinking_chars': 0, 'visible_tail': '', 'max_output_tokens': self.max_output_tokens,
            'thinking_enabled': self.think, 'last_stream_activity_seconds': 0.0,
            'health_failures': 0,
        })
        try:
            while True:
                try:
                    item = q.get(timeout=self.stall_check_seconds)
                except queue.Empty:
                    now = time.monotonic()
                    if self._healthy():
                        health_failures = 0
                        emit('alive', now, detail='No stream chunk recently, but Ollama health probes respond.')
                        continue
                    health_failures += 1
                    emit('health_check_failed', now, detail='No stream chunk and Ollama health probes failed.')
                    if health_failures >= self.health_failure_limit:
                        raise OllamaLivenessError(
                            'Ollama stopped streaming and failed '
                            f'{health_failures} consecutive health checks; generation is considered stalled.'
                        )
                    continue

                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                if not isinstance(item, dict):
                    continue

                now = time.monotonic()
                last_stream_activity = now
                health_failures = 0
                chunks_seen += 1
                message = item.get('message') or {}
                content = message.get('content') or ''
                thinking = message.get('thinking') or ''
                if content:
                    pieces.append(str(content))
                    output_chars += len(str(content))
                if thinking:
                    thinking_chars += len(str(thinking))

                if now - last_progress_event >= self.progress_event_seconds or bool(item.get('done')):
                    emit('generating' if not item.get('done') else 'completed_stream', now)
                if item.get('done'):
                    break

            result = ''.join(pieces)
            if not result.strip():
                raise RuntimeError('Ollama stream completed without a visible assistant response')
            return result
        finally:
            stop.set()
            response = response_holder.get('response')
            if response is not None and thread.is_alive():
                try:
                    response.close()
                except Exception:
                    pass
            thread.join(timeout=2.0)
            clear_progress(self.model)
