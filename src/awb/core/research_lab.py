from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ResearchLabError(RuntimeError):
    pass


class ResearchLabClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 320.0):
        self.base_url = (base_url or os.getenv('RESEARCH_LAB_URL', '')).rstrip('/')
        self.token = token or os.getenv('RESEARCH_LAB_TOKEN', '')
        self.timeout = timeout
        if not self.base_url:
            raise ResearchLabError('RESEARCH_LAB_URL is not configured')
        if not self.token:
            raise ResearchLabError('RESEARCH_LAB_TOKEN is not configured')

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', errors='replace')
            raise ResearchLabError(f'Research Lab HTTP {exc.code}: {body}') from exc
        except OSError as exc:
            raise ResearchLabError(f'Research Lab unavailable: {exc}') from exc

    def workspaces(self) -> list[dict[str, Any]]:
        return self.request('GET', '/api/workspaces')

    def ensure_workspace(self, name: str, description: str = '') -> dict[str, Any]:
        for workspace in self.workspaces():
            if workspace.get('name') == name and workspace.get('source_project') == 'expert_my_rules':
                return workspace
        return self.request('POST', '/api/workspaces', {
            'name': name,
            'description': description,
            'source_project': 'expert_my_rules',
        })

    def run(self, workspace_id: str, title: str, code: str, timeout_seconds: int | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'title': title,
            'code': code,
            'metadata': metadata or {'caller': 'expert_my_rules'},
        }
        if timeout_seconds is not None:
            payload['timeout_seconds'] = timeout_seconds
        return self.request('POST', f'/api/workspaces/{workspace_id}/runs', payload)

    def runs(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.request('GET', f'/api/workspaces/{workspace_id}/runs')


def execute_request(request_data: dict[str, Any]) -> Any:
    client = ResearchLabClient()
    action = str(request_data.get('action', 'run'))

    if action == 'list_workspaces':
        return client.workspaces()

    if action == 'capabilities':
        return client.request('GET', '/api/capabilities')

    workspace_id = request_data.get('workspace_id')
    if not workspace_id:
        workspace_name = str(request_data.get('workspace_name') or os.getenv('AWB_LAB_WORKSPACE') or Path.cwd().name)
        workspace = client.ensure_workspace(
            workspace_name,
            str(request_data.get('workspace_description') or 'Experiments created by Expert My Rules'),
        )
        workspace_id = workspace['id']

    if action == 'list_runs':
        return client.runs(str(workspace_id))

    if action == 'create_workspace':
        return {'workspace_id': workspace_id}

    if action != 'run':
        raise ResearchLabError(f'Unsupported action: {action}')

    code = request_data.get('code')
    if not isinstance(code, str) or not code.strip():
        raise ResearchLabError('run action requires non-empty code')
    return client.run(
        str(workspace_id),
        str(request_data.get('title') or 'Expert My Rules experiment'),
        code,
        timeout_seconds=(int(request_data['timeout_seconds']) if request_data.get('timeout_seconds') is not None else None),
        metadata=request_data.get('metadata') or {'caller': 'expert_my_rules'},
    )


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != 'execute':
        raise SystemExit('Usage: python -m awb.core.research_lab execute <request.json>')
    request_path = Path(sys.argv[2])
    payload = json.loads(request_path.read_text(encoding='utf-8'))
    result = execute_request(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
