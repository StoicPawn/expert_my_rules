import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.cloud_budget import (
    CloudBurstControl,
    budget_snapshot,
    cost_from_usage,
    reserve_cost_eur,
    save_control,
)
from awb.core.storage import Ledger
from awb.providers.ollama_stream import OllamaProvider


class CloudBurstTests(unittest.TestCase):
    def test_cost_meter_matches_published_sol_rate(self):
        with patch.dict(os.environ, {'AWB_USD_PER_EUR': '1.0'}):
            usd, eur = cost_from_usage('gpt-5.6-sol', 100_000, 100_000)
        self.assertAlmostEqual(usd, 2.4)
        self.assertAlmostEqual(eur, 2.4)

    def test_long_context_sol_multiplier_is_counted(self):
        with patch.dict(os.environ, {'AWB_USD_PER_EUR': '1.0'}):
            usd, _ = cost_from_usage('gpt-5.6-sol', 300_000, 10_000)
        self.assertAlmostEqual(usd, 300_000 * 8 / 1_000_000 + 10_000 * 30 / 1_000_000)

    def test_unknown_model_reservation_fails_closed(self):
        self.assertEqual(reserve_cost_eur('unknown', 's', 'u', 100), float('inf'))

    def test_persisted_meter_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ledger = Ledger(root / 'ledger.sqlite3')
            control = save_control(root, CloudBurstControl(enabled=True, budget_eur=5.0), reset_meter=True)
            ledger.event('cloud_usage_metered', {
                'input_tokens': 1000, 'output_tokens': 200, 'cost_usd': 0.01, 'cost_eur': 0.01
            })
            with patch.dict(os.environ, {'AWB_OPENAI_KEY_PRESENT': '1'}):
                snap = budget_snapshot(root)
            self.assertEqual(snap['calls'], 1)
            self.assertAlmostEqual(snap['spent_eur'], 0.01)
            self.assertAlmostEqual(snap['remaining_eur'], 4.99)
            self.assertEqual(snap['started_at'], control.started_at)
            self.assertTrue(snap['api_key_configured'])

    def test_qwen_director_is_short_non_thinking_worker_stays_full(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ('AWB_OLLAMA_DIRECTOR_MAX_OUTPUT_TOKENS', 'AWB_OLLAMA_DIRECTOR_THINK',
                        'AWB_OLLAMA_WORKER_MAX_OUTPUT_TOKENS', 'AWB_OLLAMA_WORKER_THINK'):
                os.environ.pop(key, None)
            director = OllamaProvider('qwen3:4b')
            director.configure_role('director')
            worker = OllamaProvider('qwen3:4b')
            worker.configure_role('worker')
        self.assertEqual(director.max_output_tokens, 1200)
        self.assertFalse(director.think)
        self.assertEqual(worker.max_output_tokens, 8192)
        self.assertTrue(worker.think)


if __name__ == '__main__':
    unittest.main()
