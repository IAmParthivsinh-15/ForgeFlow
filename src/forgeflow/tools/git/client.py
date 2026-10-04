"""Deterministic git operations (spec sections 21, 51).

Commands are assembled here from validated parameters and executed without a
shell. Agents never construct git commands.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from forgeflow.core.errors import GitError, ValidationFailed

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
MAX_DIFF_BYTES = 400_000


def validate_ref(ref: str) -> str:
    if not _REF_RE.match(ref) or ".." in ref or ref.endswith((".lock", "/")) or "//" in ref:
        raise ValidationFailed(f"invalid git ref: {ref!r}")
    return ref


def validate_sha(sha: str) -> str:
    if not _SHA_RE.match(sha):
        raise ValidationFailed(f"invalid commit id: {sha!r}")
    return sha


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str


class GitClient:
    def __init__(self, author_name: str = "ForgeFlow", author_email: str = "forgeflow@localhost"):
        self.author_name = author_name
        self.author_email = author_email

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(
            GIT_AUTHOR_NAME=self.author_name,
            GIT_AUTHOR_EMAIL=self.author_email,
            GIT_COMMITTER_NAME=self.author_name,
            GIT_COMMITTER_EMAIL=self.author_email,
            GIT_TERMINAL_PROMPT="0",
        )
        return env

    async def run(
        self, cwd: Path, *args: str, check: bool = True, limit_s: float = 120
    ) -> GitResult:
        # gc.auto=0: concurrent worktrees share one object store; never gc mid-flight.
        # core.fileMode=false: bind-mounted Windows folders report every file as executable.
        cmd = [
            "git",
            "-c",
            "gc.auto=0",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.fileMode=false",
            *args,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), limit_s)
        except TimeoutError as exc:
            proc.kill()
            raise GitError(f"git {args[0]} timed out") from exc
        result = GitResult(
            proc.returncode or 0,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )
        if check and result.code != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {result.stderr.strip()[:500]}")
        return result

    # ------------------------------------------------------------- repository

    async def is_repository(self, path: Path) -> bool:
        """True only if `path` is itself the top level of a git work tree.

        A plain folder nested inside some other repository (e.g. repos/ inside the
        ForgeFlow checkout) must not count, or ForgeFlow would branch the wrong repo.
        """
        if not path.is_dir():
            return False
        res = await self.run(path, "rev-parse", "--show-toplevel", check=False)
        if res.code != 0:
            return False
        return Path(res.stdout.strip()).resolve() == path.resolve()

    async def rev_parse(self, cwd: Path, rev: str = "HEAD") -> str:
        return (await self.run(cwd, "rev-parse", "--verify", rev)).stdout.strip()

    async def discard_paths(self, worktree: Path, paths: list[str]) -> None:
        """Revert tracked paths to HEAD and delete untracked ones."""
        for rel in paths:
            tracked = await self.run(
                worktree, "ls-files", "--error-unmatch", "--", rel, check=False
            )
            if tracked.code == 0:
                await self.run(worktree, "checkout", "HEAD", "--", rel, check=False)
            else:
                target = worktree / rel
                if target.is_file() or target.is_symlink():
                    target.unlink()

    async def head(self, repo: Path) -> tuple[str, str]:
        """(commit sha, symbolic branch name or 'HEAD')."""
        sha = (await self.run(repo, "rev-parse", "HEAD")).stdout.strip()
        ref = (await self.run(repo, "rev-parse", "--abbrev-ref", "HEAD")).stdout.strip()
        return sha, ref

    # -------------------------------------------------------------- worktrees

    async def add_worktree(self, repo: Path, path: Path, branch: str, base: str) -> None:
        validate_ref(branch)
        await self.run(repo, "worktree", "add", "--quiet", "-b", branch, str(path), base)

    async def add_detached_worktree(self, repo: Path, path: Path, commit: str) -> None:
        await self.run(
            repo, "worktree", "add", "--quiet", "--detach", str(path), validate_sha(commit)
        )

    async def fast_forward(self, worktree: Path, commit: str) -> None:
        """Advance the worktree's branch to `commit`; fails unless it is a fast-forward."""
        await self.run(worktree, "merge", "--ff-only", "--quiet", validate_sha(commit))

    async def remove_worktree(self, repo: Path, path: Path) -> None:
        await self.run(repo, "worktree", "remove", "--force", str(path), check=False)
        await self.run(repo, "worktree", "prune", check=False)

    async def delete_branch(self, repo: Path, branch: str) -> None:
        await self.run(repo, "branch", "-D", validate_ref(branch), check=False)

    async def branch_exists(self, repo: Path, branch: str) -> bool:
        res = await self.run(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{validate_ref(branch)}",
            check=False,
        )
        return res.code == 0

    # ---------------------------------------------------------------- changes

    async def changed_files(self, worktree: Path) -> list[str]:
        res = await self.run(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
        files: list[str] = []
        entries = res.stdout.split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            if not entry:
                i += 1
                continue
            status, path = entry[:2], entry[3:]
            files.append(path)
            if "R" in status or "C" in status:
                i += 1  # skip the rename source
            i += 1
        return sorted(set(files))

    async def commit_all(self, worktree: Path, message: str) -> str | None:
        """Stage everything and commit. Returns the new sha, or None if nothing changed."""
        await self.run(worktree, "add", "-A")
        staged = await self.run(worktree, "diff", "--cached", "--quiet", check=False)
        if staged.code == 0:
            return None
        await self.run(worktree, "commit", "--quiet", "--no-verify", "-m", message)
        return (await self.run(worktree, "rev-parse", "HEAD")).stdout.strip()

    async def merge(self, worktree: Path, commit: str, message: str) -> list[str]:
        """Merge a commit. Returns conflicted files ([] on a clean merge)."""
        validate_sha(commit)
        res = await self.run(
            worktree, "merge", "--no-ff", "--no-edit", "-m", message, commit, check=False
        )
        if res.code == 0:
            return []
        conflicted = await self.run(worktree, "diff", "--name-only", "--diff-filter=U")
        files = [f for f in conflicted.stdout.splitlines() if f.strip()]
        if not files:
            raise GitError(f"merge of {commit[:10]} failed: {res.stderr.strip()[:500]}")
        return files

    async def abort_merge(self, worktree: Path) -> None:
        await self.run(worktree, "merge", "--abort", check=False)

    async def conclude_merge(self, worktree: Path) -> str:
        await self.run(worktree, "add", "-A")
        await self.run(worktree, "commit", "--quiet", "--no-verify", "--no-edit")
        return (await self.run(worktree, "rev-parse", "HEAD")).stdout.strip()

    async def files_between(self, repo: Path, base: str, head: str) -> list[str]:
        res = await self.run(repo, "diff", "--name-only", validate_sha(base), validate_sha(head))
        return [f for f in res.stdout.splitlines() if f.strip()]

    async def diff(self, repo: Path, base: str, head: str) -> str:
        res = await self.run(
            repo, "diff", "--no-color", "--find-renames", validate_sha(base), validate_sha(head)
        )
        text = res.stdout
        if len(text) > MAX_DIFF_BYTES:
            text = text[:MAX_DIFF_BYTES] + "\n... diff truncated ...\n"
        return text
