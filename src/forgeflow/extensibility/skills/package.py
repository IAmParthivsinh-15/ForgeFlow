"""Skill package (.zip) parsing, safely, in memory (spec sections 214, 251, 252).

    my-skill/                 (an optional single top-level folder is stripped)
    ├── skill.md              required
    ├── metadata.json         required
    ├── examples/*.md
    ├── references/*.md
    └── assets/*.{md,txt,json,yaml}

Nothing is ever extracted to disk or executed.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile

from pydantic import ValidationError

from forgeflow.core.errors import ValidationFailed
from forgeflow.extensibility.skills.validation import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PACKAGE_BYTES,
    safe_path,
)
from forgeflow.schemas.extensibility import SkillMetadata

MAX_COMPRESSION_RATIO = 50


def parse_package(data: bytes) -> tuple[SkillMetadata, str, dict[str, str]]:
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValidationFailed(f"package larger than {MAX_PACKAGE_BYTES} bytes")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("the upload is not a valid .zip file") from exc

    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > MAX_FILES + 2:
        raise ValidationFailed(f"too many files in the package (max {MAX_FILES})")
    total = 0
    for info in entries:
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValidationFailed(f"{info.filename}: symbolic links are not allowed")
        if info.file_size > MAX_FILE_BYTES:
            raise ValidationFailed(f"{info.filename}: larger than {MAX_FILE_BYTES} bytes")
        if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
            raise ValidationFailed(f"{info.filename}: suspicious compression ratio")
        total += info.file_size
    if total > MAX_PACKAGE_BYTES * 2:
        raise ValidationFailed("package too large when extracted")

    names = [i.filename for i in entries]
    roots = {n.split("/", 1)[0] for n in names}
    prefix = ""
    if len(roots) == 1 and all("/" in n for n in names):
        prefix = next(iter(roots)) + "/"

    contents: dict[str, str] = {}
    for info in entries:
        rel = info.filename[len(prefix) :]
        if rel in ("skill.md", "metadata.json"):
            clean: str | None = rel
        else:
            clean = safe_path(rel)
        if clean is None:
            raise ValidationFailed(f"{info.filename}: path or file type not allowed")
        raw = archive.read(info)
        try:
            contents[clean] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed(f"{info.filename}: must be UTF-8 text") from exc

    if "skill.md" not in contents or "metadata.json" not in contents:
        raise ValidationFailed("a skill package needs skill.md and metadata.json")
    try:
        metadata = SkillMetadata.model_validate(json.loads(contents.pop("metadata.json")))
    except (ValueError, ValidationError) as exc:
        raise ValidationFailed(f"metadata.json is invalid: {exc}") from exc
    instructions = contents.pop("skill.md")
    return metadata, instructions, contents
