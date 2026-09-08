import os
import unittest
from unittest.mock import patch

from awb.providers.providers import OllamaProvider


class OllamaTimeoutTests(unittest.TestCase):
    def test_default_read_timeout_is_one_hour(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AWB_OLLAMA_READ_TIMEOUT_SECONDS', None)
            provider = OllamaProvider('qwen3:4b')
        self.assertEqual(provider.read_timeout_seconds, 3600.0)
        self.assertEqual(provider.timeout.read, 3600.0)
        self.assertEqual(provider.timeout.connect, 30.0)

    def test_read_timeout_is_configurable(self):
        with patch.dict(os.environ, {'AWB_OLLAMA_READ_TIMEOUT_SECONDS': '7200'}):
            provider = OllamaProvider('qwen3:4b')
        self.assertEqual(provider.read_timeout_seconds, 7200.0)
        self.assertEqual(provider.timeout.read, 7200.0)

    def test_invalid_timeout_falls_back_to_one_hour(self):
        with patch.dict(os.environ, {'AWB_OLLAMA_READ_TIMEOUT_SECONDS': 'not-a-number'}):
            provider = OllamaProvider('qwen3:4b')
        self.assertEqual(provider.read_timeout_seconds, 3600.0)


if __name__ == '__main__':
    unittest.main()
