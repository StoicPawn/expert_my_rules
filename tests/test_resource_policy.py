from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from awb.core.models import JobStatus
from awb.core.resource_policy import (
    ProjectResourcePolicy,
    SystemResourcePolicy,
    admission_check,
    model_options_from_resource_file,
    save_project_policy,
    save_system_policy,
)
from awb.core.storage import Ledger
from awb.providers.runtime_progress import clear_progress_for_job, get_progress, set_progress


class ResourcePolicyTests(unittest.TestCase):
    def _project(self, base: Path, name: str) -> Path:
        root = base / name
        root.mkdir(parents=True)
        (root / 'project.yaml').write_text('name: placeholder\n', encoding='utf-8')
        Ledger(root / 'ledger.sqlite3')
        return root

    def test_running_projects_must_fit_global_resource_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            save_system_policy(base, SystemResourcePolicy(
                cpu_cores=3.0, ram_gb=6.0, max_running_projects=2,
                default_context_tokens=8192, dashboard_refresh_seconds=60,
            ))
            p1 = self._project(base, 'p1')
            p2 = self._project(base, 'p2')
            save_project_policy(p1, ProjectResourcePolicy(cpu_cores=1.5, ram_gb=3.0, context_tokens=8192, max_tool_calls_per_task=50))
            save_project_policy(p2, ProjectResourcePolicy(cpu_cores=1.6, ram_gb=3.0, context_tokens=8192, max_tool_calls_per_task=50))
            ledger = Ledger(p1 / 'ledger.sqlite3')
            jid = ledger.create_job(0, 0, continuous=True)
            ledger.update_job(jid, status=JobStatus.RUNNING)
            ok, detail = admission_check(p2)
            self.assertFalse(ok)
            self.assertIn('CPU', detail)
            save_project_policy(p2, ProjectResourcePolicy(cpu_cores=1.5, ram_gb=3.0, context_tokens=8192, max_tool_calls_per_task=50))
            ok, detail = admission_check(p2)
            self.assertTrue(ok, detail)

    def test_live_resource_file_drives_ollama_thread_and_context_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            save_system_policy(base, SystemResourcePolicy(cpu_cores=3.4, ram_gb=6.0, max_running_projects=2))
            project = self._project(base, 'p')
            save_project_policy(project, ProjectResourcePolicy(cpu_cores=2.7, ram_gb=3.0, context_tokens=12288, max_tool_calls_per_task=50))
            old = os.environ.get('AWB_PROJECT_RESOURCE_FILE')
            os.environ['AWB_PROJECT_RESOURCE_FILE'] = str(project / '.awb' / 'resource-policy.json')
            try:
                options = model_options_from_resource_file()
            finally:
                if old is None:
                    os.environ.pop('AWB_PROJECT_RESOURCE_FILE', None)
                else:
                    os.environ['AWB_PROJECT_RESOURCE_FILE'] = old
            self.assertEqual(options['num_thread'], 2)
            self.assertEqual(options['num_ctx'], 12288)

    def test_progress_is_isolated_per_job_even_on_same_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('AWB_RUNTIME_PROGRESS_FILE')
            os.environ['AWB_RUNTIME_PROGRESS_FILE'] = str(Path(tmp) / 'progress.json')
            try:
                set_progress('qwen3:4b', {'job_id': 'JOB-A', 'model': 'qwen3:4b', 'state': 'generating', 'visible_tail': 'alpha'})
                set_progress('qwen3:4b', {'job_id': 'JOB-B', 'model': 'qwen3:4b', 'state': 'generating', 'visible_tail': 'beta'})
                self.assertEqual(get_progress(job_id='JOB-A')['visible_tail'], 'alpha')
                self.assertEqual(get_progress(job_id='JOB-B')['visible_tail'], 'beta')
                clear_progress_for_job('JOB-A')
                self.assertIsNone(get_progress(job_id='JOB-A'))
                self.assertEqual(get_progress(job_id='JOB-B')['visible_tail'], 'beta')
            finally:
                clear_progress_for_job('JOB-A')
                clear_progress_for_job('JOB-B')
                if old is None:
                    os.environ.pop('AWB_RUNTIME_PROGRESS_FILE', None)
                else:
                    os.environ['AWB_RUNTIME_PROGRESS_FILE'] = old


if __name__ == '__main__':
    unittest.main()
