from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock

from awb.providers.base import ModelProvider
from awb.providers.providers import make_provider
from .models import ProjectManifest, ProviderSpec


class RouteBusyError(RuntimeError):
    """Raised only when a compute node has an explicit bounded queue timeout."""


@dataclass
class _NodeRuntimeState:
    active: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    total_seconds: float = 0.0
    total_chars: int = 0


_SHARED_SLOTS: dict[str, BoundedSemaphore] = {}
_SHARED_STATES: dict[str, _NodeRuntimeState] = {}
_SHARED_LOCK = Lock()


@dataclass(frozen=True)
class ResolvedRoute:
    node_id: str
    kind: str
    model: str | None
    base_url: str | None = None
    source: str = 'runtime'
    priority: int = 100


class ModelRouter:
    """Resolve logical agent roles onto replaceable compute nodes.

    On a tiny always-on machine the important invariant is one expensive generation
    at a time. ``queue_timeout_seconds <= 0`` therefore means wait indefinitely for
    the local slot: saturation is back-pressure, not a scientific/runtime failure.
    Faster machines can still configure a positive bounded queue timeout.
    """

    def __init__(self, manifest: ProjectManifest):
        self.manifest = manifest
        self.policy = manifest.runtime.scheduler
        self.nodes = {n.id: n for n in manifest.runtime.compute_nodes if n.enabled}
        self._slot_keys: dict[str, str] = {}
        for node in self.nodes.values():
            base_url = os.getenv(node.base_url_env) if node.base_url_env else None
            base_url = base_url or node.base_url or 'default'
            capacity = max(1, int(node.max_concurrency))
            key = f'{node.kind}|{base_url}|{node.id}|{capacity}'
            self._slot_keys[node.id] = key
            with _SHARED_LOCK:
                if key not in _SHARED_SLOTS:
                    _SHARED_SLOTS[key] = BoundedSemaphore(capacity)
                if key not in _SHARED_STATES:
                    _SHARED_STATES[key] = _NodeRuntimeState()

    def _agent_provider(self, role: str) -> ProviderSpec:
        agent = next((a for a in self.manifest.agents if a.role == role), None)
        if agent and agent.provider:
            return agent.provider
        return self.manifest.runtime.default_provider

    def _state_for_node(self, node_id: str) -> _NodeRuntimeState | None:
        key = self._slot_keys.get(node_id)
        if not key:
            return None
        return _SHARED_STATES.get(key)

    def _runtime_score(self, route, node_id: str) -> tuple[int, float, str]:
        state = self._state_for_node(node_id)
        if not state or not self.policy.enabled:
            return (int(route.priority), 0.0, node_id)
        with _SHARED_LOCK:
            score = (
                int(route.priority)
                + int(state.active) * int(self.policy.load_penalty)
                + int(state.consecutive_failures) * int(self.policy.failure_penalty)
            )
            cooldown = state.cooldown_until
        return (score, cooldown, node_id)

    def candidates(self, role: str) -> list[ResolvedRoute]:
        configured = [r for r in self.manifest.runtime.role_routes.get(role, []) if r.enabled]
        resolved_scored: list[tuple[tuple[int, float, str], ResolvedRoute]] = []
        now = time.monotonic()
        cooling: list[tuple[tuple[int, float, str], ResolvedRoute]] = []

        for route in configured:
            node = self.nodes.get(route.node)
            if not node:
                continue
            base_url = os.getenv(node.base_url_env) if node.base_url_env else None
            base_url = base_url or node.base_url
            resolved = ResolvedRoute(
                node_id=node.id,
                kind=node.kind,
                model=route.model,
                base_url=base_url,
                source='compute_node',
                priority=int(route.priority),
            )
            score = self._runtime_score(route, node.id)
            if self.policy.enabled and score[1] > now:
                cooling.append((score, resolved))
            else:
                resolved_scored.append((score, resolved))

        if resolved_scored:
            resolved_scored.sort(key=lambda item: item[0])
            return [item[1] for item in resolved_scored]

        if cooling and self.policy.allow_cooldown_probe:
            cooling.sort(key=lambda item: (item[0][1], item[0][0], item[0][2]))
            return [cooling[0][1]]

        if configured and not resolved_scored and cooling:
            return []

        spec = self._agent_provider(role)
        return [ResolvedRoute(node_id='legacy', kind=spec.kind, model=spec.model, source='legacy')]

    def provider(self, route: ResolvedRoute) -> ModelProvider:
        return make_provider(route.kind, route.model, base_url=route.base_url)

    @contextmanager
    def slot(self, route: ResolvedRoute):
        key = self._slot_keys.get(route.node_id)
        semaphore = _SHARED_SLOTS.get(key) if key else None
        if semaphore is None:
            yield
            return

        configured_timeout = float(self.policy.queue_timeout_seconds) if self.policy.enabled else 0.0
        if not self.policy.enabled or configured_timeout <= 0:
            # Endurance mode: a busy CPU is expected. Wait until the previous role
            # releases the only inference slot instead of turning load into ERROR.
            semaphore.acquire()
            acquired = True
        else:
            acquired = semaphore.acquire(timeout=configured_timeout)
        if not acquired:
            raise RouteBusyError(
                f'Compute node {route.node_id} remained saturated for {configured_timeout:.1f}s'
            )
        with _SHARED_LOCK:
            _SHARED_STATES[key].active += 1
        try:
            yield
        finally:
            with _SHARED_LOCK:
                _SHARED_STATES[key].active = max(0, _SHARED_STATES[key].active - 1)
            semaphore.release()

    def record_success(self, route: ResolvedRoute, *, seconds: float, chars: int) -> None:
        key = self._slot_keys.get(route.node_id)
        if not key:
            return
        with _SHARED_LOCK:
            state = _SHARED_STATES[key]
            state.successes += 1
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            state.total_seconds += max(0.0, float(seconds))
            state.total_chars += max(0, int(chars))

    def record_failure(self, route: ResolvedRoute) -> None:
        key = self._slot_keys.get(route.node_id)
        if not key:
            return
        with _SHARED_LOCK:
            state = _SHARED_STATES[key]
            state.failures += 1
            state.consecutive_failures += 1
            threshold = max(1, int(self.policy.failure_threshold))
            if self.policy.enabled and state.consecutive_failures >= threshold:
                state.cooldown_until = time.monotonic() + max(0.0, float(self.policy.cooldown_seconds))

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        out = []
        for node_id, node in self.nodes.items():
            key = self._slot_keys[node_id]
            with _SHARED_LOCK:
                state = _SHARED_STATES[key]
                successes = state.successes
                failures = state.failures
                active = state.active
                consecutive = state.consecutive_failures
                cooldown_remaining = max(0.0, state.cooldown_until - now)
                total_seconds = state.total_seconds
                total_chars = state.total_chars
            out.append({
                'node': node_id,
                'kind': node.kind,
                'max_concurrency': max(1, int(node.max_concurrency)),
                'active': active,
                'successes': successes,
                'failures': failures,
                'consecutive_failures': consecutive,
                'cooldown_remaining_seconds': round(cooldown_remaining, 3),
                'avg_seconds': round(total_seconds / successes, 3) if successes else None,
                'chars_per_second': round(total_chars / total_seconds, 1) if total_seconds > 0 else None,
                'queue_timeout_seconds': float(self.policy.queue_timeout_seconds),
                'waits_indefinitely_when_busy': float(self.policy.queue_timeout_seconds) <= 0,
                'tags': list(node.tags),
            })
        return out
