import tempfile
import unittest
from pathlib import Path

from awb.core.orchestrator import Orchestrator
from awb.core.routing import ModelRouter
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.providers.providers import MockProvider
from awb.templates.templates import custom_manifest


class ScalableRuntimeTests(unittest.TestCase):
    def test_default_manifest_routes_roles_through_local_compute_node(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            write_workspace(root, custom_manifest('demo', 'finish'))
            ws = load_workspace(root)
            router = ModelRouter(ws.manifest)
            worker = router.candidates('worker')[0]
            reviewer = router.candidates('reviewer')[0]
            self.assertEqual(worker.node_id, 'local-ollama')
            self.assertEqual(worker.model, 'qwen3:4b')
            self.assertEqual(reviewer.model, 'llama3.2:3b')
            self.assertEqual(ws.manifest.runtime.compute_nodes[0].max_concurrency, 1)

    def test_gpu_node_can_replace_acer_by_configuration_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            manifest = custom_manifest('demo', 'finish')
            manifest['runtime']['compute_nodes'].append({
                'id': 'gpu-box',
                'kind': 'ollama',
                'base_url': 'http://10.0.0.50:11434',
                'max_concurrency': 2,
                'priority': 10,
                'tags': ['gpu'],
            })
            manifest['runtime']['role_routes']['worker'].insert(0, {
                'node': 'gpu-box', 'model': 'qwen3-coder:30b', 'priority': 10
            })
            write_workspace(root, manifest)
            route = ModelRouter(load_workspace(root).manifest).candidates('worker')[0]
            self.assertEqual(route.node_id, 'gpu-box')
            self.assertEqual(route.model, 'qwen3-coder:30b')
            self.assertEqual(route.base_url, 'http://10.0.0.50:11434')

    def test_v02_manifest_without_compute_nodes_remains_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            manifest = custom_manifest('demo', 'finish')
            manifest['runtime'].pop('compute_nodes', None)
            manifest['runtime'].pop('role_routes', None)
            write_workspace(root, manifest)
            route = ModelRouter(load_workspace(root).manifest).candidates('worker')[0]
            self.assertEqual(route.node_id, 'legacy')
            self.assertEqual(route.model, 'qwen3:4b')

    def test_attempt_genealogy_is_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            write_workspace(root, custom_manifest('demo', 'ship evidence'))
            result = Orchestrator(load_workspace(root), MockProvider()).step()
            attempts = Ledger(root / 'ledger.sqlite3').list_attempts(result.task.id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]['status'], 'DONE')
            self.assertTrue(attempts[0]['review']['approved'])
            self.assertTrue(attempts[0]['artifact'])
            self.assertTrue(attempts[0]['route']['calls'])


if __name__ == '__main__':
    unittest.main()
