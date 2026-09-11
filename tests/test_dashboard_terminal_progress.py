from __future__ import annotations

import inspect
import unittest

from awb.web import dashboard_runtime


class DashboardTerminalProgressTests(unittest.TestCase):
    def test_dashboard_state_reads_setup_and_job_separately(self):
        source = inspect.getsource(dashboard_runtime._state)
        self.assertIn("'setup': setup", source)
        self.assertIn("'job': job", source)


if __name__ == '__main__':
    unittest.main()
