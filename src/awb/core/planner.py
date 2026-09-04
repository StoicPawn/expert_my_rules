from __future__ import annotations
import json, os, re
from awb.providers.providers import make_provider
from awb.templates.templates import get_template, with_role_providers

PLANNER_SYSTEM = """You design autonomous project workspaces. Given only a final goal, propose a rigorous project type, team and Definition of Done. Return JSON only with: type (research|software|custom), description, agents (list of id, role, instructions), gates (list of id, description, required, manual). The team must contain director, worker, reviewer and verifier roles. Use only those roles. The reviewer must be adversarial and independent; the verifier must certify evidence conservatively. Completion gates must be objective, hard to game, and together sufficient to represent the requested final result. Prefer manual=false so the independent verifier can evaluate completion autonomously; use manual=true only when the goal explicitly requires a human decision. Never use cloud/API assumptions. Model assignment and tool capabilities are handled by the workbench, not by this planner."""


def infer_type(goal: str) -> str:
    g = goal.lower()
    if any(k in g for k in ['paper', 'theorem', 'proof', 'research', 'annals', 'journal', 'conjecture', 'mathemat']):
        return 'research'
    if any(k in g for k in ['software', 'app', 'application', 'website', 'api', 'code', 'program', 'saas']):
        return 'software'
    return 'custom'


def slug_from_goal(goal: str) -> str:
    words = re.findall(r'[a-zA-Z0-9]+', goal.lower())[:6]
    return '_'.join(words) or 'project'


def _reattach_role_capabilities(template_agents: list[dict], planned_agents: list[dict]) -> list[dict]:
    """Planner may change identities/instructions, never security/tool capabilities."""
    capabilities = {a.get('role'): a.get('tools', []) for a in template_agents}
    enriched = []
    for agent in planned_agents:
        item = dict(agent)
        tools = capabilities.get(item.get('role')) or []
        if tools:
            item['tools'] = list(tools)
        enriched.append(item)
    return with_role_providers(enriched)


def propose_manifest(goal: str, name: str | None = None, use_local_ai: bool = True) -> dict:
    goal = goal.strip()
    kind = infer_type(goal)
    name = name or slug_from_goal(goal)
    manifest = get_template(kind, name, goal)
    if not use_local_ai:
        return manifest

    model = os.getenv(
        'AWB_PLANNER_MODEL',
        os.getenv('AWB_WORKER_MODEL', os.getenv('AWB_LOCAL_MODEL', 'qwen3:4b')),
    )
    try:
        raw = make_provider('ollama', model).generate(PLANNER_SYSTEM, goal)
        data = json.loads(raw)
        if data.get('type') in {'research', 'software', 'custom'}:
            kind = data['type']
            manifest = get_template(kind, name, goal)
        if isinstance(data.get('description'), str):
            manifest['description'] = data['description']

        agents = data.get('agents')
        if isinstance(agents, list) and agents:
            valid = [
                a for a in agents
                if isinstance(a, dict)
                and a.get('role') in {'director', 'worker', 'reviewer', 'verifier'}
                and a.get('id')
                and a.get('instructions')
            ]
            roles = {a['role'] for a in valid}
            if {'director', 'worker', 'reviewer', 'verifier'}.issubset(roles):
                # Preserve model diversity and template-granted capabilities. The
                # planner can design the team but cannot grant itself new tools.
                manifest['agents'] = _reattach_role_capabilities(manifest['agents'], valid)

        gates = data.get('gates')
        if isinstance(gates, list) and gates:
            valid = [g for g in gates if isinstance(g, dict) and g.get('id') and g.get('description')]
            if valid:
                manifest['gates'] = [
                    {**g, 'required': bool(g.get('required', True)), 'manual': bool(g.get('manual', False))}
                    for g in valid
                ]
    except Exception:
        pass
    return manifest
