from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

# Current GPT-5.6 Sol promotional API price plus the published Terra/Luna rates,
# in USD per million tokens. Unknown models fail closed unless overridden.
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
    # 1.0 means the burst covers normal task work by default. Raise this to keep
    # routine low-priority tasks local while still cloud-routing blockers/retries.
    priority_threshold: float = 1.0
    roles: tuple[str, ...] = ('director', 'worker', 'reviewer', 'verifier')
    started_at: str = ''
    note: str = 'Cloud burst applies at the next model-call boundary; an in-flight local call is never killed.'

    def normalized(self) -> 'CloudBurstControl':
        self.budget_eur = max(0.0, float(self.budget_eur))
        self.priority_threshold = max(0.0, float(self.priority_threshold))
        self.roles = tuple(str(r) for r in self.roles if str(r))
        if self.enabled and not self.started_at:
            self.started_at = datetime.now(timezone.utc).isoformat()
        return self


def control_path(root: Path) -> Path:
    return root / '.awb' / 'cloud_burst.json'


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


def usd_per_eur() -> float:
    """FX for the internal meter; 1.0 is intentionally conservative by default."""
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
    # GPT-5.6 Sol long-context requests above 272K input tokens are billed at
    # 2x input and 1.5x output for the whole request. Applying the multiplier to
    # the gpt-5.6 alias too keeps the meter conservative.
    if model in {'gpt-5.6-sol', 'gpt-5.6'} and it > 272_000:
        input_price *= 2.0
        output_price *= 1.5
    usd = it * input_price / 1_000_000 + ot * output_price / 1_000_000
    return usd, usd / usd_per_eur()


def reserve_cost_eur(model: str, system: str, user: str, max_output_tokens: int) -> float:
    """Conservative pre-flight reserve so a new call cannot knowingly cross the cap."""
    approx_input_tokens = max(1, (len(system) + len(user) + 2) // 3)
    try:
        _, eur = cost_from_usage(model, approx_input_tokens, max_output_tokens)
        return eur
    except ValueError:
        return float('inf')


def role_cloud_config(role: str) -> dict:
    base = dict(ROLE_DEFAULTS.get(role, ROLE_DEFAULTS['worker']))
    prefix = f'AWB_OPENAI_{role.upper()}'
    base['model'] = os.getenv(f'{prefix}_MODEL', base['model'])
    base['reasoning'] = os.getenv(f'{prefix}_REASONING', base['reasoning'])
    try:
        base['max_output_tokens'] = max(1, int(os.getenv(f'{prefix}_MAX_OUTPUT_TOKENS', str(base['max_output_tokens']))))
    except ValueError:
        pass
    return base


def cloud_spend(root: Path, started_at: str = '') -> dict:
    """Aggregate durable metered API calls from the project ledger."""
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
    finally:
        conn.close()
    totals['cost_usd'] = round(totals['cost_usd'], 6)
    totals['cost_eur'] = round(totals['cost_eur'], 6)
    return totals


def budget_snapshot(root: Path) -> dict:
    control = load_control(root)
    spend = cloud_spend(root, control.started_at)
    remaining = max(0.0, control.budget_eur - float(spend['cost_eur']))
    return {
        'enabled': control.enabled,
        'budget_eur': control.budget_eur,
        'spent_eur': spend['cost_eur'],
        'remaining_eur': round(remaining, 6),
        'calls': spend['calls'],
        'input_tokens': spend['input_tokens'],
        'output_tokens': spend['output_tokens'],
        'started_at': control.started_at,
        'priority_threshold': control.priority_threshold,
        'roles': list(control.roles),
        'api_key_configured': bool(os.getenv('OPENAI_API_KEY') or os.getenv('AWB_OPENAI_KEY_PRESENT')),
        'role_models': {role: role_cloud_config(role) for role in control.roles},
        'note': control.note,
    }
