from __future__ import annotations

import time
from pathlib import Path

from awb.core.models import JobStatus
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace


def resilient_continuous_runner(root: Path, job_id: str) -> None:
    """Run a continuous project through transient local-model/runtime failures.

    The job remains fail-closed: persistent failures eventually mark it FAILED so a
    human can inspect the blocker, while temporary Ollama/network/runtime failures are
    retried with bounded exponential backoff and the same persistent ledger.
    """
    ws = load_workspace(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    orch = Orchestrator(ws)
    ledger.update_job(job_id, status=JobStatus.RUNNING, detail='autonomous project active')
    consecutive_errors = 0
    max_errors = max(6, ws.manifest.runtime.max_task_attempts * 3)

    while True:
        current = ledger.get_job(job_id)
        if not current:
            return
        if current['status'] == JobStatus.CANCEL_REQUESTED.value:
            ledger.update_job(job_id, status=JobStatus.CANCELLED, detail='cancelled by user')
            return
        while current['status'] == JobStatus.PAUSED.value:
            time.sleep(0.5)
            current = ledger.get_job(job_id)
            if current['status'] == JobStatus.CANCEL_REQUESTED.value:
                ledger.update_job(job_id, status=JobStatus.CANCELLED, detail='cancelled by user')
                return
        if orch.is_complete():
            ledger.update_job(job_id, status=JobStatus.COMPLETE, detail='all required completion gates passed')
            return

        before = int(current['steps_done'])

        def control():
            cur = ledger.get_job(job_id)
            if cur['status'] == JobStatus.PAUSED.value:
                return 'pause'
            if cur['status'] == JobStatus.CANCEL_REQUESTED.value:
                return 'cancel'
            return 'run'

        def on_step(count, _):
            ledger.update_job(job_id, steps_done=before + count, detail='autonomous project active')

        try:
            orch.run(
                ws.manifest.runtime.continuous_session_steps,
                ws.manifest.runtime.continuous_session_minutes,
                control=control,
                on_step=on_step,
            )
            consecutive_errors = 0
            ledger.update_job(job_id, detail='autonomous project active')
            time.sleep(ws.manifest.runtime.checkpoint_pause_seconds)
        except Exception as exc:
            consecutive_errors += 1
            error_type = type(exc).__name__
            ledger.event('continuous_runtime_retry', {
                'error_type': error_type,
                'consecutive_errors': consecutive_errors,
                'max_errors': max_errors,
            })
            if consecutive_errors >= max_errors:
                ledger.update_job(
                    job_id,
                    status=JobStatus.FAILED,
                    detail=f'persistent runtime failure after {consecutive_errors} retries: {error_type}',
                )
                return
            delay = min(60.0, 2.0 ** min(consecutive_errors, 5))
            ledger.update_job(
                job_id,
                status=JobStatus.RUNNING,
                detail=f'transient runtime failure ({error_type}); retry {consecutive_errors}/{max_errors} in {delay:.0f}s',
            )
            time.sleep(delay)


def install_resilient_continuous_loop() -> None:
    # Import lazily to avoid a module cycle during FastAPI app construction.
    from awb.web import app as app_module

    app_module._run_continuous = resilient_continuous_runner
