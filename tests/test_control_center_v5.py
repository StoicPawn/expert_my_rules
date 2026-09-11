from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from awb.core import planner
from awb.core.workspace import write_workspace
from awb.templates.templates import get_template
from awb.web.control_v3 import _project_editor_panels
from awb.web.dashboard_app import dashboard_app
from awb.web.lab_app import lab_app


class _PlannerProvider:
    def __init__(self):
        self.role = None

    def configure_role(self, role: str) -> None:
        self.role = role

    def generate(self, system: str, user: str) -> str:
        return json.dumps({
            'type': 'research',
            'description': 'Configured quickly',
            'agents': [
                {'id': 'director', 'role': 'director', 'instructions': 'plan'},
                {'id': 'worker', 'role': 'worker', 'instructions': 'work'},
                {'id': 'reviewer', 'role': 'reviewer', 'instructions': 'review'},
                {'id': 'verifier', 'role': 'verifier', 'instructions': 'verify'},
            ],
            'gates': [{'id': 'done', 'description': 'done', 'required': True, 'manual': False}],
        })


class ControlCenterV5Tests(unittest.TestCase):
    def test_setup_planner_uses_dedicated_bounded_role(self):
        provider = _PlannerProvider()
        with patch('awb.core.planner.make_provider', return_value=provider):
            result = planner.propose_manifest('Prove a new theorem.', 'test', use_local_ai=True)
        self.assertEqual(provider.role, 'planner')
        self.assertEqual(result['description'], 'Configured quickly')

    def test_agents_are_collapsed_inside_openable_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            write_workspace(root, get_template('research', 'project', 'Prove a theorem.'))
            _, agents = _project_editor_panels(root)
        self.assertIn('<details', agents)
        self.assertIn('Agenti del setup', agents)
        self.assertIn('max-height:65vh', agents)

    def test_dashboard_and_lab_have_projects_navigation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'AWB_WORKSPACES_DIR': tmp, 'AWB_LAB_DATA_DIR': str(Path(tmp) / 'lab')}, clear=False):
            dashboard = TestClient(dashboard_app).get('/')
            lab = TestClient(lab_app).get('/')
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('Progetti', dashboard.text)
        self.assertEqual(lab.status_code, 200)
        self.assertIn('Progetti', lab.text)
        self.assertIn('Dashboard live', lab.text)


if __name__ == '__main__':
    unittest.main()
