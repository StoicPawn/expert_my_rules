from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


IGNORED_DIRS = {
    '.git', '.awb', 'artifacts', 'logs', '__pycache__', '.venv', 'venv',
    'node_modules', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'dist', 'build',
}
MAX_DISCOVERY_FILES = 1500
MAX_OUTPUT_CHARS = 12_000


def _iter_files(root: Path):
    emitted = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for name in sorted(files):
            yield Path(current) / name
            emitted += 1
            if emitted >= MAX_DISCOVERY_FILES:
                return


def detect_stacks(root: Path) -> list[str]:
    """Detect executable validation ecosystems without asking an LLM to guess."""
    root = root.resolve()
    stacks: list[str] = []
    if (root / 'package.json').is_file():
        stacks.append('node')
    if (root / 'go.mod').is_file():
        stacks.append('go')
    if (root / 'Cargo.toml').is_file():
        stacks.append('rust')

    python_markers = {
        'pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt', 'Pipfile',
    }
    has_python = any((root / marker).is_file() for marker in python_markers)
    if not has_python:
        has_python = any(path.suffix.lower() == '.py' for path in _iter_files(root))
    if has_python:
        stacks.append('python')
    return stacks


def _run(argv: list[str], cwd: Path, *, timeout: int = 300) -> dict[str, Any]:
    started_argv = [str(part) for part in argv]
    try:
        proc = subprocess.run(
            started_argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except FileNotFoundError as exc:
        return {
            'ok': False,
            'command': started_argv,
            'returncode': 127,
            'stdout': '',
            'stderr': str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors='replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        return {
            'ok': False,
            'command': started_argv,
            'returncode': 124,
            'stdout': str(stdout)[-MAX_OUTPUT_CHARS:],
            'stderr': ('Timed out.\n' + str(stderr))[-MAX_OUTPUT_CHARS:],
        }
    return {
        'ok': proc.returncode == 0,
        'command': started_argv,
        'returncode': proc.returncode,
        'stdout': proc.stdout[-MAX_OUTPUT_CHARS:],
        'stderr': proc.stderr[-MAX_OUTPUT_CHARS:],
    }


def _load_package_json(root: Path) -> dict[str, Any]:
    path = root / 'package.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        return {'_error': f'Invalid package.json: {exc}'}
    return data if isinstance(data, dict) else {'_error': 'package.json root must be an object'}


def _pytest_requested(root: Path) -> bool:
    if any((root / name).is_file() for name in ('pytest.ini', 'conftest.py')):
        return True
    for name in ('pyproject.toml', 'setup.cfg', 'tox.ini', 'requirements.txt'):
        path = root / name
        if not path.is_file() or path.stat().st_size > 500_000:
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='replace').lower()
        except OSError:
            continue
        if 'pytest' in text:
            return True
    return False


def _has_python_tests(root: Path) -> bool:
    for path in _iter_files(root):
        name = path.name.lower()
        if path.suffix.lower() == '.py' and (name.startswith('test_') or name.endswith('_test.py')):
            return True
    return False


def _python_tests(root: Path) -> dict[str, Any]:
    if not _has_python_tests(root):
        return {
            'ok': False,
            'stack': 'python',
            'kind': 'tests',
            'returncode': 2,
            'command': [],
            'stdout': '',
            'stderr': 'Python source detected but no test_*.py or *_test.py files were found.',
        }
    if _pytest_requested(root):
        if importlib.util.find_spec('pytest') is None:
            return {
                'ok': False,
                'stack': 'python',
                'kind': 'tests',
                'returncode': 127,
                'command': [sys.executable, '-m', 'pytest', '-q'],
                'stdout': '',
                'stderr': 'Repository appears to require pytest, but pytest is not installed in the runtime.',
            }
        result = _run([sys.executable, '-m', 'pytest', '-q'], root)
    else:
        tests_dir = root / 'tests'
        start = 'tests' if tests_dir.is_dir() else '.'
        result = _run([sys.executable, '-m', 'unittest', 'discover', '-s', start, '-v'], root)
    return {'stack': 'python', 'kind': 'tests', **result}


def _python_lint(root: Path) -> dict[str, Any]:
    if importlib.util.find_spec('ruff') is None:
        return {
            'ok': True,
            'stack': 'python',
            'kind': 'lint',
            'skipped': True,
            'returncode': 0,
            'command': [],
            'stdout': 'Ruff is not installed; Python static lint was skipped.',
            'stderr': '',
        }
    result = _run([sys.executable, '-m', 'ruff', 'check', '.'], root)
    return {'stack': 'python', 'kind': 'lint', **result}


def _node_tests(root: Path) -> dict[str, Any]:
    package = _load_package_json(root)
    if package.get('_error'):
        return {'ok': False, 'stack': 'node', 'kind': 'tests', 'returncode': 2, 'command': [], 'stdout': '', 'stderr': package['_error']}
    script = str((package.get('scripts') or {}).get('test') or '').strip()
    if not script or 'no test specified' in script.lower():
        return {
            'ok': False,
            'stack': 'node',
            'kind': 'tests',
            'returncode': 2,
            'command': [],
            'stdout': '',
            'stderr': 'Node project detected but package.json has no real test script.',
        }
    if shutil.which('npm') is None:
        return {'ok': False, 'stack': 'node', 'kind': 'tests', 'returncode': 127, 'command': ['npm', 'test', '--silent'], 'stdout': '', 'stderr': 'npm is not installed.'}
    return {'stack': 'node', 'kind': 'tests', **_run(['npm', 'test', '--silent'], root)}


def _node_lint(root: Path) -> dict[str, Any]:
    package = _load_package_json(root)
    if package.get('_error'):
        return {'ok': False, 'stack': 'node', 'kind': 'lint', 'returncode': 2, 'command': [], 'stdout': '', 'stderr': package['_error']}
    script = str((package.get('scripts') or {}).get('lint') or '').strip()
    if not script:
        return {
            'ok': True,
            'stack': 'node',
            'kind': 'lint',
            'skipped': True,
            'returncode': 0,
            'command': [],
            'stdout': 'Node project has no lint script; static lint was skipped.',
            'stderr': '',
        }
    if shutil.which('npm') is None:
        return {'ok': False, 'stack': 'node', 'kind': 'lint', 'returncode': 127, 'command': ['npm', 'run', 'lint', '--silent'], 'stdout': '', 'stderr': 'npm is not installed.'}
    return {'stack': 'node', 'kind': 'lint', **_run(['npm', 'run', 'lint', '--silent'], root)}


def _go_check(root: Path, kind: str) -> dict[str, Any]:
    if shutil.which('go') is None:
        return {'ok': False, 'stack': 'go', 'kind': kind, 'returncode': 127, 'command': ['go'], 'stdout': '', 'stderr': 'go is not installed.'}
    argv = ['go', 'test', './...'] if kind == 'tests' else ['go', 'vet', './...']
    return {'stack': 'go', 'kind': kind, **_run(argv, root)}


def _rust_check(root: Path, kind: str) -> dict[str, Any]:
    if shutil.which('cargo') is None:
        return {'ok': False, 'stack': 'rust', 'kind': kind, 'returncode': 127, 'command': ['cargo'], 'stdout': '', 'stderr': 'cargo is not installed.'}
    argv = ['cargo', 'test', '--quiet'] if kind == 'tests' else ['cargo', 'check', '--quiet']
    return {'stack': 'rust', 'kind': kind, **_run(argv, root, timeout=600)}


def run_validation(root: Path, kind: str) -> dict[str, Any]:
    """Run every detected stack's validation, failing closed for missing tests."""
    root = root.resolve()
    if kind not in {'tests', 'lint'}:
        raise ValueError(f'Unsupported validation kind: {kind}')
    stacks = detect_stacks(root)
    if not stacks:
        return {
            'ok': False if kind == 'tests' else True,
            'kind': kind,
            'stacks': [],
            'checks': [],
            'detail': 'No supported software stack was detected.' if kind == 'tests' else 'No supported stack detected; static lint skipped.',
        }

    checks = []
    for stack in stacks:
        if stack == 'python':
            checks.append(_python_tests(root) if kind == 'tests' else _python_lint(root))
        elif stack == 'node':
            checks.append(_node_tests(root) if kind == 'tests' else _node_lint(root))
        elif stack == 'go':
            checks.append(_go_check(root, kind))
        elif stack == 'rust':
            checks.append(_rust_check(root, kind))
    ok = bool(checks) and all(bool(check.get('ok')) for check in checks)
    return {
        'ok': ok,
        'kind': kind,
        'stacks': stacks,
        'checks': checks,
        'detail': 'All detected stack checks passed.' if ok else 'One or more detected stack checks failed.',
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Expert My Rules stack-aware validator')
    parser.add_argument('kind', choices=['tests', 'lint', 'detect'])
    parser.add_argument('--root', default='.')
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.kind == 'detect':
        result = {'ok': True, 'stacks': detect_stacks(root)}
        code = 0
    else:
        result = run_validation(root, args.kind)
        code = 0 if result['ok'] else 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
