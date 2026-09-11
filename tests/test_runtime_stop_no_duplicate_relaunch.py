from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class RelaunchDedupeContractTests(unittest.TestCase):
    def test_relaunch_uses_one_stable_assessment_task(self):
        source = inspect.getsource(runtime_entry.launch_runtime)
        self.assertIn("DELETE FROM tasks WHERE created_by='relaunch'", source)
        self.assertIn("RELAUNCH-REASSESS", source)


if __name__ == '__main__':
    unittest.main()
