from __future__ import annotations
import os

DEFAULT_ROLE_MODELS = {
    'director': 'qwen3:4b',
    'worker': 'qwen3:4b',
    'reviewer': 'llama3.2:3b',
    'verifier': 'gemma3:4b',
}

ROLE_MODEL_ENV = {
    'director': 'AWB_DIRECTOR_MODEL',
    'worker': 'AWB_WORKER_MODEL',
    'reviewer': 'AWB_REVIEWER_MODEL',
    'verifier': 'AWB_VERIFIER_MODEL',
}


def model_for_role(role: str) -> str:
    if role not in DEFAULT_ROLE_MODELS:
        return os.getenv('AWB_LOCAL_MODEL', 'qwen3:4b')
    env_name = ROLE_MODEL_ENV[role]
    if os.getenv(env_name):
        return os.environ[env_name]
    if role in {'director', 'worker'} and os.getenv('AWB_LOCAL_MODEL'):
        return os.environ['AWB_LOCAL_MODEL']
    return DEFAULT_ROLE_MODELS[role]


def provider_for_role(role: str) -> dict:
    return {'kind': 'ollama', 'model': model_for_role(role)}


def with_role_providers(agents: list[dict]) -> list[dict]:
    """Attach the configured local model to every standard epistemic role."""
    configured = []
    for agent in agents:
        item = dict(agent)
        if item.get('role') in DEFAULT_ROLE_MODELS:
            item['provider'] = provider_for_role(item['role'])
        configured.append(item)
    return configured


def _workflow(validate: bool = True):
    stages = [
        {'id': 'select', 'kind': 'select_task', 'role': 'director'},
        {'id': 'execute', 'kind': 'execute', 'role': 'worker', 'depends_on': ['select']},
        {'id': 'challenge', 'kind': 'review', 'role': 'reviewer', 'depends_on': ['execute']},
    ]
    if validate:
        stages.append({'id': 'validate', 'kind': 'validate', 'depends_on': ['challenge']})
    return {'review_policy': 'all', 'stages': stages}


def _runtime():
    # The local Acer is merely the first compute node. A future GPU machine can be
    # inserted ahead of it in role_routes without changing project semantics.
    role_routes = {
        role: [{'node': 'local-ollama', 'model': model_for_role(role), 'priority': 100}]
        for role in DEFAULT_ROLE_MODELS
    }
    return {
        'default_provider': provider_for_role('worker'),
        'compute_nodes': [
            {
                'id': 'local-ollama',
                'kind': 'ollama',
                'base_url_env': 'OLLAMA_BASE_URL',
                'max_concurrency': 1,
                'priority': 100,
                'tags': ['local', 'small-device'],
            },
        ],
        'role_routes': role_routes,
        'git': {'enabled': False},
        'escalation': {
            'enabled': False,
            'cloud_provider': {'kind': 'openai', 'model': 'gpt-5'},
            'daily_budget_eur': 0.0,
            'max_cloud_calls_per_run': 0,
            'after_local_failures': 3,
            'priority_threshold': 9.0,
            'roles': ['worker', 'reviewer'],
        },
        'max_steps_per_run': 25,
        'max_minutes_per_run': 60,
        'max_task_attempts': 3,
        'max_tool_calls_per_task': 12,
        'continuous_session_steps': 50,
        'continuous_session_minutes': 30,
        'checkpoint_pause_seconds': 2.0,
        'pause_seconds': 0.0,
    }


def research_manifest(name, goal):
    agents = with_role_providers([
        {'id': 'director', 'role': 'director', 'instructions': 'Select the highest-information task. Prefer falsification, unresolved blockers and theorem-critical work.'},
        {'id': 'researcher', 'role': 'worker', 'instructions': 'Develop or falsify claims rigorously. Preserve assumptions, proof dependencies, counterexamples and evidence.'},
        {'id': 'referee', 'role': 'reviewer', 'instructions': 'Act independently and adversarially. Reject gaps, hidden assumptions, unsupported novelty and overclaiming.'},
        {'id': 'verifier', 'role': 'verifier', 'instructions': 'Use available formal, symbolic, numerical and reproducibility checks. Be conservative when certifying completion gates.'},
    ])
    return {
        'name': name,
        'type': 'research',
        'goal': goal,
        'description': 'Autonomous mathematical/scientific research workspace.',
        'agents': agents,
        'workflow': _workflow(validate=False),
        'gates': [
            {'id': 'central_result_closed', 'description': 'The central theorem/result is proved or the strongest valid replacement is explicitly established.', 'required': True, 'manual': False},
            {'id': 'critical_objections_zero', 'description': 'No unresolved fatal objection remains after independent adversarial review.', 'required': True, 'manual': False},
            {'id': 'novelty_checked', 'description': 'Priority and novelty search is complete and documented against the relevant literature.', 'required': True, 'manual': False},
            {'id': 'claims_verified', 'description': 'All major claims have evidence at the strongest available verification level.', 'required': True, 'manual': False},
            {'id': 'paper_ready', 'description': 'A complete, internally consistent, reproducible manuscript package is ready for expert submission review.', 'required': True, 'manual': False},
        ],
        'validators': {},
        'tools': [],
        'runtime': _runtime(),
    }


def software_manifest(name, goal):
    agents = with_role_providers([
        {'id': 'director', 'role': 'director', 'instructions': 'Prioritize user value, blockers, correctness and release criteria.'},
        {'id': 'developer', 'role': 'worker', 'instructions': 'Implement the smallest correct increment. Inspect diffs, run lint/tests, and leave reproducible evidence.', 'tools': ['list', 'read', 'write', 'git_status', 'git_diff', 'lint', 'tests']},
        {'id': 'reviewer', 'role': 'reviewer', 'instructions': 'Reject regressions, unsafe changes, missing requirements and unsupported assumptions. Review the actual Git patch, not only the worker narrative.'},
        {'id': 'tester', 'role': 'verifier', 'instructions': 'Execute automated checks and assess completion evidence conservatively.'},
    ])
    runtime = _runtime()
    runtime['git'] = {
        'enabled': True,
        'auto_init': True,
        'checkpoint_dirty': True,
        'merge_approved': True,
        'discard_rejected': True,
    }
    workflow = _workflow(validate=True)
    workflow['stages'][-1]['validators'] = ['lint', 'tests']
    return {
        'name': name,
        'type': 'software',
        'goal': goal,
        'description': 'Autonomous software delivery workspace with transactional Git worktrees.',
        'agents': agents,
        'workflow': workflow,
        'gates': [
            {'id': 'lint_pass', 'description': 'Static lint checks pass.', 'required': True, 'validator': 'lint'},
            {'id': 'tests_pass', 'description': 'Automated test suite passes.', 'required': True, 'validator': 'tests'},
            {'id': 'critical_bugs_zero', 'description': 'No unresolved critical defect remains.', 'required': True, 'manual': False},
            {'id': 'acceptance_complete', 'description': 'The requested product behavior and acceptance criteria are satisfied.', 'required': True, 'manual': False},
            {'id': 'release_ready', 'description': 'Install/build/run instructions and release artifact are complete.', 'required': True, 'manual': False},
        ],
        'validators': {
            'lint': 'python -m ruff check .',
            'tests': 'python -m unittest discover -s tests -v',
        },
        'tools': [
            {'id': 'list', 'type': 'list_files', 'description': 'List execution-worktree files.'},
            {'id': 'read', 'type': 'read_file', 'description': 'Read a file from the execution worktree.'},
            {'id': 'write', 'type': 'write_file', 'description': 'Write code/tests in the execution worktree.', 'writable': True},
            {'id': 'git_status', 'type': 'git_status', 'description': 'Inspect the current candidate Git status.'},
            {'id': 'git_diff', 'type': 'git_diff', 'description': 'Inspect the current candidate Git diff.'},
            {'id': 'lint', 'type': 'shell', 'description': 'Run Ruff static lint checks.', 'command': 'python -m ruff check .'},
            {'id': 'tests', 'type': 'shell', 'description': 'Run the configured unit tests.', 'command': 'python -m unittest discover -s tests -v'},
        ],
        'runtime': runtime,
    }


def custom_manifest(name, goal):
    agents = with_role_providers([
        {'id': 'director', 'role': 'director', 'instructions': 'Choose the next task with the highest expected value for the goal.'},
        {'id': 'expert', 'role': 'worker', 'instructions': 'Execute the task and distinguish facts, assumptions, uncertainty and evidence.'},
        {'id': 'critic', 'role': 'reviewer', 'instructions': 'Challenge the result independently and reject unsupported claims.'},
        {'id': 'verifier', 'role': 'verifier', 'instructions': 'Check evidence and available external validators. Be conservative when certifying completion gates.'},
    ])
    return {
        'name': name,
        'type': 'custom',
        'goal': goal,
        'description': 'Custom autonomous expert workflow.',
        'agents': agents,
        'workflow': _workflow(validate=False),
        'gates': [{'id': 'goal_verified', 'description': 'The final objective has been independently verified.', 'required': True, 'manual': False}],
        'validators': {},
        'tools': [],
        'runtime': _runtime(),
    }


def get_template(kind, name, goal):
    return {'research': research_manifest, 'software': software_manifest, 'custom': custom_manifest}[kind](name, goal)
