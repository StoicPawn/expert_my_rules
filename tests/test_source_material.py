from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from awb.core.source_material import SourceMaterialError, ingest_source, list_sources
from awb.core.storage import Ledger
from awb.core.workspace import load_workspace, write_workspace
from awb.templates.templates import get_template
from awb.web.control_app import _ensure_source_access, _ensure_source_task


class SourceMaterialTests(unittest.TestCase):
    def test_text_source_is_preserved_indexed_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            write_workspace(root, get_template('research', 'project', 'Study the supplied theorem carefully and produce a rigorous result.'))
            item = ingest_source(root, 'paper.md', b'# Theorem\nAssume X. Then Y.\n', 'text/markdown')
            duplicate = ingest_source(root, 'copy.md', b'# Theorem\nAssume X. Then Y.\n', 'text/markdown')

            self.assertEqual(item['sha256'], duplicate['sha256'])
            self.assertEqual(len(list_sources(root)), 1)
            self.assertTrue((root / item['original_path']).exists())
            self.assertIn('Assume X', (root / item['text_path']).read_text(encoding='utf-8'))
            self.assertIn('paper.md', (root / 'sources' / 'INDEX.md').read_text(encoding='utf-8'))

    def test_source_access_is_readable_but_protected_and_creates_priority_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            write_workspace(root, get_template('research', 'project', 'Use the supplied source material to establish the strongest valid theorem.'))
            item = ingest_source(root, 'notes.txt', b'Lemma A is the starting point.', 'text/plain')
            _ensure_source_access(root)
            _ensure_source_task(root, item)

            ws = load_workspace(root)
            self.assertIn('sources', ws.manifest.runtime.git.protected_paths)
            researcher = next(agent for agent in ws.manifest.agents if agent.role == 'worker')
            self.assertIn('search', researcher.tools)
            self.assertIn('read_range', researcher.tools)
            task = next(task for task in Ledger(root / 'ledger.sqlite3').list_tasks() if task.id.startswith('SRC-'))
            self.assertGreater(task.priority, 10)
            self.assertIn(item['text_path'], task.description)

    def test_unsupported_source_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SourceMaterialError):
                ingest_source(Path(tmp), 'archive.exe', b'abc')


if __name__ == '__main__':
    unittest.main()
