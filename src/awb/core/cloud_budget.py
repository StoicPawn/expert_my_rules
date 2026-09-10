from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

PRICE_USD_PER_MTOK = {
    'gpt-5.6-sol': (4.0, 20.0),
    'gpt-5.6': (4.0, 20.0),
    'gpt-5.6-terra': (2.0, 12.0),
    'gpt-5.6-luna': (0.20, 1.20),
}

ROLE_DEFAULTS = {
    'director': {'model': 'gpt-5.6-terra', 'reasoning': 'low', 'max_output_tokens': 1200},
    'worker': {'model': 'gpt-5.6-sol', 'reasoning': 'high', 'max_output_tokens': 8192},
    'reviewer': {'model': 'gpt-5.6-sol', 'reasoning': 'high', 'max_output_tokens': 4096},
    'verifier': {'model': 'gpt-5.6-terra', 'reasoning': 'medium', 'max_output_tokens': 1800},
}


@dataclass
class CloudBurstControl:
    enabled: bool = False
    budget_eur: float = 5.0
    priority_threshold: float = 1.0
    roles: tuple[str, ...] = ('director', 'worker', 'reviewer', 'verifier')
    role_models: dict[str, dict] = field(default_factory=dict)
    started_at: str = ''
    note: str = 'Cloud calls are metered and fall back to local inference when a project or monthly ceiling is exhausted.'

    def normalized(self) -> 'CloudBurstControl':
        self.budget_eur = max(0.0, float(self.budget_eur))
        self.priority_threshold = max(0.0, float(self.priority_threshold))
        self.roles = tuple(str(r) for r in self.roles if str(r))
        self.role_models = {
            str(role): dict(cfg)
            for role, cfg in (self.role_models or {}).items()
            if isinstance(cfg, dict)
        }
        if self.enabled and not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        return self


@dataclass
class GlobalCloudControl:
    enabled: bool = True
    monthly_budget_eur: float = 5.0
    hard_stop: bool = True
    note: str = 'Hard monthly ceiling across all Expert My Rules projects. When exhausted, paid APIs are blocked and local routes remain available.'

    def normalized(self) -> 'GlobalCloudControl':
        self.monthly_budget_eur = max(0.0, float(self.monthly_budget_eur))
        return self


def control_path(root: Path) -> Path:
    return root / '.awb' / 'cloud_burst.json'


def _workspaces_root(root: Path) -> Path:
    configured = os.getenv('AWB_WORKSPACES_DIR', '').strip()
    if configured:
        return Path(configured).resolve()
    if root.parent.name == 'workspaces':
        return root.parent
    return root


def global_control_path(root: Path) -> Path:
    return _workspaces_root(root) / '.awb' / 'global_cloud.json'


def load_control(root: Path) -> CloudBurstControl:
    path = control_path(root)
    if not path.exists():
        return CloudBurstControl()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        roles = tuple(raw.get('roles') or ())
        return CloudBurstControl(
            enabled=bool(raw.get('enabled', False)),
            budget_eur=float(raw.get('budget_eur', 5.0)),
            priority_threshold=float(raw.get('priority_threshold', 1.0)),
            roles=roles or CloudBurstControl().roles,
            role_models=dict(raw.get('role_models') or {}),
            started_at=str(raw.get('started_at') or ''),
            note=str(raw.get('note') or CloudBurstControl().note),
        ).normalized()
    except Exception:
        return CloudBurstControl(enabled=False)


def save_control(root: Path, control: CloudBurstControl, *, reset_meter: bool = False) -> CloudBurstControl:
    path = control_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    control = control.normalized()
    if reset_meter or (control.enabled and not control.started_at):
        control.started_at = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(asdict(control), indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    return control


def load_global_control(root: Path) -> GlobalCloudControl:
    path = global_control_path(root)
    if not path.exists():
        try:
            default = float(os.getenv('AWB_MONTHLY_API_BUDGET_EUR', '5.0'))
        except ValueError:
            default = 5.0
        return GlobalCloudControl(monthly_budget_eur=default)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return GlobalCloudControl(
            enabled=bool(raw.get('enabled', True)),
            monthly_budget_eur=float(raw.get('monthly_budget_eur', 5.0)),
            hard_stop=bool(raw.get('hard_stop', True)),
            note=str(raw.get('note') or GlobalCloudControl().note),
        ).normalized()
    except Exception:
        return GlobalCloudControl(enabled=True, monthly_budget_eur=0.0, hard_stop=True)


def save_global_control(root: Path, control: GlobalCloudControl) -> GlobalCloudControl:
    control = control.normalized()
    path = global_control_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(asdict(control), indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    return control


def usd_per_eur() -> float:
    try:
        return max(0.01, float(os.getenv('AWB_USD_PER_EUR', '1.0')))
    except (TypeError, ValueError):
        return 1.0


def price_for(model: str) -> tuple[float, float] | None:
    override = os.getenv(f"AWB_PRICE_{model.upper().replace('-', '_').replace('.', '_')}_USD_PER_MTOK")
    if override:
        try:
            a, b = override.split(',', 1)
            return float(a), float(b)
        except Exception:
            pass
    return PRICE_USD_PER_MTOK.get(model)


def cost_from_usage(model: str, input_tokens: int, output_tokens: int) -> tuple[float, float]:
    price = price_for(model)
    if not price:
        raise ValueError(f'No budget price configured for {model}')
    input_price, output_price = price
    it = max(0, int(input_tokens))
    ot = max(0, int(output_tokens))
    if model.startswith('gpt-5.6') and it > 272_000:
        input_price *= 2.0
        output_price *= 1.5
    usd = it * input_price / 1_000_000 + ot * output_price / 1_000_000
    return usd, usd / usd_per_eur()


def reserve_cost_eur(model: str, system: str, user: str, max_output_tokens: int) -> float:
    approx_input_tokens = max(1, len((system + '\n' + user).encode('utf-8')) + 64)
    try:
        _, eur = cost_from_usage(model, approx_input_tokens, max_output_tokens)
        return eur
    except ValueError:
        return float('inf')


def role_cloud_config(role: str, root: Path | None = None) -> dict:
    base = dict(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS['worker']))
    prefix = f'AWB_OPENAI_{role.upper()}'
    base['model'] = os.getenv(f'{prefix}_MODEL', base['model'])
    base['reasoning'] = os.getenv(f'{prefix}_REASONING', base['reasoning'])
    try:
        base['max_output_tokens'] = max(1, int(os.getenv(f'{prefix}_MAX_OUTPUT_TOKENS', str(base['max_output_tokens']))))
    except ValueError:
        pass
    if root is not None:
        override = load_control(root).role_models.get(role) or {}
        if override.get('model'):
            base['model'] = str(override['model'])
        if override.get('reasoning'):
            base['reasoning'] = str(override['reasoning'])
        if override.get('max_output_tokens'):
            try:
                base['max_output_tokens'] = max(1, int(override['max_output_tokens']))
            except (TypeError, ValueError):
                pass
    return base


def cloud_spend(root: Path, started_at: str = '') -> dict:
    db = root / 'ledger.sqlite3'
    totals = {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'cost_eur': 0.0}
    if not db.exists():
        return totals
    conn = sqlite3.connect(str(db), timeout=5)
    try:
        sql = "SELECT payload_json FROM events WHERE kind='cloud_usage_metered'"
        args: tuple = ()
        if started_at:
            sql += ' AND ts>=?'
            args = (started_at,)
        for (payload_json,) in conn.execute(sql, args).fetchall():
            try:
                p = json.loads(payload_json or '{}')
            except Exception:
                continue
            totals['calls'] += 1
            totals['input_tokens'] += int(p.get('input_tokens') or 0)
            totals['output_tokens'] += int(p.get('output_tokens') or 0)
            totals['cost_usd'] += float(p.get('cost_usd') or 0.0)
            totals['cost_eur'] += float(p.get('cost_eur') or 0.0)
    except sqlite3.DatabaseError:
        pass
    finally:
        conn.close()
    totals['cost_usd'] = round(totals['cost_usd'], 6)
    totals['cost_eur'] = round(totals['cost_eur'], 6)
    return totals


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat()


def global_month_spend(root: Path) -> dict:
    total = {'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'cost_usd': 0.0, 'cost_eur': 0.0}
    workspace_root = _workspaces_root(root)
    since = _month_start()
    candidates = [root]
    if workspace_root != root:
        try:
            candidates = list(workspace_root.iterdir())
        except OSError:
            candidates = [root]
    for candidate in candidates:
        try:
            if not candidate.is_dir() or not (candidate / 'ledger.sqlite3').is_file():
                continue
        except OSError:
            continue
        snap = cloud_spend(candidate, since)
        for key in ('calls', 'input_tokens', 'output_tokens'):
            total[key] += int(snap[key])
        for key in ('cost_usd', 'cost_eur'):
            total[key] += float(snap[key])
    total['cost_usd'] = round(total['cost_usd'], 6)
    total['cost_eur'] = round(total['cost_eur'], 6)
    return total


def budget_snapshot(root: Path) -> dict:
    control = load_control(root)
    spend = cloud_spend(root, control.started_at)
    project_remaining = max(0.0, control.budget_eur - float(spend['cost_eur']))
    global_control = load_global_control(root)
    month = global_month_spend(root)
    if global_control.enabled:
        monthly_remaining = max(0.0, global_control.monthly_budget_eur - float(month['cost_eur']))
    else:
        monthly_remaining = float('inf')
    effective_remaining = min(project_remaining, monthly_remaining)
    hard_blocked = bool(global_control.enabled and global_control.hard_stop and monthly_remaining <= 0)
    return {
        'enabled': control.enabled and not hard_blocked,
        'requested_enabled': control.enabled,
        'budget_eur': control.budget_eur,
        'spent_eur': spend['cost_eur'],
        'project_remaining_eur': round(project_remaining, 6),
        'remaining_eur': round(max(0.0, effective_remaining), 6),
        'calls': spend['calls'],
        'input_tokens': spend['input_tokens'],
        'output_tokens': spend['output_tokens'],
        'started_at': control.started_at,
        'priority_threshold': control.priority_threshold,
        'roles': list(control.roles),
        'api_key_configured': bool(os.getenv('OPENAI_API_KEY') or os.getenv('AWB_OPENAI_KEY_PRESENT')),
        'role_models': {role: role_cloud_config(role, root) for role in control.roles},
        'monthly_budget_eur': global_control.monthly_budget_eur,
        'monthly_spent_eur': month['cost_eur'],
        'monthly_remaining_eur': None if monthly_remaining == float('inf') else round(monthly_remaining, 6),
        'monthly_calls': month['calls'],
        'hard_stop': global_control.hard_stop,
        'hard_blocked': hard_blocked,
        'global_enabled': global_control.enabled,
        'month_start': _month_start(),
        'note': control.note,
    }
