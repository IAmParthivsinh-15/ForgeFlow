"""Resolves workflow repository paths inside the configured REPOS_ROOT."""

from __future__ import annotations

from pathlib import Path

from forgeflow.core.errors import PolicyViolation, ValidationFailed


class RepositoryResolver:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def validate(self, relative: str) -> str:
        """Return a normalised relative path, or raise if it is unsafe or missing."""
        cleaned = relative.strip().replace("\\", "/").strip("/")
        if not cleaned:
            raise ValidationFailed("repository_path must not be empty")
        target = (self.root / cleaned).resolve()
        if self.root not in target.parents:
            raise PolicyViolation("repository_path must be inside REPOS_ROOT")
        if not target.is_dir():
            raise ValidationFailed(f"repository not found under REPOS_ROOT: {cleaned}")
        return target.relative_to(self.root).as_posix()

    def resolve(self, relative: str | None) -> Path | None:
        if relative is None:
            return None
        return self.root / self.validate(relative)

    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
