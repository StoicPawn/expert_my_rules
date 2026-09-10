from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

# Public list prices in USD per million tokens. Keep the table deliberately small;
# unknown models fail closed for budget reservation unless an explicit override is set.
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
    priority_threshold: float = 7.0
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
            priority_threshold=float(raw.get('priority_threshold', 7.0)),
            roles=roles or CloudBurstControl().roles,
            started_at=str(raw.get('started_at') or ''),
            note=str(raw.get('note') or CloudBurstControl().note),
        ).normalized()
    except Exception:
        # A malformed control file must never accidentally enable paid traffic.
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
    """Runtime FX conversion used only for the internal conservative meter.

    Default 1.0 intentionally treats EUR 1 as USD 1, making the internal cap more
    conservative while avoiding an external FX dependency. Users may override it.
    """
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
    usd = max(0, int(input_tokens)) * input_price / 1_000_000 + max(0, int(output_tokens)) * output_price / 1_000_000
    eur = usd / usd_per_eur()
    return usd, eur


def reserve_cost_eur(model: str, system: str, user: str, max_output_tokens: int) -> float:
    """Conservative pre-flight cost reserve so one call cannot blow through the cap.

    Input token count is estimated from characters at 3 chars/token and all output
    tokens are reserved. Actual usage replaces this reserve after the response.
    """
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
