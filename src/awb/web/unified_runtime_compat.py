from __future__ import annotations

import os
import threading
from pathlib import Path

from awb.core.resource_policy import bind_project_resource_env, load_project_policy, save_project_policy


_LOCK = threading.Lock()
_INSTALLED = False


def _unwrap_run(runtime_module):
    """Undo the temporary UI-layer wrapper around _run_continuous.

    The endurance runtime itself is authoritative and has long-standing recovery
    contracts (RouteBusyError/back-pressure, fatal-vs-recoverable semantics). Keep
    that function intact; resource binding belongs at process spawn time.
    """
    current = runtime_module._run_continuous
    closure = getattr(current, '__closure__', None) or ()
    freevars = getattr(getattr(current, '__code__', None), 'co_freevars', ())
    for name, cell in zip(freevars, closure):
        if name == 'original_run' and callable(cell.cell_contents):
            runtime_module._run_continuous = cell.cell_contents
            return


def _ensure_resource_file(root: Path) -> None:
    path = root / '.awb' / 'resource-policy.json'
    if not path.exists():
        save_project_policy(root, load_project_policy(root))


def install_runtime_compat(runtime_module, unified_module) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _unwrap_run(runtime_module)

    original_start = runtime_module._start_process
    if not getattr(original_start, '_awb_project_resource_spawn', False):
        def resource_aware_start(root: Path, job_id: str) -> None:
            # fork() snapshots os.environ. Serialize the tiny bind+fork window so two
            # simultaneous launch requests cannot inherit each other's project file.
            with _LOCK:
                _ensure_resource_file(root)
                old = os.environ.get('AWB_PROJECT_RESOURCE_FILE')
                bind_project_resource_env(root)
                try:
                    original_start(root, job_id)
                finally:
                    if old is None:
                        os.environ.pop('AWB_PROJECT_RESOURCE_FILE', None)
                    else:
                        os.environ['AWB_PROJECT_RESOURCE_FILE'] = old

        resource_aware_start._awb_project_resource_spawn = True  # type: ignore[attr-defined]
        runtime_module._start_process = resource_aware_start

    original_state = unified_module._project_state
    if not getattr(original_state, '_awb_strict_current_task', False):
        def strict_current_state(project: str):
            state = original_state(project)
            shown = state.get('current_task')
            actual = str(state.get('actual_current_task_id') or '')
            state['focused_task'] = shown if shown and shown.get('focus') and shown.get('id') != actual else None
            if not actual:
                # Focus/rework is useful context but is not IN PROGRESS until the
                # engine has actually entered that task.
                state['current_task'] = None
            return state

        strict_current_state._awb_strict_current_task = True  # type: ignore[attr-defined]
        unified_module._project_state = strict_current_state
