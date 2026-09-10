from __future__ import annotations

import difflib
import html
import json
from pathlib import Path
from urllib.parse import unquote

from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from awb.core.models import TaskStatus
from awb.core.storage import Ledger
from awb.providers.runtime_progress import get_progress
from awb.web.app import app, base_dir, page
from awb.web.clarity_overlay import _machine_summary


_SECRET_HINTS = ("token", "secret", "password", "api_key", "apikey", "authorization", "credential")
_HUMAN_KINDS = {
    "work_output",
    "review",
    "task_recovery_planned",
    "task_decomposed",
    "task_reframed",
    "task_rejected_by_evidence",
    "task_blocked",
    "task_scientifically_closed",
    "verification",
    "validator_passed",
    "validator_failed",
    "gate_evaluated",
    "tool_call",
    "model_call_started",
    "model_call_finished",
    "model_call_failed",
    "model_escalated",
    "task_created",
    "task_started",
}


def _redact(value, key: str = ""):
    if any(hint in key.lower() for hint in _SECRET_HINTS):
        return "***redacted***"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def _json(value) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, indent=2, default=str)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return _json(value)


def _task_title(tasks: dict[str, object], task_id: str | None) -> str:
    if not task_id:
        return "Project-wide"
    task = tasks.get(task_id)
    return getattr(task, "title", task_id) if task else task_id


def _format_elapsed(seconds: object) -> str:
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


def _previous_work_by_seq(events: list[dict]) -> dict[int, str]:
    previous: dict[int, str] = {}
    last_by_task: dict[str, str] = {}
    for event in reversed(events):
        if event.get("kind") != "work_output" or not event.get("task_id"):
            continue
        payload = event.get("payload") or {}
        text = _text(payload.get("text"))
        seq = int(event.get("seq") or 0)
        task_id = str(event["task_id"])
        if task_id in last_by_task:
            previous[seq] = last_by_task[task_id]
        last_by_task[task_id] = text
    return previous


def _mechanical_delta(before: str, after: str, limit_lines: int = 120, limit_chars: int = 18000) -> str:
    if not before or before == after:
        return ""
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="previous candidate",
            tofile="this candidate",
            lineterm="",
            n=2,
        )
    )
    if not lines:
        return ""
    clipped = lines[:limit_lines]
    text = "\n".join(clipped)
    if len(lines) > limit_lines:
        text += f"\n… {len(lines) - limit_lines} more diff lines omitted"
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n… diff truncated"
    return text


def _event_body(event: dict, previous_work: str = "") -> tuple[str, str, str]:
    kind = str(event.get("kind") or "event")
    payload = event.get("payload") or {}
    if kind == "work_output":
        candidate = _text(payload.get("text"))
        delta = _mechanical_delta(previous_work, candidate)
        body = f"<h4>Exact Worker candidate</h4><pre>{html.escape(candidate)}</pre>"
        if delta:
            body += (
                "<details><summary><b>Mechanical delta vs previous candidate</b></summary>"
                "<p class='muted'>Literal additions/removals only; this is not an LLM interpretation. Formula or sign changes remain visible exactly as text.</p>"
                f"<pre class='delta'>{html.escape(delta)}</pre></details>"
            )
        return "worker", "Worker candidate", body
    if kind == "review":
        approved = payload.get("approved")
        objections = payload.get("critical_objections") or []
        recommendations = payload.get("recommendations") or []
        state = "APPROVED" if approved else "CHALLENGED"
        body = f"<p><b>{state}</b></p>"
        if objections:
            body += "<h4>Critical objections</h4><ol>" + "".join(
                f"<li>{html.escape(_text(item))}</li>" for item in objections
            ) + "</ol>"
        if recommendations:
            body += "<h4>Recommendations</h4><ul>" + "".join(
                f"<li>{html.escape(_text(item))}</li>" for item in recommendations
            ) + "</ul>"
        return "reviewer", "Reviewer result", body
    if kind == "task_recovery_planned":
        body = (
            f"<h4>Next strategy</h4><pre>{html.escape(_text(payload.get('strategy')))}</pre>"
            f"<h4>Rationale</h4><pre>{html.escape(_text(payload.get('rationale')))}</pre>"
        )
        return "director", "Director recovery plan", body
    if kind == "task_decomposed":
        return "director", "Director decomposed the task", f"<pre>{html.escape(_json(payload))}</pre>"
    if kind == "task_reframed":
        return "director", "Director reframed the task", f"<pre>{html.escape(_json(payload))}</pre>"
    if kind == "task_rejected_by_evidence":
        return "director", "Task rejected by evidence", f"<pre>{html.escape(_json(payload))}</pre>"
    if kind == "task_blocked":
        return "reviewer", "Task blocked after scientific review", f"<pre>{html.escape(_json(payload))}</pre>"
    if kind == "task_scientifically_closed":
        return "reviewer", "Task scientifically closed", f"<pre>{html.escape(_json(payload))}</pre>"
    if kind in {"verification", "validator_passed", "validator_failed", "gate_evaluated"}:
        return "verifier", kind.replace("_", " ").title(), f"<pre>{html.escape(_json(payload))}</pre>"
    if kind == "tool_call":
        tool = html.escape(str(payload.get("tool") or "tool"))
        args = html.escape(_json(payload.get("arguments") or {}))
        result = html.escape(_json(payload.get("result") or {}))
        body = f"<p><b>{tool}</b></p><h4>Arguments</h4><pre>{args}</pre><h4>Result</h4><pre>{result}</pre>"
        return "tool", "Tool call", body
    if kind.startswith("model_call") or kind == "model_escalated":
        return "model", kind.replace("_", " ").title(), f"<pre>{html.escape(_json(payload))}</pre>"
    return "raw", kind.replace("_", " ").title(), f"<pre>{html.escape(_json(payload))}</pre>"


def _live_fragment(root: Path) -> str:
    ledger = Ledger(root / "ledger.sqlite3")
    tasks = ledger.list_tasks()
    current = next((task for task in tasks if task.status == TaskStatus.IN_PROGRESS), None)
    job = ledger.latest_job()
    progress = get_progress()
    machine = _machine_summary()

    if current:
        current_html = f"<b>{html.escape(current.id)} · {html.escape(current.title)}</b>"
    else:
        current_html = "<b>No task currently IN_PROGRESS</b>"
    job_html = html.escape(str(job.get("status") if job else "NOT STARTED"))

    if progress:
        model = html.escape(str(progress.get("model") or "local model"))
        elapsed = html.escape(_format_elapsed(progress.get("elapsed_seconds")))
        state = html.escape(str(progress.get("state") or "generating"))
        chars = int(progress.get("output_chars") or 0)
        chunks = int(progress.get("chunks") or 0)
        tail = str(progress.get("visible_tail") or "")
        progress_html = (
            f"<b>{model} · {state}</b><p>elapsed {elapsed} · {chunks} streamed chunks · {chars} visible chars</p>"
        )
        if tail:
            progress_html += (
                "<h4>Live visible model stream</h4>"
                "<p class='muted'>This is only text emitted in the model's visible <code>content</code> stream. Hidden/reasoning fields are not exposed. The text may be incomplete until the call finishes.</p>"
                f"<pre class='live-tail'>{html.escape(tail)}</pre>"
            )
        else:
            progress_html += "<p class='muted'>No visible content chunk has arrived yet.</p>"
    else:
        progress_html = "<span class='muted'>No active local-model stream is currently registered.</span>"

    return (
        "<div class='status-grid'>"
        f"<div><span>Current task</span>{current_html}</div>"
        f"<div><span>Project job</span><b>{job_html}</b></div>"
        f"<div class='wide'><span>Machine</span><b>{html.escape(machine)}</b></div>"
        f"<div class='wide'><span>Active model output</span>{progress_html}</div>"
        "</div>"
    )


def _events_fragment(root: Path) -> str:
    ledger = Ledger(root / "ledger.sqlite3")
    tasks_list = ledger.list_tasks()
    tasks = {task.id: task for task in tasks_list}
    events = ledger.recent_events(500)
    previous = _previous_work_by_seq(events)

    human_cards: list[str] = []
    raw_cards: list[str] = []
    for event in events:
        seq = int(event.get("seq") or 0)
        kind = str(event.get("kind") or "event")
        task_id = event.get("task_id")
        task_title = _task_title(tasks, task_id)
        ts = html.escape(str(event.get("ts") or ""))
        category, title, body = _event_body(event, previous.get(seq, ""))
        raw = html.escape(_json(event))
        search_blob = html.escape((task_title + " " + kind + " " + _text(event.get("payload"))).lower())
        card = (
            f"<article class='event-card {category}' data-search='{search_blob}' data-kind='{html.escape(kind)}' data-task='{html.escape(str(task_id or ''))}'>"
            f"<div class='event-meta'><span>#{seq}</span><span>{ts}</span><span>{html.escape(kind)}</span></div>"
            f"<h3>{html.escape(title)}</h3><p><b>{html.escape(task_title)}</b>"
            + (f" <span class='muted'>({html.escape(str(task_id))})</span>" if task_id else "")
            + f"</p>{body}"
            "<details class='raw-json'><summary>Raw ledger event / machine-readable JSON</summary>"
            f"<pre>{raw}</pre></details></article>"
        )
        raw_cards.append(card)
        if kind in _HUMAN_KINDS:
            human_cards.append(card)

    if not human_cards:
        human_cards.append("<p class='muted'>No agent-produced result has been persisted yet.</p>")
    return (
        "<div id='human-stream'>" + "".join(human_cards[:160]) + "</div>"
        "<details class='all-events'><summary><b>All recent ledger events</b> — raw operational history</summary>"
        "<div id='raw-stream'>" + "".join(raw_cards[:220]) + "</div></details>"
    )


def _root(slug: str) -> Path:
    root = base_dir() / unquote(slug)
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    return root


@app.get("/project/{slug}/research-console/live", response_class=HTMLResponse)
def research_console_live(slug: str):
    return HTMLResponse(_live_fragment(_root(slug)), headers={"Cache-Control": "no-store"})


@app.get("/project/{slug}/research-console/events", response_class=HTMLResponse)
def research_console_events(slug: str):
    return HTMLResponse(_events_fragment(_root(slug)), headers={"Cache-Control": "no-store"})


@app.get("/project/{slug}/research-console", response_class=HTMLResponse)
def research_console(slug: str):
    root = _root(slug)
    ledger = Ledger(root / "ledger.sqlite3")
    tasks = ledger.list_tasks()
    task_options = "".join(
        f"<option value='{html.escape(task.id)}'>{html.escape(task.id)} · {html.escape(task.title)}</option>"
        for task in tasks
    )
    safe_slug = html.escape(unquote(slug))
    body = f"""
<a href='/project/{safe_slug}'>← Project dashboard</a>
<h1>Research Console</h1>
<p class='muted'>Exact observable output from the autonomous research process: live visible model text, Worker candidates, Reviewer objections, Director recovery plans, tool calls, verification and raw ledger events. Read-only.</p>
<div class='panel filters'><label>Search text / formula / term<input id='console-search' placeholder='e.g. sign, minus, theorem, counterexample, covariance'></label><label>Task<select id='console-task'><option value=''>All tasks</option>{task_options}</select></label><button class='secondary' type='button' id='clear-filter'>Clear</button></div>
<div class='panel'><h2>Now</h2><div id='console-live'><span class='muted'>Loading live state…</span></div></div>
<div class='panel'><h2>Agent output stream</h2><p class='muted'>Newest first. Worker candidates are shown in full; when a prior candidate exists, a literal line diff is available so mathematical/formula changes can be inspected directly.</p><div id='console-events'><span class='muted'>Loading persisted agent output…</span></div></div>
<style>
.filters{{display:grid;grid-template-columns:2fr 2fr auto;gap:12px;align-items:end}}.filters button{{margin-bottom:10px}}.status-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.status-grid>div{{background:#f7f7f8;border-radius:12px;padding:12px}}.status-grid .wide{{grid-column:1/-1}}.status-grid span{{display:block;color:#666;font-size:12px;margin-bottom:5px}}.status-grid p{{margin:5px 0}}.live-tail{{max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-word;background:#111;color:#eee;padding:12px;border-radius:10px}}.event-card{{border:1px solid #e2e2e7;border-radius:14px;padding:14px;margin:12px 0;background:#fff}}.event-card.worker{{border-left:5px solid #1d4ed8}}.event-card.reviewer{{border-left:5px solid #8a4a00}}.event-card.director{{border-left:5px solid #6b21a8}}.event-card.tool{{border-left:5px solid #555}}.event-card.verifier{{border-left:5px solid #087c35}}.event-card.model{{border-left:5px solid #3f6212}}.event-card h3{{margin:6px 0}}.event-card h4{{margin:14px 0 6px}}.event-meta{{display:flex;gap:10px;flex-wrap:wrap;color:#777;font-size:12px}}.event-card pre{{white-space:pre-wrap;word-break:break-word;max-height:650px;overflow:auto;background:#f7f7f8;padding:12px;border-radius:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;line-height:1.45}}.event-card pre.delta{{background:#f4f4f6}}.raw-json{{margin-top:12px}}.all-events{{margin-top:24px;border-top:1px solid #ddd;padding-top:16px}}.is-hidden{{display:none!important}}@media(max-width:700px){{.filters,.status-grid{{grid-template-columns:1fr}}.status-grid .wide{{grid-column:auto}}}}
</style>
<script>
(function(){{
 const slug={json.dumps(unquote(slug))};
 const live=document.getElementById('console-live');
 const events=document.getElementById('console-events');
 const search=document.getElementById('console-search');
 const task=document.getElementById('console-task');
 let lastLive='',lastEvents='';
 function applyFilters(){{
   const q=(search.value||'').trim().toLowerCase(); const t=task.value||'';
   document.querySelectorAll('#console-events .event-card').forEach(card=>{{
     const okText=!q || (card.dataset.search||'').includes(q) || card.textContent.toLowerCase().includes(q);
     const okTask=!t || card.dataset.task===t;
     card.classList.toggle('is-hidden',!(okText&&okTask));
   }});
 }}
 async function refreshLive(){{try{{const r=await fetch('/project/'+encodeURIComponent(slug)+'/research-console/live',{{cache:'no-store'}});if(!r.ok)return;const x=await r.text();if(x!==lastLive){{lastLive=x;live.innerHTML=x;}}}}catch(e){{if(!lastLive)live.innerHTML='<span class="muted">Live state unavailable.</span>';}}}}
 async function refreshEvents(){{try{{const r=await fetch('/project/'+encodeURIComponent(slug)+'/research-console/events',{{cache:'no-store'}});if(!r.ok)return;const x=await r.text();if(x!==lastEvents){{lastEvents=x;events.innerHTML=x;applyFilters();}}}}catch(e){{if(!lastEvents)events.innerHTML='<span class="muted">Event stream unavailable.</span>';}}}}
 search.addEventListener('input',applyFilters); task.addEventListener('change',applyFilters);
 document.getElementById('clear-filter').addEventListener('click',()=>{{search.value='';task.value='';applyFilters();}});
 refreshLive();refreshEvents();setInterval(refreshLive,2000);setInterval(refreshEvents,12000);
}})();
</script>
"""
    return HTMLResponse(page("Research Console", body), headers={"Cache-Control": "no-store"})
