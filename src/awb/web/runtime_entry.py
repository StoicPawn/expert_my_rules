from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.checkpoints import write_checkpoint
from awb.core.cloud_budget import budget_snapshot, load_control, save_control
from awb.core.cloud_orchestrator import CloudAwareOrchestrator
from awb.core.models import JobStatus, TaskStatus
from awb.core.resume_trace import capture_stream_trace
from awb.core.routing import RouteBusyError
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace
from awb.web.app import base_dir
from awb.web.control_v3 import (
    _load_setup_state,
    _root,
    control_app,
    project_v3 as _project_v3,
)

_CTX = mp.get_context('fork')
_ACTIVE: dict[str, mp.Process] = {}
_ACTIVE_LOCK = threading.Lock()


def _progress_path() -> Path | None:
    raw = os.getenv('AWB_RUNTIME_PROGRESS_FILE', '').strip()
    return Path(raw) if raw else None


def _clear_runtime_progress() -> None:
    path = _progress_path()
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _controlled_sleep(ledger: Ledger, job_id: str, seconds: float) -> bool:
    """Sleep responsively. Return False when the job should stop sleeping."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < deadline:
        current = ledger.get_job(job_id)
        if not current:
            return False
        if current['status'] in {JobStatus.CANCEL_REQUESTED.value, JobStatus.PAUSED.value}:
            return False
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return True


def _run_continuous(root: Path, job_id: str) -> None:
    ledger = Ledger(root / 'ledger.sqlite3')
    ws = load_workspace(root)
    orch = CloudAwareOrchestrator(ws)
    ledger.update_job(job_id, status=JobStatus.RUNNING, detail='autonomous project active · local endurance mode')
    technical_backoff = 1.0

    try:
        while True:
            current = ledger.get_job(job_id)
            if not current:
                return
            if current['status'] == JobStatus.CANCEL_REQUESTED.value:
                ledger.recover_interrupted_tasks()
                ledger.update_job(job_id, status=JobStatus.CANCELLED, detail='stopped by user')
                _clear_runtime_progress()
                return
            while current['status'] == JobStatus.PAUSED.value:
                time.sleep(0.5)
                current = ledger.get_job(job_id)
                if not current:
                    return
                if current['status'] == JobStatus.CANCEL_REQUESTED.value:
                    ledger.recover_interrupted_tasks()
                    ledger.update_job(job_id, status=JobStatus.CANCELLED, detail='stopped by user')
                    _clear_runtime_progress()
                    return
            if orch.is_complete():
                ledger.update_job(job_id, status=JobStatus.COMPLETE, detail='all required completion gates passed')
                try:
                    write_checkpoint(root, reason='north-star-complete', manual=False)
                except Exception:
                    pass
                _clear_runtime_progress()
                return

            before = int(current['steps_done'])

            def control() -> str:
                cur = ledger.get_job(job_id)
                if not cur:
                    return 'cancel'
                if cur['status'] == JobStatus.PAUSED.value:
                    return 'pause'
                if cur['status'] == JobStatus.CANCEL_REQUESTED.value:
                    return 'cancel'
                return 'run'

            def on_step(count, _result) -> None:
                ledger.update_job(
                    job_id,
                    steps_done=before + count,
                    status=JobStatus.RUNNING,
                    detail='autonomous project active · checkpoint committed',
                )

            try:
                orch.run(
                    ws.manifest.runtime.continuous_session_steps,
                    ws.manifest.runtime.continuous_session_minutes,
                    control=control,
                    on_step=on_step,
                )
                technical_backoff = 1.0
            except RouteBusyError as exc:
                # Positive queue timeouts are supported for larger deployments, but
                # ACEPC policy uses an unbounded queue. If one occurs anyway, treat it
                # as back-pressure and never as project failure.
                ledger.update_job(
                    job_id,
                    status=JobStatus.RUNNING,
                    detail=f'attesa risorsa modello: {exc}',
                )
                ledger.event('job_waiting_for_model_capacity', {'error': str(exc)})
                _controlled_sleep(ledger, job_id, min(5.0, max(0.5, float(ws.manifest.runtime.checkpoint_pause_seconds))))
                continue
            except Exception as exc:
                # A local model/tool failure is recoverable. step() has already made
                # its task ERROR without consuming a scientific attempt. Rebuild the
                # orchestrator and let the focused scheduler retry that same chain.
                recovered = ledger.recover_interrupted_tasks()
                max_backoff = max(1.0, float(ws.manifest.runtime.technical_retry_backoff_max_seconds))
                delay = min(max_backoff, technical_backoff)
                detail = f'errore tecnico recuperabile · retry tra {delay:.0f}s · {type(exc).__name__}: {exc}'
                ledger.update_job(job_id, status=JobStatus.RUNNING, detail=detail[:1800])
                ledger.event('job_recoverable_error', {
                    'error': f'{type(exc).__name__}: {exc}',
                    'retry_seconds': delay,
                    'recovered_tasks': recovered,
                })
                try:
                    write_checkpoint(root, reason='recoverable-runtime-error', manual=False)
                except Exception:
                    pass
                _controlled_sleep(ledger, job_id, delay)
                cur = ledger.get_job(job_id)
                if not cur or cur['status'] != JobStatus.RUNNING.value:
                    continue
                ws = load_workspace(root)
                orch = CloudAwareOrchestrator(ws)
                technical_backoff = min(max_backoff, max(2.0, technical_backoff * 2.0))
                continue

            _controlled_sleep(ledger, job_id, ws.manifest.runtime.checkpoint_pause_seconds)
    except (KeyboardInterrupt, SystemExit):
        return
    except BaseException as exc:
        # Reserve FAILED for genuinely fatal process-level conditions. Ordinary
        # model/tool exceptions are handled above and keep the job RUNNING.
        current = ledger.get_job(job_id)
        if current and current['status'] == JobStatus.CANCEL_REQUESTED.value:
            return
        ledger.update_job(job_id, status=JobStatus.FAILED, detail=f'fatal runtime error: {type(exc).__name__}: {exc}')
        ledger.event('job_fatal_error', {'error': f'{type(exc).__name__}: {exc}'})
        _clear_runtime_progress()


def _reap() -> None:
    with _ACTIVE_LOCK:
        dead = [jid for jid, proc in _ACTIVE.items() if not proc.is_alive()]
        for jid in dead:
            proc = _ACTIVE.pop(jid)
            try:
                proc.join(timeout=0.1)
            except Exception:
                pass


def _start_process(root: Path, job_id: str) -> None:
    _reap()
    with _ACTIVE_LOCK:
        current = _ACTIVE.get(job_id)
        if current is not None and current.is_alive():
            return
        proc = _CTX.Process(
            target=_run_continuous,
            args=(root, job_id),
            name=f'awb-job-{job_id}',
            daemon=True,
        )
        proc.start()
        _ACTIVE[job_id] = proc


def _terminate_process(job_id: str) -> bool:
    with _ACTIVE_LOCK:
        proc = _ACTIVE.get(job_id)
    if proc is None:
        return False
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2.0)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2.0)
    with _ACTIVE_LOCK:
        _ACTIVE.pop(job_id, None)
    return True


def _unload_ollama_if_idle() -> None:
    _reap()
    with _ACTIVE_LOCK:
        if any(proc.is_alive() for proc in _ACTIVE.values()):
            return
    base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    try:
        data = httpx.get(f'{base}/api/ps', timeout=2.0).json()
        for model in data.get('models', []):
            name = model.get('name') or model.get('model')
            if name:
                try:
                    httpx.post(f'{base}/api/generate', json={'model': str(name), 'keep_alive': 0}, timeout=4.0)
                except Exception:
                    pass
    except Exception:
        pass


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    control_app.router.routes[:] = [
        route for route in control_app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and method in (getattr(route, 'methods', None) or set())
        )
    ]


for _path, _method in (
    ('/project/{project}', 'GET'),
    ('/project/{project}/launch', 'POST'),
    ('/project/{project}/job/{jid}/pause', 'POST'),
    ('/project/{project}/job/{jid}/resume', 'POST'),
    ('/project/{project}/job/{jid}/cancel', 'POST'),
):
    _remove_route(_path, _method)


@control_app.get('/project/{project}', response_class=HTMLResponse)
def project_page_runtime(request: Request, project: str):
    response = _project_v3(request, project)
    html = response.body.decode('utf-8')
    root = _root(project)
    job = Ledger(root / 'ledger.sqlite3').latest_job()
    html = html.replace('SETUP PRONTO', 'CONFIGURAZIONE PRONTA')
    if job and job['status'] == JobStatus.FAILED.value:
        detail = str(job.get('detail') or 'errore tecnico')
        html = html.replace(
            'Setup automatico pronto e modificabile.',
            f'Configurazione pronta. Ultimo run FERMO: {detail}. Premi RIPRENDI/AVVIA per continuare dallo stato persistito.',
            1,
        )

    cloud = load_control(root)
    snap = budget_snapshot(root)
    unlocked = bool(cloud.enabled and cloud.mode == 'force')
    mode_label = 'API SBLOCCATA MANUALMENTE' if unlocked else 'SOLO LOCALE'
    key_label = 'chiave pronta' if snap.get('api_key_configured') else 'chiave non configurata'
    quick = f"""
<div class='task' style='margin-bottom:14px'>
  <div class='split'><div><b>Inferenza</b><div class='small muted'>{mode_label} · {key_label}</div></div></div>
  <div class='row' style='margin-top:10px'>
    <form method='post' action='/project/{project}/cloud-mode'><input type='hidden' name='mode' value='paused'><button class='secondary'>SOLO LOCALE</button></form>
    <form method='post' action='/project/{project}/cloud-mode'><input type='hidden' name='mode' value='force'><button>SBLOCCA API</button></form>
  </div>
  <p class='small muted'>Default: nessuna chiamata a pagamento. L'API viene usata solo dopo SBLOCCA API e può essere ribloccata in qualunque momento senza riavviare il progetto.</p>
</div>
"""
    html = html.replace('<h2>Budget API di questo progetto</h2>', '<h2>Budget API di questo progetto</h2>' + quick, 1)
    return HTMLResponse(html, headers={'Cache-Control': 'no-store'})


@control_app.post('/project/{project}/cloud-mode')
def cloud_mode_runtime(project: str, mode: str = Form(...)):
    if mode not in {'auto', 'force', 'paused'}:
        raise HTTPException(400, 'Modalità API non valida')
    root = _root(project)
    control = load_control(root)
    # AUTO is retained for old clients but intentionally behaves as local-only in
    # CheckpointedFocusedOrchestrator. The UI exposes only paused/force.
    control.mode = mode
    control.enabled = mode == 'force'
    save_control(root, control)
    Ledger(root / 'ledger.sqlite3').event('cloud_mode_changed', {
        'mode': mode,
        'enabled': control.enabled,
        'source': 'user-ui',
        'manual_unlock': mode == 'force',
    })
    return RedirectResponse(f'/project/{project}?tab=api', 303)


@control_app.post('/project/{project}/launch')
def launch_runtime(project: str):
    root = _root(project)
    setup_state = _load_setup_state(root)
    if setup_state.get('status') in {'RUNNING', 'QUEUED'}:
        raise HTTPException(409, 'Attendi che la configurazione automatica sia terminata prima di avviare il job.')

    ledger = Ledger(root / 'ledger.sqlite3')
    current = ledger.latest_job()
    if current and current['status'] in {JobStatus.RUNNING.value, JobStatus.PAUSED.value}:
        if current['status'] == JobStatus.PAUSED.value:
            ledger.update_job(current['id'], status=JobStatus.RUNNING, detail='resumed from durable checkpoint')
        _start_process(root, current['id'])
        return RedirectResponse(f'/project/{project}?tab=runtime', 303)

    if current is not None:
        recovered = ledger.recover_interrupted_tasks()
        # Preserve all scientific knowledge. ERROR is operational and retryable;
        # BLOCKED stays for focused rework and REJECTED remains an epistemic result.
        reopened = []
        for task in ledger.list_tasks(statuses=[TaskStatus.ERROR]):
            task.status = TaskStatus.OPEN
            if task.metadata.get('focus_chain_active'):
                task.metadata['lifecycle_phase'] = task.metadata.get('lifecycle_phase') or 'REWORK'
            ledger.upsert_task(task)
            reopened.append(task.id)
        ledger.event('project_resumed_from_ledger', {
            'previous_job': current['id'],
            'previous_status': current['status'],
            'recovered_interrupted_tasks': recovered,
            'reopened_technical_tasks': reopened,
            'preserved_gates': True,
            'preserved_negative_results': True,
        })

    jid = ledger.create_job(0, 0, continuous=True)
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='autonomous project active · resumed from ledger')
    _clear_runtime_progress()
    _start_process(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/pause')
def pause_runtime(project: str, jid: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    capture_stream_trace(root, jid, reason='manual-pause-snapshot', interrupted=False)
    ledger.update_job(jid, status=JobStatus.PAUSED, detail='paused by user; state persisted')
    try:
        write_checkpoint(root, reason='manual-pause', manual=True)
    except Exception:
        pass
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/resume')
def resume_runtime(project: str, jid: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='resumed by user from durable state')
    _start_process(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/cancel')
def cancel_runtime(project: str, jid: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    job = ledger.get_job(jid)
    if not job:
        raise HTTPException(404, 'Job non trovato')

    # Capture every visible token/input before terminating the child process.
    capture_stream_trace(root, jid, reason='user-hard-stop', interrupted=True)
    ledger.update_job(jid, status=JobStatus.CANCEL_REQUESTED, detail='arresto immediato richiesto dall’utente')
    killed = _terminate_process(jid)
    recovered = ledger.recover_interrupted_tasks()
    _clear_runtime_progress()
    ledger.update_job(jid, status=JobStatus.CANCELLED, detail='fermato dall’utente; stato persistito per ripresa')
    ledger.event('job_hard_stopped', {'process_was_alive': killed, 'reopened_tasks': recovered})
    try:
        write_checkpoint(root, reason='hard-stop-after-stream-capture', manual=True)
    except Exception:
        pass
    _unload_ollama_if_idle()
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.on_event('startup')
def recover_runtime_jobs() -> None:
    for root in base_dir().iterdir():
        if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
            continue
        try:
            ledger = Ledger(root / 'ledger.sqlite3')
            for job in ledger.recoverable_jobs():
                _start_process(root, job['id'])
        except Exception:
            continue
