from __future__ import annotations

import unittest

from awb.core.models import JobStatus


class RuntimeStopSemanticsTests(unittest.TestCase):
    def test_cancel_requested_and_cancelled_states_exist(self):
        self.assertEqual(JobStatus.CANCEL_REQUESTED.value, 'CANCEL_REQUESTED')
        self.assertEqual(JobStatus.CANCELLED.value, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
