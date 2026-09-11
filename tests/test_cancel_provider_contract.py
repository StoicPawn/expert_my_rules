from __future__ import annotations

import inspect
import unittest

from awb.providers.ollama_stream import OllamaProvider


class CancelProviderContractTests(unittest.TestCase):
    def test_ollama_checks_stop_at_subsecond_cadence(self):
        source = inspect.getsource(OllamaProvider.generate)
        self.assertIn("q.get(timeout=min(0.5", source)
        self.assertIn("ModelGenerationCancelled", source)
        self.assertIn("response.close()", source)
        self.assertIn("generation_finished()", source)


if __name__ == '__main__':
    unittest.main()
