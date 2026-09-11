from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .base import ModelProvider
from .runtime_cancel import (
    ModelGenerationCancelled,
    current_job_id,
    event_for_current_job,
    generation_finished,
    generation_started,
)
from .runtime_progress import clear_progress, set_progress


class OllamaLivenessError(RuntimeError):
    """Raised only when a stalled stream is accompanied by repeated health failure."""


_END = object()
_ROLE_TOKEN_DEFAULTS = {
    'planner': 1400,
    'director': 1200,
    'worker': 8192,
    'reviewer': 2600,
    'verifier': 1400,
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaProvider(ModelProvider):
    """Streaming Ollama provider for very slow, always-on local hardware.

    There is intentionally no generation wall-clock deadline. Liveness is judged by
    stream activity plus Ollama health, not speed. Every visible generation is also
    journaled locally with its full model inputs and visible output so an interruption
    can be checkpointed and resumed without losing the useful intermediate work.
    Hidden reasoning text is never persisted; only its character count is exposed.
    """

    def __init__(self, model: str, base_url: str | None = None):
        self.model = model
        self.base_url = (base_url or os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')).rstrip('/')
        self.stall_check_seconds = _env_float('AWB_OLLAMA_STALL_CHECK_SECONDS', 60.0, 0.05)
        self.health_timeout_seconds = _env_float('AWB_OLLAMA_HEALTH_TIMEOUT_SECONDS', 5.0, 0.05)
        self.health_failure_limit = _env_int('AWB_OLLAMA_HEALTH_FAILURES', 15, 1)
        self.progress_event_seconds = _env_float('AWB_OLLAMA_PROGRESS_EVENT_SECONDS', 1.0, 0.05)
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
        self.role = role
        default_tokens = _ROLE_TOKEN_DEFAULTS.get(role, self.max_output_tokens)
        self.max_output_tokens = _env_int(f'AWB_OLLAMA_{role.upper()}_MAX_OUTPUT_TOKENS', default_tokens, 0)
        explicit = _env_optional_bool(f'AWB_OLLAMA_{role.upper()}_THINK')
        if explicit is not None:
            self.think = explicit
        elif role in {'planner', 'director', 'verifier'} and self.model.lower().startswith('qwen3'):
            self.think = False
        elif role in {'worker', 'reviewer'} and self.model.lower().startswith('qwen3'):
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

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Operational estimate only; exact prompt/eval counts replace it when Ollama
        # returns them on the final stream record.
        raw = str(text or '')
        return max(0, (len(raw.encode('utf-8')) + 3) // 4)

    @staticmethod
    def _trace_path(job_id: str | None) -> Path | None:
        root = os.getenv('AWB_STREAM_TRACE_DIR', '').strip()
        if not root or not job_id:
            return None
        return Path(root) / f'{job_id}.json'

    def generate(self, system: str, user: str) -> str:
        started = time.monotonic()
        last_stream_activity = started
        last_progress_event = 0.0
        chunks_seen = 0
        output_chars = 0
        thinking_chars = 0
        health_failures = 0
        prompt_tokens = self._estimate_tokens(system + '\n' + user)
        output_tokens = 0
        exact_prompt_tokens = False
        exact_output_tokens = False
        pieces: list[str] = []
        q: queue.Queue[Any] = queue.Queue()
        response_holder: dict[str, Any] = {}
        stop = threading.Event()
        cancel_event = event_for_current_job()
        job_id = current_job_id()
        final_state = 'starting'
        final_error = ''
        if cancel_event.is_set():
            raise ModelGenerationCancelled(f'Generation cancelled before start for job {job_id}')
        generation_started()

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

        trace_path = self._trace_path(job_id)

        def write_trace(state: str, now: float, *, detail: str = '', error: str = '') -> None:
            if trace_path is None:
                return
            try:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                visible = ''.join(pieces)
                doc = {
                    'schema_version': 1,
                    'written_at': _utcnow(),
                    'job_id': job_id,
                    'role': self.role,
                    'model': self.model,
                    'state': state,
                    'detail': detail,
                    'error': error,
                    'elapsed_seconds': round(now - started, 3),
                    'chunks': chunks_seen,
                    'output_chars': output_chars,
                    'thinking_chars': thinking_chars,
                    'prompt_tokens': int(prompt_tokens),
                    'output_tokens': int(output_tokens if exact_output_tokens else self._estimate_tokens(visible)),
                    'prompt_tokens_exact': exact_prompt_tokens,
                    'output_tokens_exact': exact_output_tokens,
                    'max_output_tokens': self.max_output_tokens,
                    'thinking_enabled': self.think,
                    'last_stream_activity_seconds': round(now - last_stream_activity, 3),
                    'system_prompt': system,
                    'user_prompt': user,
                    'visible_output': visible,
                }
                tmp = trace_path.with_suffix('.tmp')
                tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
                tmp.replace(trace_path)
            except Exception:
                # Observability must never be able to break scientific work.
                pass

        def reader() -> None:
            try:
                with httpx.stream('POST', f'{self.base_url}/api/chat', json=payload, timeout=self.timeout) as response:
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
            visible = ''.join(pieces)
            effective_output_tokens = output_tokens if exact_output_tokens else self._estimate_tokens(visible)
            payload_event = {
                'state': state,
                'model': self.model,
                'role': self.role,
                'job_id': job_id,
                'elapsed_seconds': round(now - started, 3),
                'chunks': chunks_seen,
                'output_chars': output_chars,
                'thinking_chars': thinking_chars,
                'prompt_tokens': int(prompt_tokens),
                'output_tokens': int(effective_output_tokens),
                'prompt_tokens_exact': exact_prompt_tokens,
                'output_tokens_exact': exact_output_tokens,
                'visible_tail': visible[-12000:],
                'max_output_tokens': self.max_output_tokens,
                'thinking_enabled': self.think,
                'last_stream_activity_seconds': round(now - last_stream_activity, 3),
                'health_failures': health_failures,
                **extra,
            }
            set_progress(self.model, payload_event)
            self._emit_progress(payload_event)
            write_trace(state, now, detail=str(extra.get('detail') or ''))
            last_progress_event = now

        now = time.monotonic()
        set_progress(self.model, {
            'state': 'starting', 'model': self.model, 'role': self.role, 'job_id': job_id,
            'elapsed_seconds': 0.0, 'chunks': 0, 'output_chars': 0,
            'thinking_chars': 0, 'prompt_tokens': prompt_tokens, 'output_tokens': 0,
            'prompt_tokens_exact': False, 'output_tokens_exact': False,
            'visible_tail': '', 'max_output_tokens': self.max_output_tokens,
            'thinking_enabled': self.think, 'last_stream_activity_seconds': 0.0,
            'health_failures': 0,
        })
        write_trace('starting', now)
        try:
            while True:
                if cancel_event.is_set():
                    final_state = 'cancelled'
                    emit('cancelling', time.monotonic(), detail='STOP requested; closing Ollama stream.')
                    raise ModelGenerationCancelled(f'Generation cancelled for job {job_id}')
                try:
                    item = q.get(timeout=min(0.5, self.stall_check_seconds))
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_stream_activity < self.stall_check_seconds:
                        continue
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
                if cancel_event.is_set():
                    final_state = 'cancelled'
                    emit('cancelling', time.monotonic(), detail='STOP requested; closing Ollama stream.')
                    raise ModelGenerationCancelled(f'Generation cancelled for job {job_id}')
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
                if item.get('prompt_eval_count') is not None:
                    prompt_tokens = max(0, int(item.get('prompt_eval_count') or 0))
                    exact_prompt_tokens = True
                if item.get('eval_count') is not None:
                    output_tokens = max(0, int(item.get('eval_count') or 0))
                    exact_output_tokens = True
                if now - last_progress_event >= self.progress_event_seconds or bool(item.get('done')):
                    emit('generating' if not item.get('done') else 'completed_stream', now)
                if item.get('done'):
                    break
            result = ''.join(pieces)
            if not result.strip():
                raise RuntimeError('Ollama stream completed without a visible assistant response')
            final_state = 'completed'
            write_trace('completed', time.monotonic())
            return result
        except BaseException as exc:
            if final_state != 'cancelled':
                final_state = 'error'
            final_error = f'{type(exc).__name__}: {exc}'
            write_trace(final_state, time.monotonic(), error=final_error)
            raise
        finally:
            stop.set()
            response = response_holder.get('response')
            if response is not None and thread.is_alive():
                try:
                    response.close()
                except Exception:
                    pass
            thread.join(timeout=2.0)
            write_trace(final_state, time.monotonic(), error=final_error)
            clear_progress(self.model)
            generation_finished()
