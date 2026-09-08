from __future__ import annotations

import os
from pathlib import Path
import yaml

from .models import ProjectManifest, Workspace


def _attach_optional_research_lab(data: dict) -> dict:
    """Attach the standalone Lab as an optional HTTP-backed collaboration tool.

    Nothing is imported from Research Lab and no Lab data is stored in the AWB
    workspace. The integration exists only when both endpoint and token are present.
    Existing manifests remain valid and unchanged on disk until explicitly saved.
    """
    if not os.getenv('RESEARCH_LAB_URL') or not os.getenv('RESEARCH_LAB_TOKEN'):
        return data
    if data.get('type') not in {'research', 'custom'}:
        return data

    tools = data.setdefault('tools', [])
    tool_ids = {str(item.get('id')) for item in tools if isinstance(item, dict)}
    if 'lab_request' not in tool_ids:
        tools.append({
            'id': 'lab_request',
            'type': 'write_file',
            'description': (
                'Write .awb_lab_request.json for the shared Research Lab. JSON actions: '
                'run, list_workspaces, create_workspace, list_runs, latest_context, '
                'publish_context, capabilities. Use project_key to join Tutor LLM and '
                'Expert My Rules to the same Lab workspace.'
            ),
            'writable': True,
        })
    if 'research_lab' not in tool_ids:
        tools.append({
            'id': 'research_lab',
            'type': 'shell',
            'description': (
                'Execute the prepared .awb_lab_request.json against the standalone '
                'shared Research Lab. The Lab stores experiments independently from '
                'the Expert My Rules ledger.'
            ),
            'command': 'python -m awb.core.research_lab execute .awb_lab_request.json',
            'timeout_seconds': 330,
        })

    for agent in data.get('agents', []):
        if not isinstance(agent, dict) or agent.get('role') != 'worker':
            continue
        agent_tools = agent.setdefault('tools', [])
        for tool_id in ('lab_request', 'research_lab'):
            if tool_id not in agent_tools:
                agent_tools.append(tool_id)
        guidance = (
            '\n\nShared Research Lab: when the task is marked as synchronized/shared, '
            'use one stable project_key for the whole project. First request '
            'latest_context to consume the newest Tutor LLM theory snapshot, then run '
            'reproducible Python experiments as needed. Publish concise conclusions '
            'back with publish_context so Tutor LLM and future runs can consume them. '
            'Do not assume the Lab owns the paper library or this workspace ledger.'
        )
        instructions = str(agent.get('instructions') or '')
        if 'Shared Research Lab:' not in instructions:
            agent['instructions'] = instructions + guidance
    return data


def load_workspace(root: Path) -> Workspace:
    root = root.resolve()
    manifest_path = root / "project.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing {manifest_path}")
    data = yaml.safe_load(manifest_path.read_text())
    data = _attach_optional_research_lab(data)
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
