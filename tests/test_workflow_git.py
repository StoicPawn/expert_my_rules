import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from awb.core.git_workspace import GitWorkspaceManager
from awb.core.orchestrator import Orchestrator
from awb.core.planner import propose_manifest
from awb.core.storage import Ledger
from awb.core.tools import ToolError, ToolRunner
from awb.core.workflow import WorkflowConfigurationError, WorkflowGraph
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.base import ModelProvider
from awb.templates.templates import custom_manifest, software_manifest


class WorkflowProvider(ModelProvider):
    def __init__(self):
        self.review_systems = []

    def generate(self, system: str, user: str) -> str:
        if 'GATE_JSON' in system:
            return json.dumps({'passed': False, 'detail': 'not complete'})
        if 'DIRECTOR_JSON' in system:
            return json.dumps({'title': 'Implement', 'description': 'implement task', 'priority': 1})
        if 'REVIEW_JSON' in system:
            self.review_systems.append(system)
            return json.dumps({'approved': True, 'critical_objections': [], 'recommendations': []})
        return 'candidate output'


class PlannerProvider(ModelProvider):
    def generate(self, system: str, user: str) -> str:
        return json.dumps({
            'type': 'software',
            'description': 'planned software',
            'agents': [
                {'id': 'd', 'role': 'director', 'instructions': 'direct'},
                {'id': 'w', 'role': 'worker', 'instructions': 'code'},
                {'id': 'r', 'role': 'reviewer', 'instructions': 'review'},
                {'id': 'v', 'role': 'verifier', 'instructions': 'verify'},
            ],
            'gates': [{'id': 'done', 'description': 'done', 'required': True, 'manual': False}],
        })


class WorkflowAndGitTests(unittest.TestCase):
    def test_workflow_graph_supports_multiple_independent_review_stages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            manifest = custom_manifest('demo', 'finish')
            manifest['agents'].append({
                'id': 'audit',
                'role': 'audit',
                'instructions': 'AUDIT_REVIEW_STAGE',
                'provider': {'kind': 'mock'},
            })
            manifest['workflow']['stages'].append({
                'id': 'audit',
                'kind': 'review',
                'role': 'audit',
                'depends_on': ['challenge'],
            })
            write_workspace(root, manifest)
            provider = WorkflowProvider()
            result = Orchestrator(load_workspace(root), provider).step()
            self.assertTrue(result.review.approved)
            self.assertEqual(len(provider.review_systems), 2)
            attempts = Ledger(root / 'ledger.sqlite3').list_attempts(result.task.id)
            stages = attempts[0]['review']['stages']
            self.assertEqual(set(stages), {'challenge', 'audit'})

    def test_workflow_cycle_is_rejected(self):
        manifest = custom_manifest('demo', 'finish')
        manifest['workflow']['stages'] = [
            {'id': 'a', 'kind': 'execute', 'role': 'worker', 'depends_on': ['b']},
            {'id': 'b', 'kind': 'review', 'role': 'reviewer', 'depends_on': ['a']},
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            write_workspace(root, manifest)
            with self.assertRaises(WorkflowConfigurationError):
                WorkflowGraph(load_workspace(root).manifest)

    @unittest.skipUnless(shutil.which('git'), 'git is required')
    def test_git_worktree_isolates_then_merges_approved_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'software'
            write_workspace(root, software_manifest('software', 'change app'))
            (root / 'app.py').write_text('VALUE = 1\n')
            tests = root / 'tests'
            tests.mkdir()
            (tests / 'test_app.py').write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n')
            ws = load_workspace(root)
            manager = GitWorkspaceManager(ws)
            task_ws = manager.prepare('TASK-1')
            self.assertEqual((root / 'app.py').read_text(), 'VALUE = 1\n')
            runner = ToolRunner(ws, execution_root=task_ws.path)
            runner.execute('write', {'path': 'app.py', 'content': 'VALUE = 2\n'})
            self.assertEqual((root / 'app.py').read_text(), 'VALUE = 1\n')
            self.assertIn('VALUE = 2', manager.patch('TASK-1'))
            merged = manager.merge('TASK-1', 'test candidate')
            self.assertTrue(merged['merged'])
            self.assertEqual((root / 'app.py').read_text(), 'VALUE = 2\n')
            self.assertFalse(task_ws.path.exists())

    @unittest.skipUnless(shutil.which('git'), 'git is required')
    def test_git_rejection_discards_candidate_and_protects_control_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'software'
            write_workspace(root, software_manifest('software', 'change app'))
            (root / 'app.py').write_text('VALUE = 1\n')
            ws = load_workspace(root)
            manager = GitWorkspaceManager(ws)
            task_ws = manager.prepare('TASK-2')
            runner = ToolRunner(ws, execution_root=task_ws.path)
            with self.assertRaises(ToolError):
                runner.execute('write', {'path': 'project.yaml', 'content': 'tamper'})
            runner.execute('write', {'path': 'app.py', 'content': 'VALUE = 999\n'})
            manager.discard('TASK-2')
            self.assertEqual((root / 'app.py').read_text(), 'VALUE = 1\n')
            self.assertFalse(task_ws.path.exists())

    def test_planner_generated_software_team_keeps_template_tools(self):
        with patch('awb.core.planner.make_provider', return_value=PlannerProvider()):
            manifest = propose_manifest('Build a software application', use_local_ai=True)
        worker = next(a for a in manifest['agents'] if a['role'] == 'worker')
        self.assertIn('write', worker['tools'])
        self.assertIn('git_diff', worker['tools'])
        self.assertTrue(manifest['runtime']['git']['enabled'])


if __name__ == '__main__':
    unittest.main()
