from __future__ import annotations


def _runtime() -> dict:
    return {
        "default_provider": {"kind": "mock", "model": None},
        "max_steps_per_run": 25,
        "max_minutes_per_run": 60,
        "max_task_attempts": 3,
        "max_tool_calls_per_task": 12,
        "pause_seconds": 0.0,
    }


def _safe_file_tools() -> list[dict]:
    return [
        {"id": "files", "type": "list_files", "description": "List files inside the workspace."},
        {"id": "read", "type": "read_file", "description": "Read a UTF-8 text file inside the workspace."},
        {"id": "write", "type": "write_file", "description": "Create or replace a UTF-8 text file inside the workspace.", "writable": True},
    ]


def research_manifest(name: str, goal: str) -> dict:
    return {
        "name": name,
        "type": "research",
        "goal": goal,
        "description": "Autonomous research workspace with adversarial review and explicit evidence gates.",
        "agents": [
            {"id": "director", "role": "director", "instructions": "Select the highest-information next task. Prefer falsification and unresolved blockers."},
            {"id": "researcher", "role": "worker", "instructions": "Develop or falsify claims rigorously. State assumptions and preserve evidence.", "tools": ["files", "read", "write"]},
            {"id": "referee", "role": "reviewer", "instructions": "Search aggressively for fatal gaps, counterexamples, missing assumptions and unsupported novelty."},
            {"id": "verifier", "role": "verifier", "instructions": "Run formal, symbolic, numerical or reproducibility checks where available."},
        ],
        "gates": [
            {"id": "claims_closed", "description": "Every central claim is proved, refuted or explicitly scoped.", "required": True, "manual": True},
            {"id": "critical_objections_zero", "description": "No unresolved critical referee objection.", "required": True, "manual": True},
            {"id": "priority_checked", "description": "Priority/novelty search completed and documented.", "required": True, "manual": True},
            {"id": "artifact_builds", "description": "Final research artifact builds reproducibly.", "required": True, "manual": True},
        ],
        "validators": {},
        "tools": _safe_file_tools(),
        "runtime": _runtime(),
    }


def software_manifest(name: str, goal: str) -> dict:
    return {
        "name": name,
        "type": "software",
        "goal": goal,
        "description": "Autonomous software workspace optimized for correctness, review and acceptance gates.",
        "agents": [
            {"id": "director", "role": "director", "instructions": "Prioritize blockers, correctness and user acceptance."},
            {"id": "developer", "role": "worker", "instructions": "Inspect and modify files when needed. Implement the smallest correct change and validate it.", "tools": ["files", "read", "write", "tests"]},
            {"id": "reviewer", "role": "reviewer", "instructions": "Reject regressions, unsafe code and unsupported assumptions."},
            {"id": "tester", "role": "verifier", "instructions": "Execute automated tests and acceptance checks."},
        ],
        "gates": [
            {"id": "tests_pass", "description": "Automated test suite passes.", "required": True, "validator": "tests"},
            {"id": "critical_bugs_zero", "description": "No open critical bug.", "required": True, "manual": True},
            {"id": "acceptance_complete", "description": "User acceptance criteria are met.", "required": True, "manual": True},
        ],
        "validators": {"tests": "python -m unittest discover -s tests -v"},
        "tools": _safe_file_tools() + [
            {"id": "tests", "type": "shell", "description": "Run the configured Python unit tests.", "command": "python -m unittest discover -s tests -v", "timeout_seconds": 300}
        ],
        "runtime": _runtime(),
    }


def custom_manifest(name: str, goal: str) -> dict:
    return {
        "name": name,
        "type": "custom",
        "goal": goal,
        "description": "Custom expert workflow.",
        "agents": [
            {"id": "director", "role": "director", "instructions": "Choose the next task with the highest expected value for the goal."},
            {"id": "expert", "role": "worker", "instructions": "Execute the assigned task and distinguish facts, assumptions and uncertainty.", "tools": ["files", "read", "write"]},
            {"id": "critic", "role": "reviewer", "instructions": "Challenge the output and reject unsupported claims."},
            {"id": "verifier", "role": "verifier", "instructions": "Check evidence and external validators."},
        ],
        "gates": [
            {"id": "goal_verified", "description": "The final goal has been independently verified.", "required": True, "manual": True},
        ],
        "validators": {},
        "tools": _safe_file_tools(),
        "runtime": _runtime(),
    }


def get_template(kind: str, name: str, goal: str) -> dict:
    if kind == "research":
        return research_manifest(name, goal)
    if kind == "software":
        return software_manifest(name, goal)
    if kind == "custom":
        return custom_manifest(name, goal)
    raise ValueError(f"Unknown template: {kind}")
