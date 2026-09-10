from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from awb.web.ui import esc, shell


lab_app = FastAPI(title='Expert My Rules Research Lab')
security = HTTPBearer(auto_error=False)


def _root() -> Path:
    path = Path(os.getenv('AWB_LAB_DATA_DIR', '/data/lab')).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token() -> str:
    return os.getenv('RESEARCH_LAB_TOKEN', 'awb-local-lab')


def _require_api(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> None:
    expected = _token()
    if expected and (credentials is None or credentials.credentials != expected):
        raise HTTPException(401, 'Invalid Research Lab token')


def _workspace_dir(workspace_id: str) -> Path:
    safe = ''.join(c for c in workspace_id if c.isalnum() or c in '-_')
    path = _root() / 'workspaces' / safe
    if not safe or not path.exists():
        raise HTTPException(404, 'Workspace not found')
    return path


def _metadata(path: Path) -> dict:
    try:
        return json.loads((path / 'workspace.json').read_text(encoding='utf-8'))
    except Exception:
        return {'id': path.name, 'name': path.name, 'description': '', 'metadata': {}}


def _workspaces() -> list[dict]:
    base = _root() / 'workspaces'; base.mkdir(parents=True, exist_ok=True)
    return [_metadata(path) for path in sorted(base.iterdir()) if path.is_dir()]


def _runs(path: Path) -> list[dict]:
    runs_dir = path / 'runs'; runs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in runs_dir.glob('*.json'):
        try:
            rows.append(json.loads(item.read_text(encoding='utf-8')))
        except Exception:
            continue
    rows.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    return rows


def _execute(path: Path, title: str, code: str, timeout_seconds: int, metadata: dict | None = None) -> dict:
    run_id = f'RUN-{uuid.uuid4().hex[:10].upper()}'
    run_dir = path / 'run-files' / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    script = run_dir / 'experiment.py'
    script.write_text(code, encoding='utf-8')
    started = _now()
    timeout_seconds = max(1, min(int(timeout_seconds), 900))
    status = 'complete'; returncode = 0; stdout = ''; stderr = ''
    try:
        proc = subprocess.run(
            [sys.executable, '-I', str(script)],
            cwd=run_dir,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={
                'PATH': os.getenv('PATH', ''),
                'PYTHONUNBUFFERED': '1',
                'HOME': '/tmp',
                'LANG': os.getenv('LANG', 'C.UTF-8'),
            },
        )
        returncode = int(proc.returncode)
        stdout = proc.stdout[-100_000:]
        stderr = proc.stderr[-100_000:]
        if returncode != 0:
            status = 'failed'
    except subprocess.TimeoutExpired as exc:
        status = 'timeout'; returncode = 124
        stdout = (exc.stdout or '')[-100_000:] if isinstance(exc.stdout, str) else ''
        stderr = ((exc.stderr or '')[-100_000:] if isinstance(exc.stderr, str) else '') + f'\nTimed out after {timeout_seconds}s.'
    record = {
        'id': run_id,
        'workspace_id': path.name,
        'title': title,
        'code': code,
        'status': status,
        'returncode': returncode,
        'stdout': stdout,
        'stderr': stderr,
        'metadata': metadata or {},
        'created_at': started,
        'finished_at': _now(),
        'timeout_seconds': timeout_seconds,
    }
    runs_dir = path / 'runs'; runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f'{run_id}.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    return record


@lab_app.get('/health')
def health():
    return {'ok': True}


@lab_app.get('/api/capabilities', dependencies=[Depends(_require_api)])
def capabilities():
    return {
        'python': sys.version.split()[0],
        'max_timeout_seconds': 900,
        'persistent_workspaces': True,
        'actions': ['run', 'create_workspace', 'list_runs', 'latest_context', 'publish_context'],
    }


@lab_app.get('/api/workspaces', dependencies=[Depends(_require_api)])
def list_workspaces():
    return _workspaces()


@lab_app.post('/api/workspaces', dependencies=[Depends(_require_api)])
async def create_workspace(request: Request):
    data = await request.json()
    name = str(data.get('name') or 'Research workspace').strip()
    wid = f'LAB-{uuid.uuid4().hex[:10].upper()}'
    path = _root() / 'workspaces' / wid; path.mkdir(parents=True, exist_ok=False)
    record = {
        'id': wid,
        'name': name,
        'description': str(data.get('description') or ''),
        'source_project': str(data.get('source_project') or 'shared'),
        'metadata': dict(data.get('metadata') or {}),
        'created_at': _now(),
    }
    (path / 'workspace.json').write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf-8')
    return record


@lab_app.get('/api/workspaces/{workspace_id}/runs', dependencies=[Depends(_require_api)])
def list_runs(workspace_id: str):
    return _runs(_workspace_dir(workspace_id))


@lab_app.post('/api/workspaces/{workspace_id}/runs', dependencies=[Depends(_require_api)])
async def api_run(workspace_id: str, request: Request):
    data = await request.json()
    code = str(data.get('code') or '')
    if not code.strip():
        raise HTTPException(400, 'code is required')
    return _execute(
        _workspace_dir(workspace_id),
        str(data.get('title') or 'Experiment'),
        code,
        int(data.get('timeout_seconds') or 320),
        dict(data.get('metadata') or {}),
    )


@lab_app.get('/', response_class=HTMLResponse)
def index(project: str = ''):
    spaces = _workspaces()
    selected = next((x for x in spaces if (x.get('metadata') or {}).get('project_key') == project), spaces[0] if spaces else None)
    cards = ''.join(
        f"<option value='{esc(w['id'])}' {'selected' if selected and w['id']==selected['id'] else ''}>{esc(w['name'])}</option>"
        for w in spaces
    )
    recent = _runs(_workspace_dir(selected['id']))[:8] if selected else []
    recent_html = ''.join(
        f"<div class='task {'done' if r.get('status')=='complete' else 'bad'}'><div class='split'><b>{esc(r.get('title'))}</b><span class='badge'>{esc(r.get('status'))}</span></div><pre class='output' style='max-height:160px'>{esc(r.get('stdout') or r.get('stderr') or 'No output')}</pre></div>"
        for r in recent
    )
    body = f"""
<div class='topbar'><div><h1>Research Lab</h1><p class='sub'>Spazio separato per esperimenti Python riproducibili e contesto condiviso con Expert My Rules.</p></div></div>
<div class='grid'><div class='col-7 card hero'><h2>Nuovo esperimento</h2>{'' if spaces else "<div class='empty'>Crea prima un workspace.</div>"}<form method='post' action='/ui/run'><label>Workspace</label><select name='workspace_id' required>{cards}</select><label>Titolo</label><input name='title' value='Esperimento'><label>Codice Python</label><textarea name='code' rows='16' placeholder='print("test")'></textarea><label>Timeout secondi</label><input name='timeout_seconds' type='number' min='1' max='900' value='320'><button {'disabled' if not spaces else ''}>Esegui nel Lab</button></form></div><div class='col-5 card'><h2>Workspace</h2><form method='post' action='/ui/workspace'><label>Nome</label><input name='name' required value='{esc(project or 'Research workspace')}'><label>Project key</label><input name='project_key' value='{esc(project)}'><button>Crea workspace</button></form><h2 style='margin-top:20px'>Run recenti</h2>{recent_html or "<div class='empty'>Nessun run ancora.</div>"}</div></div>"""
    return HTMLResponse(shell('Expert My Rules — Research Lab', body, active='lab'), headers={'Cache-Control':'no-store'})


@lab_app.post('/ui/workspace')
def ui_workspace(name: str = Form(...), project_key: str = Form('')):
    wid = f'LAB-{uuid.uuid4().hex[:10].upper()}'
    path = _root() / 'workspaces' / wid; path.mkdir(parents=True, exist_ok=False)
    record = {'id':wid,'name':name.strip(),'description':'','source_project':'ui','metadata':{'project_key':project_key.strip()} if project_key.strip() else {},'created_at':_now()}
    (path/'workspace.json').write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
    return RedirectResponse(f'/?project={project_key}',303)


@lab_app.post('/ui/run')
def ui_run(workspace_id: str = Form(...), title: str = Form('Experiment'), code: str = Form(...), timeout_seconds: int = Form(320)):
    if not code.strip():
        raise HTTPException(400,'code is required')
    _execute(_workspace_dir(workspace_id), title, code, timeout_seconds, {'caller':'research_lab_ui'})
    return RedirectResponse('/',303)
