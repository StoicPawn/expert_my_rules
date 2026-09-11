from __future__ import annotations

import inspect
import unittest

from awb.providers.ollama_stream import OllamaProvider


class ModelStopReleaseContractTests(unittest.TestCase):
    def test_cancel_finally_releases_stream_and_generation_slot(self):
        source = inspect.getsource(OllamaProvider.generate)
        self.assertIn('response.close()', source)
        self.assertIn('thread.join(timeout=2.0)', source)
        self.assertIn('generation_finished()', source)


if __name__ == '__main__':
    unittest.main()
