from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from awb.core.deep_engine import DeepIterativeEngine
from awb.core.models import Task, TaskStatus
from awb.core.project_memory import ProjectMemory
from awb.core.storage import Ledger
from awb.core.task_graph import TaskGraph, VerificationContract, normalize_contract, strategy_fingerprint
from awb.core.workspace import write_workspace
from awb.templates.templates import custom_manifest


class GeneralPurposeEngineTests(unittest.TestCase):
    def _ledger(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / 'demo'
        write_workspace(root, custom_manifest('demo', 'Finish an arbitrary complex project with verified evidence.'))
        return tmp, root, Ledger(root / 'ledger.sqlite3')

    def test_dependency_graph_only_releases_ready_microtasks(self):
        tmp, root, ledger = self._ledger()
        self.addCleanup(tmp.cleanup)
        parent = Task(id='P', title='parent', description='p', metadata={
            'depends_on': ['D'],
            'verification_contract': normalize_contract({}, fallback_criterion='parent evidence'),
        })
        dep = Task(id='D', title='dependency', description='d', status=TaskStatus.OPEN, metadata={
            'verification_contract': normalize_contract({}, fallback_criterion='dependency evidence'),
        })
        ledger.upsert_task(parent); ledger.upsert_task(dep)
        graph = TaskGraph(ledger)
        self.assertFalse(graph.ready(parent))
        self.assertTrue(graph.ready(dep))
        dep.status = TaskStatus.DONE; ledger.upsert_task(dep)
        self.assertTrue(graph.ready(parent))

    def test_verification_contract_is_explicit_before_execution(self):
        task = Task(id='T', title='x', description='x', metadata={
            'verification_contract': normalize_contract({}, fallback_criterion='observable criterion')
        })
        contract = VerificationContract.from_task(task)
        self.assertTrue(contract.valid())
        self.assertIn('observable criterion', contract.criteria)
        self.assertTrue(contract.reviewer_required)

    def test_external_memory_survives_context_boundaries(self):
        tmp, root, ledger = self._ledger()
        self.addCleanup(tmp.cleanup)
        memory = ProjectMemory(root / 'ledger.sqlite3')
        memory.remember('failed_strategy', 'matrix inversion route failed on singular boundary', task_id='T1')
        again = ProjectMemory(root / 'ledger.sqlite3')
        hits = again.relevant('singular matrix boundary', limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]['kind'], 'failed_strategy')

    def test_strategy_identity_is_stable_for_blind_loop_detection(self):
        self.assertEqual(strategy_fingerprint('Try  A   then B'), strategy_fingerprint('try a then b'))
        self.assertNotEqual(strategy_fingerprint('try a then b'), strategy_fingerprint('falsify b first'))

    def test_engine_cycle_is_domain_neutral_and_not_kellerer_specialized(self):
        source = inspect.getsource(DeepIterativeEngine)
        for phase in ('EXECUTE', 'VERIFY', 'CRITIQUE', 'REWORK'):
            self.assertIn(phase, source)
        self.assertIn('verification_contract', source)
        self.assertIn('ProjectMemory', source)
        self.assertNotIn('Kellerer', source)


if __name__ == '__main__':
    unittest.main()
