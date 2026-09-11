from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awb.web.dashboard_runtime import dashboard_app
from awb.web import dashboard_story


class DashboardStoryTests(unittest.TestCase):
    def test_project_page_explains_agents_and_decisions(self):
        route = next(
            r for r in dashboard_app.router.routes
            if getattr(r, 'path', None) == '/project/{project}'
            and 'GET' in (getattr(r, 'methods', None) or set())
        )
        request = SimpleNamespace(url=SimpleNamespace(hostname='testserver'))
        workspace = SimpleNamespace(manifest=SimpleNamespace(name='Demo', goal='Prove the result'))
        with patch.object(dashboard_story, '_root', return_value=Path('/tmp/demo')), patch.object(
            dashboard_story, 'load_workspace', return_value=workspace
        ):
            response = route.endpoint(request, 'demo')
        body = response.body.decode('utf-8')
        self.assertIn('Agenti — chi fa cosa', body)
        self.assertIn('Director', body)
        self.assertIn('Worker', body)
        self.assertIn('Reviewer', body)
        self.assertIn('Verifier', body)
        self.assertIn('Task e prossima mossa', body)
        self.assertIn('Timeline comprensibile', body)
        self.assertIn("setInterval(tick,3000)", body)
        self.assertIn("task_recovery_planned", body)
        self.assertIn("work_output", body)
        self.assertIn("review", body)
        self.assertIn("verification", body)

    def test_only_one_project_get_route_is_installed(self):
        routes = [
            r for r in dashboard_app.router.routes
            if getattr(r, 'path', None) == '/project/{project}'
            and 'GET' in (getattr(r, 'methods', None) or set())
        ]
        self.assertEqual(len(routes), 1)


if __name__ == '__main__':
    unittest.main()
