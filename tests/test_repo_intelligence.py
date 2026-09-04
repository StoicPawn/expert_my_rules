import json
import shutil
import tempfile
import unittest
from pathlib import Path

from awb.core.orchestrator import Orchestrator
from awb.core.tools import ToolError, ToolRunner
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.base import ModelProvider
from awb.templates.templates import software_manifest


class SurgicalWorkflowProvider(ModelProvider):
    def __init__(self):
        self.worker_call = 0
        self.review_patch_seen = False

    def generate(self, system: str, user: str) -> str:
        if 'GATE_JSON' in system:
            return json.dumps({'passed': False, 'detail': 'benchmark does not certify release gates'})
        if 'REVIEW_JSON' in system:
            self.review_patch_seen = 'return "hello " + name' in user
            return json.dumps({
                'approved': self.review_patch_seen,
                'critical_objections': [] if self.review_patch_seen else ['expected patch not visible'],
                'recommendations': [],
            })
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'Next task', 'description': 'continue', 'priority': 1})
        self.worker_call += 1
        sequence = {
            1: {'tool': 'repo_map', 'arguments': {'max_files': 50}},
            2: {'tool': 'search', 'arguments': {'query': 'return "bye"'}},
            3: {'tool': 'read_range', 'arguments': {'path': 'app.py', 'start_line': 1, 'end_line': 5}},
            4: {
                'tool': 'replace',
                'arguments': {
                    'path': 'app.py',
                    'old': 'return "bye" + name',
                    'new': 'return "hello " + name',
                },
            },
            5: {'tool': 'git_diff', 'arguments': {}},
            6: {'tool': 'tests', 'arguments': {}},
        }
        if self.worker_call in sequence:
            return json.dumps(sequence[self.worker_call])
        return 'Mapped the repository, located the defect, applied one exact replacement, inspected the diff and passed tests.'


class RepositoryIntelligenceTests(unittest.TestCase):
    def _workspace(self, root: Path):
        write_workspace(root, software_manifest('software', 'change existing repository safely'))
        return load_workspace(root)

    def test_software_template_grants_compact_navigation_tools(self):
        manifest = software_manifest('software', 'ship feature')
        worker = next(a for a in manifest['agents'] if a['role'] == 'worker')
        for tool in ('repo_map', 'search', 'read_range', 'replace', 'git_diff', 'tests'):
            self.assertIn(tool, worker['tools'])
        specs = {tool['id']: tool for tool in manifest['tools']}
        self.assertEqual(specs['repo_map']['type'], 'repo_map')
        self.assertEqual(specs['search']['type'], 'search_text')
        self.assertTrue(specs['replace']['writable'])

    def test_repo_map_returns_python_symbols_and_ignores_runtime_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            (root / 'pkg').mkdir()
            (root / 'pkg' / 'service.py').write_text(
                'class Service:\n    pass\n\n\ndef build():\n    return Service()\n',
                encoding='utf-8',
            )
            (root / '.awb').mkdir(exist_ok=True)
            (root / '.awb' / 'secret.py').write_text('def hidden(): pass\n', encoding='utf-8')
            runner = ToolRunner(ws)
            result = runner.execute('repo_map', {'max_files': 50})
            paths = {item['path'] for item in result['files']}
            self.assertIn('pkg/service.py', paths)
            self.assertNotIn('.awb/secret.py', paths)
            service = next(item for item in result['files'] if item['path'] == 'pkg/service.py')
            symbols = {(item['kind'], item['name']) for item in service['symbols']}
            self.assertIn(('class', 'Service'), symbols)
            self.assertIn(('function', 'build'), symbols)

    def test_search_and_targeted_read_return_file_line_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            (root / 'app.py').write_text(
                'FIRST = 1\nneedle = "alpha"\nTHIRD = 3\nneedle = "beta"\nFIFTH = 5\n',
                encoding='utf-8',
            )
            runner = ToolRunner(ws)
            found = runner.execute('search', {'query': 'NEEDLE', 'case_sensitive': False})
            self.assertEqual(found['result_count'], 2)
            self.assertEqual(found['results'][0]['path'], 'app.py')
            self.assertEqual(found['results'][0]['line'], 2)
            read = runner.execute('read_range', {'path': 'app.py', 'start_line': 2, 'end_line': 4})
            self.assertEqual(read['start_line'], 2)
            self.assertEqual(read['end_line'], 4)
            self.assertIn('2: needle = "alpha"', read['content'])
            self.assertIn('4: needle = "beta"', read['content'])
            self.assertNotIn('FIRST', read['content'])

    def test_search_skips_binary_and_ignored_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            (root / 'visible.txt').write_text('find-me\n', encoding='utf-8')
            (root / 'blob.bin').write_bytes(b'\x00find-me\x00')
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'dependency.js').write_text('find-me\n', encoding='utf-8')
            result = ToolRunner(ws).execute('search', {'query': 'find-me'})
            paths = {item['path'] for item in result['results']}
            self.assertEqual(paths, {'visible.txt'})

    def test_replace_requires_exact_match_count_and_is_atomic_on_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            path = root / 'module.py'
            original = 'VALUE = 1\nOTHER = 1\n'
            path.write_text(original, encoding='utf-8')
            runner = ToolRunner(ws)
            with self.assertRaises(ToolError):
                runner.execute('replace', {'path': 'module.py', 'old': '1', 'new': '2'})
            self.assertEqual(path.read_text(encoding='utf-8'), original)
            result = runner.execute(
                'replace',
                {'path': 'module.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'},
            )
            self.assertEqual(result['replacements'], 1)
            self.assertEqual(path.read_text(encoding='utf-8'), 'VALUE = 2\nOTHER = 1\n')

    def test_targeted_edit_cannot_touch_control_paths_or_escape_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            runner = ToolRunner(ws)
            project_before = (root / 'project.yaml').read_text(encoding='utf-8')
            with self.assertRaises(ToolError):
                runner.execute(
                    'replace',
                    {'path': 'project.yaml', 'old': 'software', 'new': 'tampered'},
                )
            self.assertEqual((root / 'project.yaml').read_text(encoding='utf-8'), project_before)
            with self.assertRaises(ToolError):
                runner.execute('read_range', {'path': '../outside.txt', 'start_line': 1, 'end_line': 1})
            with self.assertRaises(ToolError):
                runner.execute('search', {'query': 'x', 'path': '..'})

    def test_targeted_read_enforces_context_bound(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            ws = self._workspace(root)
            (root / 'large.py').write_text(''.join(f'line_{i}\n' for i in range(700)), encoding='utf-8')
            runner = ToolRunner(ws)
            with self.assertRaises(ToolError):
                runner.execute('read_range', {'path': 'large.py', 'start_line': 1, 'end_line': 600})

    @unittest.skipUnless(shutil.which('git'), 'git is required')
    def test_repository_tools_work_end_to_end_inside_transactional_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'workspace'
            write_workspace(root, software_manifest('software', 'fix greeting without weakening tests'))
            (root / 'app.py').write_text('def greet(name):\n    return "bye" + name\n', encoding='utf-8')
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_app.py').write_text(
                'import unittest\n\nfrom app import greet\n\n\n'
                'class GreetingTests(unittest.TestCase):\n'
                '    def test_greeting(self):\n'
                '        self.assertEqual(greet("Ada"), "hello Ada")\n\n\n'
                "if __name__ == '__main__':\n"
                '    unittest.main()\n',
                encoding='utf-8',
            )
            provider = SurgicalWorkflowProvider()
            result = Orchestrator(load_workspace(root), provider).step()
            self.assertEqual(result.task.status.value, 'DONE')
            self.assertTrue(result.review.approved)
            self.assertTrue(result.verification_passed)
            self.assertTrue(provider.review_patch_seen)
            self.assertIn('return "hello " + name', (root / 'app.py').read_text(encoding='utf-8'))
            self.assertFalse((root / '.awb' / 'worktrees' / result.task.id.lower()).exists())


if __name__ == '__main__':
    unittest.main()
