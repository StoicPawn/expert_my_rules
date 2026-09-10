from __future__ import annotations

import html
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from awb.web.app import app, base_dir
from awb.web.resource_monitor import _gib, _resource_snapshot


def _format_duration(seconds: object) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _open_readonly(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _task_title(conn: sqlite3.Connection, task_id: str | None) -> str:
    if not task_id:
        return "project-wide"
    row = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
    return str(row["title"]) if row else task_id


def _ollama_compute_placement() -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=1.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("models") or []
        if not models:
            return "No Ollama model loaded"
        item = models[0]
        name = str(item.get("name") or item.get("model") or "model")
        size = int(item.get("size") or 0)
        size_vram = int(item.get("size_vram") or 0)
        if size <= 0:
            return f"{name} · compute placement unavailable"
        gpu_share = min(100.0, max(0.0, size_vram * 100.0 / size))
        cpu_share = 100.0 - gpu_share
        return (
            f"{name} · model placement ≈ {cpu_share:.0f}% CPU / {gpu_share:.0f}% GPU · "
            f"VRAM {_gib(size_vram)}"
        )
    except Exception:
        return "Ollama compute placement unavailable"


def _machine_summary() -> str:
    snap = _resource_snapshot()
    ram_total = int(snap.get("ram_total") or 0)
    ram_used = int(snap.get("ram_used") or 0)
    ram_pct = snap.get("ram_percent")
    cpu_pct = snap.get("cpu_percent")
    if ram_total:
        ram = f"{_gib(ram_used)} / {_gib(ram_total)}"
        if ram_pct is not None:
            ram += f" ({float(ram_pct):.0f}%)"
    else:
        ram = "unavailable"
    cpu = "sampling…" if cpu_pct is None else f"{float(cpu_pct):.0f}%"
    return f"RAM {ram} · CPU {cpu} · {_ollama_compute_placement()}"


def _phase_from_events(events: list[sqlite3.Row], current_title: str) -> tuple[str, str]:
    for row in events:
        kind = str(row["kind"])
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        if kind == "model_call_started":
            role = str(payload.get("role") or "agent").capitalize()
            model = str(payload.get("model") or payload.get("provider") or "local model")
            return (
                f"{role} is running the model",
                f"{role} is currently computing on “{current_title}” with {model}. This is the active model call, not a historical feed item.",
            )
        if kind == "workflow_stage_started":
            stage_kind = str(payload.get("kind") or "")
            role = str(payload.get("role") or "agent").capitalize()
            if stage_kind == "execute":
                return "Worker is building the candidate", f"The Worker is developing the candidate answer for “{current_title}”."
            if stage_kind == "review":
                return "Reviewer is auditing the candidate", f"The Worker draft exists; the Reviewer is now trying to find fatal or major problems in “{current_title}”."
            if stage_kind == "validate":
                return "Verifier is checking evidence", f"The candidate/review cycle for “{current_title}” is in verification."
            return f"{role} stage is active", f"The current workflow stage for “{current_title}” is {stage_kind or 'active'}."
        if kind == "work_output":
            return (
                "Worker candidate has been saved",
                f"The Worker finished its draft for “{current_title}”. The next normal step is adversarial review; the task remains IN_PROGRESS until that review/recovery cycle finishes.",
            )
        if kind == "review":
            approved = bool(payload.get("approved"))
            objections = payload.get("critical_objections") or []
            if approved:
                return "Reviewer approved the candidate", f"Review of “{current_title}” passed; Expert is finishing the task cycle and gate checks."
            return (
                "Reviewer challenged the candidate",
                f"Review of “{current_title}” found {len(objections)} critical objection(s). Expert is deciding whether to retry, decompose or reframe it.",
            )
        if kind in {"task_blocked", "task_recovery_planned", "task_decomposed", "task_reframed"}:
            return "Recovery planning is active", f"The latest candidate for “{current_title}” did not close cleanly; Expert is planning the next scientific move."
        if kind == "task_started":
            return "Task cycle has started", f"Expert selected “{current_title}” and is preparing its execution stage."
        if kind == "model_call_finished":
            role = str(payload.get("role") or "agent").capitalize()
            duration = _format_duration(payload.get("seconds"))
            return f"{role} model call just finished", f"A model call for “{current_title}” completed in {duration}; Expert is persisting the result and moving to the next workflow stage."
    return "Task is in progress", f"“{current_title}” is marked IN_PROGRESS; no newer explanatory event is available yet."


def _clarity_html(root: Path) -> str:
    db = root / "ledger.sqlite3"
    if not db.exists():
        return "<div class='muted'>No task ledger yet.</div>"
    try:
        conn = _open_readonly(db)
    except sqlite3.Error:
        return "<div class='muted'>Task state is temporarily unavailable.</div>"
    try:
        current = conn.execute(
            "SELECT id,title,status FROM tasks WHERE status='IN_PROGRESS' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        job = conn.execute("SELECT status,detail FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        if current:
            current_id = str(current["id"])
            current_title = str(current["title"])
            events = conn.execute(
                "SELECT seq,ts,kind,payload_json FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 30",
                (current_id,),
            ).fetchall()
            phase, explanation = _phase_from_events(events, current_title)
            current_line = f"<b>{html.escape(current_id)} · {html.escape(current_title)}</b>"
        else:
            phase = "No task is executing right now"
            explanation = "The project may be paused, between iterations, or already complete."
            current_line = "<b>None</b>"

        last_call = conn.execute(
            "SELECT task_id,role,model,seconds,success,ts FROM model_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last_call:
            call_task = _task_title(conn, last_call["task_id"])
            role = str(last_call["role"] or "agent").capitalize()
            model = str(last_call["model"] or "local model")
            duration = _format_duration(last_call["seconds"])
            result = "completed" if bool(last_call["success"]) else "failed"
            last_call_html = (
                f"<b>{html.escape(role)} · {html.escape(call_task)}</b><br>"
                f"{html.escape(model)} · {html.escape(result)} · {html.escape(duration)}"
            )
        else:
            last_call_html = "<span class='muted'>No completed model call recorded yet.</span>"

        job_status = str(job["status"]) if job else "NOT STARTED"
        job_detail = str(job["detail"] or "") if job else ""
        machine = _machine_summary()
        return (
            "<div class='clarity-current'>"
            "<div><span>Current task</span>" + current_line + "</div>"
            f"<div><span>What is happening now</span><b>{html.escape(phase)}</b><p>{html.escape(explanation)}</p></div>"
            f"<div><span>Project job</span><b>{html.escape(job_status)}</b><p>{html.escape(job_detail)}</p></div>"
            f"<div><span>Most recent completed model call</span>{last_call_html}</div>"
            f"<div class='clarity-machine'><span>Machine now</span><b>{html.escape(machine)}</b><p>CPU/GPU percentages for Ollama describe where model memory is placed, not instantaneous GPU utilisation.</p></div>"
            "</div>"
            "<p class='clarity-note'><b>Important:</b> the activity feed below is a history across tasks. An older line about SEED-0001 can remain visible while SEED-0005 is the task currently executing.</p>"
        )
    finally:
        conn.close()


@app.get("/project/{slug}/what-is-happening", response_class=HTMLResponse)
def project_what_is_happening(slug: str):
    root = base_dir() / slug
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    return HTMLResponse(_clarity_html(root), headers={"Cache-Control": "no-store"})


CLARITY_INJECTION = r"""
<style>
#clarity-card{border:1px solid #dedee5}.clarity-current{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.clarity-current>div{background:#f7f7f8;border-radius:10px;padding:11px 12px;min-width:0}.clarity-current .clarity-machine{grid-column:1/-1}.clarity-current span{display:block;color:#666;font-size:12px;margin-bottom:4px}.clarity-current b{display:block;overflow-wrap:anywhere}.clarity-current p{margin:5px 0 0;line-height:1.4;color:#555}.clarity-note{font-size:13px;line-height:1.45;background:#fff7e8;border-radius:10px;padding:10px 12px;margin:10px 0 0}@media(max-width:640px){.clarity-current{grid-template-columns:1fr}.clarity-current .clarity-machine{grid-column:auto}}
</style>
<div class='panel' id='clarity-card'><h2>What is happening now?</h2><p class='muted'>Current task, current workflow phase and machine load, separated from historical activity.</p><div id='clarity-values'><div class='muted'>Loading current state…</div></div></div>
<script>
(function(){
 const parts=window.location.pathname.split('/').filter(Boolean);
 if(parts.length!==2 || parts[0]!=='project') return;
 const slug=encodeURIComponent(parts[1]);
 const card=document.getElementById('clarity-card');
 const live=document.getElementById('live-activity-card');
 if(card && live && live.parentNode) live.parentNode.insertBefore(card,live);
 let busy=false,last='';
 async function refreshClarity(){
   if(busy) return; busy=true;
   const host=document.getElementById('clarity-values');
   if(!host){busy=false;return;}
   try{
     const r=await fetch('/project/'+slug+'/what-is-happening',{cache:'no-store'});
     if(!r.ok) return;
     const next=await r.text();
     if(next!==last){last=next;host.innerHTML=next;}
   }catch(e){if(!last)host.innerHTML='<div class="muted">Current state temporarily unavailable.</div>';}
   finally{busy=false;}
 }
 refreshClarity(); setInterval(refreshClarity,2000);
})();
</script>
"""


class ClarityInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip("/")
        parts = [part for part in path.split("/") if part]
        if request.method != "GET" or len(parts) != 2 or parts[0] != "project":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode("utf-8", errors="replace")
        text = text.replace("</body>", CLARITY_INJECTION + "</body>")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type="text/html")


app.add_middleware(ClarityInjectionMiddleware)
