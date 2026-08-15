"""Workspace-local configuration for canonical knowledge services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.compiled_wiki import CompiledWikiSettings


@dataclass(frozen=True, slots=True)
class SearchRootSettings:
    """One read-only directory included in the local knowledge index."""

    path: Path
    source_type: str


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    """Resolved paths and adapter selections for one private knowledge vault."""

    vault: Path
    canonical_root: Path
    runtime_root: Path
    search_adapter: str
    search_database: Path
    search_roots: tuple[SearchRootSettings, ...]
    search_exclusions: tuple[str, ...]
    max_chunk_chars: int
    style_guide: Path
    diagram_guide: Path
    compiled_wiki: CompiledWikiSettings | None

    @classmethod
    def load(
        cls,
        vault: Path,
        repository_resolver: Callable[[str], Path] | None = None,
    ) -> KnowledgeSettings:
        resolved_vault = vault.expanduser().resolve()
        config_path = resolved_vault / "config/canonical-knowledge.yaml"
        if not config_path.is_file():
            raise WoonError(f"knowledge configuration not found: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise WoonError(f"knowledge configuration must be a mapping: {config_path}")
        version = raw.get("version")
        if version not in {1, 2}:
            raise WoonError(f"unsupported knowledge configuration version: {version!r}")
        canonical = _mapping(raw, "canonical")
        search = _mapping(raw, "search")
        style = _mapping(raw, "style")
        canonical_root = _inside(resolved_vault, canonical.get("root"), "canonical.root")
        runtime_root = _inside(resolved_vault, raw.get("runtime_root"), "runtime_root")
        search_database = _inside(resolved_vault, search.get("database"), "search.database")
        search_roots = tuple(
            SearchRootSettings(
                path=_inside(
                    resolved_vault,
                    _object_mapping(item, "search.roots entry").get("path"),
                    "search.roots.path",
                ),
                source_type=_source_type(_object_mapping(item, "search.roots entry").get("type")),
            )
            for item in _list(search.get("roots", []), "search.roots")
        )
        search_exclusions = tuple(
            _relative_pattern(value) for value in _list(search.get("exclude", []), "search.exclude")
        )
        max_chunk_chars = search.get("max_chunk_chars", 6000)
        if not isinstance(max_chunk_chars, int) or isinstance(max_chunk_chars, bool):
            raise WoonError("knowledge configuration search.max_chunk_chars must be an integer")
        if max_chunk_chars < 1000 or max_chunk_chars > 20000:
            raise WoonError(
                "knowledge configuration search.max_chunk_chars must be between 1000 and 20000"
            )
        compiled_wiki = (
            _compiled_wiki_settings(resolved_vault, _mapping(raw, "compiled_wiki"))
            if version == 2
            else None
        )
        return cls(
            vault=resolved_vault,
            canonical_root=canonical_root,
            runtime_root=runtime_root,
            search_adapter=str(search.get("adapter", "sqlite-fts")),
            search_database=search_database,
            search_roots=search_roots,
            search_exclusions=search_exclusions,
            max_chunk_chars=max_chunk_chars,
            style_guide=_guide(
                resolved_vault,
                style.get("document_guide"),
                "style.document_guide",
                repository_resolver,
            ),
            diagram_guide=_guide(
                resolved_vault,
                style.get("diagram_guide"),
                "style.diagram_guide",
                repository_resolver,
            ),
            compiled_wiki=compiled_wiki,
        )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise WoonError(f"knowledge configuration {key!r} must be a mapping")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WoonError(f"knowledge configuration {field!r} must be a list")
    return value


def _object_mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"knowledge configuration {field!r} must be a mapping")
    return value


def _source_type(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError("knowledge configuration search.roots.type must be a non-empty string")
    return value.strip()


def _relative_pattern(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise WoonError("knowledge configuration search.exclude entries must be relative patterns")
    if ".." in Path(value).parts:
        raise WoonError("knowledge configuration search.exclude must not escape the vault")
    return value.strip()


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


def _compiled_wiki_settings(vault: Path, raw: dict[str, Any]) -> CompiledWikiSettings:
    """Resolve a compiler layout without allowing catalog paths outside the vault."""

    output_root = _inside(vault, raw.get("output_root"), "compiled_wiki.output_root")
    return CompiledWikiSettings(
        vault=vault,
        output_root=output_root,
        sources_path=_inside(vault, raw.get("sources"), "compiled_wiki.sources"),
        claims_path=_inside(vault, raw.get("claims"), "compiled_wiki.claims"),
        pages_path=_inside(vault, raw.get("pages"), "compiled_wiki.pages"),
        curation_path=_inside(vault, raw.get("curation"), "compiled_wiki.curation"),
        relations_path=_inside(vault, raw.get("relations"), "compiled_wiki.relations"),
        receipts_path=_inside(vault, raw.get("receipts"), "compiled_wiki.receipts"),
        review_queue_path=_inside(vault, raw.get("review_queue"), "compiled_wiki.review_queue"),
    )


def _guide(
    vault: Path,
    raw: object,
    field: str,
    repository_resolver: Callable[[str], Path] | None,
) -> Path:
    if isinstance(raw, str) and raw.startswith("repo://"):
        if repository_resolver is None:
            raise WoonError(f"knowledge configuration {field!r} requires a repository resolver")
        resolved = repository_resolver(raw).resolve()
    else:
        resolved = _inside(vault, raw, field)
    if not resolved.exists():
        raise WoonError(f"knowledge configuration {field!r} target does not exist: {resolved}")
    return resolved
