import tempfile
import unittest
from pathlib import Path

from awb.core.orchestrator import Orchestrator
from awb.core.routing import ModelRouter, RouteBusyError
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
            manifest['runtime'].pop('scheduler', None)
            write_workspace(root, manifest)
            route = ModelRouter(load_workspace(root).manifest).candidates('worker')[0]
            self.assertEqual(route.node_id, 'legacy')
            self.assertEqual(route.model, 'qwen3:4b')

    def test_failed_node_enters_cooldown_and_fallback_is_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            manifest = custom_manifest('demo', 'finish')
            manifest['runtime']['scheduler'] = {
                'failure_threshold': 1,
                'cooldown_seconds': 60,
                'queue_timeout_seconds': 0.01,
            }
            manifest['runtime']['compute_nodes'].append({
                'id': 'gpu-circuit-test',
                'kind': 'ollama',
                'base_url': 'http://127.0.0.1:65530',
                'max_concurrency': 1,
                'tags': ['gpu'],
            })
            manifest['runtime']['role_routes']['worker'].insert(0, {
                'node': 'gpu-circuit-test', 'model': 'coder', 'priority': 1
            })
            write_workspace(root, manifest)
            router = ModelRouter(load_workspace(root).manifest)
            gpu = router.candidates('worker')[0]
            self.assertEqual(gpu.node_id, 'gpu-circuit-test')
            router.record_failure(gpu)
            fallback = router.candidates('worker')[0]
            self.assertEqual(fallback.node_id, 'local-ollama')
            snap = {x['node']: x for x in router.snapshot()}
            self.assertGreater(snap['gpu-circuit-test']['cooldown_remaining_seconds'], 0)

    def test_saturated_node_has_bounded_queue_wait(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            manifest = custom_manifest('demo', 'finish')
            manifest['runtime']['scheduler']['queue_timeout_seconds'] = 0.01
            write_workspace(root, manifest)
            router = ModelRouter(load_workspace(root).manifest)
            route = router.candidates('worker')[0]
            with router.slot(route):
                with self.assertRaises(RouteBusyError):
                    with router.slot(route):
                        pass

    def test_attempt_genealogy_and_model_telemetry_are_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'demo'
            write_workspace(root, custom_manifest('demo', 'ship evidence'))
            result = Orchestrator(load_workspace(root), MockProvider()).step()
            ledger = Ledger(root / 'ledger.sqlite3')
            attempts = ledger.list_attempts(result.task.id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]['status'], 'DONE')
            self.assertTrue(attempts[0]['review']['approved'])
            self.assertTrue(attempts[0]['artifact'])
            self.assertTrue(attempts[0]['route']['calls'])
            stats = ledger.model_stats()
            self.assertTrue(stats)
            self.assertTrue(all(item['success_rate'] == 1.0 for item in stats))
            self.assertTrue(any(item['role'] == 'worker' for item in stats))


if __name__ == '__main__':
    unittest.main()
