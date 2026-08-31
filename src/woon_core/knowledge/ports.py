"""Replaceable ports used by the knowledge application service."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Protocol

from woon_core.knowledge.domain import (
    CanonicalDocument,
    DocumentMetadata,
    HistoryEntry,
    IndexedDocument,
    IndexStatistics,
    KnowledgeExcerpt,
    SaveResult,
    SearchResult,
)


class CanonicalDocumentRepository(Protocol):
    """Persistence port for canonical Markdown documents."""

    def get(self, canonical_id: str) -> CanonicalDocument | None: ...

    def list_documents(self) -> Iterable[CanonicalDocument]: ...

    def state_token(self) -> tuple[tuple[str, int, int, int, int], ...]: ...

    def save(
        self,
        metadata: DocumentMetadata,
        body: str,
        expected_revision: str | None,
    ) -> SaveResult: ...

    def save_body(
        self,
        canonical_id: str,
        body: str,
        expected_revision: str,
    ) -> SaveResult: ...

    def validate(self) -> list[str]: ...

    def parse(self, relative_path: str, text: str) -> CanonicalDocument: ...

    def exclusive(self) -> AbstractContextManager[None]: ...

    def snapshot(self, canonical_id: str) -> bytes | None: ...

    def restore_snapshot(self, canonical_id: str, snapshot: bytes | None) -> None: ...


class KnowledgeSearchIndex(Protocol):
    """Index port; FTS and vector implementations can be exchanged."""

    def rebuild(self, documents: Iterable[IndexedDocument]) -> int: ...

    def search(self, query: str, limit: int) -> list[SearchResult]: ...

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt: ...

    def statistics(self) -> IndexStatistics: ...

    def generation(self) -> str | None: ...


class ReadOnlyKnowledgeCorpus(Protocol):
    """Read-only source documents that may be searched but never overwritten."""

    def list_documents(self) -> Iterable[IndexedDocument]: ...

    def state_token(self) -> tuple[tuple[str, int, int, int, int], ...]: ...


class KnowledgeHistory(Protocol):
    """Version-history port used for recovery without coupling to Git."""

    def list(self, relative_path: str, limit: int) -> list[HistoryEntry]: ...

    def read(self, relative_path: str, revision: str) -> str: ...
