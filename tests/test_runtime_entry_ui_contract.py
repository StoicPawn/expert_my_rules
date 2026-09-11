from __future__ import annotations

import inspect
import unittest

from awb.web import runtime_entry


class RuntimeEntryUiContractTests(unittest.TestCase):
    def test_failed_run_does_not_masquerade_as_healthy_setup(self):
        source = inspect.getsource(runtime_entry.project_page_runtime)
        self.assertIn('CONFIGURAZIONE PRONTA', source)
        self.assertIn('Ultimo run FERMO per errore tecnico', source)


if __name__ == '__main__':
    unittest.main()
