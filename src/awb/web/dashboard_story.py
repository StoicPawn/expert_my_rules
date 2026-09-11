from __future__ import annotations

import json

from fastapi import Request
from fastapi.responses import HTMLResponse

from awb.core.workspace import load_workspace
from awb.web.dashboard_app import _cross_link, _root
from awb.web.ui import esc, shell


def _remove_route(app, path: str, method: str) -> None:
    method = method.upper()
    app.router.routes[:] = [
        route for route in app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and method in (getattr(route, 'methods', None) or set())
        )
    ]


def install_agent_story_dashboard(app) -> None:
    """Replace the project dashboard with a compact, agent-readable live view.

    The data contract remains `/project/{project}/state`, so this is deliberately
    presentation-only: no research state is mutated and the observer stays cheap.
    """
    _remove_route(app, '/project/{project}', 'GET')

    @app.get('/project/{project}', response_class=HTMLResponse)
    def project_story(request: Request, project: str):
        root = _root(project)
        ws = load_workspace(root)
        body = f"""
<div class='topbar'>
  <div><a class='small muted' href='/'>← dashboard</a><h1>{esc(ws.manifest.name)}</h1><p class='sub' id='northstar'>{esc(ws.manifest.goal)}</p></div>
  <div class='nav'><a href='{_cross_link(request,'AWB_PORT',8100)}'>← Progetti</a><a href='{_cross_link(request,'AWB_PORT',8100,f'/project/{project}')}'>Setup progetto</a><a href='{_cross_link(request,'AWB_LAB_PORT',8102,f'/?project={project}')}'>Research Lab</a></div>
</div>
<div class='grid'>
  <div class='col-8 card hero'>
    <div class='split'><div><h2>Che cosa sta succedendo</h2><div id='headline' class='story-head'>Caricamento…</div><div id='headlineDetail' class='muted'></div></div><span id='runBadge' class='badge'>—</span></div>
    <div class='meter' style='margin-top:14px'><i id='progressBar' style='width:0%'></i></div><div class='small muted' id='progressText' style='margin-top:6px'></div>
  </div>
  <div class='col-4 card'><h2>Costi e macchina</h2><div class='kpis compact-kpis'><div class='kpi'><span>API mese</span><b id='apiCost'>—</b></div><div class='kpi'><span>CPU</span><b id='cpu'>—</b></div><div class='kpi'><span>Modello locale</span><b id='model'>—</b></div><div class='kpi'><span>Runtime</span><b id='modelState'>—</b></div></div></div>

  <div class='col-12 card'>
    <div class='split'><div><h2>Agenti — chi fa cosa</h2><p class='small muted'>Una riga per agente: stato, ultima azione utile e proposta/giudizio più recente.</p></div><span id='activeAgent' class='badge'>nessuno live</span></div>
    <div id='agents' class='agent-grid'></div>
  </div>

  <div class='col-7 card'><div class='split'><div><h2>Task e prossima mossa</h2><p class='small muted'>Il task corrente o, se bloccato, la strategia proposta e il prossimo task aperto.</p></div><span id='taskBadge' class='badge'>—</span></div><div id='focusTask' class='empty'>Caricamento…</div></div>
  <div class='col-5 card'><h2>Ultimo esito</h2><div id='lastOutcome' class='empty'>Nessun esito disponibile.</div></div>

  <div class='col-7 card'><h2>Piano dei task</h2><div id='tasks'></div></div>
  <div class='col-5 card'><h2>North Star</h2><div id='gates'></div></div>

  <div class='col-12 card'>
    <div class='split'><div><h2>Timeline comprensibile</h2><p class='small muted'>Solo gli eventi che spiegano decisioni, output, review, errori e passaggi tra agenti.</p></div><button id='toggleRaw' class='secondary' type='button'>Dettagli tecnici</button></div>
    <div id='storyTimeline' class='timeline'></div><div id='rawWrap' class='hide'><h3 style='margin-top:16px'>Eventi grezzi recenti</h3><div id='rawEvents' class='timeline'></div></div>
  </div>
</div>
"""
        extra_head = """
<style>
.story-head{font-size:23px;font-weight:850;line-height:1.18;margin-bottom:5px}.agent-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.agent-card{border:1px solid var(--line);border-radius:13px;padding:12px;background:#f8fafc;min-height:145px}.agent-card.live{border-color:#8fb0ff;background:#f4f7ff}.agent-card h3{display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:15px}.agent-action{font-weight:750;font-size:13px;margin:7px 0 5px}.agent-note{font-size:12px;color:var(--muted);line-height:1.38}.agent-meta{font-size:11px;color:var(--muted);margin-top:8px}.compact-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.compact-kpis .kpi b{font-size:15px}.decision{border-left:4px solid var(--accent);background:#f5f8ff;padding:11px 12px;border-radius:9px}.decision.warn{border-color:var(--warn);background:#fffbf2}.decision.bad{border-color:var(--bad);background:#fff6f5}.decision.ok{border-color:var(--ok);background:#f1fcf6}.story-event{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fff}.story-event .who{font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.03em}.story-event .what{font-size:13px;font-weight:700;margin-top:3px}.story-event .why{font-size:12px;color:var(--muted);margin-top:3px}.taskline{display:flex;gap:8px;align-items:flex-start}.taskline .grow{min-width:0}.truncate2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(max-width:900px){.agent-grid{grid-template-columns:1fr 1fr}.story-head{font-size:21px}}
@media(max-width:560px){.agent-grid{grid-template-columns:1fr}.compact-kpis{grid-template-columns:1fr 1fr}}
</style>
"""
        script = r"""
const project=__PROJECT_JSON__;
const endpoint='/project/'+encodeURIComponent(project)+'/state';
let latest=null;
function h(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function clip(v,n=260){const s=String(v??'').replace(/\s+/g,' ').trim();return s.length>n?s.slice(0,n)+'…':s;}
function fmtSeconds(v){const n=Math.max(0,Number(v)||0);if(n<60)return Math.round(n)+'s';const m=Math.floor(n/60);if(m<60)return m+'m '+Math.round(n%60)+'s';return Math.floor(m/60)+'h '+(m%60)+'m';}
function fmtTs(ts){if(!ts)return '';try{return new Date(ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(_){return String(ts);}}
function badgeKind(status){status=String(status||'').toUpperCase();if(['DONE','COMPLETE','PASSED','APPROVED'].includes(status))return 'ok';if(['ERROR','FAILED','TECHNICAL_ERROR'].includes(status))return 'bad';if(['BLOCKED','REJECTED'].includes(status))return 'warn';if(['RUNNING','IN_PROGRESS','GENERATING','ALIVE'].includes(status))return 'live';return '';}
function evs(s, kinds){return (s.events||[]).filter(e=>kinds.includes(e.kind));}
function firstEvent(s,kinds,pred=()=>true){return (s.events||[]).find(e=>kinds.includes(e.kind)&&pred(e));}
function latestCall(s,role){return (s.model_calls||[]).find(c=>c.role===role)||null;}
function callMeta(c){if(!c)return 'nessuna chiamata registrata';return (c.model||c.node||'modello')+' · '+(c.success?'ok':'errore')+' · '+fmtSeconds(c.seconds||0);}
function roleEvent(s,role){
  if(role==='director')return firstEvent(s,['task_recovery_planned','task_decomposed','task_reframed','task_created','recovery_planner_error','model_call_finished','model_call_failed'],e=>['task_recovery_planned','task_decomposed','task_reframed','task_created','recovery_planner_error'].includes(e.kind)||(e.payload||{}).role==='director');
  if(role==='worker')return firstEvent(s,['work_output','model_call_finished','model_call_failed'],e=>e.kind==='work_output'||(e.payload||{}).role==='worker');
  if(role==='reviewer')return firstEvent(s,['review','model_call_finished','model_call_failed'],e=>e.kind==='review'||(e.payload||{}).role==='reviewer');
  return firstEvent(s,['verification','gate_evaluated','model_call_finished','model_call_failed'],e=>['verification','gate_evaluated'].includes(e.kind)||(e.payload||{}).role==='verifier');
}
function describeRole(s,role){
  const e=roleEvent(s,role), p=(e&&e.payload)||{}, c=latestCall(s,role), live=(s.runtime_progress||{}).role===role && !['completed_stream','idle'].includes((s.runtime_progress||{}).state);
  let action='In attesa', note='Nessuna decisione recente registrata.';
  if(e){
    if(e.kind==='work_output'){action='Ha prodotto un candidato';note=clip(p.text,300);}
    else if(e.kind==='review'){action=p.approved?'Ha approvato il candidato':'Ha sollevato obiezioni';const obs=(p.critical_objections||[])[0], rec=(p.recommendations||[])[0];note=clip(obs?('Obiezione: '+obs):(rec?('Raccomanda: '+rec):'Review completata.'),300);}
    else if(e.kind==='verification'||e.kind==='gate_evaluated'){action=p.passed?'Verifica superata':'Verifica non superata';note=clip(p.detail||('Gate '+(p.gate||p.stage||'')),300);}
    else if(e.kind==='task_recovery_planned'){action='Ha deciso la strategia di recupero';note=clip(p.strategy||p.rationale,300);}
    else if(e.kind==='task_decomposed'){action='Ha scomposto il problema';note=clip(p.strategy||('Creati '+((p.subtask_ids||[]).length)+' sottotask'),300);}
    else if(e.kind==='task_reframed'){action='Ha riformulato il task';note=clip(p.strategy||p.rationale,300);}
    else if(e.kind==='task_created'){action='Ha proposto il prossimo task';note=clip(p.title?((p.title||'')+': '+(p.description||'')):JSON.stringify(p),300);}
    else if(e.kind==='model_call_failed'){action='Chiamata modello fallita';note=clip(p.error,300);}
    else if(e.kind==='model_call_finished'){action='Chiamata modello completata';note='Risposta ricevuta dal modello.';}
  }
  if(live){action='Sta lavorando adesso';const rp=s.runtime_progress||{};note=clip((rp.detail||'')+(rp.visible_tail?' · output: '+rp.visible_tail:''),300)||'Generazione in corso.';}
  return {role,e,c,live,action,note};
}
function roleLabel(r){return {director:'Director',worker:'Worker',reviewer:'Reviewer',verifier:'Verifier'}[r]||r;}
function rolePurpose(r){return {director:'sceglie e pianifica',worker:'produce il candidato',reviewer:'attacca e critica',verifier:'controlla evidenze/gate'}[r]||'';}
function renderAgents(s){
  const roles=['director','worker','reviewer','verifier'];
  const rows=roles.map(r=>describeRole(s,r));
  const active=rows.find(x=>x.live);const ab=document.getElementById('activeAgent');ab.textContent=active?roleLabel(active.role)+' live':'nessun agente live';ab.className='badge '+(active?'live':'');
  document.getElementById('agents').innerHTML=rows.map(x=>'<div class="agent-card '+(x.live?'live':'')+'"><h3><span>'+roleLabel(x.role)+'</span><span class="badge '+(x.live?'live':'')+'">'+(x.live?'LIVE':'ULTIMO')+'</span></h3><div class="small muted">'+rolePurpose(x.role)+'</div><div class="agent-action">'+h(x.action)+'</div><div class="agent-note">'+h(x.note)+'</div><div class="agent-meta">'+h(callMeta(x.c))+(x.e?' · '+h(fmtTs(x.e.ts)):'')+'</div></div>').join('');
}
function renderFocus(s){
  const current=s.current_task;const tasks=s.tasks||[];const blocked=tasks.find(t=>t.status==='BLOCKED');const open=tasks.find(t=>t.status==='OPEN');const focus=current||blocked||open;
  const tb=document.getElementById('taskBadge');tb.textContent=focus?focus.status:'NESSUNO';tb.className='badge '+badgeKind(focus&&focus.status);
  if(!focus){document.getElementById('focusTask').className='empty';document.getElementById('focusTask').textContent='Nessun task attivo o aperto.';return;}
  let next='';if(focus.next_strategy)next='<div style="margin-top:8px"><b>Proposta successiva:</b> '+h(clip(focus.next_strategy,700))+'</div>';if(focus.status==='BLOCKED'&&open&&open.id!==focus.id)next+='<div style="margin-top:8px"><b>Primo task aperto:</b> '+h(open.title)+'</div>';
  document.getElementById('focusTask').className='decision '+badgeKind(focus.status);document.getElementById('focusTask').innerHTML='<b>'+h(focus.title)+'</b><div class="small muted" style="margin-top:4px">'+h(clip(focus.description,500))+'</div>'+next+'<div class="small muted" style="margin-top:8px">tentativi scientifici '+focus.scientific_attempts+' · errori tecnici '+focus.technical_failures+'</div>';
}
function renderOutcome(s){
  const a=s.latest_attempt||{};const review=a.review||{};const verification=a.verification||{};let status=a.status||'—';let text='';
  if(review&&Object.keys(review).length){const obs=(review.critical_objections||[])[0],rec=(review.recommendations||[])[0];text=review.approved?'Review approvata.':'Review non approvata.';if(obs)text+=' '+clip(obs,350);else if(rec)text+=' '+clip(rec,350);}
  if(verification&&Object.keys(verification).length)text+=(text?' ':'')+(verification.passed?'Verifica superata. ':'Verifica non superata. ')+clip(verification.detail,300);
  if(a.error)text='Errore tecnico: '+clip(a.error,450);
  const box=document.getElementById('lastOutcome');box.className='decision '+badgeKind(status);box.innerHTML='<div class="split"><b>'+h(status)+'</b><span class="small muted">'+h(a.task_id||'')+'</span></div><div style="margin-top:6px">'+h(text||'Tentativo registrato; nessun giudizio sintetico disponibile.')+'</div>';
}
function humanEvent(e){const p=e.payload||{};let who='',what='',why='';
  if(e.kind==='work_output'){who='Worker';what='Candidato prodotto';why=clip(p.text,240);}
  else if(e.kind==='review'){who='Reviewer';what=p.approved?'Candidato approvato':'Candidato contestato';why=clip(((p.critical_objections||[])[0]||((p.recommendations||[])[0])||''),240);}
  else if(e.kind==='verification'){who='Verifier';what=p.passed?'Verifica superata':'Verifica fallita';why=clip(p.detail,240);}
  else if(e.kind==='gate_evaluated'){who='Verifier';what=(p.passed?'Gate chiuso: ':'Gate aperto: ')+(p.gate||'');why=clip(p.detail,240);}
  else if(e.kind==='task_recovery_planned'){who='Director';what='Nuova strategia proposta';why=clip(p.strategy||p.rationale,240);}
  else if(e.kind==='task_decomposed'){who='Director';what='Problema scomposto in sottotask';why=clip(p.strategy,240);}
  else if(e.kind==='task_created'){who='Director';what='Nuovo task creato';why=clip((p.title||'')+' '+(p.description||''),240);}
  else if(e.kind==='task_blocked'){who='Sistema';what='Task bloccato dalla review/verifica';why=clip((p.critical_objections||[])[0]||'',240);}
  else if(e.kind==='task_scientifically_closed'){who='Sistema';what='Task chiuso scientificamente';why='Tentativi: '+(p.scientific_attempts??'—');}
  else if(e.kind==='task_technical_error'){who='Sistema';what='Errore tecnico';why=clip(p.error,240);}
  else if(e.kind==='cloud_call_fallback_local'){who='Router';what='Fallback al modello locale';why=clip(p.error,240);}
  else if(e.kind==='model_call_failed'){who=roleLabel(p.role||'modello');what='Chiamata modello fallita';why=clip(p.error,240);}
  else if(e.kind==='model_call_finished'){who=roleLabel(p.role||'modello');what='Chiamata modello completata';why=(p.model||p.node||'')+(p.seconds!=null?' · '+fmtSeconds(p.seconds):'')+(p.cost_eur!=null?' · €'+Number(p.cost_eur).toFixed(3):'');}
  else return null;return {who,what,why,ts:e.ts};}
function renderTimeline(s){const items=(s.events||[]).map(humanEvent).filter(Boolean).slice(0,14);document.getElementById('storyTimeline').innerHTML=items.length?items.map(x=>'<div class="story-event"><div class="split"><span class="who">'+h(x.who)+'</span><span class="small muted">'+h(fmtTs(x.ts))+'</span></div><div class="what">'+h(x.what)+'</div>'+(x.why?'<div class="why">'+h(x.why)+'</div>':'')+'</div>').join(''):'<div class="empty">Nessun evento significativo recente.</div>';document.getElementById('rawEvents').innerHTML=(s.events||[]).slice(0,20).map(e=>'<div class="event"><b>'+h(e.kind)+'</b><span>'+h(e.task_id||'')+' · '+h(fmtTs(e.ts))+'</span></div>').join('');}
function renderTasks(s){const rows=(s.tasks||[]).slice(0,18);document.getElementById('tasks').innerHTML=rows.length?rows.map(t=>'<div class="task '+(t.status==='DONE'?'done':t.status==='IN_PROGRESS'?'live':t.status==='ERROR'?'bad':'')+'"><div class="taskline"><div class="grow"><b>'+h(t.title)+'</b><div class="small muted truncate2">'+h(t.description)+'</div>'+(t.next_strategy?'<div class="small"><b>Next:</b> '+h(clip(t.next_strategy,350))+'</div>':'')+'</div><span class="badge '+badgeKind(t.status)+'">'+h(t.status)+'</span></div></div>').join(''):'<div class="empty">Nessun task.</div>';}
function renderGates(s){document.getElementById('gates').innerHTML=(s.gates||[]).map(g=>'<div class="task '+(g.passed?'done':'')+'"><div class="split"><div><b>'+h(g.id)+'</b><div class="small muted">'+h(clip(g.description,220))+'</div></div><span class="badge '+(g.passed?'ok':'warn')+'">'+(g.passed?'DONE':'OPEN')+'</span></div></div>').join('')||'<div class="empty">Nessun gate.</div>';}
function render(s){latest=s;document.getElementById('northstar').textContent=s.goal||'';const j=s.job||{};const status=s.overall_status||s.run_status||j.status||'NOT STARTED';const current=s.current_task;const blocked=(s.tasks||[]).find(t=>t.status==='BLOCKED');const open=(s.tasks||[]).find(t=>t.status==='OPEN');let headline='',detail='';if(current){headline=roleLabel((s.runtime_progress||{}).role||'worker')+' sta lavorando: '+current.title;detail='Il run è attivo sul task corrente.';}else if(status==='RUNNING'&&blocked){headline='Task bloccato, il Director ha già proposto il recupero';detail=blocked.next_strategy||('Prossimo task: '+(open?open.title:'in pianificazione'));}else if(status==='RUNNING'&&open){headline='Run attivo, prossimo task pronto';detail=open.title;}else if(status==='COMPLETE'){headline='North Star completata';detail='Tutti i gate richiesti sono chiusi.';}else{headline='Nessun agente in esecuzione';detail=j.detail||status;}
  document.getElementById('headline').textContent=headline;document.getElementById('headlineDetail').textContent=detail;const rb=document.getElementById('runBadge');rb.textContent=status;rb.className='badge '+badgeKind(status);document.getElementById('progressBar').style.width=Math.max(0,Math.min(100,Number(s.progress_percent)||0))+'%';document.getElementById('progressText').textContent=(s.progress_percent||0)+'% · '+s.passed_gates+'/'+s.total_gates+' gate · '+s.done_tasks+'/'+s.total_tasks+' task chiusi';const r=s.resources||{},b=s.budget||{},p=s.runtime_progress||{};document.getElementById('cpu').textContent=r.cpu_percent==null?'—':Math.round(r.cpu_percent)+'%';document.getElementById('model').textContent=r.model||'—';document.getElementById('modelState').textContent=(p.role?p.role+' · ':'')+(p.state||'idle');document.getElementById('apiCost').textContent='€'+Number(b.monthly_spent_eur||0).toFixed(3)+' / €'+Number(b.monthly_budget_eur||0).toFixed(2);renderAgents(s);renderFocus(s);renderOutcome(s);renderTasks(s);renderGates(s);renderTimeline(s);}
async function tick(){try{const r=await fetch(endpoint,{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json());}catch(err){document.getElementById('headline').textContent='Dashboard non raggiungibile';document.getElementById('headlineDetail').textContent=String(err);}}
document.getElementById('toggleRaw').addEventListener('click',()=>document.getElementById('rawWrap').classList.toggle('hide'));
tick();setInterval(tick,3000);
""".replace('__PROJECT_JSON__', json.dumps(project))
        return HTMLResponse(shell('Expert My Rules — Live', body, active='dashboard', extra_head=extra_head, extra_script=script), headers={'Cache-Control': 'no-store'})
