"""Replaceable ports used by the knowledge application service."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from woon_core.knowledge.domain import (
    CanonicalDocument,
    DocumentMetadata,
    HistoryEntry,
    SaveResult,
    SearchResult,
)


class CanonicalDocumentRepository(Protocol):
    """Persistence port for canonical Markdown documents."""

    def get(self, canonical_id: str) -> CanonicalDocument | None: ...

    def list_documents(self) -> Iterable[CanonicalDocument]: ...

    def save(
        self,
        metadata: DocumentMetadata,
        body: str,
        expected_revision: str | None,
    ) -> SaveResult: ...

    def validate(self) -> list[str]: ...

    def parse(self, relative_path: str, text: str) -> CanonicalDocument: ...


class KnowledgeSearchIndex(Protocol):
    """Index port; FTS and vector implementations can be exchanged."""

    def rebuild(self, documents: Iterable[CanonicalDocument]) -> int: ...

    def search(self, query: str, limit: int) -> list[SearchResult]: ...


class KnowledgeHistory(Protocol):
    """Version-history port used for recovery without coupling to Git."""

    def list(self, relative_path: str, limit: int) -> list[HistoryEntry]: ...

    def read(self, relative_path: str, revision: str) -> str: ...
