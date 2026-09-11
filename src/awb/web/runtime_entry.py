from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from awb.core.cloud_budget import budget_snapshot, load_control, save_control
from awb.core.cloud_orchestrator import CloudAwareOrchestrator
from awb.core.models import JobStatus, Task, TaskStatus
from awb.core.routing import RouteBusyError
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace
from awb.web.app import base_dir
from awb.web.control_v3 import (
    TERMINAL_JOB_STATES,
    _load_setup_state,
    _root,
    control_app,
    project_v3 as _project_v3,
)

# Autonomous runs are isolated in child processes. Killing a run therefore closes
# in-flight model HTTP connections as well; Stop is no longer a ledger-only flag.
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


def _run_continuous(root: Path, job_id: str) -> None:
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    # CloudAwareOrchestrator is the single runtime engine: API mode is evaluated
    # at every model-call boundary, so FORZA API can start/continue on OpenAI and
    # PAUSA API switches the same running job back to local LLM routes.
    orch = CloudAwareOrchestrator(ws)
    ledger.update_job(job_id, status=JobStatus.RUNNING, detail='autonomous project active')
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
                ledger.update_job(job_id, steps_done=before + count, detail='autonomous project active')

            try:
                orch.run(
                    ws.manifest.runtime.continuous_session_steps,
                    ws.manifest.runtime.continuous_session_minutes,
                    control=control,
                    on_step=on_step,
                )
            except RouteBusyError as exc:
                # Resource contention is operational back-pressure, not a project
                # failure. The task is already recorded as a technical ERROR by the
                # orchestrator and will be retried/replanned without consuming a
                # scientific attempt.
                ledger.update_job(
                    job_id,
                    status=JobStatus.RUNNING,
                    detail=f'attesa risorsa modello: {exc}',
                )
                ledger.event('job_waiting_for_model_capacity', {'error': str(exc)})
                time.sleep(min(5.0, max(0.5, float(ws.manifest.runtime.checkpoint_pause_seconds))))
                continue

            time.sleep(ws.manifest.runtime.checkpoint_pause_seconds)
    except BaseException as exc:
        current = ledger.get_job(job_id)
        # A hard Stop terminates this process; the parent finalizes CANCELLED.
        if current and current['status'] == JobStatus.CANCEL_REQUESTED.value:
            return
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return
        ledger.update_job(job_id, status=JobStatus.FAILED, detail=f'{type(exc).__name__}: {exc}')
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
                    httpx.post(
                        f'{base}/api/generate',
                        json={'model': str(name), 'keep_alive': 0},
                        timeout=4.0,
                    )
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


# Replace launch/control routes with process-isolated lifecycle operations.
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
    # Configuration readiness and run health are separate states. Avoid presenting
    # a green "setup ready" as if a failed run were healthy.
    html = html.replace('SETUP PRONTO', 'CONFIGURAZIONE PRONTA')
    if job and job['status'] == JobStatus.FAILED.value:
        detail = str(job.get('detail') or 'errore tecnico')
        html = html.replace(
            'Setup automatico pronto e modificabile.',
            f'Configurazione pronta. Ultimo run FERMO per errore tecnico: {detail}',
            1,
        )

    cloud = load_control(root)
    snap = budget_snapshot(root)
    mode_label = {
        'auto': 'AUTO',
        'force': 'API FORZATA',
        'paused': 'API IN PAUSA',
    }.get(cloud.mode, cloud.mode.upper())
    if not cloud.enabled:
        mode_label = 'API DISABILITATA'
    key_label = 'chiave pronta' if snap.get('api_key_configured') else 'chiave NON configurata'
    reserve_note = ''
    reserved = float(snap.get('reserved_eur') or 0.0)
    monthly_reserved = float(snap.get('monthly_reserved_eur') or 0.0)
    if reserved or monthly_reserved:
        reserve_note = f" · riservato €{reserved:.4f} progetto / €{monthly_reserved:.4f} mese"
    quick = f"""
<div class='task' style='margin-bottom:14px'>
  <div class='split'><div><b>Modalità API adesso</b><div class='small muted'>{mode_label} · {key_label}{reserve_note}</div></div></div>
  <div class='row' style='margin-top:10px'>
    <form method='post' action='/project/{project}/cloud-mode'><input type='hidden' name='mode' value='auto'><button class='secondary'>AUTO</button></form>
    <form method='post' action='/project/{project}/cloud-mode'><input type='hidden' name='mode' value='force'><button>FORZA API</button></form>
    <form method='post' action='/project/{project}/cloud-mode'><input type='hidden' name='mode' value='paused'><button class='secondary'>PAUSA API</button></form>
  </div>
  <p class='small muted'>FORZA API manda tutte le prossime chiamate eleggibili su OpenAI; PAUSA API blocca nuove chiamate esterne e continua lo stesso job con i modelli locali. La modalità viene riletta a ogni chiamata, quindi non serve riavviare il progetto.</p>
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
    control.mode = mode
    if mode in {'auto', 'force'}:
        control.enabled = True
    save_control(root, control)
    Ledger(root / 'ledger.sqlite3').event('cloud_mode_changed', {
        'mode': mode,
        'enabled': control.enabled,
        'source': 'user-ui',
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
        _start_process(root, current['id'])
        return RedirectResponse(f'/project/{project}?tab=runtime', 303)

    if current is not None:
        # Repair any task left IN_PROGRESS by a crashed/killed previous run first.
        ledger.recover_interrupted_tasks()
        ws = load_workspace(root)
        for gate in ws.manifest.gates:
            if gate.required:
                ledger.set_gate(gate.id, False, f'reopened for new run after {current["status"]}')
        for task in ledger.list_tasks(statuses=[TaskStatus.BLOCKED, TaskStatus.ERROR, TaskStatus.REJECTED]):
            task.status = TaskStatus.OPEN
            ledger.upsert_task(task)

        # A relaunch has one logical reassessment task. Keep historical events/jobs,
        # but do not accumulate identical OPEN cards on every button press.
        ledger.conn.execute("DELETE FROM tasks WHERE created_by='relaunch'")
        ledger.conn.commit()
        reassess = Task(
            id='RELAUNCH-REASSESS',
            title='Reassess project under the current setup',
            description='Re-read the current North Star, supplied material, setup and ledger history; identify the highest-value unresolved work for this new run.',
            priority=100.0,
            created_by='relaunch',
        )
        ledger.upsert_task(reassess)
        ledger.event('project_relaunched', {'previous_job': current['id'], 'previous_status': current['status']})

    jid = ledger.create_job(0, 0, continuous=True)
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='autonomous project active')
    _clear_runtime_progress()
    _start_process(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/pause')
def pause_runtime(project: str, jid: str):
    Ledger(_root(project) / 'ledger.sqlite3').update_job(
        jid, status=JobStatus.PAUSED, detail='paused by user'
    )
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/resume')
def resume_runtime(project: str, jid: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    ledger.update_job(jid, status=JobStatus.RUNNING, detail='resumed by user')
    _start_process(root, jid)
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.post('/project/{project}/job/{jid}/cancel')
def cancel_runtime(project: str, jid: str):
    root = _root(project)
    ledger = Ledger(root / 'ledger.sqlite3')
    job = ledger.get_job(jid)
    if not job:
        raise HTTPException(404, 'Job non trovato')

    ledger.update_job(jid, status=JobStatus.CANCEL_REQUESTED, detail='arresto immediato richiesto dall’utente')
    killed = _terminate_process(jid)
    recovered = ledger.recover_interrupted_tasks()
    _clear_runtime_progress()
    ledger.update_job(
        jid,
        status=JobStatus.CANCELLED,
        detail='fermato dall’utente; processo di inferenza terminato',
    )
    ledger.event(
        'job_hard_stopped',
        {'process_was_alive': killed, 'reopened_tasks': recovered},
    )
    _unload_ollama_if_idle()
    return RedirectResponse(f'/project/{project}?tab=runtime', 303)


@control_app.on_event('startup')
def recover_runtime_jobs() -> None:
    # Any job that survived a container restart is re-homed in an isolated child
    # process so subsequent Stop requests can terminate it for real.
    for root in base_dir().iterdir():
        if not (root / 'project.yaml').exists() or not (root / 'ledger.sqlite3').exists():
            continue
        try:
            ledger = Ledger(root / 'ledger.sqlite3')
            for job in ledger.recoverable_jobs():
                _start_process(root, job['id'])
        except Exception:
            continue
