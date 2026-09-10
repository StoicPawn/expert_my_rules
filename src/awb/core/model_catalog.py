from __future__ import annotations

import os
from typing import Any

import httpx


OPENAI_MODELS = [
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
]


def _safe_get(url: str, headers: dict[str, str] | None = None) -> tuple[bool, Any, str]:
    try:
        response = httpx.get(url, headers=headers, timeout=2.5)
        response.raise_for_status()
        return True, response.json(), ''
    except Exception as exc:
        return False, None, f'{type(exc).__name__}: {exc}'


def discover_ollama(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip('/')
    ok, data, error = _safe_get(f'{base}/api/tags')
    models: list[str] = []
    if ok and isinstance(data, dict):
        for item in data.get('models') or []:
            name = str(item.get('name') or item.get('model') or '').strip()
            if name:
                models.append(name)
    return {'kind': 'ollama', 'base_url': base, 'healthy': ok, 'models': sorted(set(models)), 'error': error}


def discover_lmstudio(base_url: str, api_key: str | None = None) -> dict[str, Any]:
    base = base_url.rstrip('/')
    if not base.endswith('/v1'):
        base = base + '/v1'
    headers = {'Authorization': f'Bearer {api_key}'} if api_key else None
    ok, data, error = _safe_get(f'{base}/models', headers=headers)
    models: list[str] = []
    if ok and isinstance(data, dict):
        for item in data.get('data') or []:
            model_id = str(item.get('id') or '').strip()
            if model_id:
                models.append(model_id)
    return {'kind': 'lmstudio', 'base_url': base, 'healthy': ok, 'models': sorted(set(models)), 'error': error}


def catalog_for_manifest(manifest) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node in manifest.runtime.compute_nodes:
        raw = os.getenv(node.base_url_env) if node.base_url_env else None
        base_url = raw or node.base_url or ''
        if node.kind == 'ollama':
            base_url = base_url or os.getenv('OLLAMA_BASE_URL', 'http://ollama:11434')
            nodes[node.id] = {**discover_ollama(base_url), 'enabled': node.enabled}
        elif node.kind == 'lmstudio':
            base_url = base_url or os.getenv('LM_STUDIO_BASE_URL', 'http://host.docker.internal:1234/v1')
            nodes[node.id] = {
                **discover_lmstudio(base_url, os.getenv('LM_STUDIO_API_KEY') or None),
                'enabled': node.enabled,
            }
        else:
            nodes[node.id] = {
                'kind': node.kind,
                'base_url': base_url,
                'healthy': False,
                'models': [],
                'enabled': node.enabled,
                'error': 'Discovery not supported for this node kind',
            }
    return {
        'nodes': nodes,
        'openai': {
            'healthy': bool(os.getenv('OPENAI_API_KEY')),
            'models': list(OPENAI_MODELS),
        },
    }
