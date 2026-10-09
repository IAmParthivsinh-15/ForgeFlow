"""Skill registry (spec sections 212-222, 232-234).

- Versions are immutable; a change is a new, higher version (reproducibility).
- PRIVATE skills are usable only by their owner. PUBLIC skills are discoverable and
  usable by everyone, but only the owner can change them; others fork.
- Enabling a skill records an installation pinned to a version, optionally for one
  project. Rolling back = enabling an older version.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from forgeflow.core.errors import NotFoundError, ValidationFailed
from forgeflow.core.ids import new_id, utcnow
from forgeflow.extensibility.skills.package import parse_package
from forgeflow.extensibility.skills.validation import validate_skill
from forgeflow.extensibility.store import DocumentStore
from forgeflow.schemas.extensibility import (
    AGENT_TYPES,
    Skill,
    SkillInstallation,
    SkillMetadata,
    SkillVersion,
)

# Owner of the skills ForgeFlow ships (config/skills); they are public and verified.
BUILTIN_OWNER = "system"


def _semver(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def checksum(metadata: SkillMetadata, instructions: str, files: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "metadata": metadata.model_dump(mode="json"),
            "skill.md": instructions,
            "files": dict(sorted(files.items())),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class SkillService:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    # ---------------------------------------------------------------- create

    async def create(
        self,
        owner_id: str,
        metadata: SkillMetadata,
        instructions: str,
        files: dict[str, str] | None = None,
    ) -> tuple[Skill, SkillVersion]:
        files = files or {}
        if await self._find_slug(owner_id, metadata.slug):
            raise ValidationFailed(f"you already have a skill '{metadata.slug}'; add a version")
        version = self._version(new_id("skill"), metadata, instructions, files, public=False)
        now = utcnow()
        skill = Skill(
            skill_id=version.skill_id,
            owner_id=owner_id,
            slug=metadata.slug,
            name=metadata.name,
            description=metadata.description,
            tags=metadata.tags,
            status="active",
            latest_version=metadata.version,
            created_at=now,
            updated_at=now,
        )
        await self.store.put("skill_versions", version)
        await self.store.put("skills", skill)
        return skill, version

    async def upload(self, owner_id: str, data: bytes) -> tuple[Skill, SkillVersion]:
        metadata, instructions, files = parse_package(data)
        existing = await self._find_slug(owner_id, metadata.slug)
        if existing:
            version = await self.add_version(
                owner_id, existing.skill_id, metadata, instructions, files
            )
            return await self.get(owner_id, existing.skill_id), version
        return await self.create(owner_id, metadata, instructions, files)

    async def add_version(
        self,
        owner_id: str,
        skill_id: str,
        metadata: SkillMetadata,
        instructions: str,
        files: dict[str, str] | None = None,
    ) -> SkillVersion:
        skill = await self._owned(owner_id, skill_id)
        if metadata.slug != skill.slug:
            raise ValidationFailed("the slug of a skill cannot change")
        if _semver(metadata.version) <= _semver(skill.latest_version):
            raise ValidationFailed(
                f"version {metadata.version} must be greater than {skill.latest_version}; "
                "published versions are immutable"
            )
        version = self._version(
            skill_id, metadata, instructions, files or {}, public=skill.visibility == "public"
        )
        await self.store.put("skill_versions", version)
        skill.latest_version = metadata.version
        skill.name, skill.description, skill.tags = (
            metadata.name,
            metadata.description,
            metadata.tags,
        )
        skill.updated_at = utcnow()
        await self.store.put("skills", skill)
        return version

    def _version(
        self,
        skill_id: str,
        metadata: SkillMetadata,
        instructions: str,
        files: dict[str, str],
        public: bool,
    ) -> SkillVersion:
        report = validate_skill(metadata, instructions, files, public=public)
        if not report.passed:
            raise ValidationFailed("skill rejected: " + "; ".join(report.errors))
        return SkillVersion(
            skill_version_id=f"{skill_id}@{metadata.version}",
            skill_id=skill_id,
            version=metadata.version,
            metadata=metadata,
            instructions=instructions,
            files=files,
            checksum=checksum(metadata, instructions, files),
            size_bytes=len(instructions.encode()) + sum(len(t.encode()) for t in files.values()),
            validation=report,
            created_at=utcnow(),
        )

    # ------------------------------------------------------------- lifecycle

    async def publish(self, owner_id: str, skill_id: str) -> Skill:
        """PRIVATE -> PUBLIC after stricter validation (spec section 218)."""
        skill = await self._owned(owner_id, skill_id)
        version = await self.get_version(owner_id, skill_id, skill.latest_version)
        report = validate_skill(version.metadata, version.instructions, version.files, public=True)
        if not report.passed:
            raise ValidationFailed("cannot publish: " + "; ".join(report.errors))
        skill.visibility, skill.status, skill.trust = "public", "published", "unverified"
        skill.updated_at = utcnow()
        await self.store.put("skills", skill)
        return skill

    async def ensure_builtin(self, root: Path) -> list[Skill]:
        """Seed ForgeFlow's own skills (config/skills/<slug>/) as verified public skills.

        Idempotent: a version is added only when the file's version is new. They pass the
        same public validation as any user skill.
        """
        seeded: list[Skill] = []
        if not root.is_dir():
            return seeded
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            meta_file, body_file = folder / "metadata.json", folder / "skill.md"
            if not (meta_file.is_file() and body_file.is_file()):
                continue
            metadata = SkillMetadata.model_validate_json(meta_file.read_text(encoding="utf-8"))
            instructions = body_file.read_text(encoding="utf-8")
            existing = await self._find_slug(BUILTIN_OWNER, metadata.slug)
            if existing is None:
                skill, _ = await self.create(BUILTIN_OWNER, metadata, instructions)
            elif _semver(metadata.version) > _semver(existing.latest_version):
                await self.add_version(BUILTIN_OWNER, existing.skill_id, metadata, instructions)
                skill = await self._owned(BUILTIN_OWNER, existing.skill_id)
            else:
                skill = existing
            report = validate_skill(metadata, instructions, {}, public=True)
            if not report.passed:
                raise ValidationFailed(f"built-in skill {metadata.slug}: {report.errors}")
            skill.visibility, skill.status, skill.trust = "public", "published", "verified"
            await self.store.put("skills", skill)
            seeded.append(skill)
        return seeded

    async def find_builtin(self, slug: str) -> Skill | None:
        return await self._find_slug(BUILTIN_OWNER, slug)

    async def set_status(self, owner_id: str, skill_id: str, status: str) -> Skill:
        if status not in ("deprecated", "archived", "active"):
            raise ValidationFailed("status must be active, deprecated or archived")
        skill = await self._owned(owner_id, skill_id)
        if status == "active" and skill.visibility == "public":
            status = "published"
        skill.status = status  # type: ignore[assignment]
        skill.updated_at = utcnow()
        await self.store.put("skills", skill)
        return skill

    async def fork(self, owner_id: str, skill_id: str, version: str | None = None) -> Skill:
        source = await self.get(owner_id, skill_id)
        src_version = await self.get_version(owner_id, skill_id, version or source.latest_version)
        slug = source.slug
        while await self._find_slug(owner_id, slug):
            slug = f"{slug}-fork"[:64]
        metadata = src_version.metadata.model_copy(update={"slug": slug, "version": "1.0.0"})
        skill, _ = await self.create(
            owner_id, metadata, src_version.instructions, dict(src_version.files)
        )
        skill.forked_from = f"{source.skill_id}@{src_version.version}"
        await self.store.put("skills", skill)
        return skill

    # ----------------------------------------------------------- installation

    async def enable(
        self,
        owner_id: str,
        skill_id: str,
        version: str | None = None,
        project_id: str | None = None,
        agents: list[str] | None = None,
    ) -> SkillInstallation:
        skill = await self.get(owner_id, skill_id)
        if skill.status in ("archived",):
            raise ValidationFailed("an archived skill cannot be enabled")
        pinned = version or skill.latest_version
        await self.get_version(owner_id, skill_id, pinned)
        bad = sorted(set(agents or []) - set(AGENT_TYPES))
        if bad:
            raise ValidationFailed(f"unknown agents: {bad}")
        installation = SkillInstallation(
            installation_id=f"inst_{owner_id}_{skill_id}_{project_id or 'all'}",
            skill_id=skill_id,
            version=pinned,
            user_id=owner_id,
            project_id=project_id,
            enabled=True,
            enabled_agents=agents or [],
            installed_at=utcnow(),
        )
        await self.store.put("skill_installations", installation)
        return installation

    async def disable(self, owner_id: str, skill_id: str, project_id: str | None = None) -> None:
        doc = await self.store.get(
            "skill_installations", f"inst_{owner_id}_{skill_id}_{project_id or 'all'}"
        )
        if doc is None:
            raise NotFoundError("the skill is not enabled here")
        doc["enabled"] = False
        await self.store.put("skill_installations", doc)

    async def installations(self, owner_id: str) -> list[SkillInstallation]:
        docs = await self.store.find("skill_installations", {"user_id": owner_id})
        return [SkillInstallation.model_validate(d) for d in docs]

    # ------------------------------------------------------------------ reads

    async def get(self, owner_id: str, skill_id: str) -> Skill:
        doc = await self.store.get("skills", skill_id)
        if doc is None or not self._visible(doc, owner_id):
            raise NotFoundError(f"skill {skill_id} not found")
        return Skill.model_validate(doc)

    async def get_version(self, owner_id: str, skill_id: str, version: str) -> SkillVersion:
        await self.get(owner_id, skill_id)
        doc = await self.store.get("skill_versions", f"{skill_id}@{version}")
        if doc is None:
            raise NotFoundError(f"version {version} of {skill_id} not found")
        return SkillVersion.model_validate(doc)

    async def versions(self, owner_id: str, skill_id: str) -> list[SkillVersion]:
        await self.get(owner_id, skill_id)
        docs = await self.store.find("skill_versions", {"skill_id": skill_id})
        versions = [SkillVersion.model_validate(d) for d in docs]
        return sorted(versions, key=lambda v: _semver(v.version), reverse=True)

    async def search(self, owner_id: str, scope: str = "all", query: str = "") -> list[Skill]:
        docs = await self.store.find("skills", {}, sort="name")
        skills = [Skill.model_validate(d) for d in docs if self._visible(d, owner_id)]
        if scope == "mine":
            skills = [s for s in skills if s.owner_id == owner_id]
        elif scope == "public":
            skills = [s for s in skills if s.visibility == "public"]
        if query:
            q = query.lower()
            skills = [
                s for s in skills if q in f"{s.name} {s.description} {' '.join(s.tags)}".lower()
            ]
        return [s for s in skills if s.status != "archived" or s.owner_id == owner_id]

    @staticmethod
    def _visible(doc: dict[str, Any], owner_id: str) -> bool:
        """Server-side ownership check (spec section 240)."""
        return doc["owner_id"] == owner_id or (
            doc["visibility"] == "public" and doc["status"] in ("published", "deprecated")
        )

    async def _owned(self, owner_id: str, skill_id: str) -> Skill:
        doc = await self.store.get("skills", skill_id)
        if doc is None or doc["owner_id"] != owner_id:
            raise NotFoundError(f"skill {skill_id} not found")
        return Skill.model_validate(doc)

    async def _find_slug(self, owner_id: str, slug: str) -> Skill | None:
        docs = await self.store.find("skills", {"owner_id": owner_id, "slug": slug}, limit=1)
        return Skill.model_validate(docs[0]) if docs else None
