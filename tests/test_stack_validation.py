import json
import tempfile
import unittest
from pathlib import Path

from awb.core.validation import detect_stacks, run_validation
from awb.templates.templates import software_manifest


class StackAwareValidationTests(unittest.TestCase):
    def test_python_unittest_project_is_detected_and_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'calc.py').write_text('def add(a, b):\n    return a + b\n', encoding='utf-8')
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_calc.py').write_text(
                'import unittest\n\nfrom calc import add\n\n\n'
                'class CalcTests(unittest.TestCase):\n'
                '    def test_add(self):\n'
                '        self.assertEqual(add(2, 3), 5)\n',
                encoding='utf-8',
            )
            self.assertEqual(detect_stacks(root), ['python'])
            result = run_validation(root, 'tests')
            self.assertTrue(result['ok'])
            self.assertEqual(result['checks'][0]['stack'], 'python')
            self.assertIn('unittest', ' '.join(result['checks'][0]['command']))

    def test_python_project_with_no_tests_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'app.py').write_text('VALUE = 1\n', encoding='utf-8')
            result = run_validation(root, 'tests')
            self.assertFalse(result['ok'])
            self.assertIn('no test_', result['checks'][0]['stderr'].lower())

    def test_failing_python_test_is_not_misreported_as_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'app.py').write_text('VALUE = 1\n', encoding='utf-8')
            (root / 'test_app.py').write_text(
                'import unittest\n\nimport app\n\n\n'
                'class T(unittest.TestCase):\n'
                '    def test_value(self):\n'
                '        self.assertEqual(app.VALUE, 2)\n',
                encoding='utf-8',
            )
            result = run_validation(root, 'tests')
            self.assertFalse(result['ok'])
            self.assertNotEqual(result['checks'][0]['returncode'], 0)

    def test_node_default_placeholder_test_script_fails_without_executing_npm(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'package.json').write_text(json.dumps({
                'name': 'demo',
                'scripts': {'test': 'echo "Error: no test specified" && exit 1'},
            }), encoding='utf-8')
            self.assertEqual(detect_stacks(root), ['node'])
            result = run_validation(root, 'tests')
            self.assertFalse(result['ok'])
            self.assertIn('no real test script', result['checks'][0]['stderr'])

    def test_mixed_project_requires_every_detected_stack_to_validate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'app.py').write_text('VALUE = 1\n', encoding='utf-8')
            (root / 'test_app.py').write_text(
                'import unittest\n\nimport app\n\n\n'
                'class T(unittest.TestCase):\n'
                '    def test_value(self):\n'
                '        self.assertEqual(app.VALUE, 1)\n',
                encoding='utf-8',
            )
            (root / 'package.json').write_text(json.dumps({'name': 'frontend'}), encoding='utf-8')
            self.assertEqual(detect_stacks(root), ['node', 'python'])
            result = run_validation(root, 'tests')
            self.assertFalse(result['ok'])
            self.assertTrue(next(c for c in result['checks'] if c['stack'] == 'python')['ok'])
            self.assertFalse(next(c for c in result['checks'] if c['stack'] == 'node')['ok'])

    def test_new_software_template_uses_validation_dispatcher(self):
        manifest = software_manifest('demo', 'ship it')
        self.assertEqual(manifest['validators']['tests'], 'python -m awb.core.validation tests')
        self.assertEqual(manifest['validators']['lint'], 'python -m awb.core.validation lint')
        tests_tool = next(t for t in manifest['tools'] if t['id'] == 'tests')
        self.assertEqual(tests_tool['command'], manifest['validators']['tests'])
        tests_gate = next(g for g in manifest['gates'] if g['id'] == 'tests_pass')
        self.assertIn('zero-test', tests_gate['description'])


if __name__ == '__main__':
    unittest.main()
