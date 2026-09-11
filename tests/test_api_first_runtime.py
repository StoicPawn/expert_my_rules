from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

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
        self.last_response_id = 'resp_api_first'

    def generate(self, system, user):
        self.last_usage = {'input_tokens': 50, 'output_tokens': 25}
        return 'cloud candidate'


class _RejectedOpenAI(_FakeOpenAI):
    def generate(self, system, user):
        request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
        response = httpx.Response(
            401,
            request=request,
            json={
                'error': {
                    'message': 'Incorrect API key provided',
                    'type': 'invalid_request_error',
                    'code': 'invalid_api_key',
                }
            },
        )
        raise httpx.HTTPStatusError(
            '401 Unauthorized',
            request=request,
            response=response,
        )


class ApiFirstRuntimeTests(unittest.TestCase):
    def _workspace(self, root: Path):
        manifest = custom_manifest('demo', 'advance the result')
        # Deterministic local fallback: no external model service is needed by
        # these routing tests.
        manifest['runtime']['compute_nodes'] = []
        manifest['runtime']['role_routes'] = {}
        manifest['runtime']['default_provider'] = {'kind': 'mock', 'model': None}
        for agent in manifest['agents']:
            agent['provider'] = {'kind': 'mock', 'model': None}
        write_workspace(root, manifest)
        return load_workspace(root)

    def test_force_mode_is_api_first_even_before_a_task_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            save_control(root, CloudBurstControl(enabled=True, mode='force', budget_eur=5.0), reset_meter=True)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key', 'AWB_USD_PER_EUR': '1.0'}), \
                 patch('awb.core.cloud_orchestrator.OpenAIProvider', _FakeOpenAI):
                result = CloudAwareOrchestrator(ws)._call_model('director', 'sys', 'user', None)
            self.assertEqual(result, 'cloud candidate')
            self.assertEqual(budget_snapshot(root)['calls'], 1)

    def test_paused_mode_uses_local_without_attempting_openai(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            task = Task(id='T1', title='Local', description='work', priority=10)
            Ledger(root / 'ledger.sqlite3').upsert_task(task)
            save_control(root, CloudBurstControl(enabled=True, mode='paused', budget_eur=5.0), reset_meter=True)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), \
                 patch('awb.core.cloud_orchestrator.OpenAIProvider', side_effect=AssertionError('OpenAI must stay paused')):
                result = CloudAwareOrchestrator(ws)._call_model('worker', 'sys', 'user', task)
            self.assertIn('Mock work result', result)
            self.assertEqual(budget_snapshot(root)['calls'], 0)

    def test_explicit_api_rejection_releases_reservation_and_falls_back_local(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            ws = self._workspace(root)
            task = Task(id='T2', title='Cloud then local', description='work', priority=10)
            Ledger(root / 'ledger.sqlite3').upsert_task(task)
            save_control(root, CloudBurstControl(enabled=True, mode='force', budget_eur=5.0), reset_meter=True)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}), \
                 patch('awb.core.cloud_orchestrator.OpenAIProvider', _RejectedOpenAI):
                result = CloudAwareOrchestrator(ws)._call_model('worker', 'sys', 'user', task)
                snap = budget_snapshot(root)
            self.assertIn('Mock work result', result)
            self.assertEqual(snap['reserved_eur'], 0.0)
            self.assertEqual(snap['calls'], 0)
            events = Ledger(root / 'ledger.sqlite3').recent_events(30)
            failed = next(e for e in events if e['kind'] == 'model_call_failed')
            fallback = next(e for e in events if e['kind'] == 'cloud_call_fallback_local')
            self.assertIn('OpenAI HTTP 401 invalid_api_key', failed['payload']['error'])
            self.assertFalse(fallback['payload']['reservation_held_fail_closed'])
            self.assertTrue(any(
                e['kind'] == 'cloud_budget_released'
                and e['payload'].get('reason') == 'clear_request_failure'
                for e in events
            ))

    def test_web_autonomous_runtime_uses_cloud_aware_engine(self):
        source = (
            Path(__file__).resolve().parents[1]
            / 'src' / 'awb' / 'web' / 'runtime_entry.py'
        ).read_text(encoding='utf-8')
        self.assertIn('from awb.core.cloud_orchestrator import CloudAwareOrchestrator', source)
        self.assertIn('orch = CloudAwareOrchestrator(ws)', source)
        self.assertNotIn('orch = Orchestrator(ws)', source)


if __name__ == '__main__':
    unittest.main()
