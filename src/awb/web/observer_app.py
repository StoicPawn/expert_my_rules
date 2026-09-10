from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from awb.core.cloud_budget import CloudBurstControl, budget_snapshot, load_control, save_control
from awb.core.storage import Ledger
from awb.web.app import base_dir, page
from awb.web.research_console import _events_fragment, _live_fragment, research_console


observer_app = FastAPI(title="Expert My Rules Observer")


def _project_root(slug: str) -> Path:
    root = base_dir() / slug
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    return root


def _budget_fragment(root: Path) -> str:
    snap = budget_snapshot(root)
    budget = float(snap['budget_eur'])
    spent = float(snap['spent_eur'])
    remaining = float(snap['remaining_eur'])
    pct = min(100.0, (spent / budget * 100.0) if budget > 0 else 0.0)
    state = 'ENABLED' if snap['enabled'] else 'OFF'
    key = 'configured' if snap['api_key_configured'] else 'NOT configured'
    models = ' · '.join(
        f"{html.escape(role)}: {html.escape(str(cfg['model']))}/{html.escape(str(cfg['reasoning']))}"
        for role, cfg in snap['role_models'].items()
    )
    warning = ''
    if snap['enabled'] and not snap['api_key_configured']:
        warning = "<p class='open'><b>API key missing:</b> paid routing stays local until OPENAI_API_KEY is configured on the ACEPC.</p>"
    return (
        f"<p><b>{state}</b> · internal burst cap <b>€{budget:.2f}</b> · metered <b>€{spent:.4f}</b> · remaining <b>€{remaining:.4f}</b></p>"
        f"<div style='height:10px;background:#e5e5ea;border-radius:8px;overflow:hidden'><div style='height:100%;width:{pct:.2f}%;background:#111'></div></div>"
        f"<p>{snap['calls']} API calls · {snap['input_tokens']:,} input tokens · {snap['output_tokens']:,} output tokens · API key {key}</p>"
        f"<p class='muted'>{models}</p>{warning}"
        "<p class='muted'>The internal meter uses token usage returned by the API and refuses to knowingly start a call that cannot fit in the remaining cap. Enabling a burst never terminates an in-flight local model call; it applies at the next eligible model-call boundary.</p>"
    )


@observer_app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@observer_app.get("/", response_class=HTMLResponse)
def index():
    cards = []
    for root in sorted(base_dir().iterdir()):
        if not (root / "project.yaml").exists() or not (root / "ledger.sqlite3").exists():
            continue
        try:
            ledger = Ledger(root / "ledger.sqlite3")
            job = ledger.latest_job()
            tasks = ledger.list_tasks()
            current = next((task for task in tasks if task.status.value == "IN_PROGRESS"), None)
            current_text = (
                f"Current: {html.escape(current.id)} · {html.escape(current.title)}"
                if current else "No task currently executing"
            )
            status = html.escape(str(job.get("status") if job else "NOT STARTED"))
            cards.append(
                f"<a class='card' href='/project/{html.escape(root.name)}/research-console'>"
                f"<h2>{html.escape(root.name)}</h2><p><b>{status}</b></p><p>{current_text}</p>"
                "<b>Open Research Console →</b></a>"
            )
        except Exception as exc:
            cards.append(
                f"<div class='card'><h2>{html.escape(root.name)}</h2>"
                f"<p class='muted'>Observer could not read this project: {html.escape(type(exc).__name__)}</p></div>"
            )
    body = (
        "<h1>Expert My Rules Observer</h1>"
        "<p class='muted'>Production observer. Research data is read-only; explicit Cloud Burst controls write only the routing policy file and never edit scientific artifacts.</p>"
        + ("".join(cards) if cards else "<p>No project ledger found.</p>")
    )
    return HTMLResponse(page("Expert Observer", body), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console/live", response_class=HTMLResponse)
def console_live(slug: str):
    return HTMLResponse(_live_fragment(_project_root(slug)), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console/events", response_class=HTMLResponse)
def console_events(slug: str):
    return HTMLResponse(_events_fragment(_project_root(slug)), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console/cloud-budget", response_class=HTMLResponse)
def cloud_budget(slug: str):
    return HTMLResponse(_budget_fragment(_project_root(slug)), headers={"Cache-Control": "no-store"})


@observer_app.post("/project/{slug}/research-console/cloud-burst")
def cloud_burst(
    slug: str,
    action: str = Form(...),
    budget_eur: float = Form(5.0),
    priority_threshold: float = Form(1.0),
):
    root = _project_root(slug)
    current = load_control(root)
    if action == 'disable':
        current.enabled = False
        save_control(root, current)
    elif action == 'start':
        control = CloudBurstControl(
            enabled=True,
            budget_eur=max(0.01, min(float(budget_eur), 1000.0)),
            priority_threshold=max(0.0, float(priority_threshold)),
        )
        save_control(root, control, reset_meter=True)
        Ledger(root / 'ledger.sqlite3').event('cloud_burst_enabled', {
            'budget_eur': control.budget_eur,
            'priority_threshold': control.priority_threshold,
            'safe_handoff': 'next model-call boundary',
        })
    else:
        raise HTTPException(400, 'Unknown cloud burst action')
    return RedirectResponse(f'/project/{slug}/research-console', status_code=303)


@observer_app.get("/project/{slug}/research-console", response_class=HTMLResponse)
def console(slug: str):
    root = _project_root(slug)
    rendered = research_console(slug)
    body = rendered.body.decode('utf-8')
    control = load_control(root)
    panel = f"""
<div class='panel' id='cloud-burst-panel'>
<h2>Cloud Burst</h2>
<div id='cloud-budget'>{_budget_fragment(root)}</div>
<form method='post' action='/project/{html.escape(slug)}/research-console/cloud-burst'>
<label>Internal burst spending cap (€)</label><input name='budget_eur' type='number' min='0.01' max='1000' step='0.01' value='{control.budget_eur:.2f}'>
<label>Task priority threshold (1 = essentially all task work)</label><input name='priority_threshold' type='number' min='0' step='0.1' value='{control.priority_threshold:.1f}'>
<button name='action' value='start'>Start / reset cloud burst</button>
<button class='secondary' name='action' value='disable'>Disable cloud burst</button>
</form>
<p class='muted'>Worker and Reviewer default to GPT-5.6 Sol for difficult scientific work; Director and Verifier use GPT-5.6 Terra to conserve the burst. If the next Sol call cannot fit, the router may use Terra or fall back locally according to role.</p>
</div>
<script>
setInterval(async()=>{{try{{const r=await fetch('/project/{html.escape(slug)}/research-console/cloud-budget',{{cache:'no-store'}});if(r.ok)document.getElementById('cloud-budget').innerHTML=await r.text();}}catch(e){{}}}},2000);
</script>
"""
    marker = '<div class=\'panel filters\'>'
    if marker in body:
        body = body.replace(marker, panel + marker, 1)
    else:
        body = body.replace('</body>', panel + '</body>', 1)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})
