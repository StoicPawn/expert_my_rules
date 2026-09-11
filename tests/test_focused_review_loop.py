import json
import tempfile
import unittest
from pathlib import Path

from awb.core.focused_cloud_orchestrator import FocusedCloudAwareOrchestrator
from awb.core.models import Task, TaskStatus
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.base import ModelProvider
from awb.templates.templates import custom_manifest


class FocusProvider(ModelProvider):
    def __init__(self, approvals, recovery_actions=None):
        self.approvals = list(approvals)
        self.recovery_actions = list(recovery_actions or ['retry'])
        self.review_calls = 0
        self.recovery_calls = 0
        self.worker_prompts = []

    def generate(self, system: str, user: str) -> str:
        if 'GATE_JSON' in system:
            return json.dumps({'passed': False, 'detail': 'gate open in test'})
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'Director task', 'description': 'continue', 'priority': 1})
        if 'RECOVERY_JSON' in system:
            action = self.recovery_actions[min(self.recovery_calls, len(self.recovery_actions) - 1)]
            self.recovery_calls += 1
            if action == 'decompose':
                return json.dumps({
                    'action': 'decompose',
                    'strategy': 'Prove the missing prerequisite before revisiting the parent.',
                    'rationale': 'The reviewer objection is a real prerequisite.',
                    'resolution_type': '',
                    'replacement_title': '',
                    'replacement_description': '',
                    'subtasks': [
                        {
                            'title': 'Resolve prerequisite lemma',
                            'description': 'Establish the missing prerequisite with evidence.',
                            'priority': 1,
                        }
                    ],
                })
            return json.dumps({
                'action': 'retry',
                'strategy': 'Repair the exact reviewer objection before doing anything unrelated.',
                'rationale': 'The candidate is repairable.',
                'resolution_type': '',
                'replacement_title': '',
                'replacement_description': '',
                'subtasks': [],
            })
        if 'REVIEW_JSON' in system:
            approved = self.approvals[min(self.review_calls, len(self.approvals) - 1)]
            self.review_calls += 1
            return json.dumps({
                'approved': approved,
                'critical_objections': [] if approved else ['missing edge-case proof'],
                'recommendations': [] if approved else ['repair the edge case and resubmit'],
            })
        self.worker_prompts.append(user)
        return 'candidate with inspectable evidence'


class FocusedReviewLoopTests(unittest.TestCase):
    def _workspace(self, root: Path):
        manifest = custom_manifest('focus-demo', 'Finish one rigorous result')
        write_workspace(root, manifest)
        return load_workspace(root)

    def test_review_rejection_requeues_same_task_before_unrelated_open_work(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='ROOT', title='Main theorem', description='prove', priority=100))
            ledger.upsert_task(Task(id='OTHER', title='Unrelated', description='later', priority=1))
            provider = FocusProvider([False, True], ['retry'])
            orch = FocusedCloudAwareOrchestrator(ws, provider)

            first = orch.step()
            self.assertEqual(first.task.id, 'ROOT')
            # The completed attempt remains BLOCKED in the attempt ledger, while
            # next_task is already the same task reopened as focused REWORK.
            self.assertEqual(first.task.status, TaskStatus.BLOCKED)
            self.assertEqual(first.task.metadata['lifecycle_phase'], 'REWORK')
            self.assertEqual(first.next_task.id, 'ROOT')
            self.assertEqual(first.next_task.status, TaskStatus.OPEN)

            second = orch.step()
            self.assertEqual(second.task.id, 'ROOT')
            self.assertEqual(second.task.status, TaskStatus.DONE)
            self.assertIn('missing edge-case proof', provider.worker_prompts[1])
            self.assertIn('Repair the exact reviewer objection', provider.worker_prompts[1])

    def test_decomposition_runs_prerequisite_before_unrelated_work_then_returns_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='ROOT', title='Main theorem', description='prove', priority=100))
            ledger.upsert_task(Task(id='OTHER', title='Unrelated high priority', description='later', priority=99))
            provider = FocusProvider([False, True, True], ['decompose'])
            orch = FocusedCloudAwareOrchestrator(ws, provider)

            first = orch.step()
            self.assertEqual(first.task.status, TaskStatus.BLOCKED)
            self.assertEqual(first.task.metadata['lifecycle_phase'], 'WAITING_ON_DEPENDENCY')
            child_id = first.task.metadata['waiting_on_recovery_tasks'][0]
            self.assertEqual(first.next_task.id, child_id)
            child = ledger.get_task(child_id)
            self.assertEqual(child.metadata['lifecycle_phase'], 'PREREQUISITE')

            second = orch.step()
            self.assertEqual(second.task.id, child_id)
            self.assertEqual(second.task.status, TaskStatus.DONE)
            self.assertEqual(second.next_task.id, 'ROOT')
            reopened = ledger.get_task('ROOT')
            self.assertEqual(reopened.status, TaskStatus.OPEN)
            self.assertEqual(reopened.metadata['lifecycle_phase'], 'REWORK')
            self.assertTrue(reopened.metadata['dependency_outcomes'])

    def test_legacy_blocked_retry_is_adopted_as_focused_rework(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(
                id='OLD',
                title='Old blocked theorem',
                description='resume',
                status=TaskStatus.BLOCKED,
                priority=5,
                metadata={
                    'last_recovery_action': 'retry',
                    'next_strategy': 'repair reviewer objection',
                    'critical_objections': ['gap'],
                },
            ))
            ledger.upsert_task(Task(id='OTHER', title='Unrelated', description='later', priority=100))
            orch = FocusedCloudAwareOrchestrator(ws, FocusProvider([True]))
            chosen = orch.choose_next_task()
            self.assertEqual(chosen.id, 'OLD')
            self.assertEqual(chosen.status, TaskStatus.OPEN)
            self.assertEqual(chosen.metadata['lifecycle_phase'], 'REWORK')


if __name__ == '__main__':
    unittest.main()
