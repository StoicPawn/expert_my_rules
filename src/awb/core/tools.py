from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator

from .models import Workspace


class ToolError(RuntimeError):
    pass


class ToolRunner:
    """Execute only tools explicitly declared in a workspace manifest.

    An optional execution_root lets software agents work inside a transactional Git
    worktree while the canonical project workspace and ledger remain untouched.

    Repository-intelligence tools intentionally use Python filesystem primitives
    rather than arbitrary shell commands. This keeps them portable to Windows and
    gives small local models compact, targeted context instead of forcing them to
    read/rewrite entire repositories.
    """

    IGNORED_DIR_NAMES = {
        '.git', '.awb', 'artifacts', 'logs', '__pycache__', '.venv', 'venv',
        'node_modules', '.mypy_cache', '.pytest_cache', '.ruff_cache', 'dist', 'build',
    }
    MAX_SCAN_FILES = 800
    MAX_TEXT_FILE_BYTES = 1_000_000
    MAX_READ_LINES = 500
    MAX_SEARCH_RESULTS = 100

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
            elif spec.type == "read_file_range":
                item["arguments"] = {
                    "path": "workspace-relative path",
                    "start_line": "1-based inclusive line (default 1)",
                    "end_line": f"1-based inclusive line, at most {self.MAX_READ_LINES} lines",
                }
            elif spec.type == "list_files":
                item["arguments"] = {"path": "optional workspace-relative directory"}
            elif spec.type == "repo_map":
                item["arguments"] = {
                    "path": "optional workspace-relative directory",
                    "max_files": f"optional, <= {self.MAX_SCAN_FILES}",
                }
            elif spec.type == "search_text":
                item["arguments"] = {
                    "query": "literal text to search for",
                    "path": "optional workspace-relative directory/file",
                    "case_sensitive": "optional boolean",
                    "max_results": f"optional, <= {self.MAX_SEARCH_RESULTS}",
                }
            elif spec.type == "replace_text":
                item["arguments"] = {
                    "path": "workspace-relative path",
                    "old": "exact text that must already exist",
                    "new": "replacement text",
                    "expected_count": "exact number of matches required (default 1)",
                }
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

    @staticmethod
    def _bounded_int(value: Any, *, default: int, low: int, high: int, name: str) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ToolError(f'{name} must be an integer') from exc
        if parsed < low or parsed > high:
            raise ToolError(f'{name} must be between {low} and {high}')
        return parsed

    def _iter_files(self, base: Path, max_files: int) -> Iterator[Path]:
        if base.is_file():
            yield base
            return
        if not base.exists() or not base.is_dir():
            raise ToolError('Directory not found')
        emitted = 0
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in self.IGNORED_DIR_NAMES)
            current_path = Path(current)
            for name in sorted(files):
                path = current_path / name
                # A file symlink may still point outside the worktree.
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved != self.root and self.root not in resolved.parents:
                    continue
                yield resolved
                emitted += 1
                if emitted >= max_files:
                    return

    def _read_text_candidate(self, path: Path) -> str | None:
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > self.MAX_TEXT_FILE_BYTES:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b'\x00' in data:
            return None
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.decode('utf-8', errors='replace')

    @staticmethod
    def _python_symbols(text: str) -> list[dict[str, Any]]:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return []
        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({'kind': 'function', 'name': node.name, 'line': node.lineno})
            elif isinstance(node, ast.ClassDef):
                symbols.append({'kind': 'class', 'name': node.name, 'line': node.lineno})
            if len(symbols) >= 40:
                break
        return symbols

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

        if spec.type == "repo_map":
            base = self._safe_path(str(arguments.get('path', '.')))
            max_files = self._bounded_int(
                arguments.get('max_files'), default=300, low=1,
                high=self.MAX_SCAN_FILES, name='max_files',
            )
            entries = []
            for path in self._iter_files(base, max_files):
                rel = str(self._relative(path))
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                item: dict[str, Any] = {'path': rel, 'bytes': size}
                if path.suffix.lower() == '.py' and size <= 250_000:
                    text = self._read_text_candidate(path)
                    if text is not None:
                        symbols = self._python_symbols(text)
                        if symbols:
                            item['symbols'] = symbols
                entries.append(item)
            return {
                'ok': True,
                'root': str(self._relative(base)) if base != self.root else '.',
                'files': entries,
                'file_count': len(entries),
                'truncated': len(entries) >= max_files,
            }

        if spec.type == "read_file":
            path = self._safe_path(str(arguments.get("path", "")))
            if not path.exists() or not path.is_file():
                raise ToolError("File not found")
            text = path.read_text(errors="replace")
            return {"ok": True, "path": str(path.relative_to(self.root)), "content": text[:100_000], "truncated": len(text) > 100_000}

        if spec.type == 'read_file_range':
            path = self._safe_path(str(arguments.get('path', '')))
            if not path.exists() or not path.is_file():
                raise ToolError('File not found')
            text = self._read_text_candidate(path)
            if text is None:
                raise ToolError('File is binary or too large for targeted reading')
            lines = text.splitlines()
            start = self._bounded_int(arguments.get('start_line'), default=1, low=1, high=max(1, len(lines) + 1), name='start_line')
            default_end = min(len(lines), start + 199)
            end = self._bounded_int(arguments.get('end_line'), default=default_end, low=start, high=max(start, len(lines)), name='end_line')
            if end - start + 1 > self.MAX_READ_LINES:
                raise ToolError(f'read_file_range is limited to {self.MAX_READ_LINES} lines')
            selected = lines[start - 1:end]
            numbered = '\n'.join(f'{number}: {line}' for number, line in enumerate(selected, start=start))
            return {
                'ok': True,
                'path': str(self._relative(path)),
                'start_line': start,
                'end_line': end,
                'total_lines': len(lines),
                'content': numbered,
            }

        if spec.type == 'search_text':
            query = arguments.get('query')
            if not isinstance(query, str) or not query:
                raise ToolError('query must be a non-empty string')
            base = self._safe_path(str(arguments.get('path', '.')))
            case_sensitive = bool(arguments.get('case_sensitive', False))
            max_results = self._bounded_int(
                arguments.get('max_results'), default=40, low=1,
                high=self.MAX_SEARCH_RESULTS, name='max_results',
            )
            needle = query if case_sensitive else query.casefold()
            results = []
            scanned = 0
            for path in self._iter_files(base, self.MAX_SCAN_FILES):
                text = self._read_text_candidate(path)
                if text is None:
                    continue
                scanned += 1
                for line_no, line in enumerate(text.splitlines(), start=1):
                    haystack = line if case_sensitive else line.casefold()
                    if needle in haystack:
                        results.append({
                            'path': str(self._relative(path)),
                            'line': line_no,
                            'text': line[:500],
                        })
                        if len(results) >= max_results:
                            return {
                                'ok': True,
                                'query': query,
                                'results': results,
                                'result_count': len(results),
                                'scanned_files': scanned,
                                'truncated': True,
                            }
            return {
                'ok': True,
                'query': query,
                'results': results,
                'result_count': len(results),
                'scanned_files': scanned,
                'truncated': False,
            }

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

        if spec.type == 'replace_text':
            if not spec.writable:
                raise ToolError('Tool is read-only')
            path = self._safe_path(str(arguments.get('path', '')))
            self._ensure_writable(path)
            if not path.exists() or not path.is_file():
                raise ToolError('File not found')
            old = arguments.get('old')
            new = arguments.get('new')
            if not isinstance(old, str) or not old:
                raise ToolError('old must be a non-empty string')
            if not isinstance(new, str):
                raise ToolError('new must be a string')
            expected = self._bounded_int(arguments.get('expected_count'), default=1, low=1, high=100, name='expected_count')
            text = self._read_text_candidate(path)
            if text is None:
                raise ToolError('File is binary or too large for targeted editing')
            actual = text.count(old)
            if actual != expected:
                raise ToolError(f'Expected exactly {expected} match(es) but found {actual}; no change was made')
            updated = text.replace(old, new, expected)
            path.write_text(updated, encoding='utf-8')
            return {
                'ok': True,
                'path': str(self._relative(path)),
                'replacements': expected,
                'bytes': len(updated.encode('utf-8')),
            }

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
