from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from awb.core.checkpoints import latest_checkpoint
from awb.core.project_memory import ProjectMemory
from awb.core.resume_trace import load_stream_trace
from awb.core.storage import Ledger
from awb.web.dashboard_app import _cross_link, _root
from awb.web.ui import esc, shell
from awb.core.workspace import load_workspace


def _remove_route(app, path: str, method: str) -> None:
    method = method.upper()
    app.router.routes[:] = [r for r in app.router.routes if not (
        getattr(r, 'path', None) == path and method in (getattr(r, 'methods', None) or set())
    )]


def _clip(value, limit=30000):
    text = str(value or '')
    return text if len(text) <= limit else text[-limit:]


def install_deep_live_dashboard(app) -> None:
    @app.get('/project/{project}/live-detail')
    def live_detail(project: str):
        root = _root(project)
        ledger = Ledger(root / 'ledger.sqlite3')
        job = ledger.latest_job() or {}
        trace = load_stream_trace(root, job.get('id')) or {}
        events = ledger.recent_events(160)
        latest_tool = next((e for e in events if e.get('kind') == 'tool_call'), None)
        latest_review = next((e for e in events if e.get('kind') == 'review'), None)
        latest_verification = next((e for e in events if e.get('kind') in {'verification', 'gate_evaluated'}), None)
        checkpoint = latest_checkpoint(root) or {}
        memory = ProjectMemory(root / 'ledger.sqlite3').latest(limit=16)
        return JSONResponse({
            'trace': {
                'job_id': trace.get('job_id'), 'role': trace.get('role'), 'model': trace.get('model'),
                'state': trace.get('state'), 'detail': trace.get('detail'),
                'elapsed_seconds': trace.get('elapsed_seconds'), 'chunks': trace.get('chunks'),
                'prompt_tokens': trace.get('prompt_tokens'), 'output_tokens': trace.get('output_tokens'),
                'prompt_tokens_exact': trace.get('prompt_tokens_exact'),
                'output_tokens_exact': trace.get('output_tokens_exact'),
                'output_chars': trace.get('output_chars'),
                'last_stream_activity_seconds': trace.get('last_stream_activity_seconds'),
                'system_prompt': _clip(trace.get('system_prompt'), 40000),
                'user_prompt': _clip(trace.get('user_prompt'), 50000),
                'visible_output': _clip(trace.get('visible_output'), 50000),
                'written_at': trace.get('written_at'), 'error': trace.get('error'),
            } if trace else {},
            'latest_tool': latest_tool,
            'latest_review': latest_review,
            'latest_verification': latest_verification,
            'memory': memory,
            'checkpoint': {
                'checkpoint_id': checkpoint.get('checkpoint_id'),
                'generated_at': checkpoint.get('generated_at'),
                'reason': checkpoint.get('reason'),
                'summary': checkpoint.get('summary'),
                'next_best_action': checkpoint.get('next_best_action'),
            } if checkpoint else {},
        }, headers={'Cache-Control': 'no-store'})

    _remove_route(app, '/project/{project}', 'GET')

    @app.get('/project/{project}', response_class=HTMLResponse)
    def deep_project(request: Request, project: str):
        ws = load_workspace(_root(project))
        body = f"""
<div class='topbar'><div><a class='small muted' href='/'>← dashboard</a><h1>{esc(ws.manifest.name)}</h1><p class='sub'>{esc(ws.manifest.goal)}</p></div><div class='nav'><a href='{_cross_link(request,'AWB_PORT',8100,f'/project/{project}')}'>Control Center</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102,f'/?project={project}')}'>Research Lab</a></div></div>
<div class='grid'>
<div class='col-8 card hero'><div class='split'><div><h2>Stato reale</h2><div id='headline' class='big'>Caricamento…</div><div id='detail' class='muted'></div></div><span id='jobBadge' class='badge'>—</span></div><div class='meter'><i id='meter' style='width:0%'></i></div><div id='meterText' class='small muted'></div></div>
<div class='col-4 card'><h2>ACEPC</h2><div class='kpis compact'><div class='kpi'><span>CPU</span><b id='cpu'>—</b></div><div class='kpi'><span>RAM</span><b id='ram'>—</b></div><div class='kpi'><span>Swap</span><b id='swap'>—</b></div><div class='kpi'><span>API</span><b id='api'>—</b></div></div><div id='cpuNote' class='small muted'></div></div>
<div class='col-12 card livebox'><div class='split'><div><h2>Produzione live del modello</h2><p class='small muted'>Output visibile, token, chunk e durata. Il reasoning nascosto non viene mostrato o salvato.</p></div><span id='liveBadge' class='badge'>idle</span></div><div class='tokenrow'><span>agente <b id='role'>—</b></span><span>modello <b id='model'>—</b></span><span>input <b id='ptok'>—</b></span><span>output <b id='otok'>—</b></span><span>chunk <b id='chunks'>—</b></span><span>tempo <b id='elapsed'>—</b></span></div><pre id='stream'>Nessuna generazione locale attiva.</pre><details><summary>Input inviato al modello</summary><h3>System</h3><pre id='systemPrompt'></pre><h3>User/task context</h3><pre id='userPrompt'></pre></details></div>
<div class='col-12 card'><div class='split'><div><h2>Agenti — catena corrente</h2><p class='small muted'>Director/Planner → Worker → deterministic verify → Reviewer → Verifier → re-plan. Un solo modello fisico può cambiare ruolo in sequenza.</p></div><span id='focusPhase' class='badge'>—</span></div><div id='agents' class='agents'></div></div>
<div class='col-7 card'><h2>Task corrente e obiezioni</h2><div id='focus'></div></div><div class='col-5 card'><h2>Checkpoint / ripresa</h2><div id='checkpoint'></div></div>
<div class='col-7 card'><h2>Research Lab / strumenti</h2><div id='tool'></div></div><div class='col-5 card'><h2>Ultimo giudizio</h2><div id='judgement'></div></div>
<div class='col-7 card'><h2>Piano task / grafo</h2><div id='tasks'></div></div><div class='col-5 card'><h2>Memoria esterna / gate</h2><div id='memory'></div><hr><div id='gates'></div></div>
</div>"""
        head = """<style>.big{font-size:23px;font-weight:850}.compact{grid-template-columns:1fr 1fr}.compact b{font-size:14px}.livebox pre{max-height:430px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#0d1117;color:#e6edf3;border-radius:10px;padding:12px;font-size:12px}.tokenrow{display:flex;flex-wrap:wrap;gap:8px 18px;margin:10px 0;font-size:12px}.agents{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.agent{border:1px solid var(--line);border-radius:10px;padding:10px;background:#fafbfc}.agent.live{border-color:#7597ff;background:#f3f6ff}.agent .name{font-weight:800}.agent .note{font-size:12px;color:var(--muted);margin-top:5px}.focusbox{border-left:4px solid var(--accent);padding:10px 12px;background:#f6f8ff;border-radius:8px}.taskrow{border-top:1px solid var(--line);padding:9px 0}.taskrow:first-child{border-top:0}.code{white-space:pre-wrap;word-break:break-word;max-height:350px;overflow:auto;background:#f7f8fa;padding:8px;border-radius:8px;font:11px/1.4 ui-monospace,monospace}@media(max-width:850px){.agents{grid-template-columns:1fr 1fr}}@media(max-width:520px){.agents{grid-template-columns:1fr}}</style>"""
        script = r"""
const project=__PROJECT__,stateUrl='/project/'+encodeURIComponent(project)+'/state',detailUrl='/project/'+encodeURIComponent(project)+'/live-detail';
function h(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));} function clip(v,n=500){const s=String(v??'').replace(/\s+/g,' ').trim();return s.length>n?s.slice(0,n)+'…':s;} function secs(v){let n=Number(v)||0;if(n<60)return Math.round(n)+'s';let m=Math.floor(n/60);if(m<60)return m+'m '+Math.round(n%60)+'s';return Math.floor(m/60)+'h '+m%60+'m';} function gb(v){return v?Number(v/1073741824).toFixed(1)+' GB':'—';}
function eventFor(s,role){const es=s.events||[];const kinds=role==='director'?['micro_task_created','micro_task_rework_planned','task_recovery_planned','task_decomposed','task_created']:role==='worker'?['work_output','model_call_started']:role==='reviewer'?['review']:['verification','gate_evaluated'];return es.find(e=>kinds.includes(e.kind)&&(!e.payload?.role||e.payload.role===role))||null;}
function renderAgents(s,d){const active=(d.trace||{}).role||(s.runtime_progress||{}).role;const roles=[['director','Director','pianifica / re-plan'],['worker','Worker','esegue il micro-task'],['reviewer','Reviewer','attacca il candidato'],['verifier','Verifier','controlla contratto/evidenze']];document.getElementById('agents').innerHTML=roles.map(([r,n,p])=>{const e=eventFor(s,r),pl=e?.payload||{};let note=e?clip(pl.text||pl.strategy||pl.detail||pl.title||JSON.stringify(pl),230):'In attesa.';if(e?.kind==='review')note=pl.approved?'Ultima review: OK':'Obiezione: '+clip((pl.critical_objections||[])[0]||'',220);if(active===r)note='LIVE · '+(clip((d.trace||{}).visible_output,220)||note);return '<div class="agent '+(active===r?'live':'')+'"><div class="name">'+n+' <span class="badge">'+(active===r?'LIVE':'ultimo')+'</span></div><div class="small muted">'+p+'</div><div class="note">'+h(note)+'</div></div>';}).join('');}
function render(s,d){const j=s.job||{},rp=s.runtime_progress||{},tr=d.trace||{},status=s.overall_status||j.status||'NOT STARTED';document.getElementById('jobBadge').textContent=status;const tasks=s.tasks||[],focus=s.current_task||s.focused_task||tasks.find(t=>t.focus_chain_active)||tasks.find(t=>t.status==='BLOCKED')||tasks.find(t=>t.status==='OPEN');document.getElementById('headline').textContent=focus?focus.title:(status==='RUNNING'?'In attesa del prossimo micro-task':'Nessun task in esecuzione');document.getElementById('detail').textContent=j.detail||'';document.getElementById('meter').style.width=Math.max(0,Math.min(100,Number(s.progress_percent)||0))+'%';document.getElementById('meterText').textContent=(s.progress_percent||0)+'% · '+s.passed_gates+'/'+s.total_gates+' gate · '+s.done_tasks+'/'+s.total_tasks+' task chiusi';const r=s.resources||{};document.getElementById('cpu').textContent=r.cpu_percent==null?'sampling…':Math.round(r.cpu_percent)+'%';document.getElementById('ram').textContent=r.ram_total?gb(r.ram_used)+' / '+gb(r.ram_total):'—';document.getElementById('swap').textContent=r.swap_total?gb(r.swap_used)+' / '+gb(r.swap_total):'0';const b=s.budget||{};document.getElementById('api').textContent=(b.mode==='force'&&b.requested_enabled)?'SBLOCCATA':'LOCALE';document.getElementById('cpuNote').textContent=(Number(r.cpu_percent)||0)>80?'CPU alta = Ollama usa il budget disponibile; non è un errore.':'La CPU può scendere tra un ruolo e il successivo.';const live=Object.keys(tr).length?tr:rp;document.getElementById('liveBadge').textContent=live.state||'idle';document.getElementById('role').textContent=live.role||'—';document.getElementById('model').textContent=live.model||r.model||'—';document.getElementById('ptok').textContent=live.prompt_tokens==null?'—':live.prompt_tokens+(live.prompt_tokens_exact?' esatti':' ~');document.getElementById('otok').textContent=live.output_tokens==null?'—':live.output_tokens+(live.output_tokens_exact?' esatti':' ~');document.getElementById('chunks').textContent=live.chunks??'—';document.getElementById('elapsed').textContent=live.elapsed_seconds==null?'—':secs(live.elapsed_seconds);document.getElementById('stream').textContent=tr.visible_output||rp.visible_tail||'Nessuna generazione locale attiva.';document.getElementById('systemPrompt').textContent=tr.system_prompt||'';document.getElementById('userPrompt').textContent=tr.user_prompt||'';renderAgents(s,d);document.getElementById('focusPhase').textContent=focus?(focus.lifecycle_phase||focus.status):'—';if(focus){const obs=focus.critical_objections||[],vc=focus.verification_contract||{},deps=focus.depends_on||[];document.getElementById('focus').innerHTML='<div class="focusbox"><b>'+h(focus.title)+'</b><div class="small muted">'+h(focus.description)+'</div><div><b>fase:</b> '+h(focus.lifecycle_phase||focus.status)+'</div>'+(focus.next_strategy?'<div><b>strategia:</b> '+h(focus.next_strategy)+'</div>':'')+'<div><b>dipendenze:</b> '+h(deps.join(', ')||'nessuna')+'</div><div><b>verifica predefinita:</b> '+h((vc.criteria||[]).join(' | ')||'in definizione')+'</div>'+(obs.length?'<div><b>obiezioni:</b><br>'+obs.map(x=>'• '+h(x)).join('<br>')+'</div>':'')+'</div>';}else document.getElementById('focus').innerHTML='<div class="empty">Nessun task irrisolto.</div>';const cp=d.checkpoint||{};document.getElementById('checkpoint').innerHTML=cp.checkpoint_id?'<div class="focusbox"><b>'+h(cp.checkpoint_id)+'</b><div class="small muted">'+h(cp.reason||'')+'</div><div><b>ripresa:</b> '+h(clip(cp.next_best_action?.title||'nessuna',300))+'</div></div>':'<div class="empty">Nessun checkpoint ancora.</div>';const te=d.latest_tool;if(te){const p=te.payload||{};document.getElementById('tool').innerHTML='<b>'+h(p.role||'Worker')+' → '+h(p.tool||'tool')+'</b><div class="code">'+h(JSON.stringify({arguments:p.arguments,result:p.result},null,2))+'</div>';}else document.getElementById('tool').innerHTML='<div class="empty">Nessuna chiamata strumento recente.</div>';const re=d.latest_review?.payload,ve=d.latest_verification?.payload;let judge='';if(re)judge+='<b>Reviewer:</b> '+(re.approved?'APPROVATO':'DA RIVEDERE')+'<br>'+h(clip((re.critical_objections||[]).join(' | '),600));if(ve)judge+='<br><br><b>Verifier:</b> '+(ve.passed?'OK':'NON PASSA')+'<br>'+h(clip(ve.detail,500));document.getElementById('judgement').innerHTML=judge||'<div class="empty">Nessun giudizio recente.</div>';document.getElementById('tasks').innerHTML=tasks.map(t=>'<div class="taskrow"><div><b>'+h(t.title)+'</b> <span class="badge">'+h(t.status)+'</span> <span class="small">'+h(t.lifecycle_phase||'')+'</span></div><div class="small muted">'+h(clip(t.description,220))+'</div><div class="small">deps: '+h((t.depends_on||[]).join(', ')||'—')+'</div></div>').join('');document.getElementById('memory').innerHTML=(d.memory||[]).slice(0,8).map(m=>'<div class="taskrow"><b>'+h(m.kind)+'</b><div class="small muted">'+h(clip(m.summary,220))+'</div></div>').join('')||'<div class="empty">Memoria ancora vuota.</div>';document.getElementById('gates').innerHTML=(s.gates||[]).map(g=>'<div class="taskrow"><b>'+(g.passed?'✓ ':'○ ')+h(g.id)+'</b><div class="small muted">'+h(clip(g.detail||g.description,220))+'</div></div>').join('');}
async function tick(){try{const [sr,dr]=await Promise.all([fetch(stateUrl,{cache:'no-store'}),fetch(detailUrl,{cache:'no-store'})]);if(!sr.ok||!dr.ok)throw new Error('HTTP');render(await sr.json(),await dr.json());}catch(e){document.getElementById('detail').textContent='Dashboard non raggiungibile: '+e;}} tick();setInterval(tick,1000);
""".replace('__PROJECT__', json.dumps(project))
        return HTMLResponse(shell(f'{ws.manifest.name} — live', body, active='dashboard', extra_head=head, extra_script=script), headers={'Cache-Control': 'no-store'})
