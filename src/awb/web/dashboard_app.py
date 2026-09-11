from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from awb.core.cloud_budget import budget_snapshot
from awb.core.models import TaskStatus
from awb.core.routing import ModelRouter
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace
from awb.providers.runtime_progress import get_progress
from awb.web.app import base_dir
from awb.web.resource_monitor import _resource_snapshot
from awb.web.ui import esc, shell


dashboard_app = FastAPI(title='Expert My Rules Live Dashboard')


def _root(project: str) -> Path:
    root = base_dir() / project
    if not (root / 'project.yaml').exists():
        raise HTTPException(404, 'Project not found')
    return root


def _cross_link(request: Request, port_env: str, default_port: int, path: str = '/') -> str:
    host = request.url.hostname or '127.0.0.1'
    return f"http://{host}:{int(os.getenv(port_env,str(default_port)))}{path}"


def _clean_payload(value: Any, depth: int = 0):
    if depth > 4:
        return '…'
    if isinstance(value, dict):
        return {str(k): _clean_payload(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_clean_payload(v, depth + 1) for v in value[:20]]
    if isinstance(value, str) and len(value) > 3000:
        return value[:3000] + '…'
    return value


def _setup_state(root: Path) -> dict[str, Any]:
    path = root / '.awb' / 'setup-state.json'
    if not path.exists():
        return {'status': 'NOT_STARTED', 'stage': 'IDLE', 'detail': '', 'elapsed_seconds': None}
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict):
            raise ValueError('not an object')
    except Exception:
        return {'status': 'ERROR', 'stage': 'ERROR', 'detail': 'Stato setup non leggibile', 'elapsed_seconds': None}
    if state.get('status') in {'RUNNING', 'QUEUED'}:
        raw = state.get('started_at') or state.get('queued_at')
        if raw:
            try:
                started = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                state['elapsed_seconds'] = max(0.0, (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds())
            except Exception:
                pass
    return state


def _state(project: str) -> dict[str, Any]:
    root = _root(project)
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.reconcile_task_counters()
    tasks = ledger.list_tasks()
    current = next((t for t in tasks if t.status == TaskStatus.IN_PROGRESS), None)
    job = ledger.latest_job()
    gate_state = ledger.gate_state()
    gates = [
        {
            'id': g.id,
            'description': g.description,
            'passed': bool(gate_state.get(g.id, {}).get('passed')),
            'detail': gate_state.get(g.id, {}).get('detail', ''),
        }
        for g in ws.manifest.gates
    ]
    task_rows = []
    for task in tasks:
        task_rows.append({
            'id': task.id,
            'title': task.title,
            'description': task.description,
            'status': task.status.value,
            'created_by': task.created_by,
            'priority': task.priority,
            'scientific_attempts': int(task.metadata.get('scientific_attempts', task.metadata.get('attempts', 0))),
            'technical_failures': int(task.metadata.get('technical_failures', 0)),
            'next_strategy': str(task.metadata.get('next_strategy') or ''),
            'last_error': str(task.metadata.get('last_error') or ''),
        })
    attempts = ledger.list_attempts(current.id, limit=8) if current else ledger.list_attempts(limit=8)
    latest_attempt = attempts[0] if attempts else None
    events = [_clean_payload(e) for e in ledger.recent_events(30)]
    progress = get_progress() or {}
    resources = _resource_snapshot()
    budget = budget_snapshot(root)
    setup = _setup_state(root)
    routes = {
        role: [r.model_dump(mode='json') for r in ws.manifest.runtime.role_routes.get(role, []) if r.enabled]
        for role in ('director','worker','reviewer','verifier')
    }
    total_gates = len(gates)
    passed_gates = sum(1 for g in gates if g['passed'])
    total_tasks = len(task_rows)
    done_tasks = sum(1 for t in task_rows if t['status'] == 'DONE')
    progress_pct = round((0.7 * (passed_gates / total_gates if total_gates else 0) + 0.3 * (done_tasks / total_tasks if total_tasks else 0)) * 100, 1)
    return {
        'project': project,
        'name': ws.manifest.name,
        'goal': ws.manifest.goal,
        'job': job,
        'setup': setup,
        'current_task': next((x for x in task_rows if current and x['id'] == current.id), None),
        'tasks': task_rows,
        'gates': gates,
        'passed_gates': passed_gates,
        'total_gates': total_gates,
        'done_tasks': done_tasks,
        'total_tasks': total_tasks,
        'progress_percent': progress_pct,
        'runtime_progress': _clean_payload(progress),
        'resources': resources,
        'budget': budget,
        'events': events,
        'latest_attempt': _clean_payload(latest_attempt),
        'model_calls': ledger.recent_model_calls(20),
        'model_stats': ledger.model_stats(),
        'routes': routes,
        'build_sha': os.getenv('AWB_BUILD_SHA', 'unknown'),
    }


@dashboard_app.get('/health')
def health():
    return {'ok': True}


@dashboard_app.get('/', response_class=HTMLResponse)
def index(request: Request):
    cards = []
    for root in sorted(base_dir().iterdir()):
        if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
            continue
        try:
            state = _state(root.name)
            setup = state.get('setup') or {}
            status = (state['job'] or {}).get('status', 'NOT STARTED')
            if setup.get('status') in {'RUNNING', 'QUEUED'}:
                status = 'SETTING UP'
            current = state['current_task']
            subtitle = current['title'] if current else (setup.get('detail') if setup.get('status') in {'RUNNING','QUEUED'} else 'Nessun task in esecuzione')
            cards.append(f"<a class='card linkcard' href='/project/{esc(root.name)}'><div class='split'><div><h2>{esc(state['name'])}</h2><div class='small muted'>{esc(subtitle)}</div></div><span class='badge {'live' if status in {'RUNNING','SETTING UP'} else ''}'>{esc(status)}</span></div><div class='meter'><i style='width:{state['progress_percent']}%'></i></div><div class='small muted' style='margin-top:7px'>{state['progress_percent']}% · {state['passed_gates']}/{state['total_gates']} gate</div></a>")
        except Exception:
            continue
    body = f"""<div class='topbar'><div><h1>Dashboard live</h1><p class='sub'>Solo ciò che serve per capire cosa sta facendo il sistema adesso.</p></div><div class='nav'><a href='{_cross_link(request,'AWB_PORT',8100)}'>← Progetti</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102)}'>Research Lab</a></div></div>{''.join(cards) if cards else "<div class='empty'>Nessun progetto attivo.</div>"}"""
    return HTMLResponse(shell('Expert My Rules — Live', body, active='dashboard'), headers={'Cache-Control':'no-store'})


@dashboard_app.get('/project/{project}/state')
def state(project: str):
    return JSONResponse(_state(project), headers={'Cache-Control':'no-store'})


@dashboard_app.get('/project/{project}', response_class=HTMLResponse)
def project(request: Request, project: str):
    root = _root(project)
    ws = load_workspace(root)
    body = f"""
<div class='topbar'><div><a class='small muted' href='/'>← dashboard</a><h1>{esc(ws.manifest.name)}</h1><p class='sub' id='northstar'>{esc(ws.manifest.goal)}</p></div><div class='nav'><a href='{_cross_link(request,'AWB_PORT',8100)}'>← Progetti</a><a href='{_cross_link(request,'AWB_PORT',8100,f'/project/{project}')}'>Setup progetto</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102,f'/?project={project}')}'>Research Lab</a></div></div>
<div class='grid'>
 <div class='col-8 card hero'><div class='split'><div><h2>Attività in corso</h2><div id='jobTitle' style='font-size:22px;font-weight:800'>Caricamento…</div><div class='muted' id='jobDetail'></div></div><span id='jobBadge' class='badge'>—</span></div><div class='meter' style='margin-top:14px'><i id='progressBar' style='width:0%'></i></div><div class='small muted' id='progressText' style='margin-top:6px'></div></div>
 <div class='col-4 card'><h2>ACEPC</h2><div class='kpis' style='grid-template-columns:repeat(2,minmax(0,1fr))'><div class='kpi'><span>CPU</span><b id='cpu'>—</b></div><div class='kpi'><span>RAM</span><b id='ram' style='font-size:13px'>—</b></div><div class='kpi'><span>Swap</span><b id='swap' style='font-size:13px'>—</b></div><div class='kpi'><span>Modello caricato</span><b id='model' style='font-size:13px'>—</b></div><div class='kpi'><span>Attività modello</span><b id='modelState' style='font-size:13px'>—</b></div><div class='kpi'><span>API mese</span><b id='apiCost' style='font-size:16px'>—</b></div></div></div>
 <div class='col-7 card'><div class='split'><div><h2>Cosa sta producendo</h2><p class='small muted'>Vedi task/setup corrente, tempo di elaborazione e output pubblico del modello.</p></div><span id='liveState' class='badge'>idle</span></div><div id='currentTask' class='empty'>Nessun task attivo.</div><button id='toggleOutput' class='secondary' type='button'>Mostra output live / ultimo tentativo</button><pre id='output' class='output hide'></pre></div>
 <div class='col-5 card'><h2>North Star progress</h2><div id='gates'></div></div>
 <div class='col-7 card'><h2>Piano dei task</h2><div id='tasks'></div></div>
 <div class='col-5 card'><h2>Attività recente</h2><div id='events' class='timeline'></div></div>
</div>
"""
    script = f"""
const endpoint='/project/{esc(project)}/state';let latest=null;
function h(v){{return String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));}}
function pct(v){{return Math.max(0,Math.min(100,Number(v)||0));}}
function fmtBytes(n){{if(!n)return '—';return (n/1073741824).toFixed(1)+' GB';}}
function fmtSeconds(v){{const n=Math.max(0,Number(v)||0);if(n<60)return Math.round(n)+' s';const m=Math.floor(n/60);if(m<60)return m+'m '+Math.round(n%60)+'s';return Math.floor(m/60)+'h '+(m%60)+'m';}}
function render(s){{latest=s;document.getElementById('northstar').textContent=s.goal;const j=s.job||{{status:'NOT STARTED',detail:''}};const su=s.setup||{{}};const setupActive=['RUNNING','QUEUED'].includes(su.status);let badgeStatus=setupActive?'SETTING UP':j.status;document.getElementById('jobBadge').textContent=badgeStatus;document.getElementById('jobBadge').className='badge '+((badgeStatus==='RUNNING'||badgeStatus==='SETTING UP')?'live':badgeStatus==='FAILED'?'bad':'');if(setupActive){{document.getElementById('jobTitle').textContent='Autoconfigurazione setup';document.getElementById('jobDetail').textContent=(su.detail||'')+(su.elapsed_seconds!=null?' · '+fmtSeconds(su.elapsed_seconds):'');}}else{{document.getElementById('jobTitle').textContent=s.current_task?s.current_task.title:(j.status==='COMPLETE'?'North Star completata':'Nessun task in esecuzione');document.getElementById('jobDetail').textContent=(j.detail||'')+(j.steps_done!=null?' · '+j.steps_done+' iterazioni':'');}}document.getElementById('progressBar').style.width=pct(s.progress_percent)+'%';document.getElementById('progressText').textContent=s.progress_percent+'% · '+s.passed_gates+'/'+s.total_gates+' gate · '+s.done_tasks+'/'+s.total_tasks+' task chiusi';const r=s.resources||{{}};document.getElementById('cpu').textContent=r.cpu_percent==null?'sampling…':Math.round(r.cpu_percent)+'%';document.getElementById('ram').textContent=r.ram_total?fmtBytes(r.ram_used)+' / '+fmtBytes(r.ram_total)+' · '+Math.round(r.ram_percent||0)+'%':'—';document.getElementById('swap').textContent=r.swap_total?fmtBytes(r.swap_used)+' / '+fmtBytes(r.swap_total):'0 GB';document.getElementById('model').textContent=r.model||'—';const b=s.budget||{{}};document.getElementById('apiCost').textContent='€'+Number(b.monthly_spent_eur||0).toFixed(3)+' / €'+Number(b.monthly_budget_eur||0).toFixed(2);const p=s.runtime_progress||{{}};document.getElementById('modelState').textContent=(p.role?p.role+' · ':'')+(p.state||'idle');document.getElementById('liveState').textContent=p.state||'idle';const c=s.current_task;if(c){{document.getElementById('currentTask').className='task live';document.getElementById('currentTask').innerHTML='<b>'+h(c.title)+'</b><div class="small muted">'+h(c.description)+'</div><div class="small muted">scientific attempts '+c.scientific_attempts+' · technical failures '+c.technical_failures+(p.elapsed_seconds!=null?' · model '+h(fmtSeconds(p.elapsed_seconds)):'')+'</div>'}}else if(setupActive){{document.getElementById('currentTask').className='task live';document.getElementById('currentTask').innerHTML='<b>Setup: '+h(su.stage||'PLANNING')+'</b><div class="small muted">'+h(su.detail||'')+'</div><div class="small muted">'+(p.model?'modello '+h(p.model)+' · ':'')+(p.elapsed_seconds!=null?h(fmtSeconds(p.elapsed_seconds))+' · ':'')+(p.chunks!=null?h(p.chunks)+' chunk · ':'')+(p.output_chars!=null?h(p.output_chars)+' caratteri output':'')+'</div>'}}else{{document.getElementById('currentTask').className='empty';document.getElementById('currentTask').textContent='Nessun task attivo.'}}const liveTail=p.visible_tail||'';const a=s.latest_attempt||{{}};let out=liveTail;if(!out&&a.artifact)out=a.artifact;if(!out&&a.review)out='REVIEW\n'+JSON.stringify(a.review,null,2)+'\n\nVERIFICATION\n'+JSON.stringify(a.verification||{{}},null,2);document.getElementById('output').textContent=out||'Nessun output visibile ancora.';document.getElementById('gates').innerHTML=(s.gates||[]).map(g=>'<div class="task '+(g.passed?'done':'')+'"><div class="split"><b>'+h(g.id.replaceAll('_',' '))+'</b><span class="badge '+(g.passed?'ok':'warn')+'">'+(g.passed?'PASS':'OPEN')+'</span></div><div class="small muted">'+h(g.description)+'</div></div>').join('');document.getElementById('tasks').innerHTML=(s.tasks||[]).map(t=>'<div class="task '+(t.status==='DONE'?'done':t.status==='IN_PROGRESS'?'live':(t.status==='ERROR'||t.status==='BLOCKED')?'bad':'')+'"><div class="split"><b>'+h(t.title)+'</b><span class="badge">'+h(t.status)+'</span></div><div class="small muted">'+h(t.description)+'</div>'+(t.next_strategy?'<div class="small"><b>Next:</b> '+h(t.next_strategy)+'</div>':'')+'</div>').join('')||'<div class="empty">Nessun task.</div>';document.getElementById('events').innerHTML=(s.events||[]).slice(0,12).map(e=>'<div class="event"><b>'+h(e.kind.replaceAll('_',' '))+'</b><span>'+h(e.task_id||'project')+' · '+h(e.ts||'')+'</span></div>').join('');}}
let timer=null;async function tick(){{if(document.hidden){{timer=setTimeout(tick,8000);return;}}try{{const r=await fetch(endpoint,{{cache:'no-store'}});if(r.ok)render(await r.json());}}catch(e){{}}timer=setTimeout(tick,2000);}}
document.getElementById('toggleOutput').addEventListener('click',()=>document.getElementById('output').classList.toggle('hide'));document.addEventListener('visibilitychange',()=>{{if(!document.hidden){{if(timer)clearTimeout(timer);timer=setTimeout(tick,100);}}}});tick();
"""
    return HTMLResponse(shell(f'{ws.manifest.name} — Live', body, active='dashboard', extra_script=script), headers={'Cache-Control':'no-store'})
