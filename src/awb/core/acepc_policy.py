from __future__ import annotations

import argparse
import os
from pathlib import Path

from awb.core.cloud_budget import load_control, save_control
from awb.core.models import ComputeNodeSpec, ModelRouteSpec, ProviderSpec
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest

ROLES = ('director', 'worker', 'reviewer', 'verifier')
PRIMARY_MODEL = os.getenv('AWB_LOCAL_MODEL', 'qwen3:4b')
EFFECTIVELY_UNBOUNDED_TOOL_TIMEOUT = 2_147_483_647


def apply_acepc_policy(root: Path, *, lock_cloud: bool = False) -> dict:
    """Tune one workspace for slow, durable, local-only-by-default ACEPC operation.

    The policy deliberately optimizes for eventual completion rather than latency:
    one resident local model, one generation at a time, unbounded scheduler wait,
    no orchestration wall-clock budget, unlimited technical recovery, and larger
    tool-call budgets. Paid APIs remain unavailable unless the user explicitly
    switches the project to FORCE mode in the UI.
    """
    ws = load_workspace(root)
    runtime = ws.manifest.runtime

    local = next((node for node in runtime.compute_nodes if node.id == 'local-ollama'), None)
    if local is None:
        local = ComputeNodeSpec(
            id='local-ollama',
            kind='ollama',
            base_url_env='OLLAMA_BASE_URL',
            enabled=True,
            max_concurrency=1,
            priority=100,
            tags=['local', 'small-device', 'acepc'],
        )
        runtime.compute_nodes.insert(0, local)
    else:
        local.kind = 'ollama'
        local.base_url_env = local.base_url_env or 'OLLAMA_BASE_URL'
        local.enabled = True
        local.max_concurrency = 1
        local.priority = 100
        tags = set(local.tags)
        tags.update({'local', 'small-device', 'acepc'})
        local.tags = sorted(tags)

    runtime.default_provider = ProviderSpec(kind='ollama', model=PRIMARY_MODEL)
    for role in ROLES:
        runtime.role_routes[role] = [
            ModelRouteSpec(node='local-ollama', model=PRIMARY_MODEL, priority=100, enabled=True)
        ]
        agent = next((a for a in ws.manifest.agents if a.role == role), None)
        if agent is not None:
            agent.provider = ProviderSpec(kind='ollama', model=PRIMARY_MODEL)

    runtime.scheduler.enabled = True
    runtime.scheduler.queue_timeout_seconds = 0.0
    runtime.scheduler.failure_threshold = max(5, int(runtime.scheduler.failure_threshold))
    runtime.scheduler.cooldown_seconds = min(30.0, max(1.0, float(runtime.scheduler.cooldown_seconds)))
    runtime.scheduler.allow_cooldown_probe = True

    runtime.max_minutes_per_run = 0
    runtime.continuous_session_minutes = 0
    runtime.technical_retry_limit = 0
    runtime.technical_retry_backoff_max_seconds = max(600.0, float(runtime.technical_retry_backoff_max_seconds))
    runtime.max_tool_calls_per_task = max(50, int(runtime.max_tool_calls_per_task))
    runtime.recovery_history_limit = max(20, int(runtime.recovery_history_limit))
    runtime.checkpoint_pause_seconds = max(0.5, float(runtime.checkpoint_pause_seconds))

    runtime.escalation.enabled = False
    runtime.escalation.daily_budget_eur = 0.0
    runtime.escalation.max_cloud_calls_per_run = 0

    for tool in ws.manifest.tools:
        if tool.id == 'lab_execute':
            # ToolRunner currently expects a positive subprocess timeout. This is
            # ~68 years, i.e. operationally unbounded while remaining compatible.
            tool.timeout_seconds = EFFECTIVELY_UNBOUNDED_TOOL_TIMEOUT

    save_manifest(ws)

    control = load_control(root)
    if lock_cloud:
        control.enabled = False
        control.mode = 'paused'
        save_control(root, control)

    ledger = Ledger(root / 'ledger.sqlite3')
    payload = {
        'model': PRIMARY_MODEL,
        'roles': list(ROLES),
        'max_concurrency': 1,
        'queue_timeout_seconds': 0.0,
        'continuous_session_minutes': 0,
        'technical_retry_limit': 0,
        'max_tool_calls_per_task': runtime.max_tool_calls_per_task,
        'cloud_locked': bool(lock_cloud),
    }
    ledger.event('acepc_local_policy_applied', payload)
    return payload


def apply_all(workspaces_dir: Path, *, lock_cloud: bool = False) -> list[dict]:
    out = []
    if not workspaces_dir.exists():
        return out
    for root in sorted(workspaces_dir.iterdir()):
        if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
            continue
        result = apply_acepc_policy(root, lock_cloud=lock_cloud)
        out.append({'project': root.name, **result})
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--workspaces-dir', type=Path, default=Path(os.getenv('AWB_WORKSPACES_DIR', '/data/workspaces')))
    parser.add_argument('--lock-cloud', action='store_true')
    args = parser.parse_args()
    if args.all:
        results = apply_all(args.workspaces_dir, lock_cloud=args.lock_cloud)
        for row in results:
            print(row)
        return
    if args.root is None:
        parser.error('use --all or --root PATH')
    print(apply_acepc_policy(args.root, lock_cloud=args.lock_cloud))


if __name__ == '__main__':
    main()
