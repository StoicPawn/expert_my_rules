from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from awb.core.models import Gate, JobStatus, Task, TaskStatus
from awb.core.planner import propose_manifest
from awb.core.source_material import SourceMaterialError, ingest_source, list_sources
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.web.control_app import (
    _ensure_all_source_tasks,
    _ensure_source_access,
    _ensure_source_task,
    _project_body as legacy_project_body,
    _root,
    _seed_plan,
    control_app,
    index as legacy_index,
)
from awb.web.app import _start, base_dir, slug
from awb.web.ui import badge, esc

SETUP_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='awb-setup')
SETUP_LOCK = threading.Lock()
SETUP_ACTIVE: set[str] = set()
TERMINAL_JOB_STATES = {
    JobStatus.CANCELLED.value, JobStatus.COMPLETE.value,
    JobStatus.BUDGET_FINISHED.value, JobStatus.FAILED.value,
}


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    control_app.router.routes[:] = [
        route for route in control_app.router.routes
        if not (getattr(route, 'path', None) == path and method in (getattr(route, 'methods', None) or set()))
    ]


for _path, _method in (
    ('/', 'GET'), ('/create', 'POST'), ('/project/{project}', 'GET'),
    ('/project/{project}/sources', 'POST'), ('/project/{project}/goal', 'POST'),
    ('/project/{project}/launch', 'POST'),
):
    _remove_route(_path, _method)


def _setup_state_path(root: Path) -> Path:
    path = root / '.awb' / 'setup-state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_setup_state(root: Path) -> dict:
    path = _setup_state_path(root)
    if not path.exists():
        return {'status': 'NOT_STARTED', 'revision': 0, 'detail': ''}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {'status': 'ERROR', 'revision': 0, 'detail': 'Stato setup non leggibile'}


def _save_setup_state(root: Path, **updates) -> dict:
    state = _load_setup_state(root)
    state.update(updates)
    state['updated_at'] = datetime.now(timezone.utc).isoformat()
    tmp = _setup_state_path(root).with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(_setup_state_path(root))
    return state


def _source_context(root: Path, max_chars: int = 14000) -> str:
    chunks: list[str] = []
    remaining = max_chars
    index = root / 'sources' / 'INDEX.md'
    if index.exists() and remaining > 0:
        text = index.read_text(encoding='utf-8', errors='replace')[: min(4000, remaining)]
        chunks.append(text)
        remaining -= len(text)
    for item in list_sources(root):
        if remaining <= 0:
            break
        rel = str(item.get('text_path') or '').strip()
        if not rel:
            continue
        path = (root / rel).resolve()
        if root.resolve() not in path.parents or not path.exists():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        take = min(5000, remaining)
        chunks.append(f"\n--- {item.get('filename', path.name)} ---\n{text[:take]}")
        remaining -= min(len(text), take)
    return ''.join(chunks)[:max_chars]


def _refresh_system_plan(root: Path, revision: int) -> None:
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.conn.execute("DELETE FROM tasks WHERE created_by='system-planner' AND status IN ('OPEN','BLOCKED','ERROR','REJECTED')")
    ledger.conn.commit()
    plan = [('Map current state against the North Star', 'Read supplied sources, existing artifacts and ledger history; identify the highest-information unresolved blockers under the current setup.')]
    for gate in ws.manifest.gates[:8]:
        plan.append((f'Close: {gate.id.replace("_", " ")}', f'Produce and independently verify evidence sufficient to satisfy this completion condition: {gate.description}'))
    for idx, (title, description) in enumerate(plan, start=1):
        ledger.upsert_task(Task(id=f'AUTO-{revision:03d}-{idx:03d}', title=title, description=description, priority=max(20.0, 60.0 - idx), created_by='system-planner'))
    ledger.event('automatic_plan_refreshed', {'revision': revision, 'tasks': len(plan)})


def _run_auto_setup(root: Path) -> None:
    project = root.name
    try:
        current = _load_setup_state(root)
        revision = int(current.get('revision', 0)) + 1
        _save_setup_state(root, status='RUNNING', revision=revision, detail='Sto generando team, ruoli, Definition of Done e piano iniziale.')
        ws = load_workspace(root)
        original_goal = ws.manifest.goal.strip()
        context = _source_context(root)
        planner_goal = original_goal
        if context:
            planner_goal += '\n\nUSER-PROVIDED STARTING MATERIAL CONTEXT. Tailor the team instructions and completion gates to this material, but do not treat its claims as automatically correct:\n' + context
        planned = propose_manifest(planner_goal, ws.manifest.name, use_local_ai=True)
        ws = load_workspace(root)
        if isinstance(planned.get('description'), str) and planned['description'].strip():
            ws.manifest.description = planned['description'].strip()
        planned_agents = {str(item.get('role')): item for item in planned.get('agents', []) if isinstance(item, dict) and item.get('role')}
        for agent in ws.manifest.agents:
            candidate = planned_agents.get(agent.role)
            if candidate and str(candidate.get('instructions') or '').strip():
                agent.instructions = str(candidate['instructions']).strip()
        planned_gates = planned.get('gates')
        if isinstance(planned_gates, list) and planned_gates:
            validated: list[Gate] = []
            seen: set[str] = set()
            for item in planned_gates:
                try:
                    gate = Gate.model_validate(item)
                except Exception:
                    continue
                if gate.id in seen:
                    continue
                seen.add(gate.id)
                validated.append(gate)
            if validated:
                ws.manifest.gates = validated
        save_manifest(ws)
        _ensure_source_access(root)
        ledger = Ledger(root / 'ledger.sqlite3')
        for gate in load_workspace(root).manifest.gates:
            ledger.set_gate(gate.id, False, f'reset by automatic setup revision {revision}')
        _refresh_system_plan(root, revision)
        _ensure_all_source_tasks(root)
        ledger.event('automatic_setup_completed', {'revision': revision, 'source_count': len(list_sources(root)), 'gate_count': len(load_workspace(root).manifest.gates)})
        _save_setup_state(root, status='READY', revision=revision, detail='Setup automatico pronto e modificabile.')
    except Exception as exc:
        try:
            Ledger(root / 'ledger.sqlite3').event('automatic_setup_failed', {'error': f'{type(exc).__name__}: {exc}'})
        except Exception:
            pass
        _save_setup_state(root, status='ERROR', detail=f'{type(exc).__name__}: {exc}')
    finally:
        with SETUP_LOCK:
            SETUP_ACTIVE.discard(project)


def _schedule_auto_setup(root: Path, reason: str) -> bool:
    project = root.name
    current_job = Ledger(root / 'ledger.sqlite3').latest_job()
    if current_job and current_job['status'] in {JobStatus.RUNNING.value, JobStatus.PAUSED.value}:
        _save_setup_state(root, status='DEFERRED', detail='Setup automatico rinviato: termina il job prima di rigenerarlo.')
        return False
    with SETUP_LOCK:
        if project in SETUP_ACTIVE:
            _save_setup_state(root, status='QUEUED', detail='Nuovo materiale ricevuto: rigenerazione accodata.')
            return False
        SETUP_ACTIVE.add(project)
    _save_setup_state(root, status='QUEUED', detail=f'Autoconfigurazione accodata: {reason}.')
    SETUP_EXECUTOR.submit(_run_auto_setup, root)
    return True


def _project_editor_panels(root: Path) -> tuple[str, str]:
    ws = load_workspace(root)
    gate_forms = []
    for gate in ws.manifest.gates:
        gate_forms.append(f"<div class='task'><form method='post' action='/project/{esc(root.name)}/gate/edit'><input type='hidden' name='gate_id' value='{esc(gate.id)}'><div class='split'><b>{esc(gate.id)}</b>{badge('required' if gate.required else 'optional')}</div><label>Condizione di completamento</label><textarea name='description' rows='3'>{esc(gate.description)}</textarea><label><input style='width:auto' type='checkbox' name='required' {'checked' if gate.required else ''}> obbligatoria</label><div class='row'><button class='secondary'>Salva</button><button class='danger' formaction='/project/{esc(root.name)}/gate/delete' onclick=\"return confirm('Rimuovere questa condizione?')\">Rimuovi</button></div></form></div>")
    gate_editor = f"<div class='card' style='margin-top:14px'><h2>Modifica Definition of Done</h2><p class='small muted'>Il setup automatico è un punto di partenza: puoi cambiare le condizioni prima di ogni run.</p>{''.join(gate_forms)}<form method='post' action='/project/{esc(root.name)}/gate/add'><label>Nuova condizione</label><input name='gate_id' placeholder='es. application_validated' required><textarea name='description' rows='3' required placeholder='Cosa deve essere verificato per considerarla chiusa?'></textarea><label><input style='width:auto' type='checkbox' name='required' checked> obbligatoria</label><button>Aggiungi condizione</button></form></div>"
    agent_forms = []
    for agent in ws.manifest.agents:
        agent_forms.append(f"<div class='task'><form method='post' action='/project/{esc(root.name)}/agent/edit'><input type='hidden' name='role' value='{esc(agent.role)}'><div class='split'><b>{esc(agent.id)}</b>{badge(agent.role)}</div><label>Cosa deve fare</label><textarea name='instructions' rows='4'>{esc(agent.instructions)}</textarea><button class='secondary'>Salva ruolo</button></form></div>")
    agent_editor = f"<div class='card' style='margin-top:14px'><h2>Istruzioni dei ruoli</h2><p class='small muted'>Puoi modificare cosa fa ciascun agente senza cambiare il modello assegnato.</p>{''.join(agent_forms)}</div>"
    return gate_editor, agent_editor


def _setup_banner(root: Path) -> str:
    state = _load_setup_state(root)
    status = str(state.get('status', 'NOT_STARTED'))
    kind = 'ok' if status == 'READY' else 'bad' if status == 'ERROR' else 'warn'
    label = {'READY': 'SETUP PRONTO', 'RUNNING': 'SETUP IN CORSO', 'QUEUED': 'SETUP IN CODA', 'DEFERRED': 'SETUP RINVIATO', 'ERROR': 'ERRORE SETUP'}.get(status, status)
    return f"<div class='card hero'><div class='split'><div><h2>Autoconfigurazione</h2><p class='small muted'>North Star + materiale allegato → team, ruoli, Definition of Done e piano iniziale. Poi puoi modificare tutto manualmente.</p></div>{badge(label, kind)}</div><p>{esc(str(state.get('detail') or ''))}</p><form method='post' action='/project/{esc(root.name)}/auto-setup'><button class='secondary'>Rigenera setup automaticamente</button></form></div>"


@control_app.get('/', response_class=HTMLResponse)
def index_v3(request: Request):
    response = legacy_index(request)
    html = response.body.decode('utf-8')
    html = html.replace("<form method='post' action='/create'>", "<form method='post' action='/create' enctype='multipart/form-data'>", 1)
    html = html.replace('<button>Crea setup proposto</button>', "<label>Materiale iniziale opzionale</label><input type='file' name='files' multiple accept='.pdf,.txt,.md,.markdown,.tex,.csv,.json,application/pdf,text/plain,text/markdown'><p class='small muted'>Puoi allegare subito PDF o testi. Il progetto viene creato immediatamente; l'autoconfigurazione AI continua in background senza bloccare la pagina.</p><button>Crea progetto</button>", 1)
    html = html.replace('Dopo la creazione puoi caricare PDF e altri materiali prima di avviare il job.', 'Puoi anche aggiungere materiale in seguito dal tab Fonti & documenti.')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@control_app.get('/create')
def create_get_fallback():
    return RedirectResponse('/', status_code=303)


@control_app.post('/create')
async def create_v3(goal: str = Form(...), name: str = Form(''), files: list[UploadFile] | None = File(None)):
    manifest = propose_manifest(goal, name.strip() or None, use_local_ai=False)
    safe = slug(manifest['name'])
    root = base_dir() / safe
    i = 2
    while root.exists():
        root = base_dir() / f'{safe}_{i}'
        i += 1
    manifest['name'] = root.name
    write_workspace(root, manifest)
    try:
        selected = [f for f in (files or []) if f.filename]
        if len(selected) > 20:
            raise HTTPException(400, 'Massimo 20 fonti per caricamento')
        for upload in selected:
            data = await upload.read()
            item = ingest_source(root, upload.filename or 'source', data, upload.content_type)
            _ensure_source_task(root, item)
        _seed_plan(root)
        _schedule_auto_setup(root, 'nuovo progetto')
    except SourceMaterialError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f'/project/{root.name}?tab=north', status_code=303)


@control_app.get('/project/{project}', response_class=HTMLResponse)
def project_v3(request: Request, project: str):
    root = _root(project)
    html = legacy_project_body(request, project)
    gate_editor, agent_editor = _project_editor_panels(root)
    html = html.replace("<section class='pane active' data-pane='north'><div class='grid'>", f"<section class='pane active' data-pane='north'>{_setup_banner(root)}<div class='grid'>", 1)
    html = html.replace("</div></div></section>\n<section class='pane' data-pane='sources'>", f"</div></div>{gate_editor}</section>\n<section class='pane' data-pane='sources'>", 1)
    html = html.replace("</div></div></section>\n<section class='pane' data-pane='plan'>", f"</div></div>{agent_editor}</section>\n<section class='pane' data-pane='plan'>", 1)
    job = Ledger(root / 'ledger.sqlite3').latest_job()
    if job and job['status'] in TERMINAL_JOB_STATES:
        html = html.replace('>Avvia progetto autonomo</button>', '>Rilancia progetto autonomo</button>', 1)
        html = html.replace('Job autonomo</h2>', "Job autonomo</h2><p class='small muted'>Il progetto resta riutilizzabile: modifica setup e rilancialo quando vuoi.</p>", 1)
    delete_panel = f"<div class='card'><h2>Gestione progetto</h2><p class='small muted'>Il progetto e la cronologia restano disponibili finché non li elimini esplicitamente.</p><form method='post' action='/project/{esc(project)}/delete' onsubmit=\"return confirm('Eliminare definitivamente questo progetto e la sua cronologia?')\"><button class='danger'>Elimina progetto</button></form></div>"
    html = html.replace("<div class='col-5 card'><h2>Collegamenti</h2>", "<div class='col-5'><div class='card'><h2>Collegamenti</h2>", 1)
    html = html.replace("Setup, monitoraggio e laboratorio sono separati per evitare un'unica pagina tecnica troppo densa.</p></div></div></section>", f"Setup, monitoraggio e laboratorio sono separati per evitare un'unica pagina tecnica troppo densa.</p></div>{delete_panel}</div></div></section>", 1)
    state = _load_setup_state(root)
    if state.get('status') in {'RUNNING', 'QUEUED'}:
        html = html.replace('</script></body></html>', 'setTimeout(()=>location.reload(),2500);</script></body></html>')
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@control_app.get('/project/{project}/setup-status')
def setup_status(project: str):
    return JSONResponse(_load_setup_state(_root(project)))


@control_app.post('/project/{project}/auto-setup')
def auto_setup(project: str):
    _schedule_auto_setup(_root(project), 'rigenerazione richiesta dall’utente')
    return RedirectResponse(f'/project/{project}?tab=north', 303)


@control_app.post('/project/{project}/sources')
async def upload_sources_v3(project: str, files: list[UploadFile] = File(...)):
    root = _root(project)
    if not files or len(files) > 20:
        raise HTTPException(400, 'Seleziona da 1 a 20 file')
    _ensure_source_access(root)
    uploaded = 0
    for upload in files:
        if not upload.filename:
            continue
        try:
            data = await upload.read()
            item = ingest_source(root, upload.filename, data, upload.content_type)
        except SourceMaterialError as exc:
            raise HTTPException(400, str(exc)) from exc
        _ensure_source_task(root, item)
        Ledger(root / 'ledger.sqlite3').event('source_uploaded', {'id': item.get('id'), 'filename': item.get('filename'), 'sha256': item.get('sha256'), 'bytes': item.get('bytes'), 'text_chars': item.get('text_chars'), 'status': item.get('status')})
        uploaded += 1
    if not uploaded:
        raise HTTPException(400, 'Nessun file valido caricato')
    _schedule_auto_setup(root, 'nuovo materiale allegato')
    return RedirectResponse(f'/project/{project}?tab=sources', 303)


@control_app.post('/project/{project}/goal')
def set_goal_v3(project: str, goal: str = Form(...)):
    root = _root(project)
    ws = load_workspace(root)
    ws.manifest.goal = goal.strip()
    save_manifest(ws)
    ledger = Ledger(root / 'ledger.sqlite3')
    for gate in ws.manifest.gates:
        ledger.set_gate(gate.id, False, 'North Star modificata: condizione riaperta')
    ledger.event('north_star_changed', {'goal': goal.strip()[:500]})
    _schedule_auto_setup(root, 'North Star modificata')
    return RedirectResponse(f'/project/{project}?tab=north', 303)


@control_app.post('/project/{project}/agent/edit')
def edit_agent(project: str, role: str = Form(...), instructions: str = Form(...)):
    root = _root(project)
    ws = load_workspace(root)
    match = next((agent for agent in ws.manifest.agents if agent.role == role), None)
    if not match:
        raise HTTPException(404, 'Ruolo non trovato')
    match.instructions = instructions.strip()
    save_manifest(ws)
    Ledger(root / 'ledger.sqlite3').event('agent_instructions_changed', {'role': role})
    return RedirectResponse(f'/project/{project}?tab=team', 303)


@control_app.post('/project/{project}/gate/edit')
def edit_gate(project: str, gate_id: str = Form(...), description: str = Form(...), required: str | None = Form(None)):
    root = _root(project)
    ws = load_workspace(root)
    gate = next((g for g in ws.manifest.gates if g.id == gate_id), None)
    if not gate:
        raise HTTPException(404, 'Condizione non trovata')
    gate.description = description.strip(); gate.required = required is not None; save_manifest(ws)
    Ledger(root / 'ledger.sqlite3').set_gate(gate.id, False, 'Condition edited by user')
    return RedirectResponse(f'/project/{project}?tab=north', 303)


@control_app.post('/project/{project}/gate/add')
def add_gate(project: str, gate_id: str = Form(...), description: str = Form(...), required: str | None = Form(None)):
    root = _root(project); ws = load_workspace(root); gid = slug(gate_id)
    if not gid or any(g.id == gid for g in ws.manifest.gates):
        raise HTTPException(400, 'ID condizione non valido o già esistente')
    ws.manifest.gates.append(Gate(id=gid, description=description.strip(), required=required is not None, manual=False)); save_manifest(ws)
    Ledger(root / 'ledger.sqlite3').set_gate(gid, False, 'Condition added by user')
    return RedirectResponse(f'/project/{project}?tab=north', 303)


@control_app.post('/project/{project}/gate/delete')
def delete_gate(project: str, gate_id: str = Form(...)):
    root = _root(project); ws = load_workspace(root); before = len(ws.manifest.gates)
    ws.manifest.gates = [g for g in ws.manifest.gates if g.id != gate_id]
    if len(ws.manifest.gates) == before:
        raise HTTPException(404, 'Condizione non trovata')
    if not ws.manifest.gates:
        raise HTTPException(400, 'Il progetto deve avere almeno una condizione di completamento')
    save_manifest(ws); Ledger(root / 'ledger.sqlite3').event('gate_deleted', {'gate_id': gate_id})
    return RedirectResponse(f'/project/{project}?tab=north', 303)


@control_app.post('/project/{project}/launch')
def launch_v3(project: str):
    root = _root(project); ledger = Ledger(root / 'ledger.sqlite3'); current = ledger.latest_job()
    if current and current['status'] in {JobStatus.RUNNING.value, JobStatus.PAUSED.value}:
        jid = current['id']
    else:
        if current is not None:
            ws = load_workspace(root)
            for gate in ws.manifest.gates:
                if gate.required:
                    ledger.set_gate(gate.id, False, f'reopened for new run after {current["status"]}')
            for task in ledger.list_tasks(statuses=[TaskStatus.BLOCKED, TaskStatus.ERROR, TaskStatus.REJECTED]):
                task.status = TaskStatus.OPEN; ledger.upsert_task(task)
            rerun_id = datetime.now(timezone.utc).strftime('RERUN-%Y%m%d%H%M%S')
            ledger.upsert_task(Task(id=rerun_id, title='Reassess project under the current setup', description='Re-read the current North Star, supplied material, setup and ledger history; identify the highest-value unresolved work for this new run.', priority=100.0, created_by='relaunch'))
            ledger.event('project_relaunched', {'previous_job': current['id'], 'previous_status': current['status']})
        jid = ledger.create_job(0, 0, continuous=True)
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='autonomous project active'); _start(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/delete')
def delete_project(project: str):
    root = _root(project); current = Ledger(root / 'ledger.sqlite3').latest_job()
    if current and current['status'] in {JobStatus.RUNNING.value, JobStatus.PAUSED.value, JobStatus.QUEUED.value}:
        raise HTTPException(409, 'Termina prima il job attivo')
    shutil.rmtree(root)
    return RedirectResponse('/', 303)


@control_app.on_event('startup')
def recover_setup_jobs() -> None:
    for root in base_dir().iterdir():
        if not (root / 'project.yaml').exists():
            continue
        state = _load_setup_state(root)
        if state.get('status') in {'RUNNING', 'QUEUED'}:
            _schedule_auto_setup(root, 'ripristino dopo riavvio')
