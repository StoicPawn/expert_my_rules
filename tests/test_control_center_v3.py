from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from awb.core.models import JobStatus
from awb.core.storage import Ledger
from awb.core.workspace import write_workspace
from awb.templates.templates import get_template
from awb.web.control_v3 import control_app


class ControlCenterV3Tests(unittest.TestCase):
    def test_create_is_immediate_and_accepts_starting_material(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            with patch('awb.web.control_v3._schedule_auto_setup', return_value=True):
                client = TestClient(control_app)
                response = client.post(
                    '/create',
                    data={'goal': 'Prove a rigorous new theorem and produce a submission-ready research paper.', 'name': 'fast-create'},
                    files=[('files', ('notes.txt', b'Lemma 1. Starting evidence.', 'text/plain'))],
                    follow_redirects=False,
                )
            self.assertEqual(response.status_code, 303)
            self.assertIn('/project/fast-create', response.headers['location'])
            root = Path(tmp) / 'fast-create'
            self.assertTrue((root / 'project.yaml').exists())
            self.assertTrue((root / 'sources' / 'INDEX.md').exists())

    def test_get_create_never_strands_mobile_browser_on_405(self):
        client = TestClient(control_app)
        response = client.get('/create', follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/')

    def test_cancelled_project_can_be_relaunched_without_erasing_scientific_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp}, clear=False):
            root = Path(tmp) / 'rerunnable'
            manifest = get_template('research', 'rerunnable', 'Prove a theorem and produce a research paper.')
            write_workspace(root, manifest)
            ledger = Ledger(root / 'ledger.sqlite3')
            old_job = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(old_job, status=JobStatus.CANCELLED, detail='cancelled')
            for gate in manifest['gates']:
                ledger.set_gate(gate['id'], True, 'already established')
            with patch('awb.web.runtime_entry._start_process', return_value=None):
                client = TestClient(control_app)
                response = client.post('/project/rerunnable/launch', follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            after = Ledger(root / 'ledger.sqlite3')
            latest = after.latest_job()
            self.assertNotEqual(latest['id'], old_job)
            self.assertEqual(latest['status'], JobStatus.RUNNING.value)
            self.assertFalse(any(t.created_by == 'relaunch' for t in after.list_tasks()))
            self.assertTrue(all(v['passed'] for v in after.gate_state().values()))
            self.assertTrue(any(e['kind'] == 'project_resumed_from_ledger' for e in after.recent_events(20)))

    def test_v3_routes_replace_blocking_create_route(self):
        matches = [
            route for route in control_app.router.routes
            if getattr(route, 'path', None) == '/create'
        ]
        methods = {method for route in matches for method in (getattr(route, 'methods', set()) or set())}
        self.assertIn('GET', methods)
        self.assertIn('POST', methods)


if __name__ == '__main__':
    unittest.main()
