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
from awb.providers.runtime_progress import get_progress
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
    if kind == "task_technical_error":
        return "error", f"Technical error: {task}", f"{payload.get('error', 'Unknown runtime error')} · scientific attempts unchanged; technical failures: {payload.get('technical_failures', '?')}."
    if kind == "task_blocked":
        objections = payload.get("critical_objections") or []
        detail = str(objections[0]) if objections else "The candidate did not yet satisfy adversarial review/verification."
        return "warn", f"Scientific review blocked: {task}", f"Scientific attempt {payload.get('scientific_attempts', '?')}: {detail}"
    if kind == "task_recovery_planned":
        return "info", "Director planned a different approach", str(payload.get("strategy", "Retry by explicitly resolving the objections."))[:350]
    if kind == "task_decomposed":
        return "info", "Blocked task decomposed", f"Created {len(payload.get('subtask_ids') or [])} prerequisite/falsification task(s). {str(payload.get('strategy', ''))[:260]}"
    if kind == "task_reframed":
        return "info", "Task reframed", f"The original formulation was superseded by {payload.get('replacement_task_id') or 'a replacement task'}. {str(payload.get('rationale', ''))[:260]}"
    if kind == "task_rejected_by_evidence":
        return "warn", "Task resolved as rejected by evidence", f"{payload.get('resolution_type', 'evidence')}: {str(payload.get('rationale', ''))[:280]}"
    if kind == "task_reopened_after_review":
        return "active", f"Retrying scientifically blocked task: {task}", str(payload.get("next_strategy", "Use a materially different route that addresses the objections."))[:350]
    if kind == "task_reopened_after_technical_error":
        return "active", f"Retrying after technical error: {task}", "The scientific attempt counter was not consumed."
    if kind == "task_scientifically_closed":
        return "ok", f"Task scientifically closed: {task}", f"Accepted after {payload.get('scientific_attempts', '?')} scientific attempt(s)."
    if kind == "task_failed":
        return "error", f"Legacy task failure: {task}", str(payload.get("error", "Unknown task error"))
    if kind == "interrupted_tasks_recovered":
        return "warn", "Recovered interrupted work", f"Reopened {len(payload.get('task_ids') or [])} task(s) left in progress by a previous stop or crash."
    if kind == "run_started":
        return "info", "Autonomous session started", "The project is continuing from its persistent ledger."
    if kind == "run_finished":
        return "info", "Checkpoint session finished", f"Completed {payload.get('steps', 0)} iteration(s); reason: {payload.get('reason', 'checkpoint')}."
    return "info", kind.replace("_", " ").title(), json.dumps(payload, ensure_ascii=False)[:300]


def _format_elapsed(seconds: object) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return '?'
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours:d}:{minutes:02d}:{secs:02d}' if hours else f'{minutes:d}:{secs:02d}'


def _progress_html() -> str:
    progress = get_progress()
    if not progress:
        return ''
    state = str(progress.get('state') or 'generating')
    elapsed = _format_elapsed(progress.get('elapsed_seconds'))
    chunks = int(progress.get('chunks') or 0)
    output_chars = int(progress.get('output_chars') or 0)
    silent = _format_elapsed(progress.get('last_stream_activity_seconds'))
    model = html.escape(str(progress.get('model') or 'local model'))
    failures = int(progress.get('health_failures') or 0)
    if state == 'health_check_failed':
        status = f'health probe failed ({failures}); watchdog is still checking'
        cls = 'warn'
    elif state == 'alive':
        status = f'no recent chunk for {silent}, but Ollama is healthy — continuing without a total timeout'
        cls = 'ok'
    elif state == 'starting':
        status = 'request accepted; waiting for streamed output'
        cls = 'active'
    else:
        status = 'stream is producing activity'
        cls = 'ok'
    return (
        f"<div class='live-runtime {cls}'><b>Local model liveness</b><br>"
        f"{model} · elapsed {html.escape(elapsed)} · chunks {chunks} · visible output {output_chars} chars<br>"
        f"<span class='muted'>{html.escape(status)}</span></div>"
    )


def _text(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2)


def _artifact_fallback(root: Path, task) -> str:
    relative = str(task.metadata.get('artifact') or '').strip()
    if not relative:
        return ''
    try:
        root_resolved = root.resolve()
        path = (root / relative).resolve()
        if root_resolved not in path.parents or not path.is_file():
            return ''
        return path.read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError):
        return ''


def _candidate_review_html(root: Path, ledger: Ledger, current_task_id: str | None = None) -> str:
    # One WAL-safe read snapshot. Events are returned newest first, so the first
    # work/review event per task is the latest persisted result. No status or task
    # mutation happens here: this endpoint is intentionally observational only.
    events = ledger.recent_events(1000)
    latest_work: dict[str, dict] = {}
    latest_review: dict[str, dict] = {}
    for event in events:
        task_id = event.get('task_id')
        if not task_id:
            continue
        if event.get('kind') == 'work_output' and task_id not in latest_work:
            latest_work[task_id] = event
        elif event.get('kind') == 'review' and task_id not in latest_review:
            latest_review[task_id] = event

    tasks = ledger.list_tasks()
    if current_task_id:
        tasks.sort(key=lambda t: 0 if t.id == current_task_id else 1)

    cards: list[str] = []
    for task in tasks:
        work_event = latest_work.get(task.id)
        review_event = latest_review.get(task.id)
        # A task may have several scientific attempts. Never attach an objection
        # from an older attempt to a newer Worker candidate that is still under
        # review. The durable event sequence gives us the exact causal ordering.
        if work_event and review_event and int(review_event.get('seq', 0)) <= int(work_event.get('seq', 0)):
            review_event = None
        artifact_text = '' if work_event else _artifact_fallback(root, task)
        if not work_event and not review_event and not artifact_text:
            continue

        work_payload = (work_event or {}).get('payload') or {}
        review_payload = (review_event or {}).get('payload') or {}
        candidate = _text(work_payload.get('text')) or artifact_text
        stage = _text(work_payload.get('stage'))
        approved = review_payload.get('approved') if review_event else None
        objections = review_payload.get('critical_objections') or []
        recommendations = review_payload.get('recommendations') or []

        if approved is True:
            review_state = "<span class='inspection-pill ok'>approved</span>"
        elif approved is False:
            review_state = "<span class='inspection-pill warn'>challenged</span>"
        else:
            review_state = "<span class='inspection-pill active'>review pending / not yet persisted</span>"

        if objections:
            objection_html = "<ol class='inspection-objections'>" + ''.join(
                f"<li>{html.escape(_text(item))}</li>" for item in objections
            ) + "</ol>"
        elif review_event:
            objection_html = "<div class='muted'>No critical objections in the latest persisted Reviewer result.</div>"
        else:
            objection_html = "<div class='muted'>The candidate is persisted; the Reviewer has not persisted a result for this candidate yet.</div>"

        recommendation_html = ''
        if recommendations:
            recommendation_html = (
                "<h4>Reviewer recommendations</h4><ul class='inspection-objections'>"
                + ''.join(f"<li>{html.escape(_text(item))}</li>" for item in recommendations)
                + "</ul>"
            )

        source_label = f"Worker stage: {html.escape(stage)}" if stage else "Persisted attempt artifact"
        cards.append(
            "<details class='inspection-card'" + (" open" if task.id == current_task_id else "") + ">"
            f"<summary><b>{html.escape(task.title)}</b> <span class='muted'>({html.escape(task.id)})</span> {review_state}</summary>"
            "<div class='inspection-body'>"
            f"<div class='muted inspection-source'>{source_label}</div>"
            "<h4>Full Worker candidate</h4>"
            f"<pre class='inspection-text'>{html.escape(candidate)}</pre>"
            "<h4>Latest Reviewer objections</h4>"
            f"{objection_html}{recommendation_html}"
            "<p class='muted inspection-note'>Read-only view from the persistent ledger/artifact store. Opening or refreshing it does not pause, restart, change task state, or consume a scientific attempt.</p>"
            "</div></details>"
        )

    if not cards:
        return "<div class='muted'>No persisted Worker candidate or Reviewer result is available yet.</div>"
    return ''.join(cards)


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
        headline = f"<div class='live-current'><span class='live-pulse'></span><div><b>Now working on</b><br>{html.escape(current.title)} <span class='muted'>({html.escape(current.id)})</span></div></div>" + _progress_html()
    elif job and job.get("status") == "RUNNING":
        headline = "<div class='live-current'><span class='live-pulse'></span><div><b>Project is active</b><br><span class='muted'>Preparing or selecting the next step.</span></div></div>" + _progress_html()
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


@app.get("/project/{slug}/inspection", response_class=HTMLResponse)
def project_inspection(slug: str):
    root = base_dir() / unquote(slug)
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    ledger = Ledger(root / "ledger.sqlite3")
    current = next((t for t in ledger.list_tasks() if t.status == TaskStatus.IN_PROGRESS), None)
    return HTMLResponse(_candidate_review_html(root, ledger, current.id if current else None))


INJECTION = r"""
<style>
#live-activity-card{border:1px solid #dedee5}.live-current{display:flex;gap:12px;align-items:center;padding:12px 14px;background:#f4f7ff;border-radius:12px;margin-bottom:12px}.live-current.idle{background:#f3f3f5}.live-pulse{width:11px;height:11px;border-radius:50%;background:#2563eb;box-shadow:0 0 0 0 rgba(37,99,235,.5);animation:livepulse 1.6s infinite}@keyframes livepulse{70%{box-shadow:0 0 0 10px rgba(37,99,235,0)}100%{box-shadow:0 0 0 0 rgba(37,99,235,0)}}.live-runtime{padding:10px 14px;border-radius:10px;margin:-4px 0 12px 0;background:#f7f7f8;font-size:14px;line-height:1.35}.live-runtime.ok b{color:#087c35}.live-runtime.warn b{color:#9a5200}.live-runtime.active b{color:#1d4ed8}.live-feed{max-height:430px;overflow:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}.live-row{display:grid;grid-template-columns:70px 1fr;gap:10px;padding:10px 4px;border-bottom:1px solid #eee}.live-time{font-variant-numeric:tabular-nums;color:#777;font-size:13px}.live-detail{color:#555;margin-top:3px;line-height:1.35}.live-row.active b{color:#1d4ed8}.live-row.ok b{color:#087c35}.live-row.warn b{color:#9a5200}.live-row.error b{color:#a11b1b}
#candidate-review-card{border:1px solid #dedee5}.inspection-card{border:1px solid #e4e4e8;border-radius:12px;margin:10px 0;background:#fff}.inspection-card summary{cursor:pointer;padding:12px 14px;line-height:1.4}.inspection-body{padding:0 14px 14px}.inspection-source{font-size:12px;margin-top:2px}.inspection-text{white-space:pre-wrap;word-break:break-word;max-height:560px;overflow:auto;background:#f7f7f8;padding:12px;border-radius:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;line-height:1.45;-webkit-overflow-scrolling:touch}.inspection-objections{padding-left:22px}.inspection-objections li{margin:8px 0}.inspection-pill{display:inline-block;margin-left:6px;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:600}.inspection-pill.ok{background:#e9f8ef;color:#087c35}.inspection-pill.warn{background:#fff2df;color:#8a4a00}.inspection-pill.active{background:#eef3ff;color:#1d4ed8}.inspection-note{font-size:12px;margin-top:12px}
</style>
<div class='panel' id='live-activity-card'><h2>Live activity</h2><p class='muted'>Plain-language activity from the Director, Worker, Reviewer, Verifier and tools. Updates automatically while the project runs.</p><div id='live-activity'><div class='muted'>Loading activity…</div></div></div>
<div class='panel' id='candidate-review-card'><h2>Candidate & review inspector</h2><p class='muted'>Read-only access to the full persisted Worker candidate and the latest Reviewer objections. It does not pause or alter the autonomous cycle.</p><div id='candidate-review-inspector'><div class='muted'>Loading persisted candidate/review data…</div></div></div>
<script>
(function(){
 const parts=window.location.pathname.split('/').filter(Boolean);
 if(parts.length!==2 || parts[0]!=='project') return;
 const slug=encodeURIComponent(parts[1]);
 let refreshing=false;
 let lastHtml='';
 async function refreshActivity(){
   if(refreshing) return;
   refreshing=true;
   const host=document.getElementById('live-activity');
   if(!host){refreshing=false;return;}
   const oldFeed=host.querySelector('.live-feed');
   const oldTop=oldFeed ? oldFeed.scrollTop : 0;
   const pinned=oldFeed ? (oldFeed.scrollHeight-oldFeed.clientHeight-oldFeed.scrollTop < 24) : true;
   try{
     const r=await fetch('/project/'+slug+'/activity',{cache:'no-store'});
     if(!r.ok) return;
     const next=await r.text();
     if(next===lastHtml) return;
     lastHtml=next;
     host.innerHTML=next;
     const newFeed=host.querySelector('.live-feed');
     if(newFeed){
       if(pinned) newFeed.scrollTop=newFeed.scrollHeight;
       else newFeed.scrollTop=Math.min(oldTop,Math.max(0,newFeed.scrollHeight-newFeed.clientHeight));
     }
   }catch(e){
     if(!lastHtml) host.innerHTML='<div class="muted">Activity feed temporarily unavailable.</div>';
   }finally{refreshing=false;}
 }
 let inspectRefreshing=false;
 let inspectLast='';
 async function refreshInspection(){
   if(inspectRefreshing) return;
   inspectRefreshing=true;
   const host=document.getElementById('candidate-review-inspector');
   if(!host){inspectRefreshing=false;return;}
   const openIds=Array.from(host.querySelectorAll('details[open]')).map(d=>d.querySelector('summary')?.textContent || '');
   const oldScrolls=Array.from(host.querySelectorAll('.inspection-text')).map(x=>x.scrollTop);
   try{
     const r=await fetch('/project/'+slug+'/inspection',{cache:'no-store'});
     if(!r.ok) return;
     const next=await r.text();
     if(next===inspectLast) return;
     inspectLast=next;
     host.innerHTML=next;
     Array.from(host.querySelectorAll('details')).forEach(d=>{const s=d.querySelector('summary');if(s && openIds.includes(s.textContent || ''))d.open=true;});
     Array.from(host.querySelectorAll('.inspection-text')).forEach((x,i)=>{if(oldScrolls[i]!==undefined)x.scrollTop=oldScrolls[i];});
   }catch(e){
     if(!inspectLast) host.innerHTML='<div class="muted">Inspection data temporarily unavailable.</div>';
   }finally{inspectRefreshing=false;}
 }
 refreshActivity(); refreshInspection();
 setInterval(refreshActivity,2000);
 setInterval(refreshInspection,15000);
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

# Register generic private-workspace import endpoints only in the deployed live app.
# Imported project contents remain in the local workspace volume and never enter Git.
from awb.web import private_import_routes as _private_import_routes  # noqa: E402,F401
