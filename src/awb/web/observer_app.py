from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from awb.core.storage import Ledger
from awb.web.app import base_dir, page
from awb.web.research_console import _events_fragment, _live_fragment, research_console


observer_app = FastAPI(title="Expert My Rules Observer")


def _project_root(slug: str) -> Path:
    root = base_dir() / slug
    if not (root / "project.yaml").exists():
        raise HTTPException(404, "Project not found")
    return root


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
        "<p class='muted'>Read-only production observer. It never launches, pauses, resumes or modifies autonomous research.</p>"
        + ("".join(cards) if cards else "<p>No project ledger found.</p>")
    )
    return HTMLResponse(page("Expert Observer", body), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console/live", response_class=HTMLResponse)
def console_live(slug: str):
    return HTMLResponse(_live_fragment(_project_root(slug)), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console/events", response_class=HTMLResponse)
def console_events(slug: str):
    return HTMLResponse(_events_fragment(_project_root(slug)), headers={"Cache-Control": "no-store"})


@observer_app.get("/project/{slug}/research-console", response_class=HTMLResponse)
def console(slug: str):
    # Reuse the exact console renderer used by the main dashboard. The decorated
    # function is observational: it only reads the project ledger and renders HTML.
    return research_console(slug)
