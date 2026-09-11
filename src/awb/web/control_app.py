from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from awb.core.cloud_budget import (
    GlobalCloudControl,
    budget_snapshot,
    load_control,
    load_global_control,
    save_control,
    save_global_control,
)
from awb.core.model_catalog import OPENAI_MODELS, catalog_for_manifest
from awb.core.models import ComputeNodeSpec, JobStatus, ModelRouteSpec, Task, ToolSpec
from awb.core.planner import propose_manifest
from awb.core.source_material import SourceMaterialError, ingest_source, list_sources
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.web.app import _start, base_dir, slug
from awb.web.ui import badge, esc, shell


control_app = FastAPI(title='Expert My Rules Control Center')
ROLES = ('director', 'worker', 'reviewer', 'verifier')


def _root(project: str) -> Path:
    root = base_dir() / project
    if not (root / 'project.yaml').exists():
        raise HTTPException(404, 'Project not found')
    return root


def _cross_link(request: Request, port_env: str, default_port: int, path: str = '/') -> str:
    host = request.url.hostname or '127.0.0.1'
    port = int(os.getenv(port_env, str(default_port)))
    return f'http://{host}:{port}{path}'


def _seed_plan(root: Path) -> None:
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    if ledger.list_tasks():
        return
    plan = [
        ('SYS-0001', 'Map the current state', 'Collect the strongest existing evidence, assumptions, artifacts and unresolved blockers for the North Star.'),
    ]
    for idx, gate in enumerate(ws.manifest.gates[:6], start=2):
        plan.append((
            f'SYS-{idx:04d}',
            f'Close: {gate.id.replace("_", " ")}',
            f'Produce and independently verify evidence sufficient to satisfy this completion condition: {gate.description}',
        ))
    for idx, (task_id, title, description) in enumerate(plan):
        task = Task(id=task_id, title=title, description=description, priority=max(1.0, 10.0 - idx), created_by='system-planner')
        ledger.upsert_task(task)
        ledger.event('task_created', task.model_dump(mode='json'), task.id)


def _ensure_source_access(root: Path) -> None:
    ws = load_workspace(root)
    changed = False
    protected = ws.manifest.runtime.git.protected_paths
    if 'sources' not in protected:
        protected.append('sources')
        changed = True

    required = {
        'list': ToolSpec(id='list', type='list_files', description='List files in the private project workspace.'),
        'read': ToolSpec(id='read', type='read_file', description='Read a private project text artifact.'),
        'read_range': ToolSpec(id='read_range', type='read_file_range', description='Read a targeted line range from a large source or artifact.'),
        'search': ToolSpec(id='search', type='search_text', description='Search literal text across project sources and artifacts.'),
    }
    existing = {tool.id for tool in ws.manifest.tools}
    for tool_id, spec in required.items():
        if tool_id not in existing:
            ws.manifest.tools.append(spec)
            existing.add(tool_id)
            changed = True

    source_instruction = (
        'User-provided starting material is stored immutably under sources/. '
        'Read sources/INDEX.md and the extracted text files before making source-dependent claims; '
        'distinguish the supplied material from later hypotheses or derived artifacts.'
    )
    for agent in ws.manifest.agents:
        if agent.role not in {'worker', 'verifier'}:
            continue
        for tool_id in ('list', 'read', 'read_range', 'search'):
            if tool_id not in agent.tools:
                agent.tools.append(tool_id)
                changed = True
        if source_instruction not in agent.instructions:
            agent.instructions = f'{agent.instructions.rstrip()} {source_instruction}'
            changed = True
    if changed:
        save_manifest(ws)


def _ensure_source_task(root: Path, source: dict) -> None:
    ledger = Ledger(root / 'ledger.sqlite3')
    task_id = f"SRC-{str(source.get('id') or source.get('sha256',''))[:12].upper()}"
    if any(task.id == task_id for task in ledger.list_tasks()):
        return
    status = str(source.get('status') or '')
    text_path = str(source.get('text_path') or '')
    if status == 'ready':
        description = (
            f"Read `sources/INDEX.md` and `{text_path}` as user-provided starting material. "
            'Map its definitions, assumptions, claims, proof dependencies, empirical evidence and unresolved gaps. '
            'Use it as primary evidence where relevant, without treating its claims as automatically correct.'
        )
        priority = 50.0
    else:
        description = (
            f"The user supplied `{source.get('filename','source')}`, but no readable embedded text was extracted. "
            'Record that OCR or a text-readable version is required before source-dependent reasoning can be verified.'
        )
        priority = 15.0
    task = Task(
        id=task_id,
        title=f"Digest supplied source: {source.get('filename','source')}",
        description=description,
        priority=priority,
        created_by='source-upload',
    )
    ledger.upsert_task(task)
    ledger.event('task_created', task.model_dump(mode='json'), task.id)


def _ensure_all_source_tasks(root: Path) -> None:
    if not list_sources(root):
        return
    _ensure_source_access(root)
    for source in list_sources(root):
        _ensure_source_task(root, source)


@control_app.on_event('startup')
def recover_jobs() -> None:
    for root in base_dir().iterdir():
        if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
            continue
        try:
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.recover_interrupted_tasks()
            for job in ledger.recoverable_jobs():
                _start(root, job['id'])
        except Exception:
            continue


@control_app.get('/health')
def health():
    return {'ok': True}


@control_app.get('/', response_class=HTMLResponse)
def index(request: Request):
    cards = []
    for root in sorted(base_dir().iterdir()):
        if not (root / 'project.yaml').exists():
            continue
        try:
            ws = load_workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            job = ledger.latest_job()
            state = ledger.gate_state()
            passed = sum(1 for gate in ws.manifest.gates if state.get(gate.id, {}).get('passed'))
            cards.append(
                f"<a class='card linkcard' href='/project/{esc(root.name)}'><div class='split'><div><h2>{esc(ws.manifest.name)}</h2><div class='muted small'>{esc(ws.manifest.type)}</div></div>{badge(job['status'] if job else 'NOT STARTED','live' if job and job['status']=='RUNNING' else '')}</div><p>{esc(ws.manifest.goal)}</p><div class='muted small'>{passed}/{len(ws.manifest.gates)} condizioni completate · {len(list_sources(root))} fonti caricate</div></a>"
            )
        except Exception:
            continue
    body = f"""
<div class='topbar'><div><h1>Setup</h1><p class='sub'>Configura obiettivo, materiale di partenza, team, modelli, piano e limiti API. La dashboard live è separata.</p></div><div class='nav'><a href='{_cross_link(request,'AWB_OBSERVER_PORT',8101)}'>Dashboard live</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102)}'>Research Lab</a></div></div>
<div class='grid'><div class='col-5 card hero'><h2>Nuovo progetto</h2><form method='post' action='/create'><label>North Star</label><textarea name='goal' required rows='5' placeholder='Cosa deve esistere quando il progetto è davvero finito?'></textarea><label>Nome opzionale</label><input name='name' placeholder='generato automaticamente'><button>Crea setup proposto</button></form><p class='small muted'>Dopo la creazione puoi caricare PDF e altri materiali prima di avviare il job.</p></div><div class='col-7'><h2>Progetti</h2>{''.join(cards) if cards else "<div class='empty'>Nessun progetto configurato.</div>"}</div></div>"""
    return HTMLResponse(shell('Expert My Rules — Setup', body, active='setup'), headers={'Cache-Control': 'no-store'})


@control_app.post('/create')
def create(goal: str = Form(...), name: str = Form('')):
    manifest = propose_manifest(goal, name.strip() or None, use_local_ai=True)
    safe = slug(manifest['name'])
    root = base_dir() / safe
    i = 2
    while root.exists():
        root = base_dir() / f'{safe}_{i}'
        i += 1
    manifest['name'] = root.name
    write_workspace(root, manifest)
    _seed_plan(root)
    return RedirectResponse(f'/project/{root.name}', status_code=303)


def _route_for(ws, role: str):
    routes = [r for r in ws.manifest.runtime.role_routes.get(role, []) if r.enabled]
    if routes:
        return routes[0]
    return ModelRouteSpec(node='local-ollama', model=ws.manifest.runtime.default_provider.model, priority=100)


def _project_body(request: Request, project: str) -> str:
    root = _root(project)
    _seed_plan(root)
    _ensure_all_source_tasks(root)
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.reconcile_task_counters()
    job = ledger.latest_job()
    tasks = ledger.list_tasks()
    gates = ledger.gate_state()
    sources = list_sources(root)
    budget = budget_snapshot(root)
    global_policy = load_global_control(root)
    cloud = load_control(root)

    role_cards = []
    for agent in ws.manifest.agents:
        role = agent.role
        route = _route_for(ws, role)
        model_value = esc(route.model or '')
        cloud_cfg = budget['role_models'].get(role, {})
        openai_options = ''.join(
            f"<option value='{esc(model)}' {'selected' if model==cloud_cfg.get('model') else ''}>{esc(model)}</option>"
            for model in OPENAI_MODELS
        )
        reasoning_options = ''.join(
            f"<option value='{level}' {'selected' if level==cloud_cfg.get('reasoning') else ''}>{level}</option>"
            for level in ('low','medium','high')
        )
        role_cards.append(f"""
<div class='agent'><div class='split'><div><h3>{esc(agent.id)}</h3><span class='badge'>{esc(role)}</span></div><span class='muted small'>Ruolo epistemico</span></div><p class='small muted'>{esc(agent.instructions)}</p>
<form method='post' action='/project/{esc(project)}/route'><input type='hidden' name='role' value='{esc(role)}'><div class='grid'><div class='col-6'><label>Modello locale / nodo</label><select name='node' class='node-select' data-role='{esc(role)}'>{''.join(f"<option value='{esc(n.id)}' {'selected' if n.id==route.node else ''}>{esc(n.id)} · {esc(n.kind)}</option>" for n in ws.manifest.runtime.compute_nodes)}</select></div><div class='col-6'><label>Modello</label><select name='model' class='model-select' data-role='{esc(role)}'><option value='{model_value}'>{model_value or 'auto'}</option></select></div></div><button class='secondary'>Salva routing locale</button></form>
<form method='post' action='/project/{esc(project)}/cloud-role'><input type='hidden' name='role' value='{esc(role)}'><div class='grid'><div class='col-6'><label>Modello API esterno</label><select name='model'>{openai_options}</select></div><div class='col-6'><label>Reasoning</label><select name='reasoning'>{reasoning_options}</select></div></div><button class='secondary'>Salva modello esterno</button></form></div>""")

    node_cards = []
    for node in ws.manifest.runtime.compute_nodes:
        node_cards.append(f"<div class='task'><div class='split'><div><b>{esc(node.id)}</b><div class='small muted'>{esc(node.kind)} · {esc(node.base_url or node.base_url_env or 'default endpoint')}</div></div>{badge('ON' if node.enabled else 'OFF','ok' if node.enabled else '')}</div></div>")

    task_cards = []
    for task in tasks:
        kind = 'done' if task.status.value == 'DONE' else 'live' if task.status.value == 'IN_PROGRESS' else 'bad' if task.status.value in {'ERROR','BLOCKED'} else ''
        task_cards.append(f"<div class='task {kind}'><div class='split'><div><b>{esc(task.title)}</b><div class='small muted'>{esc(task.description)}</div></div>{badge(task.status.value,'ok' if task.status.value=='DONE' else 'live' if task.status.value=='IN_PROGRESS' else '')}</div><div class='small muted'>ID {esc(task.id)} · creato da {esc(task.created_by)} · priorità {task.priority:g}</div></div>")

    gate_cards = []
    for gate in ws.manifest.gates:
        passed = bool(gates.get(gate.id, {}).get('passed'))
        gate_cards.append(f"<div class='task {'done' if passed else ''}'><div class='split'><div><b>{esc(gate.id.replace('_',' '))}</b><div class='small muted'>{esc(gate.description)}</div></div>{badge('PASS' if passed else 'OPEN','ok' if passed else 'warn')}</div></div>")

    source_cards = []
    for source in sources:
        ready = source.get('status') == 'ready'
        size_mb = float(source.get('bytes') or 0) / 1024 / 1024
        detail = f"{size_mb:.2f} MB · {int(source.get('text_chars') or 0):,} caratteri estratti"
        if source.get('pages') is not None:
            detail += f" · {source.get('pages')} pagine"
        source_cards.append(
            f"<div class='task {'done' if ready else 'bad'}'><div class='split'><div><b>{esc(source.get('filename','source'))}</b><div class='small muted'>{esc(detail)}</div><div class='small muted'>Testo per gli agenti: {esc(source.get('text_path',''))}</div></div>{badge('PRONTO' if ready else 'OCR RICHIESTO','ok' if ready else 'warn')}</div></div>"
        )

    job_status = job['status'] if job else 'NOT STARTED'
    controls = "<form method='post' action='/project/{}/launch'><button>Avvia progetto autonomo</button></form>".format(esc(project))
    if job and job_status == JobStatus.RUNNING.value:
        controls = f"<div class='row'>{badge('RUNNING','live')}<form method='post' action='/project/{esc(project)}/job/{esc(job['id'])}/pause'><button class='secondary'>Pausa</button></form><form method='post' action='/project/{esc(project)}/job/{esc(job['id'])}/cancel'><button class='danger'>Termina job</button></form></div>"
    elif job and job_status == JobStatus.PAUSED.value:
        controls = f"<div class='row'>{badge('PAUSED','warn')}<form method='post' action='/project/{esc(project)}/job/{esc(job['id'])}/resume'><button>Riprendi</button></form><form method='post' action='/project/{esc(project)}/job/{esc(job['id'])}/cancel'><button class='danger'>Termina</button></form></div>"

    month_budget = float(budget['monthly_budget_eur'])
    month_spent = float(budget['monthly_spent_eur'])
    month_pct = min(100.0, month_spent / month_budget * 100.0) if month_budget else 100.0
    project_budget = float(budget['budget_eur'])
    project_spent = float(budget['spent_eur'])
    project_pct = min(100.0, project_spent / project_budget * 100.0) if project_budget else 100.0

    body = f"""
<div class='topbar'><div><a class='small muted' href='/'>← progetti</a><h1>{esc(ws.manifest.name)}</h1><p class='sub'>{esc(ws.manifest.description)}</p></div><div class='nav'><a href='{_cross_link(request,'AWB_OBSERVER_PORT',8101,f'/project/{project}')}' >Dashboard live</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102,f'/?project={project}')}' >Research Lab</a></div></div>
<div class='tabs'><button class='tab active' data-tab='north'>North Star</button><button class='tab' data-tab='sources'>Fonti & documenti</button><button class='tab' data-tab='team'>Team & modelli</button><button class='tab' data-tab='plan'>Piano</button><button class='tab' data-tab='api'>API & costi</button><button class='tab' data-tab='runtime'>Avvio</button></div>
<section class='pane active' data-pane='north'><div class='grid'><div class='col-8 card hero'><h2>North Star</h2><form method='post' action='/project/{esc(project)}/goal'><textarea name='goal' rows='5'>{esc(ws.manifest.goal)}</textarea><button>Salva North Star</button></form></div><div class='col-4 card'><h2>Definition of Done</h2>{''.join(gate_cards)}</div></div></section>
<section class='pane' data-pane='sources'><div class='grid'><div class='col-7 card'><div class='split'><div><h2>Materiale di partenza</h2><p class='small muted'>I file caricati restano immutabili in <code>sources/</code>. Il sistema estrae il testo e crea automaticamente task ad alta priorità per leggerli prima del resto del lavoro.</p></div>{badge(f'{len(sources)} fonti')}</div>{''.join(source_cards) if source_cards else "<div class='empty'>Nessun materiale caricato. Puoi avviare un progetto da zero oppure caricare qui le fonti iniziali.</div>"}</div><div class='col-5 card hero'><h2>Carica documenti</h2><form method='post' action='/project/{esc(project)}/sources' enctype='multipart/form-data'><label>PDF o file di testo</label><input type='file' name='files' multiple required accept='.pdf,.txt,.md,.markdown,.tex,.csv,.json,application/pdf,text/plain,text/markdown'><p class='small muted'>Puoi selezionare più file anche da iPhone. Massimo 25 MB per file. PDF testuali vengono indicizzati automaticamente; se il PDF è una scansione senza testo vedrai “OCR richiesto”.</p><button>Carica e prepara per gli agenti</button></form></div></div></section>
<section class='pane' data-pane='team'><div class='grid'><div class='col-8 card'><div class='split'><div><h2>Chi fa cosa</h2><p class='muted small'>Ogni ruolo ha un modello locale primario. L'API esterna è un'escalation controllata dal budget.</p></div><button type='button' class='secondary' onclick='refreshModels()'>Aggiorna modelli</button></div>{''.join(role_cards)}</div><div class='col-4 card'><h2>Nodi collegati</h2>{''.join(node_cards)}<hr style='border:0;border-top:1px solid #e4e7ec;margin:14px 0'><h3>Aggiungi / aggiorna nodo</h3><form method='post' action='/project/{esc(project)}/node'><label>ID</label><input name='node_id' required placeholder='es. gpu-studio'><label>Tipo</label><select name='kind'><option value='ollama'>Ollama</option><option value='lmstudio'>LM Studio</option></select><label>Base URL</label><input name='base_url' placeholder='http://host.docker.internal:1234/v1'><label>Concorrenza</label><input name='max_concurrency' type='number' min='1' max='8' value='1'><button>Aggancia nodo</button></form></div></div></section>
<section class='pane' data-pane='plan'><div class='grid'><div class='col-8 card'><div class='split'><div><h2>Task verso la North Star</h2><p class='small muted'>Il sistema crea il piano iniziale; le fonti caricate generano task prioritari; il Director può aggiungere/decomporre task durante il lavoro.</p></div>{badge(f'{len(tasks)} task')}</div>{''.join(task_cards)}</div><div class='col-4 card'><h2>Aggiungi task</h2><form method='post' action='/project/{esc(project)}/task'><label>Titolo</label><input name='title' required><label>Descrizione</label><textarea name='description'></textarea><label>Priorità</label><input name='priority' type='number' step='0.1' value='10'><button>Aggiungi al piano</button></form></div></div></section>
<section class='pane' data-pane='api'><div class='grid'><div class='col-6 card'><h2>Tetto API mensile — sistema</h2><div class='split'><b>€{month_spent:.4f} / €{month_budget:.2f}</b>{badge('BLOCCATO' if budget['hard_blocked'] else 'ATTIVO','bad' if budget['hard_blocked'] else 'ok')}</div><div class='meter {'bad' if month_pct>=100 else 'warn' if month_pct>=80 else 'ok'}'><i style='width:{month_pct:.2f}%'></i></div><p class='small muted'>È globale su tutti i progetti. Al raggiungimento del limite le chiamate API vengono rifiutate prima della partenza e il router torna ai modelli locali.</p><form method='post' action='/project/{esc(project)}/monthly-budget'><label>Massimo mensile €</label><input name='budget' type='number' step='0.01' min='0' value='{global_policy.monthly_budget_eur:.2f}'><label><input style='width:auto' type='checkbox' name='enabled' {'checked' if global_policy.enabled else ''}> abilita tetto globale</label><label><input style='width:auto' type='checkbox' name='hard_stop' {'checked' if global_policy.hard_stop else ''}> blocco coatto + fallback locale</label><button>Salva tetto mensile</button></form></div><div class='col-6 card'><h2>Budget API di questo progetto</h2><div class='split'><b>€{project_spent:.4f} / €{project_budget:.2f}</b>{badge('ON' if cloud.enabled else 'OFF','ok' if cloud.enabled else '')}</div><div class='meter'><i style='width:{project_pct:.2f}%'></i></div><form method='post' action='/project/{esc(project)}/cloud'><label><input style='width:auto' type='checkbox' name='enabled' {'checked' if cloud.enabled else ''}> consenti escalation OpenAI</label><label>Cap progetto €</label><input name='budget' type='number' step='0.01' min='0' value='{cloud.budget_eur:.2f}'><label>Soglia priorità</label><input name='priority_threshold' type='number' step='0.1' min='0' value='{cloud.priority_threshold:g}'><button>Salva policy progetto</button></form><p class='small muted'>API key: {'configurata' if budget['api_key_configured'] else 'non configurata'}. Chiamate questo mese: {budget['monthly_calls']}.</p></div></div></section>
<section class='pane' data-pane='runtime'><div class='grid'><div class='col-7 card hero'><h2>Job autonomo</h2><p>Stato: <b>{esc(job_status)}</b>{' · '+str(job['steps_done'])+' iterazioni' if job else ''}</p><p class='small muted'>{len(sources)} fonte/i di partenza collegate. Se presenti, i relativi task hanno priorità sul piano generico.</p>{controls}</div><div class='col-5 card'><h2>Collegamenti</h2><p><a href='{_cross_link(request,'AWB_OBSERVER_PORT',8101,f'/project/{project}')}'>Apri Dashboard live →</a></p><p><a href='{_cross_link(request,'AWB_LAB_PORT',8102,f'/?project={project}')}'>Apri Research Lab →</a></p><p class='small muted'>Setup, monitoraggio e laboratorio sono separati per evitare un'unica pagina tecnica troppo densa.</p></div></div></section>
"""
    catalog_url = f'/project/{esc(project)}/model-catalog'
    script = f"""
function openTab(name){{
  const tabs=[...document.querySelectorAll('.tab')], panes=[...document.querySelectorAll('.pane')];
  if(!tabs.some(x=>x.dataset.tab===name))return;
  tabs.forEach(x=>x.classList.toggle('active',x.dataset.tab===name));
  panes.forEach(x=>x.classList.toggle('active',x.dataset.pane===name));
}}
const requestedTab=new URLSearchParams(window.location.search).get('tab'); if(requestedTab)openTab(requestedTab);
async function refreshModels(){{
  try{{
    const r=await fetch('{catalog_url}',{{cache:'no-store'}}); const data=await r.json();
    document.querySelectorAll('.model-select').forEach(function(sel){{
      const role=sel.dataset.role; const nodeSel=document.querySelector('.node-select[data-role="'+role+'"]');
      if(!nodeSel)return; const node=data.nodes[nodeSel.value]||{{models:[]}}; const current=sel.value;
      sel.innerHTML=''; const models=[...(node.models||[])]; if(!models.length)models.push(current||'auto');
      models.forEach(function(m){{const o=document.createElement('option');o.value=m;o.textContent=m;if(m===current)o.selected=true;sel.appendChild(o);}});
    }});
  }}catch(e){{}}
}}
document.querySelectorAll('.node-select').forEach(x=>x.addEventListener('change',refreshModels));refreshModels();
"""
    return shell(f'{ws.manifest.name} — Setup', body, active='setup', extra_script=script)


@control_app.get('/project/{project}', response_class=HTMLResponse)
def project_page(request: Request, project: str):
    return HTMLResponse(_project_body(request, project), headers={'Cache-Control': 'no-store'})


@control_app.get('/project/{project}/model-catalog')
def model_catalog(project: str):
    ws = load_workspace(_root(project))
    return JSONResponse(catalog_for_manifest(ws.manifest))


@control_app.post('/project/{project}/goal')
def set_goal(project: str, goal: str = Form(...)):
    ws = load_workspace(_root(project)); ws.manifest.goal = goal.strip(); save_manifest(ws)
    return RedirectResponse(f'/project/{project}', 303)


@control_app.post('/project/{project}/sources')
async def upload_sources(project: str, files: list[UploadFile] = File(...)):
    root = _root(project)
    if not files or len(files) > 20:
        raise HTTPException(400, 'Select between 1 and 20 source files per upload')
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
        Ledger(root / 'ledger.sqlite3').event('source_uploaded', {
            'id': item.get('id'),
            'filename': item.get('filename'),
            'sha256': item.get('sha256'),
            'bytes': item.get('bytes'),
            'text_chars': item.get('text_chars'),
            'status': item.get('status'),
        })
        uploaded += 1
    if not uploaded:
        raise HTTPException(400, 'No valid source file was uploaded')
    return RedirectResponse(f'/project/{project}?tab=sources', 303)


@control_app.post('/project/{project}/task')
def add_task(project: str, title: str = Form(...), description: str = Form(''), priority: float = Form(10.0)):
    root = _root(project); ledger = Ledger(root / 'ledger.sqlite3')
    task = Task(id=f'USER-{len(ledger.list_tasks())+1:04d}', title=title.strip(), description=(description.strip() or title.strip()), priority=float(priority), created_by='user')
    ledger.upsert_task(task); ledger.event('task_created', task.model_dump(mode='json'), task.id)
    return RedirectResponse(f'/project/{project}?tab=plan', 303)


@control_app.post('/project/{project}/node')
def save_node(project: str, node_id: str = Form(...), kind: str = Form(...), base_url: str = Form(''), max_concurrency: int = Form(1)):
    if kind not in {'ollama','lmstudio'}:
        raise HTTPException(400, 'Unsupported node type')
    ws = load_workspace(_root(project)); nid = slug(node_id)
    if not nid:
        raise HTTPException(400, 'Invalid node id')
    spec = ComputeNodeSpec(id=nid, kind=kind, base_url=base_url.strip() or None, enabled=True, max_concurrency=max(1,min(int(max_concurrency),8)), priority=100, tags=['local' if kind in {'ollama','lmstudio'} else 'external'])
    nodes = [n for n in ws.manifest.runtime.compute_nodes if n.id != nid]; nodes.append(spec); ws.manifest.runtime.compute_nodes = nodes; save_manifest(ws)
    return RedirectResponse(f'/project/{project}?tab=team', 303)


@control_app.post('/project/{project}/route')
def save_route(project: str, role: str = Form(...), node: str = Form(...), model: str = Form(...)):
    if role not in ROLES:
        raise HTTPException(400, 'Unknown role')
    ws = load_workspace(_root(project))
    if node not in {n.id for n in ws.manifest.runtime.compute_nodes if n.enabled}:
        raise HTTPException(400, 'Unknown compute node')
    ws.manifest.runtime.role_routes[role] = [ModelRouteSpec(node=node, model=model.strip() or None, priority=100, enabled=True)]
    save_manifest(ws)
    return RedirectResponse(f'/project/{project}?tab=team', 303)


@control_app.post('/project/{project}/cloud-role')
def save_cloud_role(project: str, role: str = Form(...), model: str = Form(...), reasoning: str = Form('medium')):
    if role not in ROLES or model not in OPENAI_MODELS or reasoning not in {'low','medium','high'}:
        raise HTTPException(400, 'Invalid cloud role configuration')
    root = _root(project); control = load_control(root)
    control.role_models[role] = {'model': model, 'reasoning': reasoning}
    save_control(root, control)
    return RedirectResponse(f'/project/{project}?tab=team', 303)


@control_app.post('/project/{project}/cloud')
def save_cloud(project: str, enabled: str | None = Form(None), budget: float = Form(5.0), priority_threshold: float = Form(1.0)):
    root = _root(project); current = load_control(root)
    current.enabled = enabled is not None
    current.budget_eur = max(0.0, float(budget))
    current.priority_threshold = max(0.0, float(priority_threshold))
    save_control(root, current)
    return RedirectResponse(f'/project/{project}?tab=api', 303)


@control_app.post('/project/{project}/monthly-budget')
def save_monthly(project: str, budget: float = Form(...), enabled: str | None = Form(None), hard_stop: str | None = Form(None)):
    root = _root(project)
    save_global_control(root, GlobalCloudControl(enabled=enabled is not None, monthly_budget_eur=max(0.0,float(budget)), hard_stop=hard_stop is not None))
    return RedirectResponse(f'/project/{project}?tab=api', 303)


@control_app.post('/project/{project}/launch')
def launch(project: str):
    root = _root(project)
    _ensure_all_source_tasks(root)
    ledger = Ledger(root / 'ledger.sqlite3'); current = ledger.latest_job()
    if current and current['status'] in {JobStatus.RUNNING.value, JobStatus.PAUSED.value}:
        jid = current['id']
    else:
        jid = ledger.create_job(0, 0, continuous=True)
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='autonomous project active')
    _start(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/pause')
def pause(project: str, jid: str):
    Ledger(_root(project) / 'ledger.sqlite3').update_job(jid, status=JobStatus.PAUSED, detail='paused by user')
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/resume')
def resume(project: str, jid: str):
    root = _root(project); ledger = Ledger(root / 'ledger.sqlite3')
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='resumed by user'); _start(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/cancel')
def cancel(project: str, jid: str):
    Ledger(_root(project) / 'ledger.sqlite3').update_job(jid, status=JobStatus.CANCEL_REQUESTED, detail='cancel requested by user')
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)
