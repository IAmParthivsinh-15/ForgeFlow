"""Read-only, path-sandboxed repository inspection.

These are deterministic operations. Agents call them through thin
`function_tool` wrappers; the agent never constructs filesystem paths that can
escape the repository root, and never sees secret-bearing files.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

from forgeflow.core.errors import PolicyViolation

IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".next",
        ".turbo",
        "coverage",
        ".idea",
        ".vscode",
        "target",
        ".gradle",
    }
)
SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*.keystore",
    "credentials*.json",
    "secrets.*",
    "*.kubeconfig",
    "kubeconfig",
)
MAX_READ_BYTES = 200_000
MAX_FILE_SCAN_BYTES = 1_000_000
MAX_FILES_SCANNED = 5_000


def is_secret_file(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env.example":
        return False
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SECRET_PATTERNS)


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    text: str


class RepositorySandbox:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            raise PolicyViolation(f"repository root does not exist: {root}")

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative.strip().lstrip("/\\")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PolicyViolation(f"path escapes repository root: {relative}")
        parts = candidate.relative_to(self.root).parts
        if any(part in IGNORED_DIRS for part in parts):
            raise PolicyViolation(f"path is in an excluded directory: {relative}")
        if candidate.is_file() and is_secret_file(candidate.name):
            raise PolicyViolation(f"access to secret-bearing files is denied: {relative}")
        return candidate

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _walk(self, start: Path, max_depth: int | None = None):
        start_depth = len(start.relative_to(self.root).parts)
        for dirpath, dirnames, filenames in os.walk(start):
            current = Path(dirpath)
            depth = len(current.relative_to(self.root).parts) - start_depth
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []
            for name in sorted(filenames):
                file_path = current / name
                if is_secret_file(name):
                    continue
                if file_path.is_symlink() and self.root not in file_path.resolve().parents:
                    continue
                yield file_path, depth

    def list_files(self, path: str = ".", max_depth: int = 3, limit: int = 300) -> list[str]:
        start = self.resolve(path)
        if not start.is_dir():
            raise PolicyViolation(f"not a directory: {path}")
        results: list[str] = []
        for file_path, _ in self._walk(start, max_depth=max(1, min(max_depth, 8))):
            results.append(self._rel(file_path))
            if len(results) >= limit:
                results.append(f"... truncated at {limit} entries")
                break
        return results

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise PolicyViolation(f"not a file: {path}")
        data = target.read_bytes()[:MAX_READ_BYTES]
        if b"\x00" in data[:8192]:
            return f"[binary file: {path}]"
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), start - 1 + max(1, min(max_lines, 2000)))
        numbered = [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)]
        footer = f"\n[lines {start}-{end} of {len(lines)}]" if lines else "[empty file]"
        return "\n".join(numbered) + footer

    def search_code(
        self, query: str, glob: str | None = None, is_regex: bool = False, limit: int = 50
    ) -> list[SearchMatch]:
        if not query:
            raise PolicyViolation("search query must not be empty")
        try:
            pattern = re.compile(query if is_regex else re.escape(query), re.IGNORECASE)
        except re.error as exc:
            raise PolicyViolation(f"invalid regular expression: {exc}") from exc
        matches: list[SearchMatch] = []
        for scanned, (file_path, _) in enumerate(self._walk(self.root)):
            if scanned >= MAX_FILES_SCANNED or len(matches) >= limit:
                break
            rel = self._rel(file_path)
            if (
                glob
                and not fnmatch.fnmatch(rel, glob)
                and not fnmatch.fnmatch(file_path.name, glob)
            ):
                continue
            try:
                if file_path.stat().st_size > MAX_FILE_SCAN_BYTES:
                    continue
                data = file_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    matches.append(SearchMatch(rel, lineno, line.strip()[:240]))
                    if len(matches) >= limit:
                        break
        return matches
