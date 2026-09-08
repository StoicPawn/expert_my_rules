from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from awb.core.private_import import PrivateImportError, import_private_bundle
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace


def bundle_bytes(bootstrap: dict, extras: dict[str, bytes] | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('PROJECT_BOOTSTRAP.json', json.dumps(bootstrap))
        zf.writestr('PAPER_SOURCE.txt', 'private source text')
        for name, content in (extras or {}).items():
            zf.writestr(name, content)
    return out.getvalue()


class PrivateImportTests(unittest.TestCase):
    def test_private_bundle_creates_fail_closed_research_workspace(self):
        bootstrap = {
            'project_key': 'private-research-01',
            'kind': 'research',
            'goal': 'Produce a rigorous final result that survives independent adversarial verification.',
            'auto_launch': True,
            'gates': [
                {'id': 'proof_closed', 'description': 'Proof is closed.', 'required': True, 'manual': False},
                {'id': 'application_closed', 'description': 'Application is defensible.', 'required': True, 'manual': False},
            ],
            'agent_instructions': {'referee': 'Reject unsupported claims aggressively.'},
            'seed_tasks': [
                {'title': 'Attack the main theorem', 'description': 'Try to falsify it first.', 'priority': 10},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            root, returned = import_private_bundle(bundle_bytes(bootstrap), Path(td))
            self.assertEqual(returned['project_key'], 'private-research-01')
            self.assertTrue((root / 'sources' / 'PAPER_SOURCE.txt').exists())
            self.assertTrue((root / 'sources' / 'PROJECT_BOOTSTRAP.json').exists())
            self.assertTrue((root / 'PRIVATE_IMPORT.json').exists())

            ws = load_workspace(root)
            self.assertEqual(ws.manifest.type, 'research')
            self.assertEqual([g.id for g in ws.manifest.gates], ['proof_closed', 'application_closed'])
            referee = next(a for a in ws.manifest.agents if a.id == 'referee')
            self.assertIn('Reject unsupported claims', referee.instructions)
            self.assertIn('sources', ws.manifest.runtime.git.protected_paths)

            tasks = Ledger(root / 'ledger.sqlite3').list_tasks()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].title, 'Attack the main theorem')

    def test_private_bundle_rejects_path_traversal(self):
        bootstrap = {
            'project_key': 'private-research-02',
            'kind': 'research',
            'goal': 'Produce a rigorous research result with independently verified completion evidence.',
        }
        payload = bundle_bytes(bootstrap, {'../escape.txt': b'nope'})
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(PrivateImportError):
                import_private_bundle(payload, Path(td))
            self.assertFalse((Path(td).parent / 'escape.txt').exists())

    def test_private_bundle_rejects_duplicate_workspace(self):
        bootstrap = {
            'project_key': 'private-research-03',
            'kind': 'research',
            'goal': 'Produce a rigorous research result with independently verified completion evidence.',
        }
        payload = bundle_bytes(bootstrap)
        with tempfile.TemporaryDirectory() as td:
            import_private_bundle(payload, Path(td))
            with self.assertRaises(PrivateImportError):
                import_private_bundle(payload, Path(td))


if __name__ == '__main__':
    unittest.main()
