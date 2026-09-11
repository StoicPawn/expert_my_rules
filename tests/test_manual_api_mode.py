from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from awb.core.cloud_budget import (
    CloudBurstControl,
    GlobalCloudControl,
    budget_snapshot,
    load_control,
    save_control,
    save_global_control,
)
from awb.core.storage import Ledger


class ManualApiModeTests(unittest.TestCase):
    def _workspace(self, tmp: str) -> Path:
        root = Path(tmp) / 'workspaces' / 'project'
        root.mkdir(parents=True)
        Ledger(root / 'ledger.sqlite3')
        return root

    def test_mode_round_trips_and_invalid_mode_falls_back_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            save_control(root, CloudBurstControl(enabled=True, mode='force', budget_eur=5.0))
            self.assertEqual(load_control(root).mode, 'force')
            save_control(root, CloudBurstControl(enabled=True, mode='nonsense', budget_eur=5.0))
            self.assertEqual(load_control(root).mode, 'auto')

    def test_outstanding_reservation_reduces_project_and_monthly_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            save_control(root, CloudBurstControl(enabled=True, mode='force', budget_eur=5.0))
            save_global_control(root, GlobalCloudControl(enabled=True, monthly_budget_eur=5.0, hard_stop=True))
            ledger = Ledger(root / 'ledger.sqlite3')
            ledger.event('cloud_budget_reserved', {
                'reservation_id': 'r1',
                'amount_eur': 4.75,
                'role': 'worker',
                'model': 'gpt-5.6-sol',
            })
            snap = budget_snapshot(root)
            self.assertAlmostEqual(snap['reserved_eur'], 4.75)
            self.assertAlmostEqual(snap['project_remaining_eur'], 0.25)
            self.assertAlmostEqual(snap['monthly_reserved_eur'], 4.75)
            self.assertAlmostEqual(snap['monthly_remaining_eur'], 0.25)

            ledger.event('cloud_budget_released', {'reservation_id': 'r1', 'reason': 'usage_recorded'})
            snap = budget_snapshot(root)
            self.assertEqual(snap['reserved_eur'], 0.0)
            self.assertEqual(snap['monthly_reserved_eur'], 0.0)
            self.assertAlmostEqual(snap['project_remaining_eur'], 5.0)

    def test_paused_mode_disables_new_cloud_calls_without_disabling_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._workspace(tmp)
            save_control(root, CloudBurstControl(enabled=True, mode='paused', budget_eur=5.0))
            snap = budget_snapshot(root)
            self.assertTrue(snap['requested_enabled'])
            self.assertEqual(snap['mode'], 'paused')
            self.assertFalse(snap['enabled'])


if __name__ == '__main__':
    unittest.main()
