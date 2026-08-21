from __future__ import annotations

import html
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.models import Gate, JobStatus, ProviderSpec, Task, ToolSpec
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.templates.templates import get_template

app = FastAPI(title="Expert My Rules")
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="awb")
ACTIVE_FUTURES: dict[str, object] = {}


def base_dir() -> Path:
    p = Path(os.getenv("AWB_WORKSPACES_DIR", "workspaces")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def cards():
    out = []
    for p in sorted(base_dir().iterdir()):
        if not (p / "project.yaml").exists():
            continue
        try:
            ws = load_workspace(p)
            ledger = Ledger(p / "ledger.sqlite3")
            tasks = ledger.list_tasks()
            gates = ledger.gate_state()
            passed = sum(1 for g in ws.manifest.gates if gates.get(g.id, {}).get("passed"))
            out.append((p.name, ws, tasks, passed, len(ws.manifest.gates), ledger.latest_job()))
        except Exception:
            continue
    return out


STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1150px;margin:36px auto;padding:0 18px;background:#f5f5f7;color:#161616}
.card,.panel{display:block;text-decoration:none;color:inherit;background:white;padding:20px;margin:14px 0;border-radius:18px;box-shadow:0 1px 6px #0001}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}.muted{color:#666}.type{text-transform:uppercase;font-size:12px;letter-spacing:.08em;color:#666}
input,select,textarea,button{font:inherit;padding:10px;border:1px solid #ccc;border-radius:10px;box-sizing:border-box}input,select,textarea{width:100%;margin:5px 0 10px}button{cursor:pointer;background:#111;color:white;border:0}.secondary{background:#e8e8ec;color:#111}.warn{background:#873800}.danger{background:#8f1d1d}
table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}.pass{color:#087c35}.open{color:#a44b00}code{background:#eee;padding:2px 5px;border-radius:5px}.inline{display:inline}.inline button{width:auto;margin-right:4px}.small{font-size:13px}
"""


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"


def _safe_slug(name: str) -> str:
    return "".join(c for c in name.strip().replace(" ", "_") if c.isalnum() or c in "_-." )


@app.get("/", response_class=HTMLResponse)
def index():
    project_html = ""
    for slug, ws, tasks, passed, total, job in cards():
        open_count = sum(1 for t in tasks if t.status.value not in {"DONE", "REJECTED"})
        job_text = f" · last job {job['status']}" if job else ""
        project_html += f"""<a class='card' href='/project/{html.escape(slug)}'><h2>{html.escape(ws.manifest.name)}</h2><div class='type'>{html.escape(ws.manifest.type)}</div><p>{html.escape(ws.manifest.goal)}</p><b>{passed}/{total} gates</b> · {open_count} unresolved tasks{html.escape(job_text)}</a>"""
    create = """
    <div class='panel'><h2>New workspace</h2><form method='post' action='/create'>
    <label>Name</label><input name='name' required placeholder='my_project'>
    <label>Type</label><select name='kind'><option>custom</option><option>research</option><option>software</option></select>
    <label>Final objective</label><textarea name='goal' required rows='4' placeholder='Define a stable, testable end state'></textarea>
    <button>Create workspace</button></form></div>
    """
    return page("Expert My Rules", f"<h1>Expert My Rules</h1><p class='muted'>Experts work, review, challenge and iterate — by your rules.</p>{create}<h2>Workspaces</h2>{project_html or '<p>No workspace yet.</p>'}")


@app.post("/create")
def create(name: str = Form(...), kind: str = Form(...), goal: str = Form(...)):
    safe = _safe_slug(name)
    if not safe:
        raise HTTPException(400, "Invalid name")
    root = base_dir() / safe
    if root.exists():
        raise HTTPException(409, "Workspace already exists")
    write_workspace(root, get_template(kind, safe, goal.strip()))
    return RedirectResponse(f"/project/{safe}", status_code=303)


def _job_controls(slug: str, job: dict | None) -> str:
    if not job:
        return "<span class='muted'>No run yet.</span>"
    status = job["status"]
    jid = html.escape(job["id"])
    buttons = ""
    if status == JobStatus.RUNNING.value:
        buttons = f"<form class='inline' method='post' action='/project/{slug}/job/{jid}/pause'><button class='secondary'>Pause</button></form><form class='inline' method='post' action='/project/{slug}/job/{jid}/cancel'><button class='danger'>Cancel</button></form>"
    elif status == JobStatus.PAUSED.value:
        buttons = f"<form class='inline' method='post' action='/project/{slug}/job/{jid}/resume'><button>Resume</button></form><form class='inline' method='post' action='/project/{slug}/job/{jid}/cancel'><button class='danger'>Cancel</button></form>"
    elif status in {JobStatus.FAILED.value, JobStatus.BUDGET_FINISHED.value, JobStatus.CANCELLED.value}:
        buttons = f"<form class='inline' method='post' action='/project/{slug}/job/{jid}/resume'><button>Continue</button></form>"
    return f"<p><b>{html.escape(status)}</b> · {job['steps_done']} steps · {html.escape(job['detail'] or '')}</p>{buttons}"


@app.get("/project/{slug}", response_class=HTMLResponse)
def project(slug: str):
    root = base_dir() / slug
    try:
        ws = load_workspace(root)
    except Exception as e:
        raise HTTPException(404, str(e))
    ledger = Ledger(root / "ledger.sqlite3")
    tasks = ledger.list_tasks()
    task_rows = "".join(
        f"<tr><td>{html.escape(t.id)}</td><td><b>{html.escape(t.title)}</b><br><span class='muted'>{html.escape(t.description[:300])}</span></td><td>{t.status.value}</td><td>{t.metadata.get('attempts',0)}</td></tr>" for t in tasks
    ) or "<tr><td colspan='4'>No tasks yet. The Director will create one on first run.</td></tr>"
    state = ledger.gate_state()
    gate_rows = "".join(
        f"<tr><td>{html.escape(g.id)}</td><td class='{'pass' if state.get(g.id,{}).get('passed') else 'open'}'>{'PASS' if state.get(g.id,{}).get('passed') else 'OPEN'}</td><td>{html.escape(g.description)}</td><td><form class='inline' method='post' action='/project/{slug}/gate'><input type='hidden' name='gate_id' value='{html.escape(g.id)}'><button name='state' value='pass'>Pass</button><button class='secondary' name='state' value='open'>Reopen</button></form></td></tr>" for g in ws.manifest.gates
    ) or "<tr><td colspan='4'>No gates.</td></tr>"

    agent_panels = ""
    for a in ws.manifest.agents:
        p = a.provider or ProviderSpec()
        agent_panels += f"""<div class='panel'><h3>{html.escape(a.id)} <span class='type'>{html.escape(a.role)}</span></h3><form method='post' action='/project/{slug}/agent'>
        <input type='hidden' name='agent_id' value='{html.escape(a.id)}'>
        <label>Instructions</label><textarea name='instructions' rows='4'>{html.escape(a.instructions)}</textarea>
        <label>Provider</label><select name='kind'><option value=''>inherit default</option><option {'selected' if p.kind=='mock' and a.provider else ''}>mock</option><option {'selected' if p.kind=='ollama' else ''}>ollama</option><option {'selected' if p.kind=='openai' else ''}>openai</option></select>
        <label>Model</label><input name='model' value='{html.escape(p.model or '')}' placeholder='inherit/default model'>
        <label>Allowed tools (comma-separated IDs)</label><input name='tools' value='{html.escape(', '.join(a.tools))}'>
        <button>Save agent</button></form></div>"""

    tool_rows = "".join(
        f"<tr><td>{html.escape(t.id)}</td><td>{html.escape(t.type)}</td><td>{html.escape(t.description)}</td><td><code>{html.escape(t.command or '')}</code></td></tr>" for t in ws.manifest.tools
    ) or "<tr><td colspan='4'>No tools.</td></tr>"
    provider = ws.manifest.runtime.default_provider
    job = ledger.latest_job()
    body = f"""
    <a href='/'>← Workspaces</a><h1>{html.escape(ws.manifest.name)}</h1><div class='type'>{html.escape(ws.manifest.type)}</div>
    <div class='panel'><h2>North Star</h2><form method='post' action='/project/{slug}/goal'><textarea name='goal' rows='4'>{html.escape(ws.manifest.goal)}</textarea><button>Save objective</button></form><p class='muted'>Default provider: <code>{html.escape(provider.kind)}</code> {html.escape(provider.model or '')}</p></div>
    <div class='grid'><div class='panel'><h2>Run now</h2><form method='post' action='/project/{slug}/run'><label>Steps</label><input name='steps' type='number' value='1' min='1' max='25'><button>Run autonomous loop</button></form><hr><h3>End-of-day run</h3><form method='post' action='/project/{slug}/overnight'><label>Hours</label><input name='hours' type='number' value='8' min='0.1' max='24' step='0.5'><button>Launch background job</button></form>{_job_controls(slug, job)}<p class='muted small'>The job state is persisted in SQLite. Browser closure does not stop the running process.</p></div>
    <div class='panel'><h2>Add task</h2><form method='post' action='/project/{slug}/task'><input name='title' required placeholder='Task title'><textarea name='description' rows='3' placeholder='Acceptance criteria / context'></textarea><button>Add high-priority task</button></form></div></div>
    <div class='grid'><div class='panel'><h2>Default model provider</h2><form method='post' action='/project/{slug}/provider'><select name='kind'><option {'selected' if provider.kind=='mock' else ''}>mock</option><option {'selected' if provider.kind=='ollama' else ''}>ollama</option><option {'selected' if provider.kind=='openai' else ''}>openai</option></select><input name='model' value='{html.escape(provider.model or '')}' placeholder='e.g. qwen3:8b'><button>Save default provider</button></form><p class='muted'>API keys remain environment variables and are never written into the workspace.</p></div>
    <div class='panel'><h2>Add completion gate</h2><form method='post' action='/project/{slug}/gate/add'><input name='gate_id' required placeholder='e.g. security_pass'><textarea name='description' required rows='2' placeholder='Objective completion rule'></textarea><label><input style='width:auto' type='checkbox' name='required' checked> required</label><label>Validator name (optional)</label><input name='validator'><button>Add gate</button></form></div></div>
    <h2>Agents</h2><div class='grid'>{agent_panels}</div>
    <div class='panel'><h2>Tool Layer</h2><p class='muted'>The Worker can call only tools explicitly assigned to it. File paths are sandboxed to this workspace. Shell tools execute a fixed configured command, not arbitrary model-generated shell.</p><table><tr><th>ID</th><th>Type</th><th>Description</th><th>Fixed command</th></tr>{tool_rows}</table><h3>Add tool</h3><form method='post' action='/project/{slug}/tool/add'><input name='tool_id' required placeholder='tool id'><select name='tool_type'><option>list_files</option><option>read_file</option><option>write_file</option><option>shell</option></select><input name='description' placeholder='What this tool does'><input name='command' placeholder='Fixed shell command (only for shell)'><label><input style='width:auto' type='checkbox' name='writable'> allow writes (write_file only)</label><button class='warn'>Add tool</button></form><p class='muted small'>Shell tools are intended only for trusted local workspaces.</p></div>
    <div class='panel'><h2>Completion gates</h2><table><tr><th>Gate</th><th>State</th><th>Rule</th><th>Manual control</th></tr>{gate_rows}</table></div>
    <div class='panel'><h2>Tasks</h2><table><tr><th>ID</th><th>Task</th><th>Status</th><th>Attempts</th></tr>{task_rows}</table></div>
    """
    return page(ws.manifest.name, body)


@app.post("/project/{slug}/goal")
def set_goal(slug: str, goal: str = Form(...)):
    ws = load_workspace(base_dir() / slug)
    ws.manifest.goal = goal.strip()
    save_manifest(ws)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/task")
def add_task(slug: str, title: str = Form(...), description: str = Form("")):
    ws = load_workspace(base_dir() / slug)
    ledger = Ledger(ws.root / "ledger.sqlite3")
    task = Task(id=f"USER-{len(ledger.list_tasks())+1:04d}", title=title, description=description or title, priority=10, created_by="user")
    ledger.upsert_task(task)
    ledger.event("task_created", task.model_dump(mode="json"), task.id)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/gate")
def set_gate(slug: str, gate_id: str = Form(...), state: str = Form(...)):
    ws = load_workspace(base_dir() / slug)
    if gate_id not in {g.id for g in ws.manifest.gates}:
        raise HTTPException(400, "Unknown gate")
    Ledger(ws.root / "ledger.sqlite3").set_gate(gate_id, state == "pass", "set from dashboard")
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/gate/add")
def add_gate(slug: str, gate_id: str = Form(...), description: str = Form(...), validator: str = Form(""), required: str | None = Form(None)):
    ws = load_workspace(base_dir() / slug)
    gid = _safe_slug(gate_id)
    if not gid or gid in {g.id for g in ws.manifest.gates}:
        raise HTTPException(400, "Invalid or duplicate gate id")
    ws.manifest.gates.append(Gate(id=gid, description=description.strip(), required=required is not None, validator=validator.strip() or None, manual=not bool(validator.strip())))
    save_manifest(ws)
    Ledger(ws.root / "ledger.sqlite3").set_gate(gid, False, "not evaluated")
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/agent")
def edit_agent(slug: str, agent_id: str = Form(...), instructions: str = Form(...), kind: str = Form(""), model: str = Form(""), tools: str = Form("")):
    ws = load_workspace(base_dir() / slug)
    agent = next((a for a in ws.manifest.agents if a.id == agent_id), None)
    if not agent:
        raise HTTPException(404, "Unknown agent")
    agent.instructions = instructions.strip()
    agent.tools = [x.strip() for x in tools.split(",") if x.strip()]
    agent.provider = ProviderSpec(kind=kind, model=model.strip() or None) if kind else None
    save_manifest(ws)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/tool/add")
def add_tool(slug: str, tool_id: str = Form(...), tool_type: str = Form(...), description: str = Form(""), command: str = Form(""), writable: str | None = Form(None)):
    ws = load_workspace(base_dir() / slug)
    tid = _safe_slug(tool_id)
    allowed = {"list_files", "read_file", "write_file", "shell"}
    if tool_type not in allowed or not tid or tid in {t.id for t in ws.manifest.tools}:
        raise HTTPException(400, "Invalid or duplicate tool")
    if tool_type == "shell" and not command.strip():
        raise HTTPException(400, "Shell tool requires a fixed command")
    ws.manifest.tools.append(ToolSpec(id=tid, type=tool_type, description=description.strip(), command=command.strip() or None, writable=writable is not None))
    save_manifest(ws)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/run")
def run_project(slug: str, steps: int = Form(1)):
    ws = load_workspace(base_dir() / slug)
    Orchestrator(ws).run(max_steps=max(1, min(steps, 25)), max_minutes=30)
    return RedirectResponse(f"/project/{slug}", status_code=303)


def _execute_job(slug: str, job_id: str):
    ws = load_workspace(base_dir() / slug)
    ledger = Ledger(ws.root / "ledger.sqlite3")
    job = ledger.get_job(job_id)
    if not job:
        return
    ledger.update_job(job_id, status=JobStatus.RUNNING, detail="running")

    def control():
        current = ledger.get_job(job_id)
        if not current:
            return "cancel"
        if current["status"] == JobStatus.PAUSED.value:
            return "pause"
        if current["status"] == JobStatus.CANCEL_REQUESTED.value:
            return "cancel"
        return "run"

    def on_step(count, _result):
        current = ledger.get_job(job_id)
        previous = int(current["steps_done"]) if current else 0
        ledger.update_job(job_id, steps_done=previous + 1, detail=f"completed {previous + 1} iteration(s)")

    try:
        orch = Orchestrator(ws)
        orch.run(max_steps=int(job["max_steps"]), max_minutes=float(job["requested_minutes"]), control=control, on_step=on_step)
        current = ledger.get_job(job_id)
        if current and current["status"] == JobStatus.CANCEL_REQUESTED.value:
            ledger.update_job(job_id, status=JobStatus.CANCELLED, detail="cancelled by user")
        elif orch.is_complete():
            ledger.update_job(job_id, status=JobStatus.COMPLETE, detail="all required gates passed")
        else:
            ledger.update_job(job_id, status=JobStatus.BUDGET_FINISHED, detail="run budget finished before completion")
    except Exception as exc:
        ledger.update_job(job_id, status=JobStatus.FAILED, detail=f"{type(exc).__name__}: {exc}"[:500])


def _submit_job(slug: str, job_id: str):
    ACTIVE_FUTURES[job_id] = EXECUTOR.submit(_execute_job, slug, job_id)


@app.post("/project/{slug}/overnight")
def overnight_project(slug: str, hours: float = Form(8.0)):
    ws = load_workspace(base_dir() / slug)
    ledger = Ledger(ws.root / "ledger.sqlite3")
    latest = ledger.latest_job()
    if latest and latest["status"] in {JobStatus.RUNNING.value, JobStatus.QUEUED.value, JobStatus.PAUSED.value}:
        raise HTTPException(409, "Workspace already has an active or paused job")
    minutes = max(6.0, min(hours, 24.0) * 60)
    max_steps = max(ws.manifest.runtime.max_steps_per_run, 100)
    job_id = ledger.create_job(minutes, max_steps)
    _submit_job(slug, job_id)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/job/{job_id}/pause")
def pause_job(slug: str, job_id: str):
    ledger = Ledger(load_workspace(base_dir() / slug).root / "ledger.sqlite3")
    if not ledger.get_job(job_id):
        raise HTTPException(404, "Unknown job")
    ledger.update_job(job_id, status=JobStatus.PAUSED, detail="paused by user")
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/job/{job_id}/cancel")
def cancel_job(slug: str, job_id: str):
    ledger = Ledger(load_workspace(base_dir() / slug).root / "ledger.sqlite3")
    if not ledger.get_job(job_id):
        raise HTTPException(404, "Unknown job")
    ledger.update_job(job_id, status=JobStatus.CANCEL_REQUESTED, detail="cancellation requested")
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/job/{job_id}/resume")
def resume_job(slug: str, job_id: str):
    ws = load_workspace(base_dir() / slug)
    ledger = Ledger(ws.root / "ledger.sqlite3")
    job = ledger.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    if job["status"] == JobStatus.PAUSED.value and job_id in ACTIVE_FUTURES and not getattr(ACTIVE_FUTURES[job_id], "done", lambda: True)():
        ledger.update_job(job_id, status=JobStatus.RUNNING, detail="resumed")
    else:
        ledger.update_job(job_id, status=JobStatus.QUEUED, detail="continued by user")
        _submit_job(slug, job_id)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.post("/project/{slug}/provider")
def set_provider(slug: str, kind: str = Form(...), model: str = Form("")):
    if kind not in {"mock", "ollama", "openai"}:
        raise HTTPException(400, "Unknown provider")
    ws = load_workspace(base_dir() / slug)
    ws.manifest.runtime.default_provider.kind = kind
    ws.manifest.runtime.default_provider.model = model.strip() or None
    save_manifest(ws)
    return RedirectResponse(f"/project/{slug}", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok", "workspaces": len(cards())}
