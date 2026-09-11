from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awb.web import dashboard_app as dashboard_module


class DashboardLiveTests(unittest.TestCase):
    def test_rendered_dashboard_does_not_embed_raw_newline_in_js_string(self):
        request = SimpleNamespace(url=SimpleNamespace(hostname='testserver'))
        workspace = SimpleNamespace(manifest=SimpleNamespace(name='Demo', goal='Goal'))

        with patch.object(dashboard_module, '_root', return_value=Path('/tmp/demo')), patch.object(
            dashboard_module, 'load_workspace', return_value=workspace
        ):
            response = dashboard_module.project(request, 'demo')

        body = response.body.decode('utf-8')
        self.assertIn('String.fromCharCode(10)', body)
        self.assertNotIn("out='REVIEW\n'", body)
        self.assertIn("const endpoint='/project/demo/state'", body)
        self.assertIn('showLoadError', body)


if __name__ == '__main__':
    unittest.main()
