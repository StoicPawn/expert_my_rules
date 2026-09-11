from __future__ import annotations
import os

# The ACEPC has ~8 GB RAM and four logical CPUs. Keeping three different 3-4B
# models in rotation causes expensive unload/reload churn while adding little value
# compared with independent role prompts. Default every role to one resident model;
# manifests remain configurable for future GPU/LM Studio nodes.
DEFAULT_ROLE_MODELS = {
    'director': 'qwen3:4b',
    'worker': 'qwen3:4b',
    'reviewer': 'qwen3:4b',
    'verifier': 'qwen3:4b',
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
    if os.getenv('AWB_LOCAL_MODEL'):
        return os.environ['AWB_LOCAL_MODEL']
    return DEFAULT_ROLE_MODELS[role]


def provider_for_role(role: str) -> dict:
    return {'kind': 'ollama', 'model': model_for_role(role)}


def with_role_providers(agents: list[dict]) -> list[dict]:
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
                'tags': ['local', 'small-device', 'acepc'],
            },
        ],
        'role_routes': role_routes,
        'scheduler': {
            'enabled': True,
            # <= 0 means wait forever. CPU saturation is normal back-pressure on
            # the ACEPC and must never turn into a failed scientific task.
            'queue_timeout_seconds': 0.0,
            'failure_threshold': 5,
            'cooldown_seconds': 15.0,
            'load_penalty': 10,
            'failure_penalty': 25,
            'allow_cooldown_probe': True,
        },
        'git': {'enabled': False},
        # Legacy automatic cloud escalation is permanently off. Paid calls are
        # controlled only by the explicit manual FORCE API switch.
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
        # No wall-clock orchestration deadline in endurance mode.
        'max_minutes_per_run': 0,
        'max_task_attempts': 3,
        'adaptive_replan_after_scientific_attempts': 3,
        'technical_retry_limit': 0,
        'technical_retry_backoff_max_seconds': 600.0,
        'recovery_history_limit': 20,
        'max_tool_calls_per_task': 50,
        'continuous_session_steps': 50,
        'continuous_session_minutes': 0,
        'checkpoint_pause_seconds': 2.0,
        'pause_seconds': 0.0,
    }


def research_manifest(name, goal):
    agents = with_role_providers([
        {
            'id': 'director',
            'role': 'director',
            'instructions': (
                'Select one precise highest-information task. State the concrete objective, evidence required, '
                'and observable completion criterion. Prefer falsification, unresolved blockers and theorem-critical work.'
            ),
        },
        {
            'id': 'researcher',
            'role': 'worker',
            'instructions': (
                'Develop or falsify claims rigorously. Preserve assumptions, proof dependencies, counterexamples and evidence. '
                'When a shared Research Lab is configured, use lab_request.json plus the lab_execute tool for reproducible '
                'Python, symbolic, numerical or context-sharing work instead of inventing results. Keep project-specific '
                'research inside the private workspace/Lab, never in platform source code.'
            ),
            'tools': ['list', 'read', 'write', 'lab_execute'],
        },
        {
            'id': 'referee',
            'role': 'reviewer',
            'instructions': (
                'Act independently and adversarially. Reject gaps, hidden assumptions, unsupported novelty and overclaiming. '
                'Approval means the current task is actually resolved, not merely improved.'
            ),
        },
        {
            'id': 'verifier',
            'role': 'verifier',
            'instructions': (
                'Use available formal, symbolic, numerical and reproducibility checks. Be conservative when certifying '
                'completion gates. Treat Research Lab output as experimental evidence, never as a substitute for proof.'
            ),
            'tools': ['list', 'read', 'write', 'lab_execute'],
        },
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
        'tools': [
            {'id': 'list', 'type': 'list_files', 'description': 'List files in the private research workspace.'},
            {'id': 'read', 'type': 'read_file', 'description': 'Read a private research-workspace text artifact.'},
            {'id': 'write', 'type': 'write_file', 'description': 'Write a private research-workspace text artifact or Research Lab request.', 'writable': True},
            {
                'id': 'lab_execute',
                'type': 'shell',
                'description': (
                    'Execute the request stored in lab_request.json against the configured shared Research Lab. '
                    'Supported request actions include run, create_workspace, list_runs, latest_context and publish_context.'
                ),
                'command': 'python -m awb.core.research_lab execute lab_request.json',
                # Zero means no wall-clock wrapper timeout. The lab request itself
                # can still carry an explicit bounded timeout when scientifically useful.
                'timeout_seconds': 0,
            },
        ],
        'runtime': _runtime(),
    }


def software_manifest(name, goal):
    agents = with_role_providers([
        {'id': 'director', 'role': 'director', 'instructions': 'Prioritize one clear user-value increment or blocker with explicit release evidence and completion criteria.'},
        {
            'id': 'developer',
            'role': 'worker',
            'instructions': (
                'Implement the smallest correct increment. For unfamiliar repositories, map/search first and '
                'read only the relevant ranges. Prefer exact targeted replacement over rewriting whole existing '
                'files. Inspect the resulting diff, run the stack-aware lint/tests, and leave reproducible evidence.'
            ),
            'tools': [
                'repo_map', 'search', 'read_range', 'list', 'read',
                'replace', 'write', 'git_status', 'git_diff', 'lint', 'tests',
            ],
        },
        {'id': 'reviewer', 'role': 'reviewer', 'instructions': 'Reject regressions, unsafe changes, missing requirements and unsupported assumptions. Review the actual Git patch, not only the worker narrative.'},
        {'id': 'tester', 'role': 'verifier', 'instructions': 'Execute stack-aware automated checks and assess completion evidence conservatively.'},
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
        'description': 'Autonomous software delivery workspace with transactional Git worktrees, compact repository intelligence and stack-aware validation.',
        'agents': agents,
        'workflow': workflow,
        'gates': [
            {'id': 'lint_pass', 'description': 'Configured/best-available static checks pass for every detected stack.', 'required': True, 'validator': 'lint'},
            {'id': 'tests_pass', 'description': 'A real test suite is detected and passes for every detected stack; zero-test success is not accepted.', 'required': True, 'validator': 'tests'},
            {'id': 'critical_bugs_zero', 'description': 'No unresolved critical defect remains.', 'required': True, 'manual': False},
            {'id': 'acceptance_complete', 'description': 'The requested product behavior and acceptance criteria are satisfied.', 'required': True, 'manual': False},
            {'id': 'release_ready', 'description': 'Install/build/run instructions and release artifact are complete.', 'required': True, 'manual': False},
        ],
        'validators': {
            'lint': 'python -m awb.core.validation lint',
            'tests': 'python -m awb.core.validation tests',
        },
        'tools': [
            {'id': 'repo_map', 'type': 'repo_map', 'description': 'Return a compact repository file map with top-level Python symbols.'},
            {'id': 'search', 'type': 'search_text', 'description': 'Search literal text across source files and return matching file/line snippets.'},
            {'id': 'read_range', 'type': 'read_file_range', 'description': 'Read a targeted line range with line numbers instead of loading a whole file.'},
            {'id': 'list', 'type': 'list_files', 'description': 'List one execution-worktree directory.'},
            {'id': 'read', 'type': 'read_file', 'description': 'Read a complete execution-worktree file when targeted reading is insufficient.'},
            {'id': 'replace', 'type': 'replace_text', 'description': 'Perform an exact-count targeted text replacement; refuses ambiguous edits.', 'writable': True},
            {'id': 'write', 'type': 'write_file', 'description': 'Create or fully rewrite code/tests in the execution worktree.', 'writable': True},
            {'id': 'git_status', 'type': 'git_status', 'description': 'Inspect the current candidate Git status.'},
            {'id': 'git_diff', 'type': 'git_diff', 'description': 'Inspect the current candidate Git diff.'},
            {'id': 'lint', 'type': 'shell', 'description': 'Run deterministic stack-aware static checks.', 'command': 'python -m awb.core.validation lint'},
            {'id': 'tests', 'type': 'shell', 'description': 'Detect and run real stack-appropriate tests; fail if no test suite exists.', 'command': 'python -m awb.core.validation tests'},
        ],
        'runtime': runtime,
    }


def custom_manifest(name, goal):
    agents = with_role_providers([
        {'id': 'director', 'role': 'director', 'instructions': 'Choose exactly one clear next task with an explicit objective and completion criterion.'},
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
