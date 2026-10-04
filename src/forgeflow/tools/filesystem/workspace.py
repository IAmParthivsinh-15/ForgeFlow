"""Writable task workspace (a git worktree), confined to the task's file scope.

Reads go through RepositorySandbox rules. Writes must additionally match one of
the task's `file_scope` globs, and can never touch secrets, `.git`, or
dependency/build directories.
"""

from __future__ import annotations

from pathlib import Path

from forgeflow.core.errors import PolicyViolation
from forgeflow.tools.filesystem import globs
from forgeflow.tools.filesystem.repository import IGNORED_DIRS, RepositorySandbox, is_secret_file

MAX_WRITE_BYTES = 500_000


class WorkspaceFiles(RepositorySandbox):
    def __init__(self, root: Path, file_scope: list[str]) -> None:
        super().__init__(root)
        if not file_scope:
            raise PolicyViolation("a writable workspace requires a file scope")
        self.file_scope = [globs.normalise(p) for p in file_scope]
        self.changed: set[str] = set()

    def _writable(self, relative: str) -> tuple[Path, str]:
        target = self.resolve(relative)
        rel = target.relative_to(self.root).as_posix()
        if rel in ("", "."):
            raise PolicyViolation("cannot write the repository root")
        if any(part in IGNORED_DIRS for part in Path(rel).parts) or is_secret_file(target.name):
            raise PolicyViolation(f"writing this path is not allowed: {rel}")
        if not globs.matches_any(rel, self.file_scope):
            raise PolicyViolation(
                f"{rel} is outside this task's file scope ({', '.join(self.file_scope)})"
            )
        return target, rel

    def write_file(self, path: str, content: str) -> str:
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise PolicyViolation(f"file content exceeds {MAX_WRITE_BYTES} bytes")
        target, rel = self._writable(path)
        if target.is_dir():
            raise PolicyViolation(f"{rel} is a directory")
        existed = target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self.changed.add(rel)
        return f"{'updated' if existed else 'created'} {rel} ({len(data)} bytes)"

    def replace_in_file(self, path: str, old: str, new: str, count: int = 1) -> str:
        target, rel = self._writable(path)
        if not target.is_file():
            raise PolicyViolation(f"file does not exist: {rel}")
        if not old:
            raise PolicyViolation("old text must not be empty")
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            raise PolicyViolation(f"old text not found in {rel}; read the file and retry")
        if count > 0 and occurrences != count:
            raise PolicyViolation(
                f"old text occurs {occurrences} time(s) in {rel}, expected {count}; "
                "include more surrounding context to make it unique"
            )
        updated = text.replace(old, new) if count <= 0 else text.replace(old, new, count)
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise PolicyViolation(f"resulting file exceeds {MAX_WRITE_BYTES} bytes")
        target.write_text(updated, encoding="utf-8")
        self.changed.add(rel)
        return f"replaced {occurrences if count <= 0 else count} occurrence(s) in {rel}"

    def delete_file(self, path: str) -> str:
        target, rel = self._writable(path)
        if not target.is_file():
            raise PolicyViolation(f"file does not exist: {rel}")
        target.unlink()
        self.changed.add(rel)
        return f"deleted {rel}"
