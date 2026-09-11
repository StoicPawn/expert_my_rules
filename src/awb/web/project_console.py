from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from awb.core.cloud_budget import budget_snapshot, load_control, save_control
from awb.core.model_catalog import OPENAI_MODELS, catalog_for_manifest
from awb.core.models import Gate, ModelRouteSpec
from awb.core.project_output import (
    OutputPolicy,
    latest_generated_files,
    list_output_snapshots,
    load_output_policy,
    maybe_auto_output_snapshot,
    save_output_policy,
    write_output_snapshot,
)
from awb.core.resource_policy import (
    ProjectResourcePolicy,
    load_project_policy,
    load_system_policy,
    save_project_policy,
)
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest
from awb.web.app import base_dir
from awb.web.control_app import _route_for
from awb.web.control_v3 import _root
from awb.web.ui import badge, esc, shell


_INSTALLED = False
_AUTO_THREAD_STARTED = False


def _shape(path: str) -> str:
    return re.sub(r'\{[^}]+\}', '{}', path)


def _remove_shape(app: FastAPI, path: str, method: str) -> None:
    target = _shape(path)
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (
            _shape(str(getattr(route, 'path', ''))) == target
            and method in (getattr(route, 'methods', None) or set())
        )
    ]


def _safe_project_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if candidate == resolved_root or resolved_root not in candidate.parents or not candidate.is_file():
        raise HTTPException(404, 'File non trovato')
    return candidate


def _enriched_state(uc, project: str) -> dict[str, Any]:
    state = uc._project_state(project)
    root = _root(project)
    policy = load_output_policy(root)
    state['output_policy'] = {
        'auto_every_minutes': policy.auto_every_minutes,
        'max_snapshots_shown': policy.max_snapshots_shown,
    }
    state['outputs'] = list_output_snapshots(root, policy.max_snapshots_shown)
    state['generated_files'] = latest_generated_files(root, 40)
    return state


def _config_html(project: str) -> str:
    root = _root(project)
    ws = load_workspace(root)
    policy = load_project_policy(root)
    system = load_system_policy(root.parent)
    cloud = load_control(root)
    budget = budget_snapshot(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    gate_state = ledger.gate_state()
    try:
        catalog = catalog_for_manifest(ws.manifest)
    except Exception:
        catalog = {'nodes': {}, 'openai': {'models': OPENAI_MODELS}}

    agents: list[str] = []
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
        agents.append(f"""
<div class='agent'>
  <form method='post' action='/project/{esc(project)}/config/agent'>
    <input type='hidden' name='role' value='{esc(role)}'>
    <div class='split'><h3>{esc(agent.id)}</h3>{badge(role)}</div>
    <label>Istruzioni</label><textarea name='instructions' rows='4'>{esc(agent.instructions)}</textarea>
    <div class='grid'>
      <div class='col-4'><label>Nodo locale</label><select name='node'>{nodes}</select></div>
      <div class='col-4'><label>Modello locale</label><select name='local_model'>{local_options}</select></div>
      <div class='col-4'><label>Modello API</label><select name='remote_model'>{remote_options}</select></div>
    </div>
    <label>Reasoning API</label><select name='reasoning'>{''.join(f"<option value='{x}' {'selected' if x == reasoning else ''}>{x}</option>" for x in ('low','medium','high'))}</select>
    <button class='secondary' style='margin-top:9px'>Salva agente</button>
  </form>
</div>""")

    gates = []
    for gate in ws.manifest.gates:
        passed = bool((gate_state.get(gate.id) or {}).get('passed'))
        gates.append(f"""
<div class='task {'done' if passed else ''}'>
  <form method='post' action='/project/{esc(project)}/config/gate'>
    <input type='hidden' name='gate_id' value='{esc(gate.id)}'>
    <div class='split'><b>{esc(gate.id)}</b>{badge('PASS' if passed else 'OPEN','ok' if passed else 'warn')}</div>
    <textarea name='description' rows='2'>{esc(gate.description)}</textarea>
    <label><input style='width:auto' type='checkbox' name='required' {'checked' if gate.required else ''}> obbligatoria</label>
    <button class='secondary'>Salva condizione</button>
  </form>
</div>""")

    mode = 'force' if cloud.enabled and cloud.mode == 'force' else 'paused'
    return f"""
<div class='grid'>
  <div class='col-8 card hero'>
    <h2>North Star</h2>
    <form method='post' action='/project/{esc(project)}/config/goal'>
      <textarea name='goal' rows='6'>{esc(ws.manifest.goal)}</textarea>
      <button>Salva North Star</button>
    </form>
  </div>
  <div class='col-4 card'>
    <h2>Risorse del progetto</h2>
    <p class='small muted'>Massimo Expert: {system.cpu_cores:g} CPU / {system.ram_gb:g} GB. Le quote dei progetti RUNNING si sommano.</p>
    <form method='post' action='/project/{esc(project)}/config/resources'>
      <label>CPU assegnata</label><input type='number' name='cpu_cores' step='0.1' min='0.5' max='{system.cpu_cores}' value='{policy.cpu_cores}'>
      <label>RAM riservata (GB)</label><input type='number' name='ram_gb' step='0.1' min='1' max='{system.ram_gb}' value='{policy.ram_gb}'>
      <label>Context locale (token)</label><input type='number' name='context_tokens' step='512' min='1024' value='{policy.context_tokens}'>
      <label>Tool call max / task</label><input type='number' name='max_tool_calls' min='1' max='500' value='{policy.max_tool_calls_per_task}'>
      <button>Salva risorse</button>
    </form>
  </div>
  <div class='col-12 card'><h2>Agenti e modelli</h2><p class='small muted'>Questa è la configurazione effettiva usata dal progetto, non il tempo impiegato dal setup iniziale.</p>{''.join(agents)}</div>
  <div class='col-5 card'>
    <h2>API e costi</h2>
    <div class='split'><b>€{float(budget.get('spent_eur') or 0):.4f} / €{float(budget.get('budget_eur') or 0):.2f}</b>{badge('API SBLOCCATA' if mode == 'force' else 'SOLO LOCALE','warn' if mode == 'force' else 'ok')}</div>
    <form method='post' action='/project/{esc(project)}/config/api'>
      <label>Modalità</label><select name='mode'><option value='paused' {'selected' if mode == 'paused' else ''}>Solo locale</option><option value='force' {'selected' if mode == 'force' else ''}>API sbloccata manualmente</option></select>
      <label>Cap progetto €</label><input name='budget' type='number' step='0.01' min='0' value='{float(cloud.budget_eur):.2f}'>
      <label>Soglia priorità</label><input name='priority_threshold' type='number' step='0.1' min='0' value='{float(cloud.priority_threshold):.1f}'>
      <button>Salva piano API</button>
    </form>
  </div>
  <div class='col-7 card'>
    <h2>Definition of Done</h2>
    {''.join(gates) or '<div class="empty">Nessuna condizione.</div>'}
    <form method='post' action='/project/{esc(project)}/config/gate/add'>
      <label>Nuova condizione</label><input name='gate_id' required placeholder='es. theorem_proved'>
      <textarea name='description' rows='2' required placeholder='Condizione verificabile di completamento'></textarea>
      <button class='secondary'>Aggiungi condizione</button>
    </form>
  </div>
</div>
"""


def _console_page(uc, project: str) -> HTMLResponse:
    state = _enriched_state(uc, project)
    refresh = int(load_system_policy(base_dir()).dashboard_refresh_seconds)
    refresh = max(10, min(refresh, 300))
    initial = json.dumps(state, ensure_ascii=False).replace('</', '<\\/')
    controls = uc._project_controls(project, state)
    root = _root(project)
    output_policy = load_output_policy(root)
    config_html = _config_html(project)

    body = f"""
<div class='topbar'>
  <div><a class='small muted' href='/'>← tutti i progetti</a><h1>{esc(state['name'])}</h1><p class='sub'>{esc(state['goal'])}</p></div>
  <div>{controls}</div>
</div>
<div class='tabs'>
  <button class='tab active' data-tab='overview'>Stato</button>
  <button class='tab' data-tab='output'>Output</button>
  <button class='tab' data-tab='config'>Configurazione</button>
</div>
<div class='pane active' data-pane='overview'>
  <div class='grid'>
    <div class='col-8 card hero'>
      <div class='split'><div><h2>Adesso</h2><div id='currentTitle' style='font-size:22px;font-weight:800'>—</div><div id='currentDetail' class='muted'></div></div><span id='overallBadge' class='badge'>—</span></div>
      <div id='currentMeta' class='small muted' style='margin-top:10px'></div>
      <pre id='liveOutput' class='output' style='max-height:300px;margin-top:12px'>Nessun output pubblico disponibile.</pre>
      <div class='small muted' style='margin-top:8px'>Stato aggiornato ogni {refresh}s senza ricaricare la pagina.</div>
    </div>
    <div class='col-4 card'>
      <h2>Risorse e avanzamento</h2>
      <div class='kpis' style='grid-template-columns:repeat(2,minmax(0,1fr))'>
        <div class='kpi'><span>CPU ACEPC</span><b id='hostCpu'>—</b></div>
        <div class='kpi'><span>RAM ACEPC</span><b id='hostRam' style='font-size:14px'>—</b></div>
        <div class='kpi'><span>CPU progetto</span><b id='allocCpu'>—</b></div>
        <div class='kpi'><span>RAM progetto</span><b id='allocRam'>—</b></div>
        <div class='kpi'><span>Agente / modello</span><b id='modelRole' style='font-size:13px'>—</b></div>
        <div class='kpi'><span>Token call corrente</span><b id='tokens' style='font-size:13px'>—</b></div>
      </div>
      <div id='projectProgress' class='small muted' style='margin-top:12px'></div>
    </div>
    <div class='col-5 card'><h2>Cosa è successo di recente</h2><div id='events' class='timeline'></div></div>
    <div class='col-7 card'><div class='split'><div><h2>Task</h2><p class='small muted'>OPEN mai iniziato · CLOSED risolto · IN PROGRESS il motore è qui · PAUSED iniziato ma non attivo · FAILED errore fatale.</p></div><span id='taskCount' class='badge'>—</span></div><div id='tasks'></div></div>
  </div>
</div>
<div class='pane' data-pane='output'>
  <div class='grid'>
    <div class='col-4 card hero'>
      <h2>Consolidamento output</h2>
      <p class='small muted'>Raccoglie stato, risultati positivi/negativi, file prodotti, ultimi chunk visibili e contatori token in snapshot ordinati. Il reasoning nascosto non viene salvato.</p>
      <form method='post' action='/project/{esc(project)}/output/policy'>
        <label>Snapshot automatico ogni</label>
        <select name='minutes'>
          {''.join(f"<option value='{m}' {'selected' if output_policy.auto_every_minutes == m else ''}>{'solo manuale' if m == 0 else str(m) + ' minuti'}</option>" for m in (0,5,15,30,60,120))}
        </select>
        <button class='secondary'>Salva frequenza</button>
      </form>
      <form method='post' action='/project/{esc(project)}/output/snapshot' style='margin-top:10px'><button>Consolida output ora</button></form>
    </div>
    <div class='col-8 card'><div class='split'><h2>Snapshot</h2><span id='outputCount' class='badge'>—</span></div><div id='outputs'></div></div>
    <div class='col-12 card'><div class='split'><h2>File prodotti dal progetto</h2><span id='fileCount' class='badge'>—</span></div><div id='generatedFiles'></div></div>
  </div>
</div>
<div class='pane' data-pane='config'>
  {config_html}
</div>
"""

    script = f"""
const PROJECT={json.dumps(project)}; const ENDPOINT='/project/'+encodeURIComponent(PROJECT)+'/live'; const REFRESH={refresh * 1000};
let state={initial};
function h(v){{return String(v??'').replace(/[&<>\"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[c]));}}
function fmtGB(n){{return n==null?'—':Number(n).toFixed(1)+' GB';}}
function cls(s){{return s==='CLOSED'||s==='COMPLETE'?'ok':s==='RUNNING'||s==='IN PROGRESS'||s==='SETTING UP'?'live':s==='FAILED'?'bad':s==='PAUSED'?'warn':'';}}
function openPane(name){{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===name));document.querySelectorAll('.pane').forEach(x=>x.classList.toggle('active',x.dataset.pane===name));if(location.hash!=='#'+name)history.replaceState(null,'','#'+name);}}
document.querySelectorAll('[data-tab]').forEach(btn=>btn.addEventListener('click',()=>openPane(btn.dataset.tab)));
function render(s){{
  state=s; const p=s.runtime_progress||{{}}, c=s.current_task, setup=s.setup||{{}};
  const overall=String(s.overall_status||'NOT STARTED'); const ob=document.getElementById('overallBadge'); ob.textContent=overall; ob.className='badge '+cls(overall);
  if(c){{document.getElementById('currentTitle').textContent=c.title;document.getElementById('currentDetail').textContent='Task '+c.id+' · '+(c.phase||p.role||c.status);}}
  else if(['RUNNING','QUEUED'].includes(setup.status)){{document.getElementById('currentTitle').textContent='Configurazione iniziale';document.getElementById('currentDetail').textContent=(setup.stage||'')+' · '+(setup.detail||'');}}
  else if(overall==='RUNNING'){{document.getElementById('currentTitle').textContent='Director: scelta del prossimo passo';document.getElementById('currentDetail').textContent='Il motore è tra due task/checkpoint e sta pianificando il prossimo passo.';}}
  else{{document.getElementById('currentTitle').textContent='Nessun task attivo';document.getElementById('currentDetail').textContent=(s.job||{{}}).detail||'';}}
  const meta=[]; if(p.role)meta.push('agente '+p.role); if(p.model)meta.push('modello '+p.model); if(p.state)meta.push(p.state); if(p.elapsed_seconds!=null)meta.push(Math.round(Number(p.elapsed_seconds))+'s'); if(p.chunks!=null)meta.push(p.chunks+' chunk'); document.getElementById('currentMeta').textContent=meta.join(' · ');
  document.getElementById('liveOutput').textContent=p.visible_tail||p.visible_output||'Nessun output pubblico disponibile in questo momento.';
  const r=s.resources||{{}}, a=s.allocation||{{}}; document.getElementById('hostCpu').textContent=r.cpu_percent==null?'—':Math.round(Number(r.cpu_percent))+'%'; document.getElementById('hostRam').textContent=r.ram_total?fmtGB(Number(r.ram_used)/1073741824)+' / '+fmtGB(Number(r.ram_total)/1073741824):'—'; document.getElementById('allocCpu').textContent=(a.cpu_cores??'—')+' core'; document.getElementById('allocRam').textContent=fmtGB(a.ram_gb); document.getElementById('modelRole').textContent=(p.role?p.role+' · ':'')+(p.model||r.model||'—'); document.getElementById('tokens').textContent=(p.prompt_tokens??0)+' in / '+(p.output_tokens??0)+' out';
  const tasks=[...(s.tasks||[])], done=tasks.filter(t=>t.status==='CLOSED').length, open=tasks.filter(t=>t.status==='OPEN').length, paused=tasks.filter(t=>t.status==='PAUSED').length; document.getElementById('projectProgress').textContent=done+' chiusi · '+open+' aperti · '+paused+' in attesa · '+tasks.length+' totali';
  document.getElementById('events').innerHTML=(s.events||[]).slice(0,8).map(e=>'<div class="event"><b>'+h(e.label)+'</b><span>'+h(e.detail)+'</span></div>').join('')||'<div class="empty">Nessun evento recente.</div>';
  const order={{'IN PROGRESS':0,'FAILED':1,'PAUSED':2,'OPEN':3,'CLOSED':4}}; tasks.sort((x,y)=>(order[x.status]??9)-(order[y.status]??9)); document.getElementById('taskCount').textContent=tasks.length+' task'; document.getElementById('tasks').innerHTML=tasks.map(t=>'<div class="task '+(t.status==='CLOSED'?'done':t.status==='IN PROGRESS'?'live':t.status==='FAILED'?'bad':'')+'"><div class="split"><div><b>'+h(t.title)+'</b><div class="small muted">'+h(t.id)+(t.phase?' · '+h(t.phase):'')+'</div></div><span class="badge '+cls(t.status)+'">'+h(t.status)+'</span></div>'+(t.next_strategy&&t.status!=='CLOSED'?'<div class="small muted" style="margin-top:5px">Prossima strategia: '+h(t.next_strategy)+'</div>':'')+'</div>').join('')||'<div class="empty">Nessun task.</div>';
  const outs=s.outputs||[]; document.getElementById('outputCount').textContent=outs.length+' snapshot'; document.getElementById('outputs').innerHTML=outs.map(o=>'<div class="task"><div class="split"><div><b>'+h(o.output_id)+'</b><div class="small muted">'+h(o.generated_at)+' · '+h(o.reason)+'</div></div><span class="badge">'+(o.manual?'MANUALE':'AUTO')+'</span></div><div class="small" style="margin-top:6px">'+Number((o.token_totals||{{}}).prompt_tokens||0)+' token input · '+Number((o.token_totals||{{}}).output_tokens||0)+' output · '+Number(o.files_count||0)+' file indicizzati</div><div class="row" style="margin-top:7px"><a class="tab" href="/project/'+encodeURIComponent(PROJECT)+'/file?path='+encodeURIComponent(o.summary_path)+'">summary.md</a><a class="tab" href="/project/'+encodeURIComponent(PROJECT)+'/file?path='+encodeURIComponent(o.manifest_path)+'">manifest.json</a>'+(o.live_output_path?'<a class="tab" href="/project/'+encodeURIComponent(PROJECT)+'/file?path='+encodeURIComponent(o.live_output_path)+'">live-output.txt</a>':'')+'</div></div>').join('')||'<div class="empty">Nessuno snapshot ancora. Premi “Consolida output ora”.</div>';
  const files=s.generated_files||[]; document.getElementById('fileCount').textContent=files.length+' recenti'; document.getElementById('generatedFiles').innerHTML=files.map(f=>'<div class="event"><b><a href="/project/'+encodeURIComponent(PROJECT)+'/file?path='+encodeURIComponent(f.path)+'">'+h(f.path)+'</a></b><span>'+Math.round(Number(f.size_bytes||0)/1024)+' KB · '+h(f.modified_at)+'</span></div>').join('')||'<div class="empty">Nessun file prodotto.</div>';
}}
async function refresh(){{try{{const res=await fetch(ENDPOINT,{{cache:'no-store'}});if(res.ok)render(await res.json());}}catch(e){{}}}}
render(state); const requested=(location.hash||'#overview').slice(1); if(['overview','output','config'].includes(requested))openPane(requested); setInterval(refresh,REFRESH); setTimeout(refresh,1500);
"""
    return HTMLResponse(shell(f"{state['name']} — Expert My Rules", body, script=script), headers={'Cache-Control': 'no-store'})


def _auto_loop() -> None:
    while True:
        try:
            for root in base_dir().iterdir():
                if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
                    continue
                try:
                    maybe_auto_output_snapshot(root)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(30)


def install_project_console(app: FastAPI, runtime_module, uc) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    # Parameter names differ across old generations ({slug_}, {project}). Remove by
    # route shape so the historic setup-first page cannot win FastAPI's first-match routing.
    for path, method in (
        ('/project/{project}', 'GET'),
        ('/project/{project}/setup', 'GET'),
        ('/project/{project}/live', 'GET'),
    ):
        _remove_shape(app, path, method)

    @app.on_event('startup')
    def start_output_snapshotter() -> None:
        global _AUTO_THREAD_STARTED
        if _AUTO_THREAD_STARTED:
            return
        _AUTO_THREAD_STARTED = True
        threading.Thread(target=_auto_loop, name='awb-output-snapshotter', daemon=True).start()

    @app.get('/project/{project}', response_class=HTMLResponse)
    def project_console(project: str):
        return _console_page(uc, project)

    @app.get('/project/{project}/setup')
    def legacy_setup_redirect(project: str):
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.get('/project/{project}/live')
    def project_live(project: str):
        return JSONResponse(_enriched_state(uc, project), headers={'Cache-Control': 'no-store'})

    @app.get('/project/{project}/file')
    def project_file(project: str, path: str):
        root = _root(project)
        target = _safe_project_file(root, path)
        return FileResponse(target, filename=target.name)

    @app.post('/project/{project}/output/snapshot')
    def output_snapshot(project: str):
        write_output_snapshot(_root(project), reason='manual-ui', manual=True)
        return RedirectResponse(f'/project/{project}#output', 303)

    @app.post('/project/{project}/output/policy')
    def output_policy(project: str, minutes: int = Form(...)):
        root = _root(project)
        save_output_policy(root, OutputPolicy(auto_every_minutes=minutes, max_snapshots_shown=load_output_policy(root).max_snapshots_shown))
        Ledger(root / 'ledger.sqlite3').event('output_policy_changed', {'auto_every_minutes': max(0, min(int(minutes), 1440))})
        return RedirectResponse(f'/project/{project}#output', 303)

    @app.post('/project/{project}/config/goal')
    def config_goal(project: str, goal: str = Form(...)):
        root = _root(project)
        ws = load_workspace(root)
        ws.manifest.goal = goal.strip()
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').event('north_star_changed', {'goal': goal.strip()[:500]})
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.post('/project/{project}/config/resources')
    def config_resources(
        project: str, cpu_cores: float = Form(...), ram_gb: float = Form(...),
        context_tokens: int = Form(...), max_tool_calls: int = Form(...),
    ):
        root = _root(project)
        try:
            saved = save_project_policy(root, ProjectResourcePolicy(
                cpu_cores=cpu_cores, ram_gb=ram_gb,
                context_tokens=context_tokens, max_tool_calls_per_task=max_tool_calls,
            ))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        ws = load_workspace(root)
        ws.manifest.runtime.max_tool_calls_per_task = saved.max_tool_calls_per_task
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').event('project_resources_changed', {
            'cpu_cores': saved.cpu_cores, 'ram_gb': saved.ram_gb,
            'context_tokens': saved.context_tokens, 'max_tool_calls_per_task': saved.max_tool_calls_per_task,
        })
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.post('/project/{project}/config/agent')
    def config_agent(
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
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.post('/project/{project}/config/api')
    def config_api(project: str, mode: str = Form(...), budget: float = Form(...), priority_threshold: float = Form(...)):
        if mode not in {'paused', 'force'}:
            raise HTTPException(400, 'Modalità API non valida')
        root = _root(project)
        cloud = load_control(root)
        cloud.mode = mode
        cloud.enabled = mode == 'force'
        cloud.budget_eur = max(0.0, float(budget))
        cloud.priority_threshold = max(0.0, float(priority_threshold))
        save_control(root, cloud)
        Ledger(root / 'ledger.sqlite3').event('cloud_mode_changed', {'mode': mode, 'enabled': cloud.enabled, 'source': 'project-console'})
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.post('/project/{project}/config/gate')
    def config_gate(project: str, gate_id: str = Form(...), description: str = Form(...), required: str | None = Form(None)):
        root = _root(project)
        ws = load_workspace(root)
        gate = next((g for g in ws.manifest.gates if g.id == gate_id), None)
        if gate is None:
            raise HTTPException(404, 'Condizione non trovata')
        gate.description = description.strip()
        gate.required = required is not None
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').event('gate_definition_changed', {'gate_id': gate_id, 'required': gate.required})
        return RedirectResponse(f'/project/{project}#config', 303)

    @app.post('/project/{project}/config/gate/add')
    def config_gate_add(project: str, gate_id: str = Form(...), description: str = Form(...)):
        root = _root(project)
        ws = load_workspace(root)
        safe_id = ''.join(c for c in gate_id.strip().replace(' ', '_') if c.isalnum() or c in '_-.')
        if not safe_id or safe_id in {g.id for g in ws.manifest.gates}:
            raise HTTPException(400, 'ID condizione non valido o duplicato')
        ws.manifest.gates.append(Gate(id=safe_id, description=description.strip(), required=True, manual=True))
        save_manifest(ws)
        Ledger(root / 'ledger.sqlite3').set_gate(safe_id, False, 'not evaluated')
        return RedirectResponse(f'/project/{project}#config', 303)
