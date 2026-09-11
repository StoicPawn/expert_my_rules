from __future__ import annotations

import unittest

from awb.providers import runtime_cancel


class StopBarrierImportTests(unittest.TestCase):
    def test_runtime_cancel_api_is_available(self):
        self.assertTrue(callable(runtime_cancel.request_cancel))
        self.assertTrue(callable(runtime_cancel.active_generations))
        self.assertTrue(callable(runtime_cancel.clear_cancel_barrier))


if __name__ == '__main__':
    unittest.main()
