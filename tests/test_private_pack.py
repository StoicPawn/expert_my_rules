from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from awb.core.storage import Ledger
from awb.core.workspace import load_workspace
from awb.web.private_pack import import_private_pack


class PrivatePackTests(unittest.TestCase):
    def _pack(self, bootstrap: dict, files: dict[str, bytes] | None = None) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("PROJECT_BOOTSTRAP.json", json.dumps(bootstrap))
            for name, payload in (files or {}).items():
                zf.writestr(name, payload)
        return buf.getvalue()

    def test_private_pack_creates_isolated_research_workspace_and_starts_job(self):
        bootstrap = {
            "project_key": "private-theory-001",
            "kind": "research",
            "goal": "Prove or falsify the central theorem and finish only when every gate is closed.",
            "description": "Local private research only.",
            "auto_launch": True,
            "gates": [
                {
                    "id": "proof_closed",
                    "description": "No unresolved proof objection remains.",
                    "required": True,
                    "manual": False,
                }
            ],
            "agent_instructions": {
                "researcher": "Read private sources first and attack the theorem before accepting it."
            },
            "seed_tasks": [
                {
                    "title": "Attack theorem",
                    "description": "Find a counterexample before trying to prove it.",
                    "priority": 10,
                }
            ],
        }
        launched: list[tuple[Path, str]] = []
        raw = self._pack(bootstrap, {"PAPER_SOURCE.txt": b"private theorem text"})

        with tempfile.TemporaryDirectory() as tmp:
            result = import_private_pack(
                raw,
                workspace_base=Path(tmp),
                start_callback=lambda root, jid: launched.append((root, jid)),
            )
            root = Path(tmp) / "private-theory-001"
            self.assertEqual(result["workspace"], "private-theory-001")
            self.assertTrue(result["auto_launched"])
            self.assertEqual(len(launched), 1)
            self.assertEqual((root / "private" / "PAPER_SOURCE.txt").read_text(), "private theorem text")
            self.assertTrue((root / "private" / "LOCAL_ONLY.txt").exists())

            ws = load_workspace(root)
            self.assertEqual(ws.manifest.type, "research")
            self.assertEqual([g.id for g in ws.manifest.gates], ["proof_closed"])
            researcher = next(a for a in ws.manifest.agents if a.id == "researcher")
            self.assertIn("attack the theorem", researcher.instructions)

            ledger = Ledger(root / "ledger.sqlite3")
            self.assertEqual(ledger.get_task("SEED-001").title, "Attack theorem")
            self.assertEqual(ledger.latest_job()["status"], "RUNNING")

    def test_private_pack_rejects_path_traversal(self):
        bootstrap = {"project_key": "safe", "kind": "research", "goal": "test"}
        raw = self._pack(bootstrap, {"../escape.txt": b"no"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                import_private_pack(raw, workspace_base=Path(tmp))


if __name__ == "__main__":
    unittest.main()
