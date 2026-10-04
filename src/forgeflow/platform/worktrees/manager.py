"""Git Worktree Manager (spec sections 21, 22, 184).

One worktree + branch per code-writing task:

    path:   <WORKSPACES_ROOT>/<workflow_id>/<task_key>
    branch: forgeflow/<workflow_id>/<task_key>

ForgeFlow only ever creates, commits to, and deletes `forgeflow/*` branches. It
never touches the repository's own branches and never pushes.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from forgeflow.core.errors import GitError, ValidationFailed
from forgeflow.core.ids import utcnow
from forgeflow.schemas.task import Workspace
from forgeflow.tools.git.client import GitClient

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BRANCH_PREFIX = "forgeflow"
DETACHED = "(detached)"

LockFactory = Callable[[str], AbstractAsyncContextManager[None]]


def local_locks() -> LockFactory:
    """In-process locks; the workers use Redis locks instead."""
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def factory(name: str) -> AsyncIterator[None]:
        lock = locks.setdefault(name, asyncio.Lock())
        async with lock:
            yield

    return factory


def _segment(value: str, what: str) -> str:
    if not _SEGMENT_RE.match(value):
        raise ValidationFailed(f"invalid {what}: {value!r}")
    return value


@dataclass
class PreparedWorkspace:
    workspace: Workspace
    path: Path
    merged: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class WorktreeManager:
    def __init__(
        self,
        git: GitClient,
        repos_root: Path,
        workspaces_root: Path,
        locks: LockFactory | None = None,
    ) -> None:
        self.git = git
        self.repos_root = repos_root.resolve()
        self.workspaces_root = workspaces_root.resolve()
        self.locks = locks or local_locks()

    def repository(self, repository_path: str) -> Path:
        repo = (self.repos_root / repository_path).resolve()
        if self.repos_root not in repo.parents:
            raise ValidationFailed("repository must be inside REPOS_ROOT")
        return repo

    def branch_name(self, workflow_id: str, key: str) -> str:
        return f"{BRANCH_PREFIX}/{_segment(workflow_id, 'workflow id')}/{_segment(key, 'task key')}"

    def path_for(self, workflow_id: str, key: str) -> Path:
        return (
            self.workspaces_root / _segment(workflow_id, "workflow id") / _segment(key, "task key")
        )

    async def create(
        self,
        *,
        workflow_id: str,
        task_id: str,
        key: str,
        repository_path: str,
        base_commit: str,
        merge_commits: list[tuple[str, str]] | None = None,
        detach: bool = False,
    ) -> PreparedWorkspace:
        """Create a fresh worktree from `base_commit`, then merge predecessor commits.

        `merge_commits` are (label, sha) pairs of completed upstream tasks, so a
        dependent task builds on their code. A predecessor that does not merge
        cleanly is skipped with a warning; integration resolves it later.
        """
        repo = self.repository(repository_path)
        branch = self.branch_name(workflow_id, key)
        path = self.path_for(workflow_id, key)
        async with self.locks(f"repository:{repository_path}"):
            await self._discard(repo, path, branch)  # retries start from a clean slate
            path.parent.mkdir(parents=True, exist_ok=True)
            if detach:
                # Read-only verification checkouts need no branch of their own.
                await self.git.add_detached_worktree(repo, path, base_commit)
                branch = DETACHED
            else:
                await self.git.add_worktree(repo, path, branch, base_commit)

        prepared = PreparedWorkspace(
            workspace=Workspace(
                workspace_id=f"ws_{workflow_id.removeprefix('wf_')}_{key}",
                workflow_id=workflow_id,
                task_id=task_id,
                repository_path=repository_path,
                path=str(path),
                branch=branch,
                base_commit=base_commit,
                created_at=utcnow(),
            ),
            path=path,
        )
        for label, sha in merge_commits or []:
            conflicts = await self.git.merge(path, sha, f"forgeflow: bring in {label}")
            if conflicts:
                await self.git.abort_merge(path)
                prepared.warnings.append(
                    f"could not pre-merge {label} ({', '.join(conflicts)}); "
                    "integration will reconcile it"
                )
            else:
                prepared.merged.append(label)
        return prepared

    async def remove(self, workspace: Workspace, delete_branch: bool = False) -> None:
        repo = self.repository(workspace.repository_path)
        async with self.locks(f"repository:{workspace.repository_path}"):
            await self.git.remove_worktree(repo, Path(workspace.path))
            if Path(workspace.path).exists():
                shutil.rmtree(workspace.path, ignore_errors=True)
            if delete_branch and workspace.branch != DETACHED:
                await self.git.delete_branch(repo, workspace.branch)

    async def _discard(self, repo: Path, path: Path, branch: str) -> None:
        if path.exists():
            await self.git.remove_worktree(repo, path)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        await self.git.run(repo, "worktree", "prune", check=False)
        if await self.git.branch_exists(repo, branch):
            await self.git.delete_branch(repo, branch)
            if await self.git.branch_exists(repo, branch):
                raise GitError(f"could not reset existing branch {branch}")
