from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from awb.providers.base import ModelProvider
from awb.templates.templates import software_manifest
from .models import Task, TaskStatus
from .orchestrator import Orchestrator
from .storage import Ledger
from .workspace import load_workspace, write_workspace


@dataclass(frozen=True)
class CodingBenchmarkCase:
    id: str
    title: str
    task: str
    files: dict[str, str]


BUILTIN_CODING_CASES: tuple[CodingBenchmarkCase, ...] = (
    CodingBenchmarkCase(
        id='repair_addition',
        title='Repair a small arithmetic regression',
        task='Fix calc.add so it returns the mathematical sum for integers and floats. Do not weaken or delete tests.',
        files={
            'calc.py': 'def add(a, b):\n    return a - b\n',
            'tests/test_calc.py': (
                'import unittest\n\n'
                'from calc import add\n\n\n'
                'class CalcTests(unittest.TestCase):\n'
                '    def test_positive(self):\n'
                '        self.assertEqual(add(2, 3), 5)\n\n'
                '    def test_negative(self):\n'
                '        self.assertEqual(add(-2, 1), -1)\n\n'
                '    def test_float(self):\n'
                '        self.assertAlmostEqual(add(0.25, 0.5), 0.75)\n\n\n'
                "if __name__ == '__main__':\n"
                '    unittest.main()\n'
            ),
        },
    ),
    CodingBenchmarkCase(
        id='implement_pairs_parser',
        title='Implement a specified text parser',
        task=(
            'Implement parser.parse_pairs(text). It must parse non-empty key=value lines into a dict, '
            'strip whitespace around keys and values, split only on the first equals sign, ignore blank lines, '
            'and raise ValueError for a non-blank line without equals. Do not modify the tests.'
        ),
        files={
            'parser.py': 'def parse_pairs(text):\n    raise NotImplementedError\n',
            'tests/test_parser.py': (
                'import unittest\n\n'
                'from parser import parse_pairs\n\n\n'
                'class ParserTests(unittest.TestCase):\n'
                '    def test_basic_and_whitespace(self):\n'
                "        self.assertEqual(parse_pairs(' a = 1\\n\\nb= two '), {'a': '1', 'b': 'two'})\n\n"
                '    def test_split_first_equals(self):\n'
                "        self.assertEqual(parse_pairs('url=a=b=c'), {'url': 'a=b=c'})\n\n"
                '    def test_invalid_line(self):\n'
                '        with self.assertRaises(ValueError):\n'
                "            parse_pairs('good=1\\nbad-line')\n\n\n"
                "if __name__ == '__main__':\n"
                '    unittest.main()\n'
            ),
        },
    ),
    CodingBenchmarkCase(
        id='multi_file_order_total',
        title='Repair behavior across multiple modules',
        task=(
            'Make order.total_order(lines, tax_rate) correct. Each line is (unit_price, quantity). '
            'Reject negative quantities with ValueError, sum price*quantity for all lines, then apply tax once '
            'to the subtotal. Keep money.round_money as the single rounding helper and do not modify tests.'
        ),
        files={
            'money.py': (
                'from decimal import Decimal, ROUND_HALF_UP\n\n\n'
                'def round_money(value):\n'
                "    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n"
            ),
            'order.py': (
                'from money import round_money\n\n\n'
                'def total_order(lines, tax_rate):\n'
                '    subtotal = sum(price for price, quantity in lines)\n'
                '    return round_money(subtotal + tax_rate)\n'
            ),
            'tests/test_order.py': (
                'import unittest\n'
                'from decimal import Decimal\n\n'
                'from order import total_order\n\n\n'
                'class OrderTests(unittest.TestCase):\n'
                '    def test_quantities_and_tax(self):\n'
                "        self.assertEqual(total_order([(10, 2), (2.5, 3)], 0.10), Decimal('30.25'))\n\n"
                '    def test_rounding(self):\n'
                "        self.assertEqual(total_order([(0.05, 1)], 0.10), Decimal('0.06'))\n\n"
                '    def test_negative_quantity_rejected(self):\n'
                '        with self.assertRaises(ValueError):\n'
                '            total_order([(10, -1)], 0.20)\n\n\n'
                "if __name__ == '__main__':\n"
                '    unittest.main()\n'
            ),
        },
    ),
)


def _benchmark_manifest(case: CodingBenchmarkCase, max_attempts: int) -> dict:
    manifest = software_manifest(f'benchmark_{case.id}', case.task)
    # Benchmarks need an objective, stable stop condition. Semantic release gates
    # would measure verifier optimism instead of coding ability.
    manifest['gates'] = [
        {
            'id': 'tests_pass',
            'description': 'The fixed benchmark acceptance tests pass unchanged.',
            'required': True,
            'validator': 'tests',
        }
    ]
    manifest['workflow']['stages'][-1]['validators'] = ['tests']
    manifest['runtime']['max_task_attempts'] = max(2, int(max_attempts) + 1)
    manifest['runtime']['max_steps_per_run'] = max(1, int(max_attempts))
    return manifest


def materialize_case(root: Path, case: CodingBenchmarkCase, *, max_attempts: int = 3) -> Path:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f'Benchmark workspace is not empty: {root}')
    write_workspace(root, _benchmark_manifest(case, max_attempts))
    for rel, content in case.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    ledger = Ledger(root / 'ledger.sqlite3')
    task = Task(
        id=f'BENCH-{case.id.upper()}',
        title=case.title,
        description=case.task,
        priority=100.0,
        created_by='benchmark',
    )
    ledger.upsert_task(task)
    ledger.event('benchmark_case_created', {'case': case.id, 'title': case.title}, task.id)
    return root


def _environment_snapshot() -> dict:
    return {
        'platform': platform.platform(),
        'system': platform.system(),
        'machine': platform.machine(),
        'processor': platform.processor(),
        'python': platform.python_version(),
        'cpu_count': os.cpu_count(),
    }


def _acceptance(root: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=120,
    )
    detail = (proc.stdout + '\n' + proc.stderr).strip()
    return proc.returncode == 0, detail[-6000:]


def run_case(
    root: Path,
    case: CodingBenchmarkCase,
    *,
    max_attempts: int = 3,
    provider: ModelProvider | None = None,
) -> dict:
    materialize_case(root, case, max_attempts=max_attempts)
    workspace = load_workspace(root)
    orchestrator = Orchestrator(workspace, provider)
    started = time.monotonic()
    results = []
    error = ''
    for _ in range(max(1, int(max_attempts))):
        try:
            result = orchestrator.step()
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            break
        results.append(result)
        if result.task.status in {TaskStatus.DONE, TaskStatus.REJECTED}:
            break
    elapsed = time.monotonic() - started
    passed, acceptance_detail = _acceptance(root)
    ledger = Ledger(root / 'ledger.sqlite3')
    task = ledger.get_task(f'BENCH-{case.id.upper()}')
    return {
        'case': case.id,
        'title': case.title,
        'passed': bool(passed),
        'status': task.status.value if task else 'UNKNOWN',
        'attempts': int(task.metadata.get('attempts', 0)) if task else len(results),
        'elapsed_seconds': round(elapsed, 3),
        'error': error,
        'acceptance_detail': acceptance_detail,
        'model_stats': ledger.model_stats(),
        'model_calls': len(ledger.recent_model_calls(10_000)),
        'workspace': str(root),
    }


def _aggregate(results: Iterable[dict]) -> dict:
    items = list(results)
    passed = sum(1 for item in items if item.get('passed'))
    total_seconds = sum(float(item.get('elapsed_seconds') or 0.0) for item in items)
    model_calls = sum(int(item.get('model_calls') or 0) for item in items)
    return {
        'passed': passed,
        'total': len(items),
        'score': round(passed / len(items), 4) if items else 0.0,
        'total_seconds': round(total_seconds, 3),
        'model_calls': model_calls,
    }


def run_builtin_suite(
    output_root: Path,
    *,
    max_attempts: int = 3,
    provider: ModelProvider | None = None,
    cases: Iterable[CodingBenchmarkCase] | None = None,
) -> dict:
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    selected = tuple(cases or BUILTIN_CODING_CASES)
    results = []
    for case in selected:
        case_root = output_root / case.id
        if case_root.exists():
            raise FileExistsError(f'Benchmark case output already exists: {case_root}')
        results.append(run_case(case_root, case, max_attempts=max_attempts, provider=provider))
    report = {
        'schema': 1,
        'suite': 'awb_builtin_coding_v1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'environment': _environment_snapshot(),
        'max_attempts': int(max_attempts),
        'summary': _aggregate(results),
        'cases': results,
    }
    (output_root / 'benchmark.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def load_report(path: Path) -> dict:
    path = path.resolve()
    if path.is_dir():
        path = path / 'benchmark.json'
    return json.loads(path.read_text(encoding='utf-8'))


def compare_reports(paths: Iterable[Path]) -> list[dict]:
    rows = []
    for path in paths:
        report = load_report(path)
        summary = report.get('summary') or {}
        env = report.get('environment') or {}
        rows.append({
            'path': str(Path(path)),
            'suite': report.get('suite'),
            'created_at': report.get('created_at'),
            'score': summary.get('score'),
            'passed': summary.get('passed'),
            'total': summary.get('total'),
            'total_seconds': summary.get('total_seconds'),
            'model_calls': summary.get('model_calls'),
            'machine': env.get('machine'),
            'processor': env.get('processor'),
            'cpu_count': env.get('cpu_count'),
        })
    return rows
