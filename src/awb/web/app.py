from __future__ import annotations
import html, os, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from awb.core.models import Gate, JobStatus, Task
from awb.core.orchestrator import Orchestrator
from awb.core.planner import propose_manifest
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace

app=FastAPI(title='Expert My Rules')
EXECUTOR=ThreadPoolExecutor(max_workers=4,thread_name_prefix='awb')
ACTIVE:dict[str,object]={}

def base_dir():
    p=Path(os.getenv('AWB_WORKSPACES_DIR','workspaces')).resolve(); p.mkdir(parents=True,exist_ok=True); return p

def page(title,body):
    style='''body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1100px;margin:30px auto;padding:0 18px;background:#f5f5f7;color:#161616}.card,.panel{display:block;background:white;padding:20px;margin:14px 0;border-radius:18px;box-shadow:0 1px 6px #0001;text-decoration:none;color:inherit}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.muted{color:#666}.type{text-transform:uppercase;font-size:12px;letter-spacing:.08em;color:#666}input,select,textarea,button{font:inherit;padding:10px;border:1px solid #ccc;border-radius:10px;box-sizing:border-box}input,select,textarea{width:100%;margin:5px 0 10px}button{cursor:pointer;background:#111;color:#fff;border:0}.secondary{background:#e8e8ec;color:#111}.danger{background:#8f1d1d}.warn{background:#873800}.pass{color:#087c35}.open{color:#a44b00}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}.inline{display:inline}.inline button{width:auto;margin-right:4px}code{background:#eee;padding:2px 5px;border-radius:5px}'''
    return f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{style}</style></head><body>{body}</body></html>"

def slug(s): return ''.join(c for c in s.strip().replace(' ','_') if c.isalnum() or c in '_-.')

def _run_continuous(root:Path,job_id:str):
    ws=load_workspace(root); ledger=Ledger(root/'ledger.sqlite3'); orch=Orchestrator(ws); ledger.update_job(job_id,status=JobStatus.RUNNING,detail='autonomous project active')
    try:
        while True:
            current=ledger.get_job(job_id)
            if not current:return
            if current['status']==JobStatus.CANCEL_REQUESTED.value:
                ledger.update_job(job_id,status=JobStatus.CANCELLED,detail='cancelled by user'); return
            while current['status']==JobStatus.PAUSED.value:
                time.sleep(.5); current=ledger.get_job(job_id)
                if current['status']==JobStatus.CANCEL_REQUESTED.value:
                    ledger.update_job(job_id,status=JobStatus.CANCELLED,detail='cancelled by user'); return
            if orch.is_complete(): ledger.update_job(job_id,status=JobStatus.COMPLETE,detail='all required completion gates passed'); return
            before=int(current['steps_done'])
            def control():
                cur=ledger.get_job(job_id)
                if cur['status']==JobStatus.PAUSED.value:return 'pause'
                if cur['status']==JobStatus.CANCEL_REQUESTED.value:return 'cancel'
                return 'run'
            def on_step(count,_): ledger.update_job(job_id,steps_done=before+count,detail='autonomous project active')
            orch.run(ws.manifest.runtime.continuous_session_steps,ws.manifest.runtime.continuous_session_minutes,control=control,on_step=on_step)
            time.sleep(ws.manifest.runtime.checkpoint_pause_seconds)
    except Exception as exc:
        ledger.update_job(job_id,status=JobStatus.FAILED,detail=f'{type(exc).__name__}: {exc}')

def _start(root:Path,job_id:str):
    f=ACTIVE.get(job_id)
    if f is None or getattr(f,'done',lambda:True)(): ACTIVE[job_id]=EXECUTOR.submit(_run_continuous,root,job_id)

@app.on_event('startup')
def recover_jobs():
    for root in base_dir().iterdir():
        if not (root/'project.yaml').exists():continue
        try:
            ledger=Ledger(root/'ledger.sqlite3')
            for job in ledger.recoverable_jobs(): _start(root,job['id'])
        except Exception: pass

@app.get('/',response_class=HTMLResponse)
def index():
    projects=''
    for root in sorted(base_dir().iterdir()):
        if not (root/'project.yaml').exists():continue
        try:
            ws=load_workspace(root); l=Ledger(root/'ledger.sqlite3'); gates=l.gate_state(); passed=sum(1 for g in ws.manifest.gates if gates.get(g.id,{}).get('passed')); job=l.latest_job(); status=job['status'] if job else 'NOT STARTED'
            projects+=f"<a class='card' href='/project/{html.escape(root.name)}'><h2>{html.escape(ws.manifest.name)}</h2><div class='type'>{html.escape(ws.manifest.type)}</div><p>{html.escape(ws.manifest.goal)}</p><b>{passed}/{len(ws.manifest.gates)} gates</b> · {html.escape(status)}</a>"
        except Exception: pass
    create="""<div class='panel'><h2>New project</h2><p class='muted'>Give the final goal. Expert My Rules proposes the team and Definition of Done locally; you can edit them before launch.</p><form method='post' action='/create'><label>What must exist when this project is truly finished?</label><textarea name='goal' required rows='6' placeholder='Example: Obtain a rigorous, novel result strong enough for a submission-ready Annals of Probability paper.'></textarea><label>Optional project name</label><input name='name' placeholder='auto-generated if empty'><button>Create proposed workspace</button></form></div>"""
    return page('Expert My Rules',f"<h1>Expert My Rules</h1><p class='muted'>Tell it what done looks like.</p>{create}<h2>Projects</h2>{projects or '<p>No project yet.</p>'}")

@app.post('/create')
def create(goal:str=Form(...),name:str=Form('')):
    manifest=propose_manifest(goal,name.strip() or None,use_local_ai=True); safe=slug(manifest['name']); root=base_dir()/safe
    i=2
    while root.exists(): root=base_dir()/f'{safe}_{i}'; i+=1
    manifest['name']=root.name; write_workspace(root,manifest); return RedirectResponse(f'/project/{root.name}',status_code=303)

def job_controls(slug_,job):
    if not job:return '<span class="muted">Not launched yet.</span>'
    jid=html.escape(job['id']); s=job['status']; buttons=''
    if s==JobStatus.RUNNING.value: buttons=f"<form class='inline' method='post' action='/project/{slug_}/job/{jid}/pause'><button class='secondary'>Pause</button></form><form class='inline' method='post' action='/project/{slug_}/job/{jid}/cancel'><button class='danger'>Cancel</button></form>"
    elif s==JobStatus.PAUSED.value: buttons=f"<form class='inline' method='post' action='/project/{slug_}/job/{jid}/resume'><button>Resume</button></form><form class='inline' method='post' action='/project/{slug_}/job/{jid}/cancel'><button class='danger'>Cancel</button></form>"
    elif s in {JobStatus.FAILED.value,JobStatus.CANCELLED.value,JobStatus.BUDGET_FINISHED.value}: buttons=f"<form class='inline' method='post' action='/project/{slug_}/job/{jid}/resume'><button>Continue project</button></form>"
    return f"<p><b>{html.escape(s)}</b> · {job['steps_done']} iterations · {html.escape(job['detail'] or '')}</p>{buttons}"

@app.get('/project/{slug_}',response_class=HTMLResponse)
def project(slug_:str):
    root=base_dir()/slug_
    try: ws=load_workspace(root)
    except Exception as e: raise HTTPException(404,str(e))
    l=Ledger(root/'ledger.sqlite3'); state=l.gate_state(); job=l.latest_job(); esc=ws.manifest.runtime.escalation
    gate_rows=''.join(f"<tr><td><b>{html.escape(g.id)}</b><br>{html.escape(g.description)}</td><td class='{'pass' if state.get(g.id,{}).get('passed') else 'open'}'>{'PASS' if state.get(g.id,{}).get('passed') else 'OPEN'}</td><td><form class='inline' method='post' action='/project/{slug_}/gate'><input type='hidden' name='gate_id' value='{html.escape(g.id)}'><button name='state' value='pass'>Pass</button><button class='secondary' name='state' value='open'>Reopen</button></form></td></tr>" for g in ws.manifest.gates)
    agents=''.join(f"<div class='panel'><h3>{html.escape(a.id)} <span class='type'>{html.escape(a.role)}</span></h3><form method='post' action='/project/{slug_}/agent'><input type='hidden' name='agent_id' value='{html.escape(a.id)}'><textarea name='instructions' rows='5'>{html.escape(a.instructions)}</textarea><button>Save instructions</button></form></div>" for a in ws.manifest.agents)
    tasks=''.join(f"<tr><td>{html.escape(t.id)}</td><td>{html.escape(t.title)}</td><td>{t.status.value}</td><td>{t.metadata.get('attempts',0)}</td></tr>" for t in l.list_tasks()) or "<tr><td colspan='4'>The Director will create the first task after launch.</td></tr>"
    cloud=f"{'ENABLED' if esc.enabled else 'OFF'} · budget €{esc.daily_budget_eur:.2f}/day · max {esc.max_cloud_calls_per_run} cloud calls/run"
    body=f"""<a href='/'>← Projects</a><h1>{html.escape(ws.manifest.name)}</h1><div class='type'>{html.escape(ws.manifest.type)}</div>
    <div class='panel'><h2>North Star</h2><form method='post' action='/project/{slug_}/goal'><textarea name='goal' rows='5'>{html.escape(ws.manifest.goal)}</textarea><button>Save goal</button></form></div>
    <div class='grid'><div class='panel'><h2>Launch</h2><p>Once started, the project keeps taking checkpointed autonomous sessions until every required gate passes or you pause/cancel it.</p><form method='post' action='/project/{slug_}/launch'><button>Start autonomous project</button></form>{job_controls(slug_,job)}</div>
    <div class='panel'><h2>Models</h2><p>Local default: <code>{html.escape(ws.manifest.runtime.default_provider.kind)} / {html.escape(ws.manifest.runtime.default_provider.model or '')}</code></p><p>Cloud escalation: <b>{html.escape(cloud)}</b></p><p class='muted'>With cloud OFF, budget 0, or no API key, escalation cannot occur.</p><form method='post' action='/project/{slug_}/cloud'><label><input style='width:auto' type='checkbox' name='enabled' {'checked' if esc.enabled else ''}> enable cloud escalation</label><label>Daily budget €</label><input type='number' step='0.1' min='0' name='budget' value='{esc.daily_budget_eur}'><label>Max cloud calls per run</label><input type='number' min='0' name='max_calls' value='{esc.max_cloud_calls_per_run}'><button class='warn'>Save cloud policy</button></form></div></div>
    <div class='panel'><h2>Definition of Done</h2><p class='muted'>Edit these before launch or at any time. The system may not declare completion until all required gates pass.</p><table><tr><th>Condition</th><th>State</th><th>Control</th></tr>{gate_rows}</table><h3>Add condition</h3><form method='post' action='/project/{slug_}/gate/add'><input name='gate_id' required placeholder='condition id'><textarea name='description' required rows='2' placeholder='What must be objectively true?'></textarea><button>Add condition</button></form></div>
    <h2>Proposed team</h2><div class='grid'>{agents}</div>
    <div class='panel'><h2>Direct the project without changing its goal</h2><form method='post' action='/project/{slug_}/task'><input name='title' required placeholder='Optional temporary directive'><textarea name='description' rows='3' placeholder='Example: tonight attack the converse with counterexamples first'></textarea><button>Add directive</button></form></div>
    <div class='panel'><h2>Task ledger</h2><table><tr><th>ID</th><th>Task</th><th>Status</th><th>Attempts</th></tr>{tasks}</table></div>"""
    return page(ws.manifest.name,body)

@app.post('/project/{slug_}/goal')
def set_goal(slug_:str,goal:str=Form(...)):
    ws=load_workspace(base_dir()/slug_); ws.manifest.goal=goal.strip(); save_manifest(ws); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/agent')
def set_agent(slug_:str,agent_id:str=Form(...),instructions:str=Form(...)):
    ws=load_workspace(base_dir()/slug_); a=next((a for a in ws.manifest.agents if a.id==agent_id),None)
    if not a: raise HTTPException(400,'Unknown agent')
    a.instructions=instructions.strip(); save_manifest(ws); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/gate')
def set_gate(slug_:str,gate_id:str=Form(...),state:str=Form(...)):
    ws=load_workspace(base_dir()/slug_); Ledger(ws.root/'ledger.sqlite3').set_gate(gate_id,state=='pass','set from dashboard'); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/gate/add')
def add_gate(slug_:str,gate_id:str=Form(...),description:str=Form(...)):
    ws=load_workspace(base_dir()/slug_); gid=slug(gate_id)
    if not gid or gid in {g.id for g in ws.manifest.gates}: raise HTTPException(400,'Invalid/duplicate gate')
    ws.manifest.gates.append(Gate(id=gid,description=description.strip(),required=True,manual=True)); save_manifest(ws); Ledger(ws.root/'ledger.sqlite3').set_gate(gid,False,'not evaluated'); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/task')
def add_task(slug_:str,title:str=Form(...),description:str=Form('')):
    ws=load_workspace(base_dir()/slug_); l=Ledger(ws.root/'ledger.sqlite3'); t=Task(id=f'USER-{len(l.list_tasks())+1:04d}',title=title,description=description or title,priority=10,created_by='user'); l.upsert_task(t); l.event('task_created',t.model_dump(mode='json'),t.id); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/cloud')
def cloud(slug_:str,enabled:str|None=Form(None),budget:float=Form(0),max_calls:int=Form(0)):
    ws=load_workspace(base_dir()/slug_); p=ws.manifest.runtime.escalation; p.enabled=enabled is not None; p.daily_budget_eur=max(0,budget); p.max_cloud_calls_per_run=max(0,max_calls); save_manifest(ws); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/launch')
def launch(slug_:str):
    root=base_dir()/slug_; ws=load_workspace(root); l=Ledger(root/'ledger.sqlite3'); current=l.latest_job()
    if current and current['status'] in {JobStatus.RUNNING.value,JobStatus.PAUSED.value}: jid=current['id']
    else: jid=l.create_job(0,0,continuous=True)
    l.update_job(jid,status=JobStatus.RUNNING,detail='autonomous project active'); _start(root,jid); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/job/{jid}/pause')
def pause(slug_:str,jid:str): Ledger(base_dir()/slug_/'ledger.sqlite3').update_job(jid,status=JobStatus.PAUSED,detail='paused by user'); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/job/{jid}/resume')
def resume(slug_:str,jid:str):
    root=base_dir()/slug_; l=Ledger(root/'ledger.sqlite3'); l.update_job(jid,status=JobStatus.RUNNING,detail='resumed by user'); _start(root,jid); return RedirectResponse(f'/project/{slug_}',303)
@app.post('/project/{slug_}/job/{jid}/cancel')
def cancel(slug_:str,jid:str): Ledger(base_dir()/slug_/'ledger.sqlite3').update_job(jid,status=JobStatus.CANCEL_REQUESTED,detail='cancel requested'); return RedirectResponse(f'/project/{slug_}',303)
