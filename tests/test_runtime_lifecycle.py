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
            self.assertTrue((root / 'project_state.json').exists())

    def test_relaunch_preserves_existing_scientific_state_and_reopens_only_errors(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            root = self._workspace(tmp)
            ledger = Ledger(root / 'ledger.sqlite3')
            old = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(old, status=JobStatus.FAILED, detail='old failure')
            ledger.upsert_task(Task(id='DONE', title='proved lemma', description='done', status=TaskStatus.DONE))
            ledger.upsert_task(Task(id='NEG', title='false route', description='negative', status=TaskStatus.REJECTED, metadata={'rejection_reason':'counterexample'}))
            ledger.upsert_task(Task(id='BLOCK', title='needs rework', description='blocked', status=TaskStatus.BLOCKED, metadata={'next_strategy':'repair proof'}))
            ledger.upsert_task(Task(id='ERR', title='technical retry', description='retry', status=TaskStatus.ERROR))
            with patch.object(runtime_entry, '_start_process', return_value=None):
                runtime_entry.launch_runtime('demo')
            after = Ledger(root / 'ledger.sqlite3')
            self.assertEqual(after.get_task('DONE').status, TaskStatus.DONE)
            self.assertEqual(after.get_task('NEG').status, TaskStatus.REJECTED)
            self.assertEqual(after.get_task('BLOCK').status, TaskStatus.BLOCKED)
            self.assertEqual(after.get_task('ERR').status, TaskStatus.OPEN)
            self.assertFalse(any(t.id == 'RELAUNCH-REASSESS' for t in after.list_tasks()))
            self.assertEqual(after.latest_job()['status'], JobStatus.RUNNING.value)

    def test_terminal_job_never_displays_stale_generating_progress(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            root = self._workspace(tmp)
            ledger = Ledger(root / 'ledger.sqlite3')
            jid = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(jid, status=JobStatus.FAILED, detail='boom')
            fake = {
                'job': {'id': jid, 'status': JobStatus.FAILED.value, 'detail': 'boom'},
                'setup': {'status': 'READY'},
                'runtime_progress': {'state': 'generating', 'role': 'worker'},
                'current_task': {'id': 'T1'},
                'tasks': [
                    {'id': 'A', 'created_by': 'system-planner', 'status': 'OPEN'},
                ],
                'done_tasks': 0,
                'total_tasks': 1,
            }
            with patch.object(dashboard_runtime, '_base_state', return_value=fake):
                response = dashboard_runtime.coherent_state('demo')
            payload = json.loads(response.body)
            self.assertEqual(payload['runtime_progress'], {})
            self.assertIsNone(payload['current_task'])
            self.assertEqual(payload['configuration_status'], 'READY')
            self.assertEqual(payload['run_status'], JobStatus.FAILED.value)
            self.assertEqual(payload['overall_status'], 'RUN_FAILED')


if __name__ == '__main__':
    unittest.main()
