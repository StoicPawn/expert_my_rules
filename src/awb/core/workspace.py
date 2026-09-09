from __future__ import annotations

import os
from pathlib import Path
import yaml

from .models import ProjectManifest, Workspace


def _upsert_tool(tools: list[dict], item: dict) -> None:
    for index, existing in enumerate(tools):
        if isinstance(existing, dict) and str(existing.get('id')) == str(item.get('id')):
            # Runtime-owned integration definitions supersede legacy shell adapters.
            tools[index] = item
            return
    tools.append(item)


def _attach_optional_science_services(data: dict) -> dict:
    """Attach thin research adapters without moving ownership between services.

    Expert My Rules remains the orchestrator. Tutor LLM owns indexed documents/RAG;
    Research Lab owns bounded computation; public literature metadata is queried by a
    fixed adapter. Integrations are attached in memory, so existing project manifests
    remain compatible and no running workspace must be rewritten just to gain tools.
    """
    if data.get('type') not in {'research', 'custom'}:
        return data

    tools = data.setdefault('tools', [])
    _upsert_tool(tools, {
        'id': 'literature_search',
        'type': 'literature_search',
        'description': (
            'Search fixed Crossref/arXiv metadata endpoints for candidate related work. '
            'Use results as leads with provenance; absence is never proof of novelty.'
        ),
    })

    lab_enabled = bool(os.getenv('RESEARCH_LAB_URL') and os.getenv('RESEARCH_LAB_TOKEN'))
    if lab_enabled:
        _upsert_tool(tools, {
            'id': 'research_lab',
            'type': 'research_lab',
            'description': (
                'Direct typed access to the standalone shared Research Lab for bounded '
                'reproducible Python experiments, capabilities and shared context.'
            ),
        })
        _upsert_tool(tools, {
            'id': 'symbolic_math',
            'type': 'symbolic_math',
            'description': (
                'Run a fixed SymPy operation inside the resource-bounded Research Lab. '
                'Useful for algebraic checks, derivatives, integrals, limits and series.'
            ),
        })
        _upsert_tool(tools, {
            'id': 'counterexample_search',
            'type': 'counterexample_search',
            'description': (
                'Run a bounded numerical falsification search in the Research Lab. '
                'A found witness is evidence; no witness is not a proof.'
            ),
        })

    tutor_enabled = bool(os.getenv('TUTOR_LLM_URL') and os.getenv('TUTOR_LLM_TOKEN'))
    if tutor_enabled:
        _upsert_tool(tools, {
            'id': 'tutor_knowledge',
            'type': 'tutor_knowledge',
            'description': (
                'Read Tutor LLM indexed workspaces, documents, knowledge graph and '
                'grounded retrieval chunks with document/page provenance, without '
                'asking another tutor model to generate an answer.'
            ),
        })

    granted = ['literature_search']
    if lab_enabled:
        granted += ['research_lab', 'symbolic_math', 'counterexample_search']
    if tutor_enabled:
        granted += ['tutor_knowledge']

    guidance = (
        '\n\nScientific tool policy: use grounded evidence before unsupported prose. '
        'When Tutor knowledge is available, retrieve relevant indexed chunks and keep '
        'document/page provenance. Use literature_search for candidate prior art, but '
        'never infer exhaustive novelty from a negative search. Use symbolic_math and '
        'counterexample_search to attack mathematical claims before treating them as '
        'proved. Use Research Lab for reproducible bounded computations. A numerical '
        'or symbolic check supports a proof but does not replace a proof when the goal '
        'requires one. Formal proof assistants remain capability-gated: never claim a '
        'Lean/formal verification unless such a validator is actually installed and run.'
    )

    for agent in data.get('agents', []):
        if not isinstance(agent, dict) or agent.get('role') not in {'worker', 'verifier'}:
            continue
        agent_tools = agent.setdefault('tools', [])
        # Remove the old two-step generated request helper from automatically granted
        # capabilities. Persisted manifests may still contain its definition harmlessly.
        agent_tools[:] = [x for x in agent_tools if x not in {'lab_request', 'lab_execute'}]
        for tool_id in granted:
            if tool_id not in agent_tools:
                agent_tools.append(tool_id)
        instructions = str(agent.get('instructions') or '')
        if 'Scientific tool policy:' not in instructions:
            agent['instructions'] = instructions + guidance
    return data


def load_workspace(root: Path) -> Workspace:
    root = root.resolve()
    manifest_path = root / "project.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing {manifest_path}")
    data = yaml.safe_load(manifest_path.read_text())
    data = _attach_optional_science_services(data)
    return Workspace(root=root, manifest=ProjectManifest.model_validate(data))


def write_workspace(root: Path, manifest: dict) -> None:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def save_manifest(workspace: Workspace) -> None:
    (workspace.root / "project.yaml").write_text(
        yaml.safe_dump(workspace.manifest.model_dump(mode="json"), sort_keys=False)
    )
