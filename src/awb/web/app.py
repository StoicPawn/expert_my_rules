from __future__ import annotations

import html
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.models import Task
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, save_manifest, write_workspace
from awb.templates.templates import get_template

app = FastAPI(title="Expert My Rules")
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="awb")
RUNNING: dict[str, str] = {}
RUN_LOCK = threading.Lock()


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
            out.append((p.name, ws, tasks, passed, len(ws.manifest.gates)))
        except Exception:
            continue
    return out


STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1050px;margin:36px auto;padding:0 18px;background:#f5f5f7;color:#161616}
.card,.panel{display:block;text-decoration:none;color:inherit;background:white;padding:20px;margin:14px 0;border-radius:18px;box-shadow:0 1px 6px #0001}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}.muted{color:#666}.type{text-transform:uppercase;font-size:12px;letter-spacing:.08em;color:#666}
input,select,textarea,button{font:inherit;padding:10px;border:1px solid #ccc;border-radius:10px;box-sizing:border-box}input,select,textarea{width:100%;margin:5px 0 10px}button{cursor:pointer;background:#111;color:white;border:0}.secondary{background:#e8e8ec;color:#111}
table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}.pass{color:#087c35}.open{color:#a44b00}code{background:#eee;padding:2px 5px;border-radius:5px}
"""


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"


@app.get("/", response_class=HTMLResponse)
def index():
    project_html = ""
    for slug, ws, tasks, passed, total in cards():
        open_count = sum(1 for t in tasks if t.status.value not in {"DONE", "REJECTED"})
        project_html += f"""<a class='card' href='/project/{html.escape(slug)}'><h2>{html.escape(ws.manifest.name)}</h2><div class='type'>{html.escape(ws.manifest.type)}</div><p>{html.escape(ws.manifest.goal)}</p><b>{passed}/{total} gates</b> · {open_count} unresolved tasks</a>"""
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
    safe = "".join(c for c in name.strip().replace(" ", "_") if c.isalnum() or c in "_-.")
    if not safe:
        raise HTTPException(400, "Invalid name")
    root = base_dir() / safe
    if root.exists():
        raise HTTPException(409, "Workspace already exists")
    write_workspace(root, get_template(kind, safe, goal.strip()))
    return RedirectResponse(f"/project/{safe}", status_code=303)


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
        f"<tr><td>{html.escape(g.id)}</td><td class='{'pass' if state.get(g.id,{}).get('passed') else 'open'}'>{'PASS' if state.get(g.id,{}).get('passed') else 'OPEN'}</td><td>{html.escape(g.description)}</td><td><form method='post' action='/project/{slug}/gate'><input type='hidden' name='gate_id' value='{html.escape(g.id)}'><button name='state' value='pass'>Pass</button> <button class='secondary' name='state' value='open'>Reopen</button></form></td></tr>" for g in ws.manifest.gates
    )
    agents = "".join(f"<li><b>{html.escape(a.id)}</b> — {html.escape(a.role)}: {html.escape(a.instructions)}</li>" for a in ws.manifest.agents)
    provider = ws.manifest.runtime.default_provider
    body = f"""
    <a href='/'>← Workspaces</a><h1>{html.escape(ws.manifest.name)}</h1><div class='type'>{html.escape(ws.manifest.type)}</div>
    <div class='panel'><h2>North Star</h2><p>{html.escape(ws.manifest.goal)}</p><p class='muted'>Default provider: <code>{html.escape(provider.kind)}</code> {html.escape(provider.model or '')}</p></div>
    <div class='grid'><div class='panel'><h2>Run now</h2><form method='post' action='/project/{slug}/run'><label>Steps</label><input name='steps' type='number' value='1' min='1' max='25'><button>Run autonomous loop</button></form><hr><h3>End-of-day run</h3><form method='post' action='/project/{slug}/overnight'><label>Hours</label><input name='hours' type='number' value='8' min='0.1' max='24' step='0.5'><button>Launch in background</button></form><p class='muted'>State: <b>{html.escape(RUNNING.get(slug, 'idle'))}</b>. The server keeps working after you close the page.</p></div>
    <div class='panel'><h2>Add task</h2><form method='post' action='/project/{slug}/task'><input name='title' required placeholder='Task title'><textarea name='description' rows='3' placeholder='Acceptance criteria / context'></textarea><button>Add high-priority task</button></form></div></div>
    <div class='grid'><div class='panel'><h2>Model provider</h2><form method='post' action='/project/{slug}/provider'><select name='kind'><option value='mock'>mock</option><option value='ollama'>ollama</option><option value='openai'>openai</option></select><input name='model' placeholder='e.g. qwen3:8b'><button>Save default provider</button></form><p class='muted'>API keys remain environment variables and are never written into the workspace.</p></div>
    <div class='panel'><h2>Agents</h2><ul>{agents}</ul><p class='muted'>Each agent can have its own provider/model in <code>project.yaml</code>; the default provider is used otherwise.</p></div></div>
    <div class='panel'><h2>Completion gates</h2><table><tr><th>Gate</th><th>State</th><th>Rule</th><th>Manual control</th></tr>{gate_rows}</table></div>
    <div class='panel'><h2>Tasks</h2><table><tr><th>ID</th><th>Task</th><th>Status</th><th>Attempts</th></tr>{task_rows}</table></div>
    """
    return page(ws.manifest.name, body)


@app.post("/project/{slug}/task")
def add_task(slug: str, title: str = Form(...), description: str = Form("")):
    root = base_dir() / slug
    ws = load_workspace(root)
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


@app.post("/project/{slug}/run")
def run_project(slug: str, steps: int = Form(1)):
    ws = load_workspace(base_dir() / slug)
    Orchestrator(ws).run(max_steps=max(1, min(steps, 25)), max_minutes=30)
    return RedirectResponse(f"/project/{slug}", status_code=303)


def _background_run(slug: str, hours: float):
    try:
        with RUN_LOCK:
            RUNNING[slug] = "running"
        ws = load_workspace(base_dir() / slug)
        max_steps = max(ws.manifest.runtime.max_steps_per_run, 100)
        Orchestrator(ws).run(max_steps=max_steps, max_minutes=hours * 60)
        with RUN_LOCK:
            RUNNING[slug] = "complete" if Orchestrator(load_workspace(base_dir() / slug)).is_complete() else "budget finished"
    except Exception as exc:
        with RUN_LOCK:
            RUNNING[slug] = f"failed: {type(exc).__name__}: {exc}"[:180]


@app.post("/project/{slug}/overnight")
def overnight_project(slug: str, hours: float = Form(8.0)):
    load_workspace(base_dir() / slug)
    with RUN_LOCK:
        if RUNNING.get(slug) == "running":
            raise HTTPException(409, "Workspace already has a background run")
        RUNNING[slug] = "queued"
    EXECUTOR.submit(_background_run, slug, max(0.1, min(hours, 24.0)))
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
    return {"status": "ok", "running": RUNNING}
