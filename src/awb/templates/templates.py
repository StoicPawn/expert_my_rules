from __future__ import annotations


def research_manifest(name: str, goal: str) -> dict:
    return {
        "name": name,
        "type": "research",
        "goal": goal,
        "agents": [
            {"id": "director", "role": "director", "instructions": "Select the highest-information next task."},
            {"id": "researcher", "role": "worker", "instructions": "Develop or falsify mathematical/scientific claims."},
            {"id": "referee", "role": "reviewer", "instructions": "Search aggressively for fatal gaps and counterexamples."},
            {"id": "verifier", "role": "verifier", "instructions": "Run formal, symbolic, numerical or reproducibility checks."},
        ],
        "gates": [
            {"id": "claims_closed", "description": "Every central claim is proved, refuted or explicitly scoped.", "required": True},
            {"id": "critical_objections_zero", "description": "No unresolved critical referee objection.", "required": True},
            {"id": "priority_checked", "description": "Priority/novelty search completed and documented.", "required": True},
            {"id": "artifact_builds", "description": "Final research artifact builds reproducibly.", "required": True},
        ],
        "validators": {},
        "max_consecutive_failures": 3,
    }


def software_manifest(name: str, goal: str) -> dict:
    return {
        "name": name,
        "type": "software",
        "goal": goal,
        "agents": [
            {"id": "director", "role": "director", "instructions": "Prioritize blockers, correctness and user acceptance."},
            {"id": "developer", "role": "worker", "instructions": "Implement the smallest correct change for the task."},
            {"id": "reviewer", "role": "reviewer", "instructions": "Reject regressions, unsafe code and unsupported assumptions."},
            {"id": "tester", "role": "verifier", "instructions": "Execute automated tests and acceptance checks."},
        ],
        "gates": [
            {"id": "tests_pass", "description": "Automated test suite passes.", "required": True, "validator": "tests"},
            {"id": "critical_bugs_zero", "description": "No open critical bug.", "required": True},
            {"id": "acceptance_complete", "description": "User acceptance criteria are met.", "required": True},
        ],
        "validators": {"tests": "python -m unittest discover -s tests -v"},
        "max_consecutive_failures": 3,
    }


def get_template(kind: str, name: str, goal: str) -> dict:
    if kind == "research":
        return research_manifest(name, goal)
    if kind == "software":
        return software_manifest(name, goal)
    raise ValueError(f"Unknown template: {kind}")
