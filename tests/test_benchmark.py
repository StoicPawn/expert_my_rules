import json
import shutil
import tempfile
import unittest
from pathlib import Path

from awb.core.benchmark import (
    BUILTIN_CODING_CASES,
    compare_reports,
    materialize_case,
    run_case,
)
from awb.core.workspace import load_workspace
from awb.providers.base import ModelProvider


class AddFixProvider(ModelProvider):
    def __init__(self):
        self.wrote = False

    def generate(self, system: str, user: str) -> str:
        if 'REVIEW_JSON' in system:
            return json.dumps({'approved': True, 'critical_objections': [], 'recommendations': []})
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'unused', 'description': 'unused', 'priority': 1})
        if not self.wrote:
            self.wrote = True
            return json.dumps({
                'tool': 'write',
                'arguments': {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'},
            })
        return 'Fixed calc.add and preserved the acceptance tests.'


class BenchmarkTests(unittest.TestCase):
    def test_case_materialization_has_objective_gate_and_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'case'
            materialize_case(root, BUILTIN_CODING_CASES[0], max_attempts=2)
            ws = load_workspace(root)
            self.assertEqual([g.id for g in ws.manifest.gates], ['tests_pass'])
            self.assertTrue(ws.manifest.runtime.git.enabled)
            self.assertTrue(ws.manifest.runtime.scheduler.enabled)
            validate = next(s for s in ws.manifest.workflow.stages if s.kind == 'validate')
            self.assertEqual(validate.validators, ['tests'])

    @unittest.skipUnless(shutil.which('git'), 'git is required')
    def test_repair_case_runs_end_to_end_and_scores_pass(self):
        with tempfile.TemporaryDirectory() as td:
            report = run_case(
                Path(td) / 'repair',
                BUILTIN_CODING_CASES[0],
                max_attempts=2,
                provider=AddFixProvider(),
            )
            self.assertTrue(report['passed'])
            self.assertEqual(report['status'], 'DONE')
            self.assertGreaterEqual(report['model_calls'], 2)
            self.assertTrue(report['model_stats'])
            self.assertIn('return a + b', (Path(td) / 'repair' / 'calc.py').read_text())

    def test_compare_reports_preserves_machine_and_score(self):
        with tempfile.TemporaryDirectory() as td:
            first = Path(td) / 'a.json'
            second = Path(td) / 'b.json'
            first.write_text(json.dumps({
                'suite': 'awb_builtin_coding_v1',
                'created_at': '2026-01-01T00:00:00Z',
                'environment': {'machine': 'x86_64', 'processor': 'cpu-a', 'cpu_count': 4},
                'summary': {'score': 0.5, 'passed': 1, 'total': 2, 'total_seconds': 10, 'model_calls': 5},
            }))
            second.write_text(json.dumps({
                'suite': 'awb_builtin_coding_v1',
                'created_at': '2026-01-02T00:00:00Z',
                'environment': {'machine': 'x86_64', 'processor': 'cpu-b', 'cpu_count': 16},
                'summary': {'score': 1.0, 'passed': 2, 'total': 2, 'total_seconds': 4, 'model_calls': 4},
            }))
            rows = compare_reports([first, second])
            self.assertEqual(rows[0]['score'], 0.5)
            self.assertEqual(rows[1]['score'], 1.0)
            self.assertEqual(rows[1]['processor'], 'cpu-b')


if __name__ == '__main__':
    unittest.main()
