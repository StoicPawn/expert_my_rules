from __future__ import annotations

import html
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from awb.core.storage import Ledger
from awb.core.workspace import load_workspace

app = FastAPI(title="Autonomous Workbench")
BASE = Path.cwd() / "workspaces"


def cards():
    BASE.mkdir(exist_ok=True)
    out = []
    for p in sorted(BASE.iterdir()):
        if not (p / "project.yaml").exists(): continue
        try:
            ws=load_workspace(p); ledger=Ledger(p/"ledger.sqlite3"); tasks=ledger.list_tasks(); gates=ledger.gate_state()
            passed=sum(1 for g in ws.manifest.gates if gates.get(g.id,{}).get("passed")); total=len(ws.manifest.gates)
            out.append((p.name,ws,tasks,passed,total))
        except Exception: continue
    return out

@app.get("/", response_class=HTMLResponse)
def index():
    project_html=""
    for slug,ws,tasks,passed,total in cards():
        open_count=sum(1 for t in tasks if t.status.value != "DONE")
        project_html += f"<a class='card' href='/project/{html.escape(slug)}'><h2>{html.escape(ws.manifest.name)}</h2><div class='type'>{html.escape(ws.manifest.type)}</div><p>{html.escape(ws.manifest.goal)}</p><b>{passed}/{total} gates</b> · {open_count} unresolved tasks</a>"
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>AWB</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;background:#f6f6f7;color:#111}}.card{{display:block;text-decoration:none;color:inherit;background:white;padding:22px;margin:16px 0;border-radius:18px;box-shadow:0 1px 6px #0001}}.type{{text-transform:uppercase;font-size:12px;letter-spacing:.08em;color:#666}} h1{{font-size:36px}}</style></head><body><h1>Autonomous Workbench</h1>{project_html or '<p>No workspace yet.</p>'}</body></html>"""

@app.get("/project/{slug}", response_class=HTMLResponse)
def project(slug: str):
    root=BASE/slug
    try: ws=load_workspace(root)
    except Exception as e: raise HTTPException(404,str(e))
    ledger=Ledger(root/"ledger.sqlite3")
    task_rows="".join(f"<tr><td>{html.escape(t.id)}</td><td>{html.escape(t.title)}</td><td>{t.status.value}</td></tr>" for t in ledger.list_tasks())
    gate_state=ledger.gate_state()
    gate_rows="".join(f"<tr><td>{html.escape(g.id)}</td><td>{'PASS' if gate_state.get(g.id,{}).get('passed') else 'OPEN'}</td><td>{html.escape(g.description)}</td></tr>" for g in ws.manifest.gates)
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(ws.manifest.name)}</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:1100px;margin:32px auto;padding:0 18px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px;border-bottom:1px solid #ddd;text-align:left}}.goal{{font-size:22px}}</style></head><body><a href='/'>← Projects</a><h1>{html.escape(ws.manifest.name)}</h1><p class='goal'>{html.escape(ws.manifest.goal)}</p><h2>Gates</h2><table>{gate_rows}</table><h2>Tasks</h2><table><tr><th>ID</th><th>Task</th><th>Status</th></tr>{task_rows}</table></body></html>"""
