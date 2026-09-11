from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from awb.core.project_output import (
    OutputPolicy,
    list_output_snapshots,
    load_output_policy,
    save_output_policy,
    write_output_snapshot,
)
from awb.core.storage import Ledger


class ProjectOutputTests(unittest.TestCase):
    def test_manual_snapshot_indexes_generated_files_and_writes_human_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            root.mkdir(parents=True)
            Ledger(root / 'ledger.sqlite3')
            artifact = root / 'artifacts' / 'notes' / 'candidate.md'
            artifact.parent.mkdir(parents=True)
            artifact.write_text('candidate result', encoding='utf-8')

            saved = save_output_policy(root, OutputPolicy(auto_every_minutes=15, max_snapshots_shown=7))
            self.assertEqual(saved.auto_every_minutes, 15)
            self.assertEqual(load_output_policy(root).max_snapshots_shown, 7)

            snap = write_output_snapshot(root, reason='test', manual=True)
            folder = root / 'artifacts' / 'outputs' / snap['output_id']
            self.assertTrue((folder / 'manifest.json').exists())
            self.assertTrue((folder / 'summary.md').exists())
            self.assertIn('artifacts/notes/candidate.md', {row['path'] for row in snap['files']})
            self.assertIn('Project output', (folder / 'summary.md').read_text(encoding='utf-8'))

            listed = list_output_snapshots(root)
            self.assertEqual(listed[0]['output_id'], snap['output_id'])
            manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
            self.assertTrue(manifest['manual'])
            self.assertEqual(manifest['reason'], 'test')


if __name__ == '__main__':
    unittest.main()
