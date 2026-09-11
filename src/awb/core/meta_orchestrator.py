from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from .models import Workspace
from .storage import Ledger


META_SYSTEM = """You are the meta-orchestrator of a general-purpose autonomous workbench running on weak local hardware. Do not solve the user's domain problem. Classify it and design the control strategy. Return JSON only with: problem_class, success_criteria (list), risks (list), decomposition_policy, deterministic_capabilities (list of tool/capability ids or descriptions), evidence_policy (list), and notes. Optimize for eventual correctness, verification, reversibility and long-running local execution, never for speed. Kellerer or any other example is only a benchmark unless the goal itself explicitly asks for it."""


class MetaOrchestrator:
    def __init__(self, workspace: Workspace, ledger: Ledger, call_model: Callable[[str, str, str, Any], str]):
        self.workspace = workspace
        self.ledger = ledger
        self.call_model = call_model
        self.path = workspace.root / '.awb' / 'meta-plan.json'

    def _fallback(self) -> dict[str, Any]:
        kind = str(self.workspace.manifest.type or 'custom')
        tools = [t.id for t in self.workspace.manifest.tools if t.enabled]
        gates = [g.description for g in self.workspace.manifest.gates if g.required]
        return {
            'version': 1,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'source': 'deterministic-fallback',
            'problem_class': kind,
            'success_criteria': gates or ['The North Star is supported by inspectable evidence.'],
            'risks': [
                'A weak local model may produce plausible but unsupported intermediate claims.',
                'Long execution may be interrupted; all useful state must be externalized.',
                'A repeated strategy can create a blind loop unless strategy history is checked.',
            ],
            'decomposition_policy': 'Use the smallest independently verifiable micro-task that reduces uncertainty or closes a dependency. Prefer falsification/blockers before cosmetic work.',
            'deterministic_capabilities': tools,
            'evidence_policy': [
                'Define a verification contract before executing each micro-task.',
                'Prefer deterministic tools/tests/parsers/calculators over language-model judgement whenever applicable.',
                'Reviewer approval cannot override a failing deterministic validator.',
            ],
            'notes': 'Fallback profile generated without relying on domain-specific assumptions.',
        }

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def ensure(self) -> dict[str, Any]:
        existing = self.load()
        if existing:
            return existing
        fallback = self._fallback()
        prompt = json.dumps({
            'north_star': self.workspace.manifest.goal,
            'project_type_hint': self.workspace.manifest.type,
            'required_completion_gates': fallback['success_criteria'],
            'available_tools': [
                {'id': t.id, 'type': t.type, 'description': t.description}
                for t in self.workspace.manifest.tools if t.enabled
            ],
        }, ensure_ascii=False, indent=2)
        profile = fallback
        try:
            raw = self.call_model('director', META_SYSTEM, prompt, None)
            data = json.loads(raw)
            if isinstance(data, dict):
                profile = {
                    **fallback,
                    **{k: data.get(k, fallback[k]) for k in (
                        'problem_class', 'success_criteria', 'risks', 'decomposition_policy',
                        'deterministic_capabilities', 'evidence_policy', 'notes'
                    )},
                    'source': 'local-meta-orchestrator',
                    'generated_at': datetime.now(timezone.utc).isoformat(),
                }
        except Exception as exc:
            self.ledger.event('meta_orchestrator_fallback', {'error': f'{type(exc).__name__}: {exc}'})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self.path)
        self.ledger.event('meta_plan_persisted', {
            'source': profile.get('source'),
            'problem_class': profile.get('problem_class'),
            'success_criteria': profile.get('success_criteria', [])[:8],
            'deterministic_capabilities': profile.get('deterministic_capabilities', [])[:12],
        })
        return profile
