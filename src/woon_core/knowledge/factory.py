"""Composition root for canonical knowledge ports and adapters."""

from __future__ import annotations

import os
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.config import KnowledgeSettings
from woon_core.knowledge.service import KnowledgeService
from woon_core.registry import Registry
from woon_core.workspace import discover


def build_knowledge_service(
    vault: Path | None = None,
) -> tuple[KnowledgeSettings, KnowledgeService]:
    """Resolve configuration once and connect replaceable local adapters."""

    settings = KnowledgeSettings.load(vault or resolve_knowledge_vault())
    if settings.search_adapter != "sqlite-fts":
        raise WoonError(f"unsupported search adapter: {settings.search_adapter!r}")
    repository = MarkdownDocumentRepository(settings.vault, settings.canonical_root)
    index = SQLiteFtsSearchIndex(settings.search_database)
    history = GitKnowledgeHistory(settings.vault)
    return settings, KnowledgeService(repository, index, history)


def resolve_knowledge_vault() -> Path:
    """Resolve an explicit environment path or the registered Woon knowledge repository."""

    explicit = os.environ.get("WOON_KNOWLEDGE_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    workspace = discover("")
    registry = Registry.load(workspace.root)
    return registry.resolve(workspace.root, "knowledge")
