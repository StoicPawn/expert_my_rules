from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .models import Workspace


class ToolError(RuntimeError):
    pass


class ToolRunner:
    """Execute only tools explicitly declared in a workspace manifest.

    An optional execution_root lets software agents work inside a transactional Git
    worktree while the canonical project workspace and ledger remain untouched.
    """

    def __init__(self, workspace: Workspace, execution_root: Path | None = None):
        self.workspace = workspace
        self.root = (execution_root or workspace.root).resolve()
        self.specs = {t.id: t for t in workspace.manifest.tools if t.enabled}
        self.protected = [Path(p) for p in workspace.manifest.runtime.git.protected_paths]

    def describe(self, allowed_ids: list[str]) -> list[dict[str, Any]]:
        out = []
        for tool_id in allowed_ids:
            spec = self.specs.get(tool_id)
            if not spec:
                continue
            item = {"id": spec.id, "type": spec.type, "description": spec.description}
            if spec.type in {"read_file", "write_file"}:
                item["arguments"] = {"path": "workspace-relative path"}
                if spec.type == "write_file":
                    item["arguments"]["content"] = "complete UTF-8 content"
            elif spec.type == "list_files":
                item["arguments"] = {"path": "optional workspace-relative directory"}
            elif spec.type == "shell":
                item["arguments"] = {}
                item["fixed_command"] = spec.command
            elif spec.type in {"git_status", "git_diff"}:
                item["arguments"] = {}
            out.append(item)
        return out

    def _safe_path(self, raw: str) -> Path:
        target = (self.root / raw).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError("Path escapes execution root")
        return target

    def _relative(self, path: Path) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as exc:
            raise ToolError('Path escapes execution root') from exc

    def _ensure_writable(self, path: Path) -> None:
        rel = self._relative(path)
        for protected in self.protected:
            if rel == protected or protected in rel.parents:
                raise ToolError(f'Protected control path is not writable by agents: {rel}')

    def _git(self, *args: str) -> dict[str, Any]:
        proc = subprocess.run(
            ['git', *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=60,
        )
        return {
            'ok': proc.returncode == 0,
            'returncode': proc.returncode,
            'stdout': proc.stdout[-40_000:],
            'stderr': proc.stderr[-20_000:],
        }

    def execute(self, tool_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        spec = self.specs.get(tool_id)
        if not spec:
            raise ToolError(f"Tool not enabled: {tool_id}")

        if spec.type == "list_files":
            base = self._safe_path(str(arguments.get("path", ".")))
            if not base.exists() or not base.is_dir():
                raise ToolError("Directory not found")
            items = []
            for p in sorted(base.iterdir())[:300]:
                items.append({"name": p.name, "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else None})
            return {"ok": True, "items": items}

        if spec.type == "read_file":
            path = self._safe_path(str(arguments.get("path", "")))
            if not path.exists() or not path.is_file():
                raise ToolError("File not found")
            text = path.read_text(errors="replace")
            return {"ok": True, "path": str(path.relative_to(self.root)), "content": text[:100_000], "truncated": len(text) > 100_000}

        if spec.type == "write_file":
            if not spec.writable:
                raise ToolError("Tool is read-only")
            path = self._safe_path(str(arguments.get("path", "")))
            self._ensure_writable(path)
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ToolError("content must be a string")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            return {"ok": True, "path": str(path.relative_to(self.root)), "bytes": len(content.encode())}

        if spec.type == "shell":
            if not spec.command:
                raise ToolError("No fixed command configured")
            proc = subprocess.run(
                spec.command,
                cwd=self.root,
                shell=True,
                text=True,
                capture_output=True,
                timeout=spec.timeout_seconds,
            )
            return {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-20_000:],
                "stderr": proc.stderr[-20_000:],
                "command": spec.command,
            }

        if spec.type == 'git_status':
            return self._git('status', '--short')

        if spec.type == 'git_diff':
            subprocess.run(['git', 'add', '-N', '.'], cwd=self.root, text=True, capture_output=True, timeout=60)
            return self._git('diff', '--stat', 'HEAD') | {
                'patch': self._git('diff', '--no-ext-diff', 'HEAD')['stdout'][-60_000:]
            }

        raise ToolError(f"Unsupported tool type: {spec.type}")


def parse_tool_message(raw: str) -> tuple[str, dict[str, Any]] | None:
    text = raw.strip()
    if text.startswith("```json"):
        text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "tool" not in data:
        return None
    return str(data["tool"]), data.get("arguments") or {}
