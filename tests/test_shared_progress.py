import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import awb.providers.runtime_progress as progress


class SharedProgressTests(unittest.TestCase):
    def test_progress_round_trips_through_shared_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'progress.json'
            with patch.dict(os.environ, {'AWB_RUNTIME_PROGRESS_FILE': str(path)}):
                progress.set_progress('qwen3:4b', {
                    'model': 'qwen3:4b', 'role': 'worker', 'output_chars': 42,
                    'visible_tail': 'public candidate fragment', 'thinking_chars': 999,
                })
                # Simulate a separate observer process by clearing only process RAM.
                with progress._LOCK:
                    progress._PROGRESS.clear()
                read = progress.get_progress()
                self.assertEqual(read['visible_tail'], 'public candidate fragment')
                self.assertEqual(read['thinking_chars'], 999)
                progress.clear_progress('qwen3:4b')
                self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
