from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock

from awb.providers.base import ModelProvider
from awb.providers.providers import make_provider
from .models import ProjectManifest, ProviderSpec


_SHARED_SLOTS: dict[str, BoundedSemaphore] = {}
_SHARED_SLOTS_LOCK = Lock()


@dataclass(frozen=True)
class ResolvedRoute:
    node_id: str
    kind: str
    model: str | None
    base_url: str | None = None
    source: str = 'runtime'


class ModelRouter:
    """Resolve logical agent roles onto replaceable compute nodes.

    Existing manifests that only contain AgentSpec.provider/default_provider continue
    to work unchanged. New manifests may define compute_nodes + role_routes and can
    therefore move inference from a small local CPU box to one or more GPU machines
    without changing agents, workspaces, gates or the orchestration loop.
    """

    def __init__(self, manifest: ProjectManifest):
        self.manifest = manifest
        self.nodes = {n.id: n for n in manifest.runtime.compute_nodes if n.enabled}
        self._slot_keys: dict[str, str] = {}
        for node in self.nodes.values():
            base_url = os.getenv(node.base_url_env) if node.base_url_env else None
            base_url = base_url or node.base_url or 'default'
            key = f'{node.kind}|{base_url}|{node.id}'
            self._slot_keys[node.id] = key
            with _SHARED_SLOTS_LOCK:
                if key not in _SHARED_SLOTS:
                    _SHARED_SLOTS[key] = BoundedSemaphore(max(1, int(node.max_concurrency)))

    def _agent_provider(self, role: str) -> ProviderSpec:
        agent = next((a for a in self.manifest.agents if a.role == role), None)
        if agent and agent.provider:
            return agent.provider
        return self.manifest.runtime.default_provider

    def candidates(self, role: str) -> list[ResolvedRoute]:
        configured = sorted(
            (r for r in self.manifest.runtime.role_routes.get(role, []) if r.enabled),
            key=lambda r: r.priority,
        )
        resolved: list[ResolvedRoute] = []
        for route in configured:
            node = self.nodes.get(route.node)
            if not node:
                continue
            base_url = os.getenv(node.base_url_env) if node.base_url_env else None
            base_url = base_url or node.base_url
            resolved.append(
                ResolvedRoute(
                    node_id=node.id,
                    kind=node.kind,
                    model=route.model,
                    base_url=base_url,
                    source='compute_node',
                )
            )
        if resolved:
            return resolved

        # Backwards-compatible path for all v0.2 workspaces.
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
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()
