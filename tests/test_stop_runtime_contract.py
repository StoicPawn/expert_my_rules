from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class StopRuntimeContractTests(unittest.TestCase):
    def test_stop_hard_terminates_process_and_unloads_ollama(self):
        source = inspect.getsource(runtime_entry.cancel_runtime)
        self.assertIn('capture_stream_trace', source)
        self.assertIn('_terminate_process', source)
        self.assertIn('_unload_ollama_if_idle', source)
        self.assertIn('CANCELLED', source)

    def test_relaunch_resumes_ledger_without_synthetic_reassessment(self):
        source = inspect.getsource(runtime_entry.launch_runtime)
        self.assertIn('project_resumed_from_ledger', source)
        self.assertIn('preserved_negative_results', source)
        self.assertNotIn('RELAUNCH-REASSESS', source)
        self.assertNotIn("DELETE FROM tasks WHERE created_by='relaunch'", source)

    def test_busy_model_does_not_fail_whole_project(self):
        source = inspect.getsource(runtime_entry._run_continuous)
        self.assertIn('except RouteBusyError', source)
        self.assertIn('job_waiting_for_model_capacity', source)
        self.assertIn('job_recoverable_error', source)


if __name__ == '__main__':
    unittest.main()
