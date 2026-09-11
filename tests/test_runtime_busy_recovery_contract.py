from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class BusyRecoveryContractTests(unittest.TestCase):
    def test_route_busy_is_backpressure_not_terminal_failure(self):
        source = inspect.getsource(runtime_entry._run_continuous)
        self.assertIn('except RouteBusyError', source)
        self.assertIn('attesa risorsa modello', source)
        self.assertIn('continue', source)


if __name__ == '__main__':
    unittest.main()
