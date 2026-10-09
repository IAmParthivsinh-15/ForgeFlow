"""Load and fingerprint the decision contract (additional.md section 2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from forgeflow.core.errors import ValidationFailed
from forgeflow.schemas.autonomy import DecisionContract


def contract_hash(contract: DecisionContract) -> str:
    payload = contract.model_dump(mode="json", exclude={"hash"})
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"sha256:{digest[:32]}"


def load_contract(path: Path) -> DecisionContract:
    if not path.is_file():
        raise ValidationFailed(f"decision contract not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        contract = DecisionContract.model_validate(data)
    except (yaml.YAMLError, ValueError) as exc:
        raise ValidationFailed(f"invalid decision contract {path}: {exc}") from exc
    contract.hash = contract_hash(contract)
    return contract
