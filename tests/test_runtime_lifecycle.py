from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.models import JobStatus, Task, TaskStatus
from awb.core.storage import Ledger
from awb.core.workspace import write_workspace
from awb.templates.templates import get_template
from awb.web import dashboard_runtime, runtime_entry


class _FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = False
        self.killed = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout=None):
        return None

    def kill(self):
        self.killed = True
        self.alive = False


class RuntimeLifecycleTests(unittest.TestCase):
    def _workspace(self, tmp: str, name: str = 'demo') -> Path:
        root = Path(tmp) / name
        write_workspace(root, get_template('research', name, 'Prove a theorem rigorously.'))
        return root

    def test_cancel_hard_stops_process_and_recovers_task(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            root = self._workspace(tmp)
            ledger = Ledger(root / 'ledger.sqlite3')
            task = Task(id='T1', title='work', description='work', status=TaskStatus.IN_PROGRESS)
            ledger.upsert_task(task)
            jid = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(jid, status=JobStatus.RUNNING, detail='running')
            fake = _FakeProcess()
            runtime_entry._ACTIVE[jid] = fake
            with patch.object(runtime_entry, '_unload_ollama_if_idle', return_value=None):
                runtime_entry.cancel_runtime('demo', jid)
            after = Ledger(root / 'ledger.sqlite3')
            self.assertEqual(after.get_job(jid)['status'], JobStatus.CANCELLED.value)
            self.assertEqual(after.get_task('T1').status, TaskStatus.OPEN)
            self.assertTrue(fake.terminated)

    def test_relaunch_keeps_only_one_reassessment_task(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            root = self._workspace(tmp)
            ledger = Ledger(root / 'ledger.sqlite3')
            old = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(old, status=JobStatus.FAILED, detail='old failure')
            for idx in range(3):
                ledger.upsert_task(Task(
                    id=f'OLD-{idx}',
                    title='Reassess project under the current setup',
                    description='duplicate',
                    status=TaskStatus.OPEN,
                    priority=100,
                    created_by='relaunch',
                ))
            with patch.object(runtime_entry, '_start_process', return_value=None):
                runtime_entry.launch_runtime('demo')
            tasks = [t for t in Ledger(root / 'ledger.sqlite3').list_tasks() if t.created_by == 'relaunch']
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].id, 'RELAUNCH-REASSESS')

    def test_terminal_job_never_displays_stale_generating_progress(self):
        fake = {
            'job': {'status': JobStatus.FAILED.value, 'detail': 'boom'},
            'setup': {'status': 'READY'},
            'runtime_progress': {'state': 'generating', 'role': 'worker'},
            'current_task': {'id': 'T1'},
            'tasks': [
                {'id': 'R1', 'created_by': 'relaunch', 'status': 'ERROR'},
                {'id': 'R2', 'created_by': 'relaunch', 'status': 'OPEN'},
                {'id': 'A', 'created_by': 'system-planner', 'status': 'OPEN'},
            ],
            'done_tasks': 0,
            'total_tasks': 3,
        }
        with patch.object(dashboard_runtime, '_base_state', return_value=fake):
            response = dashboard_runtime.coherent_state('demo')
        payload = json.loads(response.body)
        self.assertEqual(payload['runtime_progress'], {})
        self.assertIsNone(payload['current_task'])
        self.assertEqual(payload['configuration_status'], 'READY')
        self.assertEqual(payload['run_status'], JobStatus.FAILED.value)
        self.assertEqual(payload['overall_status'], 'RUN_FAILED')
        self.assertEqual(sum(1 for t in payload['tasks'] if t['created_by'] == 'relaunch'), 1)


if __name__ == '__main__':
    unittest.main()
