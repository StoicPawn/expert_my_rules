from __future__ import annotations

from fastapi.responses import JSONResponse

from awb.core.models import JobStatus
from awb.web.dashboard_app import _state as _base_state, dashboard_app

_TERMINAL = {
    JobStatus.CANCELLED.value,
    JobStatus.COMPLETE.value,
    JobStatus.BUDGET_FINISHED.value,
    JobStatus.FAILED.value,
}


def _remove_route(path: str, method: str) -> None:
    method = method.upper()
    dashboard_app.router.routes[:] = [
        route for route in dashboard_app.router.routes
        if not (
            getattr(route, 'path', None) == path
            and method in (getattr(route, 'methods', None) or set())
        )
    ]


_remove_route('/project/{project}/state', 'GET')


@dashboard_app.get('/project/{project}/state')
def coherent_state(project: str):
    state = _base_state(project)
    job = state.get('job') or {}
    status = str(job.get('status') or 'NOT STARTED')
    setup = state.get('setup') or {}

    # Runtime telemetry is only meaningful for an active run/setup. A terminal job
    # must never keep displaying a stale "worker · generating" record left by a
    # process that has already stopped or failed.
    if status in _TERMINAL:
        state['runtime_progress'] = {}
        state['current_task'] = None

    # Expose the two state machines explicitly so the UI/API cannot conflate
    # "configuration generated successfully" with "autonomous run is healthy".
    state['configuration_status'] = str(setup.get('status') or 'NOT_STARTED')
    state['run_status'] = status
    if status == JobStatus.FAILED.value:
        state['overall_status'] = 'RUN_FAILED'
    elif status == JobStatus.CANCELLED.value:
        state['overall_status'] = 'STOPPED'
    elif status == JobStatus.COMPLETE.value:
        state['overall_status'] = 'COMPLETE'
    elif status == JobStatus.RUNNING.value:
        state['overall_status'] = 'RUNNING'
    elif setup.get('status') in {'RUNNING', 'QUEUED'}:
        state['overall_status'] = 'SETTING_UP'
    else:
        state['overall_status'] = status

    # Old relaunch builds could leave several identical OPEN reassessment cards.
    # Keep at most the current/most relevant one in the live plan even before the
    # persistent cleanup runs on the next launch/deploy.
    tasks = list(state.get('tasks') or [])
    seen_relaunch = False
    compact = []
    for task in tasks:
        if task.get('created_by') == 'relaunch':
            if seen_relaunch:
                continue
            seen_relaunch = True
        compact.append(task)
    state['tasks'] = compact
    state['total_tasks'] = len(compact)
    state['done_tasks'] = sum(1 for task in compact if task.get('status') == 'DONE')

    return JSONResponse(state, headers={'Cache-Control': 'no-store'})
