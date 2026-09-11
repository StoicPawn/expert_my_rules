from __future__ import annotations

from fastapi.responses import JSONResponse

from awb.core.models import JobStatus
from awb.core.storage import Ledger
from awb.web.dashboard_app import _root, _state as _base_state, dashboard_app

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

    if status in _TERMINAL:
        state['runtime_progress'] = {}
        state['current_task'] = None

    state['configuration_status'] = str(setup.get('status') or 'NOT_STARTED')
    state['run_status'] = status
    if status == JobStatus.FAILED.value:
        state['overall_status'] = 'RUN_FAILED'
    elif status == JobStatus.CANCELLED.value:
        state['overall_status'] = 'STOPPED'
    elif status == JobStatus.COMPLETE.value:
        state['overall_status'] = 'COMPLETE'
    elif status == JobStatus.PAUSED.value:
        state['overall_status'] = 'PAUSED'
    elif status == JobStatus.RUNNING.value:
        state['overall_status'] = 'RUNNING'
    elif setup.get('status') in {'RUNNING', 'QUEUED'}:
        state['overall_status'] = 'SETTING_UP'
    else:
        state['overall_status'] = status

    ledger = Ledger(_root(project) / 'ledger.sqlite3')
    rich = {task.id: task for task in ledger.list_tasks()}
    tasks = list(state.get('tasks') or [])
    seen_relaunch = False
    compact = []
    for row in tasks:
        if row.get('created_by') == 'relaunch':
            if seen_relaunch:
                continue
            seen_relaunch = True
        task = rich.get(str(row.get('id') or ''))
        if task is not None:
            meta = task.metadata or {}
            row = {
                **row,
                'lifecycle_phase': str(meta.get('lifecycle_phase') or ''),
                'focus_chain_id': str(meta.get('focus_chain_id') or ''),
                'focus_chain_active': bool(meta.get('focus_chain_active', False)),
                'critical_objections': list(meta.get('critical_objections') or [])[:12],
                'review_recommendations': list(meta.get('last_review_recommendations') or [])[:12],
                'artifact': str(meta.get('artifact') or ''),
                'interrupted_resume': dict(meta.get('interrupted_resume') or {}),
                'depends_on': list(meta.get('depends_on') or []),
                'verification_contract': dict(meta.get('verification_contract') or {}),
                'strategy_fingerprints': list(meta.get('strategy_fingerprints') or [])[-8:],
            }
        compact.append(row)
    state['tasks'] = compact
    state['total_tasks'] = len(compact)
    state['done_tasks'] = sum(1 for task in compact if task.get('status') == 'DONE')

    current = next((task for task in compact if task.get('status') == 'IN_PROGRESS'), None)
    if status not in _TERMINAL:
        state['current_task'] = current
    state['focused_task'] = next(
        (task for task in compact if task.get('focus_chain_active') and task.get('status') not in {'DONE', 'REJECTED'}),
        None,
    )
    state['local_endurance'] = {
        'single_model_serial': True,
        'busy_queue_unbounded': True,
        'api_manual_only': True,
        'slow_generation_is_failure': False,
        'micro_task_verification_required': True,
        'external_memory': True,
    }
    return JSONResponse(state, headers={'Cache-Control': 'no-store'})


from awb.web.dashboard_story import install_agent_story_dashboard
install_agent_story_dashboard(dashboard_app)

from awb.web.dashboard_deep_live import install_deep_live_dashboard
install_deep_live_dashboard(dashboard_app)
