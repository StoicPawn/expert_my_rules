from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awb.core.acepc_policy import PRIMARY_MODEL, apply_acepc_policy
from awb.core.checkpoints import build_project_state
from awb.core.models import JobStatus, Task, TaskStatus
from awb.core.routing import ModelRouter
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.templates.templates import get_template
from awb.web.checkpoint_runtime import CheckpointedFocusedOrchestrator


class LocalFirstEnduranceTests(unittest.TestCase):
    def _workspace(self, tmp: str) -> Path:
        root = Path(tmp) / 'demo'
        write_workspace(root, get_template('research', 'demo', 'Prove the target theorem rigorously.'))
        return root

    def test_acepc_policy_is_single_model_unbounded_queue_and_cloud_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            result = apply_acepc_policy(root, lock_cloud=True)
            ws = load_workspace(root)
            self.assertEqual(result['model'], PRIMARY_MODEL)
            self.assertEqual(ws.manifest.runtime.scheduler.queue_timeout_seconds, 0.0)
            self.assertEqual(ws.manifest.runtime.continuous_session_minutes, 0)
            self.assertEqual(ws.manifest.runtime.max_minutes_per_run, 0)
            self.assertEqual(ws.manifest.runtime.technical_retry_limit, 0)
            self.assertGreaterEqual(ws.manifest.runtime.max_tool_calls_per_task, 50)
            for role in ('director', 'worker', 'reviewer', 'verifier'):
                self.assertEqual(ws.manifest.runtime.role_routes[role][0].model, PRIMARY_MODEL)
            control = json.loads((root / 'cloud_burst.json').read_text())
            self.assertFalse(control['enabled'])
            self.assertEqual(control['mode'], 'paused')

    def test_zero_queue_timeout_waits_instead_of_route_busy_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            ws = load_workspace(root)
            ws.manifest.runtime.scheduler.queue_timeout_seconds = 0.0
            router = ModelRouter(ws.manifest)
            route = router.candidates('worker')[0]
            entered = threading.Event()
            released = threading.Event()
            errors = []

            def waiter():
                try:
                    with router.slot(route):
                        entered.set()
                except Exception as exc:  # pragma: no cover - assertion below
                    errors.append(exc)
                finally:
                    released.set()

            with router.slot(route):
                thread = threading.Thread(target=waiter, daemon=True)
                thread.start()
                time.sleep(0.1)
                self.assertFalse(entered.is_set())
            self.assertTrue(released.wait(2.0))
            self.assertTrue(entered.is_set())
            self.assertEqual(errors, [])

    def test_paid_cloud_is_manual_force_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            ws = load_workspace(root)
            orch = CheckpointedFocusedOrchestrator(ws)
            task = Task(id='T1', title='hard', description='hard', priority=100)
            base = SimpleNamespace(enabled=True, mode='auto')
            with patch('awb.web.checkpoint_runtime.load_control', return_value=base):
                self.assertFalse(orch._cloud_important('worker', task))

    def test_checkpoint_preserves_negative_and_interrupted_resume_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            ledger = Ledger(root / 'ledger.sqlite3')
            blocked = Task(
                id='B1', title='Blocked theorem route', description='try route', status=TaskStatus.BLOCKED,
                metadata={
                    'critical_objections': ['counterexample at boundary'],
                    'next_strategy': 'prove the missing boundary lemma',
                    'interrupted_resume': {'artifact': 'artifacts/checkpoints/stream.json', 'visible_output_tail': 'partial lemma'},
                    'focus_chain_active': True,
                    'lifecycle_phase': 'REWORK',
                },
            )
            ledger.upsert_task(blocked)
            state = build_project_state(root)
            row = next(t for t in state['tasks'] if t['id'] == 'B1')
            self.assertEqual(row['critical_objections'], ['counterexample at boundary'])
            self.assertEqual(row['interrupted_resume']['visible_output_tail'], 'partial lemma')
            self.assertEqual(state['next_best_action']['task_id'], 'B1')


if __name__ == '__main__':
    unittest.main()
