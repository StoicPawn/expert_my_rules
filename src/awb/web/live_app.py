from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from awb.core.models import TaskStatus
from awb.core.storage import Ledger
from awb.web.app import app, base_dir


def _task_title(ledger: Ledger, task_id: str | None) -> str:
    if not task_id:
        return ""
    task = ledger.get_task(task_id)
    return task.title if task else task_id


def _human_event(event: dict, ledger: Ledger) -> tuple[str, str, str]:
    kind = event.get("kind", "event")
    payload = event.get("payload") or {}
    task_id = event.get("task_id")
    task = _task_title(ledger, task_id)
    role = str(payload.get("role", "")).capitalize()

    if kind == "model_call_started":
        return "active", f"{role} is thinking", f"Working on {task or 'the project'} using {payload.get('provider', 'the configured model')}."
    if kind == "model_call_finished":
        return "ok", f"{role} finished", f"Model response completed in {payload.get('seconds', '?')} s for {task or 'the project'}."
    if kind == "model_call_failed":
        return "error", f"{role} model call failed", str(payload.get("error", "Unknown model error"))
    if kind == "model_escalated":
        to = payload.get("to") or {}
        return "warn", "Cloud escalation", f"{role or 'Agent'} escalated {task or 'a task'} to {to.get('kind', 'cloud')} / {to.get('model', 'configured model')}."
    if kind == "task_created":
        return "info", "Director selected the next task", str(payload.get("title") or task or "New task")
    if kind == "task_started":
        return "active", f"Executing: {task}", f"Attempt {payload.get('attempt', 1)} has started."
    if kind == "work_output":
        text = str(payload.get("text", "")).strip().replace("\n", " ")
        return "ok", "Worker produced a candidate result", (text[:240] + "…") if len(text) > 240 else text
    if kind == "tool_call":
        result = payload.get("result") or {}
        state = "ok" if result.get("ok") else "warn"
        detail = f"Tool {payload.get('tool', '?')} returned {'success' if result.get('ok') else 'a problem'}."
        if result.get("error"):
            detail += f" {result['error']}"
        return state, "Worker used a tool", detail
    if kind == "review":
        objections = payload.get("critical_objections") or []
        if payload.get("approved"):
            return "ok", "Independent reviewer approved", f"{task or 'Candidate result'} passed adversarial review."
        return "warn", "Independent reviewer challenged the result", f"{len(objections)} critical objection(s) found for {task or 'the candidate result'}."
    if kind == "verification":
        if payload.get("passed"):
            return "ok", "External verification passed", str(payload.get("detail", "Verification completed successfully."))[:300]
        return "warn", "External verification failed", str(payload.get("detail", "Verification did not pass."))[:300]
    if kind == "validator_passed":
        return "ok", f"Validator passed: {payload.get('name', '')}", str(payload.get("detail", ""))[:300]
    if kind == "gate_evaluated":
        passed = bool(payload.get("passed"))
        return ("ok" if passed else "info"), f"Completion condition {'passed' if passed else 'remains open'}", f"{payload.get('gate', 'gate')}: {payload.get('detail', '')}"[:350]
    if kind == "task_failed":
        return "error", f"Task failed: {task}", str(payload.get("error", "Unknown task error"))
    if kind == "interrupted_tasks_recovered":
        return "warn", "Recovered interrupted work", f"Reopened {len(payload.get('task_ids') or [])} task(s) left in progress by a previous stop or crash."
    if kind == "run_started":
        return "info", "Autonomous session started", "The project is continuing from its persistent ledger."
    if kind == "run_finished":
        return "info", "Checkpoint session finished", f"Completed {payload.get('steps', 0)} iteration(s); reason: {payload.get('reason', 'checkpoint')}."
    return "info", kind.replace("_", " ").title(), json.dumps(payload, ensure_ascii=False)[:300]


def _render_activity(slug: str) -> str:
    root = base_dir() / slug
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    ledger = Ledger(root / "ledger.sqlite3")
    job = ledger.latest_job()
    tasks = ledger.list_tasks()
    current = next((t for t in tasks if t.status == TaskStatus.IN_PROGRESS), None)
    recent = list(reversed(ledger.recent_events(35)))

    if current:
        headline = f"<div class='live-current'><span class='live-pulse'></span><div><b>Now working on</b><br>{html.escape(current.title)} <span class='muted'>({html.escape(current.id)})</span></div></div>"
    elif job and job.get("status") == "RUNNING":
        headline = "<div class='live-current'><span class='live-pulse'></span><div><b>Project is active</b><br><span class='muted'>Preparing or selecting the next step.</span></div></div>"
    else:
        headline = "<div class='live-current idle'><div><b>No agent is currently executing</b><br><span class='muted'>Activity history remains available below.</span></div></div>"

    rows = []
    for event in recent:
        state, title, detail = _human_event(event, ledger)
        ts = str(event.get("ts", ""))
        clock = ts[11:19] if len(ts) >= 19 else ts
        rows.append(
            f"<div class='live-row {state}'><div class='live-time'>{html.escape(clock)}</div>"
            f"<div><b>{html.escape(title)}</b><div class='live-detail'>{html.escape(detail)}</div></div></div>"
        )
    if not rows:
        rows.append("<div class='muted'>No activity recorded yet. Start the project to see the agents working here.</div>")
    return headline + "<div class='live-feed'>" + "".join(rows) + "</div>"


@app.get("/project/{slug}/activity", response_class=HTMLResponse)
def project_activity(slug: str):
    return HTMLResponse(_render_activity(unquote(slug)))


INJECTION = r"""
<style>
#live-activity-card{border:1px solid #dedee5}.live-current{display:flex;gap:12px;align-items:center;padding:12px 14px;background:#f4f7ff;border-radius:12px;margin-bottom:12px}.live-current.idle{background:#f3f3f5}.live-pulse{width:11px;height:11px;border-radius:50%;background:#2563eb;box-shadow:0 0 0 0 rgba(37,99,235,.5);animation:livepulse 1.6s infinite}@keyframes livepulse{70%{box-shadow:0 0 0 10px rgba(37,99,235,0)}100%{box-shadow:0 0 0 0 rgba(37,99,235,0)}}.live-feed{max-height:430px;overflow:auto}.live-row{display:grid;grid-template-columns:70px 1fr;gap:10px;padding:10px 4px;border-bottom:1px solid #eee}.live-time{font-variant-numeric:tabular-nums;color:#777;font-size:13px}.live-detail{color:#555;margin-top:3px;line-height:1.35}.live-row.active b{color:#1d4ed8}.live-row.ok b{color:#087c35}.live-row.warn b{color:#9a5200}.live-row.error b{color:#a11b1b}
</style>
<div class='panel' id='live-activity-card'><h2>Live activity</h2><p class='muted'>Plain-language activity from the Director, Worker, Reviewer, Verifier and tools. Updates automatically while the project runs.</p><div id='live-activity'><div class='muted'>Loading activity…</div></div></div>
<script>
(function(){
 const parts=window.location.pathname.split('/').filter(Boolean);
 if(parts.length!==2 || parts[0]!=='project') return;
 const slug=encodeURIComponent(parts[1]);
 async function refreshActivity(){
   try{
     const r=await fetch('/project/'+slug+'/activity',{cache:'no-store'});
     if(r.ok) document.getElementById('live-activity').innerHTML=await r.text();
   }catch(e){document.getElementById('live-activity').innerHTML='<div class="muted">Activity feed temporarily unavailable.</div>';}
 }
 refreshActivity(); setInterval(refreshActivity,2000);
})();
</script>
"""


class LiveActivityInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if request.method != "GET" or len(parts) != 2 or parts[0] != "project":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode("utf-8", errors="replace")
        text = text.replace("</body>", INJECTION + "</body>")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type="text/html")


app.add_middleware(LiveActivityInjectionMiddleware)
