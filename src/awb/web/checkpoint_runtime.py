from __future__ import annotations

import html as _html

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.checkpoints import build_project_state, latest_checkpoint, write_checkpoint
from awb.core.cloud_budget import load_control
from awb.core.focused_cloud_orchestrator import FocusedCloudAwareOrchestrator
from awb.core.models import JobStatus
from awb.core.resume_trace import capture_stream_trace
from awb.core.storage import Ledger
from awb.web import runtime_entry
from awb.web.control_v3 import _root, control_app


class CheckpointedFocusedOrchestrator(FocusedCloudAwareOrchestrator):
    """Focused reviewer loop + durable checkpoints + explicit-only paid API."""

    def _cloud_important(self, role, task):
        # AUTO is intentionally treated as local-only in endurance mode. Paid API
        # calls are possible only after the user presses SBLOCCA API / FORCE.
        control = load_control(self.workspace.root)
        if control.mode != 'force':
            return False
        return super()._cloud_important(role, task)

    def step(self):
        try:
            result = super().step()
        except BaseException:
            try:
                write_checkpoint(self.workspace.root, reason='technical-step-boundary', manual=False)
            except Exception:
                pass
            raise
        try:
            write_checkpoint(
                self.workspace.root,
                reason=f'completed-step:{result.task.id}:{result.task.status.value}',
                manual=False,
            )
        except Exception as exc:
            try:
                self.ledger.event('checkpoint_write_failed', {'error': f'{type(exc).__name__}: {exc}'})
            except Exception:
                pass
        return result


runtime_entry.CloudAwareOrchestrator = CheckpointedFocusedOrchestrator


_original_project_page = runtime_entry.project_page_runtime
runtime_entry._remove_route('/project/{project}', 'GET')


def _short(text: object, limit: int = 180) -> str:
    s = str(text or '').replace('\n', ' ').strip()
    return s if len(s) <= limit else s[: limit - 1] + '…'


@control_app.get('/project/{project}', response_class=HTMLResponse)
def checkpoint_project_page(request: Request, project: str):
    response = _original_project_page(request, project)
    page = response.body.decode('utf-8')
    root = _root(project)
    state = build_project_state(root)
    saved = latest_checkpoint(root)
    summary = state['summary']
    nxt = state.get('next_best_action') or {}
    last_id = (saved or {}).get('checkpoint_id', 'nessuno')
    last_reason = _short((saved or {}).get('reason', ''), 100)
    in_flight = int(summary.get('in_progress') or 0)
    next_text = 'nessuna azione irrisolta'
    if nxt:
        next_text = f"{nxt.get('title','')} · {nxt.get('phase') or nxt.get('status','')}"
        if nxt.get('strategy'):
            next_text += f" · {_short(nxt.get('strategy'), 150)}"

    card = f"""
<div class='task' style='margin-bottom:14px'>
  <div class='split'>
    <div><b>Checkpoint & outcome del progetto</b>
      <div class='small muted'>ultimo: {_html.escape(str(last_id))}{(' · ' + _html.escape(last_reason)) if last_reason else ''}</div>
    </div>
    <div class='small'>{'chiamata/task in corso' if in_flight else 'stato consistente'}</div>
  </div>
  <div class='small' style='margin-top:8px'>
    consolidati <b>{summary['done']}</b> · blocked/negativi <b>{summary['blocked'] + summary['rejected']}</b> · aperti <b>{summary['open']}</b> · in corso <b>{summary['in_progress']}</b>
  </div>
  <div class='small muted' style='margin-top:6px'><b>Prossima azione:</b> {_html.escape(next_text)}</div>
  <div class='row' style='margin-top:10px'>
    <form method='post' action='/project/{project}/checkpoint'><button class='secondary'>SALVA CHECKPOINT ORA</button></form>
    <form method='post' action='/project/{project}/checkpoint-pause'><button>CHECKPOINT & PAUSA SICURA</button></form>
  </div>
  <p class='small muted'>Il checkpoint salva risultati positivi e negativi, obiezioni, verifiche, artifact, strategia, focus chain e la generazione locale visibile in corso. Il reasoning nascosto non viene salvato.</p>
</div>
"""
    anchor = '<h2>Budget API di questo progetto</h2>'
    if anchor in page:
        page = page.replace(anchor, card + anchor, 1)
    else:
        page = page.replace('</body>', card + '</body>', 1)
    return HTMLResponse(page, headers={'Cache-Control': 'no-store'})


@control_app.post('/project/{project}/checkpoint')
def checkpoint_now(project: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    job = ledger.latest_job()
    if job:
        capture_stream_trace(root, job.get('id'), reason='manual-checkpoint', interrupted=False)
    write_checkpoint(root, reason='manual-checkpoint', manual=True)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/checkpoint-pause')
def checkpoint_and_pause(project: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    job = ledger.latest_job()
    if not job:
        write_checkpoint(root, reason='manual-checkpoint-no-running-job', manual=True)
        return RedirectResponse(f'/project/{project}?tab=runtime', 303)
    capture_stream_trace(root, job.get('id'), reason='manual-checkpoint-pause-requested', interrupted=False)
    if job['status'] in {JobStatus.RUNNING.value, JobStatus.QUEUED.value}:
        ledger.update_job(job['id'], status=JobStatus.PAUSED, detail='checkpoint pause requested; waiting for safe step boundary')
        ledger.event('checkpoint_pause_requested', {'job_id': job['id']})
    elif job['status'] not in {JobStatus.PAUSED.value, JobStatus.COMPLETE.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value, JobStatus.BUDGET_FINISHED.value}:
        raise HTTPException(409, f"Job non checkpointabile nello stato {job['status']}")
    write_checkpoint(root, reason='manual-checkpoint-pause-requested', manual=True)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)
