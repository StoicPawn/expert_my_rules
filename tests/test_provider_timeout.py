import json
import os
import unittest
from unittest.mock import patch

from awb.providers.providers import OllamaProvider


class _FakeStreamResponse:
    def __init__(self, items):
        self.items = items
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def raise_for_status(self):
        return None

    def iter_lines(self):
        for item in self.items:
            yield json.dumps(item)

    def close(self):
        self.closed = True


class OllamaLivenessTests(unittest.TestCase):
    def test_generation_read_timeout_is_unbounded(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in (
                'AWB_OLLAMA_STALL_CHECK_SECONDS', 'AWB_OLLAMA_HEALTH_TIMEOUT_SECONDS',
                'AWB_OLLAMA_HEALTH_FAILURES', 'AWB_OLLAMA_PROGRESS_EVENT_SECONDS',
                'AWB_OLLAMA_MAX_OUTPUT_TOKENS',
            ):
                os.environ.pop(key, None)
            provider = OllamaProvider('qwen3:4b')
        self.assertIsNone(provider.timeout.read)
        self.assertEqual(provider.timeout.connect, 30.0)
        self.assertEqual(provider.max_output_tokens, 8192)
        self.assertEqual(provider.health_failure_limit, 15)

    def test_legacy_read_timeout_no_longer_controls_generation(self):
        with patch.dict(os.environ, {'AWB_OLLAMA_READ_TIMEOUT_SECONDS': '1'}):
            provider = OllamaProvider('qwen3:4b')
        self.assertEqual(provider.legacy_read_timeout_seconds, '1')
        self.assertIsNone(provider.timeout.read)

    def test_streaming_accumulates_visible_content_and_reports_progress(self):
        response = _FakeStreamResponse([
            {'message': {'thinking': 'internal reasoning'}, 'done': False},
            {'message': {'content': 'hello '}, 'done': False},
            {'message': {'content': 'world'}, 'done': True},
        ])
        events = []
        with patch.dict(os.environ, {'AWB_OLLAMA_PROGRESS_EVENT_SECONDS': '0.05'}), \
             patch('awb.providers.ollama_stream.httpx.stream', return_value=response):
            provider = OllamaProvider('qwen3:4b')
            provider.set_progress_callback(events.append)
            result = provider.generate('system', 'user')
        self.assertEqual(result, 'hello world')
        self.assertTrue(events)
        self.assertNotIn('internal reasoning', json.dumps(events))
        self.assertEqual(events[-1]['state'], 'completed_stream')
        self.assertEqual(events[-1]['output_chars'], 11)
        self.assertGreater(events[-1]['thinking_chars'], 0)

    def test_num_predict_is_sent_as_logical_output_budget(self):
        response = _FakeStreamResponse([
            {'message': {'content': 'ok'}, 'done': True},
        ])
        captured = {}

        def fake_stream(method, url, **kwargs):
            captured.update(kwargs['json'])
            return response

        with patch.dict(os.environ, {'AWB_OLLAMA_MAX_OUTPUT_TOKENS': '4096'}), \
             patch('awb.providers.ollama_stream.httpx.stream', side_effect=fake_stream):
            result = OllamaProvider('qwen3:4b').generate('s', 'u')
        self.assertEqual(result, 'ok')
        self.assertTrue(captured['stream'])
        self.assertEqual(captured['options']['num_predict'], 4096)


if __name__ == '__main__':
    unittest.main()
