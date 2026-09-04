from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Workspace


class GitWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitTaskWorkspace:
    task_id: str
    branch: str
    path: Path


class GitWorkspaceManager:
    """Transactional Git worktrees for autonomous software changes.

    The canonical workspace stays stable while an agent edits a task-specific
    worktree. Rejected attempts remain isolated for correction; approved work is
    committed and fast-forwarded into the canonical workspace. Final rejection can
    discard the worktree and branch, giving deterministic rollback.
    """

    RUNTIME_EXCLUDES = (
        'ledger.sqlite3',
        'ledger.sqlite3-shm',
        'ledger.sqlite3-wal',
        'artifacts/',
        'logs/',
        '.awb/',
    )

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self.root = workspace.root.resolve()
        self.policy = workspace.manifest.runtime.git
        self.meta_root = self.root / '.awb'
        self.worktree_root = self.meta_root / 'worktrees'

    @property
    def enabled(self) -> bool:
        return bool(self.policy.enabled)

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which('git') is None:
            raise GitWorkspaceError('Git is required for workspace isolation but was not found on PATH')
        proc = subprocess.run(
            ['git', *args],
            cwd=cwd or self.root,
            text=True,
            capture_output=True,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise GitWorkspaceError(f"git {' '.join(args)} failed: {detail}")
        return proc

    def _is_repo(self) -> bool:
        proc = self._git('rev-parse', '--is-inside-work-tree', cwd=self.root, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == 'true'

    def _ensure_identity(self) -> None:
        if self._git('config', '--get', 'user.name', check=False).returncode != 0:
            self._git('config', 'user.name', 'Expert My Rules')
        if self._git('config', '--get', 'user.email', check=False).returncode != 0:
            self._git('config', 'user.email', 'awb@localhost')

    def _ensure_runtime_excludes(self) -> None:
        git_dir = self._git('rev-parse', '--git-dir').stdout.strip()
        git_dir_path = Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = (self.root / git_dir_path).resolve()
        exclude = git_dir_path / 'info' / 'exclude'
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(errors='replace') if exclude.exists() else ''
        additions = [item for item in self.RUNTIME_EXCLUDES if item not in existing.splitlines()]
        if additions:
            with exclude.open('a', encoding='utf-8') as fh:
                if existing and not existing.endswith('\n'):
                    fh.write('\n')
                fh.write('\n'.join(additions) + '\n')

    def _init_repo(self) -> None:
        if not self.policy.auto_init:
            raise GitWorkspaceError('Workspace is not a Git repository and git.auto_init is disabled')
        proc = self._git('init', '-b', 'awb-main', cwd=self.root, check=False)
        if proc.returncode != 0:
            self._git('init', cwd=self.root)
            self._git('checkout', '-b', 'awb-main', cwd=self.root)
        self._ensure_identity()
        self._ensure_runtime_excludes()
        self._git('add', '-A')
        self._git('commit', '--allow-empty', '-m', 'AWB baseline workspace')

    def ensure_repo(self) -> None:
        if not self.enabled:
            return
        if not self._is_repo():
            self._init_repo()
        self._ensure_identity()
        self._ensure_runtime_excludes()
        status = self._git('status', '--porcelain').stdout.strip()
        if status:
            if not self.policy.checkpoint_dirty:
                raise GitWorkspaceError(
                    'Canonical workspace has uncommitted changes. Commit them first or enable runtime.git.checkpoint_dirty.'
                )
            self._git('add', '-A')
            staged = self._git('diff', '--cached', '--quiet', check=False)
            if staged.returncode != 0:
                self._git('commit', '-m', 'AWB checkpoint before autonomous task')

    @staticmethod
    def _slug(task_id: str) -> str:
        slug = re.sub(r'[^A-Za-z0-9_.-]+', '-', task_id).strip('-').lower()
        return slug[:80] or 'task'

    def _branch(self, task_id: str) -> str:
        return f'awb/task-{self._slug(task_id)}'

    def _path(self, task_id: str) -> Path:
        return self.worktree_root / self._slug(task_id)

    def _branch_exists(self, branch: str) -> bool:
        return self._git('show-ref', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0

    def prepare(self, task_id: str) -> GitTaskWorkspace:
        if not self.enabled:
            return GitTaskWorkspace(task_id=task_id, branch='', path=self.root)
        self.ensure_repo()
        branch = self._branch(task_id)
        path = self._path(task_id)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            probe = self._git('rev-parse', '--is-inside-work-tree', cwd=path, check=False)
            if probe.returncode == 0:
                return GitTaskWorkspace(task_id=task_id, branch=branch, path=path)
            shutil.rmtree(path, ignore_errors=True)
        if self._branch_exists(branch):
            self._git('worktree', 'add', str(path), branch)
        else:
            self._git('worktree', 'add', '-b', branch, str(path), 'HEAD')
        return GitTaskWorkspace(task_id=task_id, branch=branch, path=path)

    def status(self, task_id: str) -> str:
        if not self.enabled:
            return ''
        path = self._path(task_id)
        if not path.exists():
            return ''
        return self._git('status', '--short', cwd=path).stdout

    def patch(self, task_id: str) -> str:
        if not self.enabled:
            return ''
        path = self._path(task_id)
        if not path.exists():
            return ''
        # Intent-to-add makes new files visible in git diff without committing them.
        self._git('add', '-N', '.', cwd=path, check=False)
        return self._git('diff', '--binary', '--no-ext-diff', 'HEAD', cwd=path).stdout

    def diff_summary(self, task_id: str) -> str:
        if not self.enabled:
            return ''
        path = self._path(task_id)
        if not path.exists():
            return ''
        self._git('add', '-N', '.', cwd=path, check=False)
        return self._git('diff', '--stat', 'HEAD', cwd=path).stdout

    def merge(self, task_id: str, message: str) -> dict[str, str | bool]:
        if not self.enabled:
            return {'merged': False, 'detail': 'git isolation disabled'}
        branch = self._branch(task_id)
        path = self._path(task_id)
        if not path.exists():
            raise GitWorkspaceError(f'No active worktree for {task_id}')
        self._git('add', '-A', cwd=path)
        staged = self._git('diff', '--cached', '--quiet', cwd=path, check=False)
        committed = staged.returncode != 0
        if committed:
            self._git('commit', '-m', message, cwd=path)
        if self.policy.merge_approved:
            self._git('merge', '--ff-only', branch, cwd=self.root)
        self.cleanup(task_id, delete_branch=True)
        return {'merged': bool(self.policy.merge_approved), 'committed': committed, 'branch': branch}

    def cleanup(self, task_id: str, *, delete_branch: bool) -> None:
        if not self.enabled:
            return
        branch = self._branch(task_id)
        path = self._path(task_id)
        if path.exists():
            self._git('worktree', 'remove', '--force', str(path), cwd=self.root, check=False)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        self._git('worktree', 'prune', cwd=self.root, check=False)
        if delete_branch and self._branch_exists(branch):
            self._git('branch', '-D', branch, cwd=self.root, check=False)

    def discard(self, task_id: str) -> None:
        if self.enabled and self.policy.discard_rejected:
            self.cleanup(task_id, delete_branch=True)
