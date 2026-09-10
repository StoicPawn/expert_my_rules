import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.cloud_budget import CloudBurstControl, budget_snapshot, save_control
from awb.core.cloud_orchestrator import CloudAwareOrchestrator
from awb.core.models import Task
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.templates.templates import custom_manifest


class _FakeOpenAI:
    def __init__(self, model):
        self.model = model
        self.max_output_tokens = None
        self.reasoning_effort = None
        self.last_usage = {}
        self.last_response_id = 'resp_test'

    def generate(self, system, user):
        self.last_usage = {'input_tokens': 1000, 'output_tokens': 200}
        return 'cloud candidate'


class CloudOrchestratorTests(unittest.TestCase):
    def _workspace(self, root: Path):
        manifest = custom_manifest('demo', 'advance the result')
        # Make the local fallback deterministic/offline for this test.
        manifest['runtime']['compute_nodes'] = []
        manifest['runtime']['role_routes'] = {}
        manifest['runtime']['default_provider'] = {'kind': 'mock', 'model': None}
        for agent in manifest['agents']:
            agent['provider'] = {'kind': 'mock', 'model': None}
        write_workspace(root, manifest)
        return load_workspace(root)

    def test_important_task_uses_cloud_and_persists_exact_usage_cost(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            task = Task(id='T1', title='Important', description='work', priority=10)
            Ledger(root / 'ledger.sqlite3').upsert_task(task)
            save_control(root, CloudBurstControl(enabled=True, budget_eur=5.0, priority_threshold=1.0), reset_meter=True)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key', 'AWB_USD_PER_EUR': '1.0'}), \
                 patch('awb.core.cloud_orchestrator.OpenAIProvider', _FakeOpenAI):
                result = CloudAwareOrchestrator(ws)._call_model('worker', 'sys', 'user', task)
                snap = budget_snapshot(root)
            self.assertEqual(result, 'cloud candidate')
            self.assertEqual(snap['calls'], 1)
            self.assertEqual(snap['input_tokens'], 1000)
            self.assertEqual(snap['output_tokens'], 200)
            self.assertGreater(snap['spent_eur'], 0)
            events = Ledger(root / 'ledger.sqlite3').recent_events(20)
            self.assertTrue(any(e['kind'] == 'cloud_usage_metered' for e in events))

    def test_tiny_budget_refuses_paid_call_and_falls_back_local(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            task = Task(id='T2', title='Important', description='work', priority=10)
            Ledger(root / 'ledger.sqlite3').upsert_task(task)
            save_control(root, CloudBurstControl(enabled=True, budget_eur=0.000001, priority_threshold=1.0), reset_meter=True)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), \
                 patch('awb.core.cloud_orchestrator.OpenAIProvider', side_effect=AssertionError('paid call must not start')):
                result = CloudAwareOrchestrator(ws)._call_model('worker', 'sys', 'user', task)
            self.assertIn('Mock work result', result)
            self.assertEqual(budget_snapshot(root)['calls'], 0)


if __name__ == '__main__':
    unittest.main()
