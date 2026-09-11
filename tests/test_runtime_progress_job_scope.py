from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.providers.runtime_progress import clear_progress_for_job, get_progress, set_progress


class RuntimeProgressJobScopeTests(unittest.TestCase):
    def test_progress_can_be_filtered_and_cleared_by_job(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'AWB_RUNTIME_PROGRESS_FILE': str(Path(td) / 'progress.json')}, clear=False):
            set_progress('qwen3:4b', {'state': 'generating', 'job_id': 'JOB-1'})
            self.assertEqual(get_progress(job_id='JOB-1')['state'], 'generating')
            self.assertIsNone(get_progress(job_id='JOB-2'))
            clear_progress_for_job('JOB-1')
            self.assertIsNone(get_progress(job_id='JOB-1'))


if __name__ == '__main__':
    unittest.main()
