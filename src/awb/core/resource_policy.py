from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from awb.core.models import JobStatus
from awb.core.storage import Ledger


@dataclass
class SystemResourcePolicy:
    cpu_cores: float = 3.4
    ram_gb: float = 6.0
    max_running_projects: int = 2
    default_context_tokens: int = 8192
    dashboard_refresh_seconds: int = 60


@dataclass
class ProjectResourcePolicy:
    cpu_cores: float = 1.7
    ram_gb: float = 3.0
    context_tokens: int = 8192
    max_tool_calls_per_task: int = 50


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def _system_path(workspaces_dir: Path) -> Path:
    return workspaces_dir / '.awb-system-resources.json'


def _project_path(project_root: Path) -> Path:
    return project_root / '.awb' / 'resource-policy.json'


def detected_limits() -> dict[str, float]:
    cpu = float(max(1, os.cpu_count() or 1))
    ram_gb = 8.0
    try:
        for line in Path('/proc/meminfo').read_text(encoding='utf-8').splitlines():
            if line.startswith('MemTotal:'):
                ram_gb = max(1.0, float(line.split()[1]) * 1024.0 / (1024.0 ** 3))
                break
    except Exception:
        pass
    return {'cpu_cores': cpu, 'ram_gb': ram_gb}


def _default_system() -> SystemResourcePolicy:
    limits = detected_limits()
    return SystemResourcePolicy(
        cpu_cores=round(min(3.4, max(1.0, limits['cpu_cores'] * 0.85)), 2),
        ram_gb=round(min(6.0, max(2.0, limits['ram_gb'] * 0.75)), 2),
        max_running_projects=2,
        default_context_tokens=8192,
        dashboard_refresh_seconds=60,
    )


def load_system_policy(workspaces_dir: Path) -> SystemResourcePolicy:
    path = _system_path(workspaces_dir)
    default = _default_system()
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return SystemResourcePolicy(
            cpu_cores=float(raw.get('cpu_cores', default.cpu_cores)),
            ram_gb=float(raw.get('ram_gb', default.ram_gb)),
            max_running_projects=int(raw.get('max_running_projects', default.max_running_projects)),
            default_context_tokens=int(raw.get('default_context_tokens', default.default_context_tokens)),
            dashboard_refresh_seconds=int(raw.get('dashboard_refresh_seconds', default.dashboard_refresh_seconds)),
        )
    except Exception:
        return default


def save_system_policy(workspaces_dir: Path, policy: SystemResourcePolicy) -> SystemResourcePolicy:
    limits = detected_limits()
    policy.cpu_cores = round(max(0.5, min(float(policy.cpu_cores), limits['cpu_cores'])), 2)
    policy.ram_gb = round(max(1.0, min(float(policy.ram_gb), limits['ram_gb'])), 2)
    policy.max_running_projects = max(1, min(int(policy.max_running_projects), 8))
    policy.default_context_tokens = max(1024, min(int(policy.default_context_tokens), 131072))
    policy.dashboard_refresh_seconds = max(10, min(int(policy.dashboard_refresh_seconds), 300))

    active = active_allocations(workspaces_dir)
    cpu_reserved = sum(float(item['policy'].cpu_cores) for item in active)
    ram_reserved = sum(float(item['policy'].ram_gb) for item in active)
    if cpu_reserved > policy.cpu_cores + 1e-9:
        raise ValueError(f'I progetti RUNNING riservano {cpu_reserved:.2f} CPU, oltre il nuovo limite {policy.cpu_cores:.2f}.')
    if ram_reserved > policy.ram_gb + 1e-9:
        raise ValueError(f'I progetti RUNNING riservano {ram_reserved:.2f} GB RAM, oltre il nuovo limite {policy.ram_gb:.2f} GB.')
    if len(active) > policy.max_running_projects:
        raise ValueError(f'Sono già RUNNING {len(active)} progetti; il limite non può scendere a {policy.max_running_projects}.')
    _atomic_json(_system_path(workspaces_dir), asdict(policy))
    return policy


def _default_project(project_root: Path) -> ProjectResourcePolicy:
    system = load_system_policy(project_root.parent)
    divisor = max(1, system.max_running_projects)
    return ProjectResourcePolicy(
        cpu_cores=round(max(0.5, system.cpu_cores / divisor), 2),
        ram_gb=round(max(1.0, system.ram_gb / divisor), 2),
        context_tokens=system.default_context_tokens,
        max_tool_calls_per_task=50,
    )


def load_project_policy(project_root: Path) -> ProjectResourcePolicy:
    path = _project_path(project_root)
    default = _default_project(project_root)
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return ProjectResourcePolicy(
            cpu_cores=float(raw.get('cpu_cores', default.cpu_cores)),
            ram_gb=float(raw.get('ram_gb', default.ram_gb)),
            context_tokens=int(raw.get('context_tokens', default.context_tokens)),
            max_tool_calls_per_task=int(raw.get('max_tool_calls_per_task', default.max_tool_calls_per_task)),
        )
    except Exception:
        return default


def _latest_status(project_root: Path) -> str:
    try:
        job = Ledger(project_root / 'ledger.sqlite3').latest_job()
        return str((job or {}).get('status') or '')
    except Exception:
        return ''


def _project_roots(workspaces_dir: Path) -> Iterable[Path]:
    if not workspaces_dir.exists():
        return []
    return [p for p in workspaces_dir.iterdir() if p.is_dir() and (p / 'project.yaml').exists() and (p / 'ledger.sqlite3').exists()]


def active_allocations(workspaces_dir: Path, *, exclude_project: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for root in _project_roots(workspaces_dir):
        if exclude_project and root.name == exclude_project:
            continue
        status = _latest_status(root)
        if status != JobStatus.RUNNING.value:
            continue
        rows.append({'project': root.name, 'status': status, 'policy': load_project_policy(root)})
    return rows


def resource_summary(workspaces_dir: Path) -> dict:
    system = load_system_policy(workspaces_dir)
    active = active_allocations(workspaces_dir)
    cpu_reserved = round(sum(float(item['policy'].cpu_cores) for item in active), 2)
    ram_reserved = round(sum(float(item['policy'].ram_gb) for item in active), 2)
    return {
        'system': asdict(system),
        'running_projects': len(active),
        'cpu_reserved': cpu_reserved,
        'cpu_free': round(max(0.0, system.cpu_cores - cpu_reserved), 2),
        'ram_reserved': ram_reserved,
        'ram_free': round(max(0.0, system.ram_gb - ram_reserved), 2),
        'projects': [
            {
                'project': item['project'],
                'status': item['status'],
                'cpu_cores': item['policy'].cpu_cores,
                'ram_gb': item['policy'].ram_gb,
                'context_tokens': item['policy'].context_tokens,
            }
            for item in active
        ],
    }


def save_project_policy(project_root: Path, policy: ProjectResourcePolicy) -> ProjectResourcePolicy:
    system = load_system_policy(project_root.parent)
    policy.cpu_cores = round(max(0.5, min(float(policy.cpu_cores), system.cpu_cores)), 2)
    policy.ram_gb = round(max(1.0, min(float(policy.ram_gb), system.ram_gb)), 2)
    policy.context_tokens = max(1024, min(int(policy.context_tokens), 131072))
    policy.max_tool_calls_per_task = max(1, min(int(policy.max_tool_calls_per_task), 500))

    if _latest_status(project_root) == JobStatus.RUNNING.value:
        others = active_allocations(project_root.parent, exclude_project=project_root.name)
        cpu = sum(float(item['policy'].cpu_cores) for item in others) + policy.cpu_cores
        ram = sum(float(item['policy'].ram_gb) for item in others) + policy.ram_gb
        if cpu > system.cpu_cores + 1e-9:
            raise ValueError(f'Allocazione CPU totale RUNNING {cpu:.2f} > limite Expert {system.cpu_cores:.2f}.')
        if ram > system.ram_gb + 1e-9:
            raise ValueError(f'Allocazione RAM totale RUNNING {ram:.2f} GB > limite Expert {system.ram_gb:.2f} GB.')
    _atomic_json(_project_path(project_root), asdict(policy))
    return policy


def admission_check(project_root: Path) -> tuple[bool, str]:
    system = load_system_policy(project_root.parent)
    own = load_project_policy(project_root)
    others = active_allocations(project_root.parent, exclude_project=project_root.name)
    if len(others) >= system.max_running_projects:
        return False, f'Limite progetti simultanei raggiunto ({system.max_running_projects}).'
    cpu = sum(float(item['policy'].cpu_cores) for item in others) + own.cpu_cores
    ram = sum(float(item['policy'].ram_gb) for item in others) + own.ram_gb
    if cpu > system.cpu_cores + 1e-9:
        return False, f'CPU riservata {cpu:.2f} > limite Expert {system.cpu_cores:.2f}. Riduci la quota di uno dei progetti.'
    if ram > system.ram_gb + 1e-9:
        return False, f'RAM riservata {ram:.2f} GB > limite Expert {system.ram_gb:.2f} GB. Riduci la quota di uno dei progetti.'
    return True, 'ok'


def bind_project_resource_env(project_root: Path) -> None:
    """Point this child process at the live project policy.

    OllamaStream reads the file before every generation, therefore UI changes are
    applied at the next model call without restarting the project process.
    """
    os.environ['AWB_PROJECT_RESOURCE_FILE'] = str(_project_path(project_root))


def model_options_from_resource_file() -> dict[str, int]:
    raw = os.getenv('AWB_PROJECT_RESOURCE_FILE', '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(Path(raw).read_text(encoding='utf-8'))
    except Exception:
        return {}
    options: dict[str, int] = {}
    try:
        cpu = float(data.get('cpu_cores') or 0)
        if cpu > 0:
            options['num_thread'] = max(1, int(math.floor(cpu)))
    except Exception:
        pass
    try:
        ctx = int(data.get('context_tokens') or 0)
        if ctx > 0:
            options['num_ctx'] = max(1024, ctx)
    except Exception:
        pass
    return options
