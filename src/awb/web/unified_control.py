from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from awb.core.cloud_budget import budget_snapshot, load_control, save_control
from awb.core.model_catalog import OPENAI_MODELS, catalog_for_manifest
from awb.core.models import JobStatus, ModelRouteSpec, TaskStatus
from awb.core.resource_policy import (
    ProjectResourcePolicy,
    SystemResourcePolicy,
    admission_check,
    bind_project_resource_env,
    load_project_policy,
    load_system_policy,
    resource_summary,
    save_project_policy,
    save_system_policy,
)
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest
from awb.providers.runtime_progress import get_progress
from awb.web.app import base_dir
from awb.web.control_app import _route_for
from awb.web.control_v3 import _load_setup_state, _root
from awb.web.resource_monitor import _resource_snapshot
from awb.web.ui import badge, esc, shell


_INSTALLED = False


def _remove_route(app: FastAPI, path: str, method: str) -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and method in (getattr(route, 'methods', None) or set())
        )
    ]


def _safe_text(value: object, limit: int = 500) -> str:
    text = str(value or '').replace('\n', ' ').strip()
    return text if len(text) <= limit else text[: limit - 1] + '…'


def _task_ui_state(task, job_status: str, current_id: str | None) -> str:
    status = task.status.value
    meta = task.metadata or {}
    execution_attempts = int(meta.get('execution_attempts', 0))
    if status in {TaskStatus.DONE.value, TaskStatus.REJECTED.value}:
        return 'CLOSED'
    if job_status == JobStatus.FAILED.value and (
        task.id == current_id or status in {TaskStatus.ERROR.value, TaskStatus.IN_PROGRESS.value}
    ):
        return 'FAILED'
    if task.id == current_id and job_status == JobStatus.RUNNING.value:
        return 'IN PROGRESS'
    if status == TaskStatus.OPEN.value and execution_attempts == 0:
        return 'OPEN'
    return 'PAUSED'


def _status_kind(status: str) -> str:
    if status in {'CLOSED', 'COMPLETE'}:
        return 'ok'
    if status in {'RUNNING', 'IN PROGRESS', 'SETTING UP'}:
        return 'live'
    if status in {'FAILED'}:
        return 'bad'
    if status in {'PAUSED', 'CANCELLED'}:
        return 'warn'
    return ''


def _human_event(event: dict[str, Any], titles: dict[str, str]) -> dict[str, str]:
    kind = str(event.get('kind') or '')
    payload = event.get('payload') if isinstance(event.get('payload'), dict) else {}
    task_id = str(event.get('task_id') or '')
    title = titles.get(task_id, task_id or 'progetto')
    label = {
        'task_created': 'Creato un nuovo task',
        'work_output': 'Worker: candidato prodotto',
        'review': 'Reviewer: revisione completata',
        'verification': 'Verifier: controllo completato',
        'task_blocked': 'Reviewer: task da correggere',
        'task_rework_queued': 'Rework: stesso task rimesso in lavorazione',
        'task_dependencies_resolved': 'Prerequisiti completati: ritorno al task',
        'task_done': 'Task chiuso',
        'task_rejected': 'Task chiuso con esito negativo',
        'job_recoverable_error': 'Errore tecnico recuperabile',
        'job_waiting_for_model_capacity': 'In attesa della capacità del modello',
        'project_resumed_from_ledger': 'Progetto ripreso dal checkpoint',
        'checkpoint_pause_requested': 'Pausa sicura richiesta',
        'cloud_mode_changed': 'Modalità API modificata',
        'tool_call': 'Strumento eseguito',
        'tool_result': 'Risultato strumento acquisito',
    }.get(kind, kind.replace('_', ' ').strip().capitalize() or 'Aggiornamento')

    detail = ''
    for key in ('detail', 'strategy', 'rationale', 'error', 'reason', 'message'):
        if payload.get(key):
            detail = _safe_text(payload.get(key), 280)
            break
    if not detail and payload.get('critical_objections'):
        detail = f"{len(payload.get('critical_objections') or [])} obiezioni critiche"
    if not detail and payload.get('approved') is not None:
        detail = 'approvato' if payload.get('approved') else 'non approvato'
    if not detail:
        detail = _safe_text(title, 220)
    return {'ts': str(event.get('ts') or ''), 'label': label, 'detail': detail, 'task_id': task_id}


def _project_state(project: str) -> dict[str, Any]:
    root = _root(project)
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.reconcile_task_counters()
    tasks = ledger.list_tasks()
    job = ledger.latest_job() or {'status': 'NOT STARTED', 'detail': ''}
    job_status = str(job.get('status') or 'NOT STARTED')
    current = next((task for task in tasks if task.status == TaskStatus.IN_PROGRESS), None)
    focused = next(
        (
            task for task in tasks
            if bool((task.metadata or {}).get('focus_chain_active'))
            and task.status not in {TaskStatus.DONE, TaskStatus.REJECTED}
        ),
        None,
    )
    current_id = current.id if current else None
    task_rows = []
    for task in tasks:
        meta = task.metadata or {}
        task_rows.append({
            'id': task.id,
            'title': task.title,
            'description': task.description,
            'engine_status': task.status.value,
            'status': _task_ui_state(task, job_status, current_id),
            'created_by': task.created_by,
            'priority': task.priority,
            'scientific_attempts': int(meta.get('scientific_attempts', meta.get('attempts', 0))),
            'technical_failures': int(meta.get('technical_failures', 0)),
            'execution_attempts': int(meta.get('execution_attempts', 0)),
            'phase': str(meta.get('lifecycle_phase') or ''),
            'focus': bool(meta.get('focus_chain_active', False)),
            'depends_on': list(meta.get('depends_on') or []),
            'verification_contract': dict(meta.get('verification_contract') or {}),
            'critical_objections': list(meta.get('critical_objections') or [])[:8],
            'next_strategy': _safe_text(meta.get('next_strategy'), 500),
        })
    titles = {row['id']: row['title'] for row in task_rows}
    events = [_human_event(event, titles) for event in ledger.recent_events(12)]
    progress = get_progress(job_id=str(job.get('id') or '')) or {}
    host = _resource_snapshot()
    allocation = load_project_policy(root)
    system = resource_summary(root.parent)
    setup = _load_setup_state(root)
    budget = budget_snapshot(root)

    overall = job_status
    if setup.get('status') in {'RUNNING', 'QUEUED'} and job_status not in {JobStatus.RUNNING.value, JobStatus.PAUSED.value}:
        overall = 'SETTING UP'
    if job_status == JobStatus.COMPLETE.value:
        overall = 'COMPLETE'
    elif job_status == JobStatus.CANCELLED.value:
        overall = 'PAUSED'

    active_task = current or focused
    return {
        'project': project,
        'name': ws.manifest.name,
        'goal': ws.manifest.goal,
        'overall_status': overall,
        'job': job,
        'setup': setup,
        'current_task': next((row for row in task_rows if active_task and row['id'] == active_task.id), None),
        'actual_current_task_id': current_id,
        'tasks': task_rows,
        'events': events,
        'runtime_progress': progress,
        'resources': host,
        'allocation': {
            'cpu_cores': allocation.cpu_cores,
            'ram_gb': allocation.ram_gb,
            'context_tokens': allocation.context_tokens,
            'max_tool_calls_per_task': allocation.max_tool_calls_per_task,
        },
        'system_resources': system,
        'budget': budget,
    }


def _project_controls(project: str, state: dict[str, Any]) -> str:
    job = state.get('job') or {}
    status = str(job.get('status') or 'NOT STARTED')
    jid = str(job.get('id') or '')
    if status == JobStatus.RUNNING.value:
        return (
            f"<div class='row'>{badge('RUNNING','live')}"
            f"<form method='post' action='/project/{esc(project)}/job/{esc(jid)}/pause'><button class='secondary'>Pausa</button></form>"
            f"<form method='post' action='/project/{esc(project)}/checkpoint'><button class='secondary'>Checkpoint</button></form>"
            f"<form method='post' action='/project/{esc(project)}/job/{esc(jid)}/cancel'><button class='danger'>Ferma</button></form></div>"
        )
    if status == JobStatus.PAUSED.value:
        return (
            f"<div class='row'>{badge('PAUSED','warn')}"
            f"<form method='post' action='/project/{esc(project)}/job/{esc(jid)}/resume'><button>Riprendi</button></form>"
            f"<form method='post' action='/project/{esc(project)}/job/{esc(jid)}/cancel'><button class='danger'>Termina</button></form></div>"
        )
    return f"<form method='post' action='/project/{esc(project)}/launch'><button>{'Riprendi dal ledger' if jid else 'Avvia progetto'}</button></form>"


def _dashboard_page(project: str) -> HTMLResponse:
    state = _project_state(project)
    refresh = int(load_system_policy(base_dir()).dashboard_refresh_seconds)
    initial = json.dumps(state, ensure_ascii=False).replace('</', '<\\/')
    controls = _project_controls(project, state)
    body = f"""
<div class='topbar'>
  <div><a class='small muted' href='/'>← tutti i progetti</a><h1>{esc(state['name'])}</h1><p class='sub'>{esc(state['goal'])}</p></div>
  <div class='nav'><a href='/project/{esc(project)}/setup'>Setup</a></div>
</div>
<div class='grid'>
  <div class='col-8 card hero'>
    <div class='split'><div><h2>Adesso</h2><div id='currentTitle' style='font-size:22px;font-weight:800'>—</div><div id='currentDetail' class='muted'></div></div><span id='overallBadge' class='badge'>—</span></div>
    <div id='currentMeta' class='small muted' style='margin-top:10px'></div>
    <pre id='liveOutput' class='output' style='max-height:240px;margin-top:12px'>Nessun output pubblico disponibile.</pre>
    <div class='small muted' style='margin-top:8px'>Aggiornamento automatico ogni {refresh}s, senza ricaricare la pagina.</div>
  </div>
  <div class='col-4 card'>
    <div class='split'><h2>Risorse</h2><span id='resourceMode' class='badge'>quota progetto</span></div>
    <div class='kpis' style='grid-template-columns:repeat(2,minmax(0,1fr))'>
      <div class='kpi'><span>CPU ACEPC</span><b id='hostCpu'>—</b></div>
      <div class='kpi'><span>RAM ACEPC</span><b id='hostRam' style='font-size:14px'>—</b></div>
      <div class='kpi'><span>CPU assegnata</span><b id='allocCpu'>—</b></div>
      <div class='kpi'><span>RAM assegnata</span><b id='allocRam'>—</b></div>
      <div class='kpi'><span>Modello / ruolo</span><b id='modelRole' style='font-size:13px'>—</b></div>
      <div class='kpi'><span>Token</span><b id='tokens' style='font-size:13px'>—</b></div>
    </div>
    <div style='margin-top:14px'>{controls}</div>
  </div>
  <div class='col-5 card'><h2>Ultime attività</h2><div id='events' class='timeline'></div></div>
  <div class='col-7 card'><div class='split'><div><h2>Task</h2><p class='small muted'>OPEN = mai iniziato · CLOSED = risolto · IN PROGRESS = il motore è qui · PAUSED = iniziato ma non attivo · FAILED = errore fatale che ha fermato il progetto.</p></div><span id='taskCount' class='badge'>—</span></div><div id='tasks'></div></div>
</div>
"""
    script = f"""
const PROJECT={json.dumps(project)}; const ENDPOINT='/project/'+encodeURIComponent(PROJECT)+'/live'; const REFRESH={refresh * 1000};
let state={initial};
function h(v){{return String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));}}
function fmtGB(n){{return n==null?'—':Number(n).toFixed(1)+' GB';}}
function taskClass(s){{return s==='CLOSED'?'done':s==='IN PROGRESS'?'live':s==='FAILED'?'bad':'';}}
function badgeClass(s){{return s==='CLOSED'||s==='COMPLETE'?'ok':s==='RUNNING'||s==='IN PROGRESS'||s==='SETTING UP'?'live':s==='FAILED'?'bad':s==='PAUSED'?'warn':'';}}
function render(s){{
  state=s; const p=s.runtime_progress||{{}}; const c=s.current_task; const setup=s.setup||{{}};
  const overall=String(s.overall_status||'NOT STARTED'); const ob=document.getElementById('overallBadge'); ob.textContent=overall; ob.className='badge '+badgeClass(overall);
  if(c){{document.getElementById('currentTitle').textContent=c.title; const phase=c.phase||p.role||c.status; document.getElementById('currentDetail').textContent='Task '+c.id+' · '+phase;}}
  else if(['RUNNING','QUEUED'].includes(setup.status)){{document.getElementById('currentTitle').textContent='Configurazione iniziale';document.getElementById('currentDetail').textContent=(setup.stage||'')+' · '+(setup.detail||'');}}
  else if(overall==='RUNNING'){{document.getElementById('currentTitle').textContent='Director: selezione del prossimo micro-task';document.getElementById('currentDetail').textContent='Il motore sta pianificando il prossimo passo verificabile.';}}
  else{{document.getElementById('currentTitle').textContent='Nessun task attivo';document.getElementById('currentDetail').textContent=(s.job||{{}}).detail||'';}}
  const meta=[]; if(p.role)meta.push('agente '+p.role); if(p.model)meta.push('modello '+p.model); if(p.state)meta.push(p.state); if(p.elapsed_seconds!=null)meta.push(Math.round(Number(p.elapsed_seconds))+'s'); if(p.chunks!=null)meta.push(p.chunks+' chunk'); document.getElementById('currentMeta').textContent=meta.join(' · ');
  document.getElementById('liveOutput').textContent=p.visible_tail||'Nessun output pubblico disponibile in questo momento.';
  const r=s.resources||{{}}, a=s.allocation||{{}}; document.getElementById('hostCpu').textContent=r.cpu_percent==null?'—':Math.round(Number(r.cpu_percent))+'%'; document.getElementById('hostRam').textContent=r.ram_total?fmtGB(Number(r.ram_used)/1073741824)+' / '+fmtGB(Number(r.ram_total)/1073741824):'—'; document.getElementById('allocCpu').textContent=(a.cpu_cores??'—')+' core'; document.getElementById('allocRam').textContent=fmtGB(a.ram_gb); document.getElementById('modelRole').textContent=(p.role?p.role+' · ':'')+(p.model||r.model||'—'); document.getElementById('tokens').textContent=(p.prompt_tokens??0)+' in / '+(p.output_tokens??0)+' out';
  const ev=document.getElementById('events'); ev.innerHTML=(s.events||[]).slice(0,7).map(e=>'<div class="event"><b>'+h(e.label)+'</b><span>'+h(e.detail)+'</span></div>').join('')||'<div class="empty">Nessun evento recente.</div>';
  const order={{'IN PROGRESS':0,'FAILED':1,'PAUSED':2,'OPEN':3,'CLOSED':4}}; const tasks=[...(s.tasks||[])].sort((x,y)=>(order[x.status]??9)-(order[y.status]??9)); document.getElementById('taskCount').textContent=tasks.length+' task'; document.getElementById('tasks').innerHTML=tasks.map(t=>'<div class="task '+taskClass(t.status)+'"><div class="split"><div><b>'+h(t.title)+'</b><div class="small muted">'+h(t.id)+(t.phase?' · '+h(t.phase):'')+(t.focus?' · focus':'')+'</div></div><span class="badge '+badgeClass(t.status)+'">'+h(t.status)+'</span></div>'+(t.status!=='CLOSED'&&t.next_strategy?'<div class="small muted" style="margin-top:5px">Prossima strategia: '+h(t.next_strategy)+'</div>':'')+'</div>').join('')||'<div class="empty">Nessun task.</div>';
}}
async function refresh(){{try{{const res=await fetch(ENDPOINT,{{cache:'no-store'}});if(res.ok)render(await res.json());}}catch(e){{}}}}
render(state); setInterval(refresh,REFRESH); setTimeout(refresh,1500);
"""
    return HTMLResponse(shell(f"{state['name']} — Dashboard", body, script=script), headers={'Cache-Control': 'no-store'})


def _setup_page(project: str) -> HTMLResponse:
    root = _root(project)
    ws = load_workspace(root)
    policy = load_project_policy(root)
    system = load_system_policy(root.parent)
    cloud = load_control(root)
    budget = budget_snapshot(root)
    setup = _load_setup_state(root)
    try:
        catalog = catalog_for_manifest(ws.manifest)
    except Exception:
        catalog = {'nodes': {}, 'openai': {'models': OPENAI_MODELS}}

    agent_cards = []
    for agent in ws.manifest.agents:
        role = agent.role
        route = _route_for(ws, role)
        remote = (cloud.role_models or {}).get(role, {})
        nodes = ''.join(
            f"<option value='{esc(node.id)}' {'selected' if node.id == route.node else ''}>{esc(node.id)} · {esc(node.kind)}</option>"
            for node in ws.manifest.runtime.compute_nodes if node.enabled
        )
        local_models = list((catalog.get('nodes') or {}).get(route.node, {}).get('models') or [])
        if route.model and route.model not in local_models:
            local_models.insert(0, route.model)
        local_options = ''.join(
            f"<option value='{esc(model)}' {'selected' if model == route.model else ''}>{esc(model)}</option>"
            for model in local_models
        ) or f"<option value='{esc(route.model or '')}'>{esc(route.model or 'auto')}</option>"
        remote_model = str(remote.get('model') or OPENAI_MODELS[0])
        remote_options = ''.join(
            f"<option value='{esc(model)}' {'selected' if model == remote_model else ''}>{esc(model)}</option>"
            for model in OPENAI_MODELS
        )
        reasoning = str(remote.get('reasoning') or 'medium')
        agent_cards.append(f"""
<div class='agent'>
<form method='post' action='/project/{esc(project)}/setup/agent'>
<input type='hidden' name='role' value='{esc(role)}'>
<div class='split'><h3>{esc(agent.id)}</h3>{badge(role)}</div>
<label>Istruzioni del ruolo</label><textarea name='instructions' rows='4'>{esc(agent.instructions)}</textarea>
<div class='grid'><div class='col-4'><label>Nodo locale</label><select name='node'>{nodes}</select></div><div class='col-4'><label>Modello locale</label><select name='local_model'>{local_options}</select></div><div class='col-4'><label>Modello remoto</label><select name='remote_model'>{remote_options}</select></div></div>
<label>Reasoning remoto</label><select name='reasoning'>{''.join(f"<option value='{x}' {'selected' if x==reasoning else ''}>{x}</option>" for x in ('low','medium','high'))}</select>
<button class='secondary' style='margin-top:9px'>Salva agente e routing</button>
</form></div>""")

    gates = ''.join(f"<div class='task'><b>{esc(g.id)}</b><div class='small muted'>{esc(g.description)}</div></div>" for g in ws.manifest.gates)
    mode = 'force' if cloud.enabled and cloud.mode == 'force' else 'paused'
    body = f"""
<div class='topbar'><div><a class='small muted' href='/project/{esc(project)}'>← dashboard</a><h1>Setup · {esc(ws.manifest.name)}</h1><p class='sub'>Configurazione del progetto. Le modifiche alle risorse locali valgono dalla prossima chiamata modello.</p></div></div>
<div class='grid'>
  <div class='col-8 card hero'><div class='split'><h2>North Star</h2>{badge(str(setup.get('status') or 'NOT_STARTED'),'ok' if setup.get('status')=='READY' else 'warn')}</div><form method='post' action='/project/{esc(project)}/setup/goal'><textarea name='goal' rows='6'>{esc(ws.manifest.goal)}</textarea><button>Salva North Star</button></form><p class='small muted'>{esc(setup.get('detail') or '')}</p></div>
  <div class='col-4 card'><h2>Risorse del progetto</h2><p class='small muted'>Le quote dei progetti RUNNING si sommano e non possono superare il limite Expert: {system.cpu_cores:g} CPU / {system.ram_gb:g} GB RAM.</p><form method='post' action='/project/{esc(project)}/setup/resources'><label>CPU assegnata</label><input type='number' name='cpu_cores' step='0.1' min='0.5' max='{system.cpu_cores}' value='{policy.cpu_cores}'><label>RAM riservata (GB)</label><input type='number' name='ram_gb' step='0.1' min='1' max='{system.ram_gb}' value='{policy.ram_gb}'><label>Context locale (token)</label><input type='number' name='context_tokens' step='512' min='1024' value='{policy.context_tokens}'><label>Tool call max per micro-task</label><input type='number' name='max_tool_calls' min='1' max='500' value='{policy.max_tool_calls_per_task}'><button style='margin-top:9px'>Salva risorse</button></form><p class='small muted'>CPU/context sono applicati alla prossima generazione. La RAM è una quota di ammissione del progetto; il modello locale è condiviso e ha overhead comune.</p></div>
  <div class='col-12 card'><div class='split'><div><h2>Agenti e modelli</h2><p class='small muted'>Ruoli logici sequenziali; sull'ACEPC il modello locale resta uno alla volta.</p></div></div>{''.join(agent_cards)}</div>
  <div class='col-6 card'><h2>Piano API</h2><div class='split'><b>€{float(budget.get('spent_eur') or 0):.4f} / €{float(budget.get('budget_eur') or 0):.2f}</b>{badge('API SBLOCCATA' if mode=='force' else 'SOLO LOCALE','warn' if mode=='force' else 'ok')}</div><form method='post' action='/project/{esc(project)}/setup/api'><label>Modalità</label><select name='mode'><option value='paused' {'selected' if mode=='paused' else ''}>Solo locale</option><option value='force' {'selected' if mode=='force' else ''}>API sbloccata manualmente</option></select><label>Cap progetto €</label><input name='budget' type='number' step='0.01' min='0' value='{float(cloud.budget_eur):.2f}'><label>Soglia priorità</label><input name='priority_threshold' type='number' step='0.1' min='0' value='{float(cloud.priority_threshold):.1f}'><button style='margin-top:9px'>Salva piano API</button></form></div>
  <div class='col-6 card'><h2>Definition of Done</h2>{gates or '<div class="empty">Nessuna condizione.</div>'}</div>
</div>
"""
    return HTMLResponse(shell(f"{ws.manifest.name} — Setup", body), headers={'Cache-Control': 'no-store'})


def install_unified_control(app: FastAPI, runtime_module) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Bind per-project resource settings inside every future autonomous child.
    original_run = runtime_module._run_continuous
    if not getattr(original_run, '_awb_resource_bound', False):
        def resource_bound_run(root: Path, job_id: str):
            bind_project_resource_env(root)
            return original_run(root, job_id)
        resource_bound_run._awb_resource_bound = True  # type: ignore[attr-defined]
        runtime_module._run_continuous = resource_bound_run

    for path, method in (
        ('/', 'GET'),
        ('/project/{project}', 'GET'),
        ('/project/{project}/launch', 'POST'),
        ('/project/{project}/job/{jid}/resume', 'POST'),
    ):
        _remove_route(app, path, method)

    @app.get('/', response_class=HTMLResponse)
    def unified_index(request: Request):
        del request
        system = load_system_policy(base_dir())
        summary = resource_summary(base_dir())
        host = _resource_snapshot()
        cards = []
        for root in sorted(base_dir().iterdir()):
            if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
                continue
            try:
                state = _project_state(root.name)
            except Exception:
                continue
            current = state.get('current_task') or {}
            allocation = state.get('allocation') or {}
            cards.append(
                f"<a class='card linkcard' href='/project/{esc(root.name)}'><div class='split'><div><h2>{esc(state['name'])}</h2><div class='small muted'>{esc(current.get('title') or 'Nessun task attivo')}</div></div>{badge(state['overall_status'],_status_kind(state['overall_status']))}</div><div class='small muted' style='margin-top:8px'>{allocation.get('cpu_cores','—')} CPU · {allocation.get('ram_gb','—')} GB RAM · {len(state.get('tasks') or [])} task</div></a>"
            )
        body = f"""
<div class='topbar'><div><h1>Expert My Rules</h1><p class='sub'>Progetti e risorse dell'ACEPC in un solo punto.</p></div></div>
<div class='grid'>
 <div class='col-7'><h2>Progetti</h2><div class='grid'>{''.join(f"<div class='col-12'>{card}</div>" for card in cards) if cards else '<div class="col-12 empty">Nessun progetto.</div>'}</div></div>
 <div class='col-5 card hero'><h2>Risorse Expert My Rules</h2><div class='kpis' style='grid-template-columns:repeat(2,minmax(0,1fr))'><div class='kpi'><span>CPU ACEPC ora</span><b>{round(float(host.get('cpu_percent') or 0))}%</b></div><div class='kpi'><span>RAM ACEPC ora</span><b style='font-size:14px'>{float(host.get('ram_percent') or 0):.0f}%</b></div><div class='kpi'><span>CPU riservata progetti</span><b>{summary['cpu_reserved']:.1f}/{system.cpu_cores:.1f}</b></div><div class='kpi'><span>RAM riservata progetti</span><b>{summary['ram_reserved']:.1f}/{system.ram_gb:.1f}</b></div></div><form method='post' action='/system/resources'><label>CPU max Expert (core)</label><input name='cpu_cores' type='number' min='0.5' step='0.1' value='{system.cpu_cores}'><label>RAM max Expert (GB)</label><input name='ram_gb' type='number' min='1' step='0.1' value='{system.ram_gb}'><label>Progetti RUNNING simultanei</label><input name='max_running_projects' type='number' min='1' max='8' value='{system.max_running_projects}'><label>Context locale default</label><input name='default_context_tokens' type='number' min='1024' step='512' value='{system.default_context_tokens}'><label>Refresh dashboard (secondi)</label><input name='refresh_seconds' type='number' min='10' max='300' value='{system.dashboard_refresh_seconds}'><button style='margin-top:9px'>Salva limiti Expert</button></form><p class='small muted'>Le quote dei progetti RUNNING si sommano. Il motore locale resta seriale: due progetti possono essere attivi, ma le generazioni pesanti condividono Ollama.</p></div>
 <div class='col-12 card'><h2>Nuovo progetto</h2><form method='post' action='/create' enctype='multipart/form-data'><div class='grid'><div class='col-8'><label>North Star</label><textarea name='goal' rows='4' required placeholder='Cosa deve esistere quando il progetto è davvero finito?'></textarea></div><div class='col-4'><label>Nome opzionale</label><input name='name' placeholder='generato automaticamente'><label>Materiale iniziale opzionale</label><input type='file' name='files' multiple><button style='margin-top:9px'>Crea progetto</button></div></div></form></div>
</div>
"""
        return HTMLResponse(shell('Expert My Rules — Progetti', body), headers={'Cache-Control': 'no-store'})

    @app.post('/system/resources')
    def save_system_resources(
        cpu_cores: float = Form(...), ram_gb: float = Form(...),
        max_running_projects: int = Form(...), default_context_tokens: int = Form(...),
        refresh_seconds: int = Form(60),
    ):
        try:
            save_system_policy(base_dir(), SystemResourcePolicy(
                cpu_cores=cpu_cores,
                ram_gb=ram_gb,
                max_running_projects=max_running_projects,
                default_context_tokens=default_context_tokens,
                dashboard_refresh_seconds=refresh_seconds,
            ))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse('/', status_code=303)

    @app.get('/system/live')
    def system_live():
        return JSONResponse({'allocations': resource_summary(base_dir()), 'resources': _resource_snapshot()}, headers={'Cache-Control': 'no-store'})

    @app.get('/project/{project}', response_class=HTMLResponse)
    def unified_project(project: str):
        return _dashboard_page(project)

    @app.get('/project/{project}/live')
    def unified_project_live(project: str):
        return JSONResponse(_project_state(project), headers={'Cache-Control': 'no-store'})

    @app.get('/project/{project}/setup', response_class=HTMLResponse)
    def unified_setup(project: str):
        return _setup_page(project)

    @app.post('/project/{project}/setup/goal')
    def setup_goal(project: str, goal: str = Form(...)):
        root = _root(project)
        ws = load_workspace(root)
        ws.manifest.goal = goal.strip()
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').event('north_star_changed', {'goal': goal.strip()[:500]})
        return RedirectResponse(f'/project/{project}/setup', status_code=303)

    @app.post('/project/{project}/setup/resources')
    def setup_resources(
        project: str, cpu_cores: float = Form(...), ram_gb: float = Form(...),
        context_tokens: int = Form(...), max_tool_calls: int = Form(...),
    ):
        root = _root(project)
        try:
            policy = save_project_policy(root, ProjectResourcePolicy(
                cpu_cores=cpu_cores,
                ram_gb=ram_gb,
                context_tokens=context_tokens,
                max_tool_calls_per_task=max_tool_calls,
            ))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        ws = load_workspace(root)
        ws.manifest.runtime.max_tool_calls_per_task = policy.max_tool_calls_per_task
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').event('project_resources_changed', {
            'cpu_cores': policy.cpu_cores, 'ram_gb': policy.ram_gb,
            'context_tokens': policy.context_tokens,
            'max_tool_calls_per_task': policy.max_tool_calls_per_task,
        })
        return RedirectResponse(f'/project/{project}/setup', status_code=303)

    @app.post('/project/{project}/setup/agent')
    def setup_agent(
        project: str, role: str = Form(...), instructions: str = Form(...), node: str = Form(...),
        local_model: str = Form(...), remote_model: str = Form(...), reasoning: str = Form('medium'),
    ):
        if remote_model not in OPENAI_MODELS or reasoning not in {'low', 'medium', 'high'}:
            raise HTTPException(400, 'Configurazione modello remoto non valida')
        root = _root(project)
        ws = load_workspace(root)
        agent = next((a for a in ws.manifest.agents if a.role == role), None)
        if agent is None:
            raise HTTPException(404, 'Ruolo non trovato')
        if node not in {n.id for n in ws.manifest.runtime.compute_nodes if n.enabled}:
            raise HTTPException(400, 'Nodo locale non valido')
        agent.instructions = instructions.strip()
        ws.manifest.runtime.role_routes[role] = [ModelRouteSpec(node=node, model=local_model.strip() or None, priority=100, enabled=True)]
        save_manifest(ws)
        cloud = load_control(root)
        cloud.role_models[role] = {'model': remote_model, 'reasoning': reasoning}
        save_control(root, cloud)
        Ledger(root / 'ledger.sqlite3').event('agent_setup_changed', {'role': role, 'node': node, 'local_model': local_model, 'remote_model': remote_model, 'reasoning': reasoning})
        return RedirectResponse(f'/project/{project}/setup', status_code=303)

    @app.post('/project/{project}/setup/api')
    def setup_api(project: str, mode: str = Form(...), budget: float = Form(...), priority_threshold: float = Form(...)):
        if mode not in {'paused', 'force'}:
            raise HTTPException(400, 'Modalità API non valida')
        root = _root(project)
        cloud = load_control(root)
        cloud.mode = mode
        cloud.enabled = mode == 'force'
        cloud.budget_eur = max(0.0, float(budget))
        cloud.priority_threshold = max(0.0, float(priority_threshold))
        save_control(root, cloud)
        Ledger(root / 'ledger.sqlite3').event('cloud_mode_changed', {'mode': mode, 'enabled': cloud.enabled, 'source': 'unified-setup'})
        return RedirectResponse(f'/project/{project}/setup', status_code=303)

    @app.post('/project/{project}/launch')
    def unified_launch(project: str):
        root = _root(project)
        current = Ledger(root / 'ledger.sqlite3').latest_job()
        if current and current['status'] == JobStatus.RUNNING.value:
            return RedirectResponse(f'/project/{project}', status_code=303)
        allowed, detail = admission_check(root)
        if not allowed:
            raise HTTPException(409, detail)
        return runtime_module.launch_runtime(project)

    @app.post('/project/{project}/job/{jid}/resume')
    def unified_resume(project: str, jid: str):
        root = _root(project)
        allowed, detail = admission_check(root)
        if not allowed:
            raise HTTPException(409, detail)
        return runtime_module.resume_runtime(project, jid)
