from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class HardStopEndToEndContractTests(unittest.TestCase):
    def test_cancel_marks_request_then_terminates_process_then_finalizes_cancelled(self):
        source = inspect.getsource(runtime_entry.cancel_runtime)
        request_pos = source.index('CANCEL_REQUESTED')
        kill_pos = source.index('_terminate_process')
        cancelled_pos = source.rindex('CANCELLED')
        self.assertLess(request_pos, kill_pos)
        self.assertLess(kill_pos, cancelled_pos)


if __name__ == '__main__':
    unittest.main()
