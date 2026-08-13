"""Composition root for canonical knowledge ports and adapters."""

from __future__ import annotations

import os
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    CorpusRoot,
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    MarkdownKnowledgeCorpus,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import CompiledWiki
from woon_core.knowledge.config import KnowledgeSettings
from woon_core.knowledge.service import KnowledgeService
from woon_core.registry import Registry
from woon_core.workspace import discover


def build_knowledge_service(
    vault: Path | None = None,
) -> tuple[KnowledgeSettings, KnowledgeService]:
    """Resolve configuration once and connect replaceable local adapters."""

    settings = KnowledgeSettings.load(
        vault or resolve_knowledge_vault(),
        repository_resolver=_resolve_repository_reference,
    )
    if settings.search_adapter != "sqlite-fts":
        raise WoonError(f"unsupported search adapter: {settings.search_adapter!r}")
    repository = MarkdownDocumentRepository(
        settings.vault,
        settings.canonical_root,
        settings.runtime_root / "mutation.lock",
    )
    index = SQLiteFtsSearchIndex(settings.search_database, settings.max_chunk_chars)
    history = GitKnowledgeHistory(settings.vault)
    corpus = MarkdownKnowledgeCorpus(
        settings.vault,
        tuple(CorpusRoot(root.path, root.source_type) for root in settings.search_roots),
        settings.search_exclusions,
    )
    compiled_wiki = CompiledWiki(settings.compiled_wiki) if settings.compiled_wiki else None
    return settings, KnowledgeService(
        repository, index, history, corpus, compiled_wiki=compiled_wiki
    )


def resolve_knowledge_vault() -> Path:
    """Resolve an explicit environment path or the registered Woon knowledge repository."""

    explicit = os.environ.get("WOON_KNOWLEDGE_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    workspace = discover("")
    registry = Registry.load(workspace.root)
    return registry.resolve(workspace.root, "knowledge")


def _resolve_repository_reference(reference: str) -> Path:
    workspace = discover("")
    registry = Registry.load(workspace.root)
    return registry.resolve(workspace.root, reference)
