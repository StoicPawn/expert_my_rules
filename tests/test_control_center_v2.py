from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from awb.core.cloud_budget import (
    CloudBurstControl,
    GlobalCloudControl,
    budget_snapshot,
    save_control,
    save_global_control,
)
from awb.core.storage import Ledger
from awb.providers.providers import LMStudioProvider, make_provider


class ControlCenterV2Tests(unittest.TestCase):
    def test_monthly_budget_hard_stop_blocks_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'workspaces' / 'project-a'
            root.mkdir(parents=True)
            ledger = Ledger(root / 'ledger.sqlite3')
            save_control(root, CloudBurstControl(enabled=True, budget_eur=10.0), reset_meter=True)
            save_global_control(root, GlobalCloudControl(enabled=True, monthly_budget_eur=0.01, hard_stop=True))
            ledger.event('cloud_usage_metered', {
                'model': 'gpt-5.6-sol',
                'input_tokens': 1,
                'output_tokens': 1,
                'cost_usd': 0.02,
                'cost_eur': 0.02,
            })
            snap = budget_snapshot(root)
            self.assertTrue(snap['hard_blocked'])
            self.assertFalse(snap['enabled'])
            self.assertEqual(snap['remaining_eur'], 0.0)
            self.assertAlmostEqual(snap['monthly_spent_eur'], 0.02)

    def test_lm_studio_provider_is_first_class_route(self):
        provider = make_provider('lmstudio', 'test-model', base_url='http://127.0.0.1:1234/v1')
        self.assertIsInstance(provider, LMStudioProvider)
        self.assertEqual(provider.model, 'test-model')
        self.assertEqual(provider.base_url, 'http://127.0.0.1:1234/v1')

    def test_new_web_apps_import(self):
        from awb.web.control_app import control_app
        from awb.web.dashboard_app import dashboard_app
        from awb.web.lab_app import lab_app

        self.assertEqual(control_app.title, 'Expert My Rules Control Center')
        self.assertEqual(dashboard_app.title, 'Expert My Rules Live Dashboard')
        self.assertEqual(lab_app.title, 'Expert My Rules Research Lab')


if __name__ == '__main__':
    unittest.main()
