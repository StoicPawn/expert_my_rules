import json
import tempfile
import unittest
from pathlib import Path

from awb.core.checkpoints import build_project_state, latest_checkpoint, write_checkpoint
from awb.core.models import Task, TaskStatus
from awb.core.storage import Ledger


class ProjectCheckpointTests(unittest.TestCase):
    def test_checkpoint_preserves_positive_negative_and_resume_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            root.mkdir(parents=True)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(
                id='DONE', title='Established lemma', description='proved', status=TaskStatus.DONE,
                metadata={'artifact': 'artifacts/done.md', 'scientific_attempts': 1},
            ))
            ledger.upsert_task(Task(
                id='BLOCK', title='Failed route', description='repair', status=TaskStatus.BLOCKED, priority=9,
                metadata={
                    'critical_objections': ['counterexample at boundary'],
                    'last_review_recommendations': ['split the boundary case'],
                    'next_strategy': 'prove the boundary case separately',
                    'last_verification_detail': 'review failed on boundary case',
                    'artifact': 'artifacts/blocked.md',
                },
            ))
            state = write_checkpoint(root, reason='unit-test', manual=True)
            self.assertEqual(state['summary']['done'], 1)
            self.assertEqual(state['summary']['blocked'], 1)
            self.assertEqual(state['established_results'][0]['id'], 'DONE')
            negative = {x['id']: x for x in state['negative_or_blocked_findings']}
            self.assertIn('counterexample at boundary', negative['BLOCK']['critical_objections'])
            self.assertEqual(state['next_best_action']['task_id'], 'BLOCK')
            saved = latest_checkpoint(root)
            self.assertEqual(saved['checkpoint_id'], state['checkpoint_id'])
            self.assertTrue((root / 'project_state.json').exists())
            self.assertTrue((root / 'artifacts' / 'checkpoints' / f"{state['checkpoint_id']}.json").exists())

    def test_live_project_state_is_derived_from_ledger_not_stale_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            root.mkdir(parents=True)
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.upsert_task(Task(id='T1', title='First', description='x', status=TaskStatus.OPEN))
            first = build_project_state(root)
            self.assertEqual(first['summary']['open'], 1)
            task = ledger.get_task('T1')
            task.status = TaskStatus.DONE
            task.metadata['artifact'] = 'artifacts/t1.md'
            ledger.upsert_task(task)
            second = build_project_state(root)
            self.assertEqual(second['summary']['open'], 0)
            self.assertEqual(second['summary']['done'], 1)


if __name__ == '__main__':
    unittest.main()
