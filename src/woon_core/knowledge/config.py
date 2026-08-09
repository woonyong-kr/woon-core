"""Workspace-local configuration for canonical knowledge services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    """Resolved paths and adapter selections for one private knowledge vault."""

    vault: Path
    canonical_root: Path
    runtime_root: Path
    search_adapter: str
    search_database: Path
    style_guide: Path
    diagram_guide: Path

    @classmethod
    def load(cls, vault: Path) -> KnowledgeSettings:
        resolved_vault = vault.expanduser().resolve()
        config_path = resolved_vault / "config/canonical-knowledge.yaml"
        if not config_path.is_file():
            raise WoonError(f"knowledge configuration not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise WoonError(f"knowledge configuration must be a mapping: {config_path}")
        version = raw.get("version")
        if version != 1:
            raise WoonError(f"unsupported knowledge configuration version: {version!r}")
        canonical = _mapping(raw, "canonical")
        search = _mapping(raw, "search")
        style = _mapping(raw, "style")
        canonical_root = _inside(resolved_vault, canonical.get("root"), "canonical.root")
        runtime_root = _inside(resolved_vault, raw.get("runtime_root"), "runtime_root")
        search_database = _inside(resolved_vault, search.get("database"), "search.database")
        return cls(
            vault=resolved_vault,
            canonical_root=canonical_root,
            runtime_root=runtime_root,
            search_adapter=str(search.get("adapter", "sqlite-fts")),
            search_database=search_database,
            style_guide=_inside(
                resolved_vault, style.get("document_guide"), "style.document_guide"
            ),
            diagram_guide=_inside(
                resolved_vault, style.get("diagram_guide"), "style.diagram_guide"
            ),
        )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise WoonError(f"knowledge configuration {key!r} must be a mapping")
    return value


def _inside(vault: Path, raw: object, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise WoonError(f"knowledge configuration {field!r} must be a relative path")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise WoonError(f"knowledge configuration {field!r} must not be absolute")
    resolved = (vault / candidate).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as error:
        raise WoonError(f"knowledge configuration {field!r} escapes the vault") from error
    return resolved
