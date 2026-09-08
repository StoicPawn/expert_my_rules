import json
import tempfile
import unittest
from pathlib import Path

from awb.core.models import Task, TaskStatus
from awb.core.orchestrator import Orchestrator
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.base import ModelProvider
from awb.templates.templates import custom_manifest


class AdaptiveProvider(ModelProvider):
    def __init__(self, *, fail_worker_once=False, approvals=None, recovery_action='retry'):
        self.fail_worker_once = fail_worker_once
        self.approvals = list(approvals or [True])
        self.recovery_action = recovery_action
        self.review_calls = 0
        self.worker_prompts = []

    def generate(self, system: str, user: str) -> str:
        if 'GATE_JSON' in system:
            return json.dumps({'passed': False, 'detail': 'gate remains open in test'})
        if 'RECOVERY_JSON' in system:
            if self.recovery_action == 'reframe':
                return json.dumps({
                    'action': 'reframe',
                    'strategy': 'Replace the over-strong formulation with the strongest claim surviving the counterexample.',
                    'rationale': 'The current formulation is too strong but the core mechanism remains useful.',
                    'resolution_type': '',
                    'replacement_title': 'Prove the strongest surviving claim',
                    'replacement_description': 'Formulate and prove the strongest version consistent with the review evidence.',
                    'subtasks': [],
                })
            return json.dumps({
                'action': 'retry',
                'strategy': 'Use a counterexample-first route, then rebuild the candidate only on the surviving cases.',
                'rationale': 'The reviewer identified an untested edge case.',
                'resolution_type': '',
                'replacement_title': '',
                'replacement_description': '',
                'subtasks': [],
            })
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'Next', 'description': 'Continue', 'priority': 1})
        if 'REVIEW_JSON' in system:
            approved = self.approvals[min(self.review_calls, len(self.approvals) - 1)]
            self.review_calls += 1
            return json.dumps({
                'approved': approved,
                'critical_objections': [] if approved else ['edge case not ruled out'],
                'recommendations': [] if approved else ['try a counterexample-first analysis'],
            })
        self.worker_prompts.append(user)
        if self.fail_worker_once:
            self.fail_worker_once = False
            raise TimeoutError('simulated local model timeout')
        return 'candidate result with inspectable evidence'


class AdaptiveTaskingTests(unittest.TestCase):
    def _workspace(self, root: Path):
        manifest = custom_manifest('demo', 'Finish a rigorous result')
        manifest['runtime']['adaptive_replan_after_scientific_attempts'] = 2
        write_workspace(root, manifest)
        return load_workspace(root)

    def test_technical_failure_is_error_and_does_not_consume_scientific_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T1', title='Test claim', description='test rigorously', priority=10))
            provider = AdaptiveProvider(fail_worker_once=True, approvals=[True])
            orch = Orchestrator(ws, provider)
            with self.assertRaises(TimeoutError):
                orch.step()
            failed = ledger.get_task('T1')
            self.assertEqual(failed.status, TaskStatus.ERROR)
            self.assertEqual(failed.metadata['scientific_attempts'], 0)
            self.assertEqual(failed.metadata['technical_failures'], 1)
            self.assertEqual(failed.metadata['attempts'], 0)
            result = orch.step()
            self.assertEqual(result.task.status, TaskStatus.DONE)
            self.assertEqual(result.task.metadata['scientific_attempts'], 1)
            self.assertEqual(result.task.metadata['technical_failures'], 1)
            self.assertEqual(result.task.metadata['execution_attempts'], 2)

    def test_blocked_task_can_exceed_old_attempt_cap_without_auto_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ws.manifest.runtime.max_task_attempts = 2
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T2', title='Hard theorem', description='prove or falsify', priority=10))
            provider = AdaptiveProvider(approvals=[False], recovery_action='retry')
            orch = Orchestrator(ws, provider)
            last = None
            for _ in range(4):
                last = orch.step()
            self.assertEqual(last.task.status, TaskStatus.BLOCKED)
            self.assertEqual(last.task.metadata['scientific_attempts'], 4)
            self.assertNotEqual(last.task.status, TaskStatus.REJECTED)
            self.assertIn('counterexample-first', last.task.metadata['next_strategy'])

    def test_blocked_retry_carries_forward_review_objections_and_new_strategy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T3', title='Empirical claim', description='establish robust value', priority=10))
            provider = AdaptiveProvider(approvals=[False, True], recovery_action='retry')
            orch = Orchestrator(ws, provider)
            first = orch.step()
            self.assertEqual(first.task.status, TaskStatus.BLOCKED)
            second = orch.step()
            self.assertEqual(second.task.status, TaskStatus.DONE)
            self.assertGreaterEqual(len(provider.worker_prompts), 2)
            self.assertIn('edge case not ruled out', provider.worker_prompts[1])
            self.assertIn('counterexample-first', provider.worker_prompts[1])

    def test_reframe_is_explicit_resolution_and_creates_replacement_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T4', title='Overstrong claim', description='prove it', priority=10))
            provider = AdaptiveProvider(approvals=[False], recovery_action='reframe')
            result = Orchestrator(ws, provider).step()
            self.assertEqual(result.task.status, TaskStatus.REJECTED)
            self.assertEqual(result.task.metadata['resolution'], 'reframed')
            replacement_id = result.task.metadata['replacement_task_id']
            replacement = ledger.get_task(replacement_id)
            self.assertIsNotNone(replacement)
            self.assertEqual(replacement.status, TaskStatus.OPEN)
            self.assertEqual(replacement.metadata['parent_task_id'], 'T4')

    def test_legacy_mixed_attempt_counter_is_reconciled_from_attempt_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            task = Task(id='T5', title='Legacy task', description='legacy', status=TaskStatus.BLOCKED, metadata={'attempts': 4, 'last_error': 'timeout'})
            ledger.upsert_task(task)
            a1 = ledger.start_attempt('T5', 1)
            ledger.finish_attempt(a1, status='BLOCKED', review={'approved': False})
            a2 = ledger.start_attempt('T5', 2)
            ledger.finish_attempt(a2, status='FAILED', error='timeout')
            a3 = ledger.start_attempt('T5', 3)
            ledger.finish_attempt(a3, status='TECHNICAL_ERROR', error='timeout')
            Orchestrator(ws, AdaptiveProvider())
            fixed = ledger.get_task('T5')
            self.assertEqual(fixed.metadata['scientific_attempts'], 1)
            self.assertEqual(fixed.metadata['technical_failures'], 2)
            self.assertEqual(fixed.metadata['attempts'], 1)
            self.assertEqual(fixed.status, TaskStatus.ERROR)

    def test_interrupted_attempt_is_recovery_not_scientific_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            task = Task(id='T6', title='Interrupted', description='resume', status=TaskStatus.IN_PROGRESS, metadata={'attempts': 9})
            ledger.upsert_task(task)
            ledger.start_attempt('T6', 1)
            Orchestrator(ws, AdaptiveProvider())
            recovered = ledger.get_task('T6')
            self.assertEqual(recovered.status, TaskStatus.OPEN)
            self.assertEqual(recovered.metadata['scientific_attempts'], 0)
            self.assertEqual(recovered.metadata['interrupt_recoveries'], 1)
            attempts = ledger.list_attempts('T6')
            self.assertEqual(attempts[0]['status'], 'INTERRUPTED')


if __name__ == '__main__':
    unittest.main()
