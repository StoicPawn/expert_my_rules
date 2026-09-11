from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class RelaunchResumeContractTests(unittest.TestCase):
    def test_relaunch_resumes_persisted_ledger_without_generic_reassessment(self):
        source = inspect.getsource(runtime_entry.launch_runtime)
        self.assertIn('project_resumed_from_ledger', source)
        self.assertIn('preserved_negative_results', source)
        self.assertNotIn("RELAUNCH-REASSESS", source)
        self.assertNotIn("set_gate", source)


if __name__ == '__main__':
    unittest.main()
