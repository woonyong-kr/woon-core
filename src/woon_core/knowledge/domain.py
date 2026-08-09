"""Domain objects for the single-canonical-document knowledge model."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Stable identity and learning relationships for one canonical document."""

    canonical_id: str
    title: str
    domain: str
    summary: str
    difficulty: str = "foundation"
    prerequisites: tuple[str, ...] = field(default_factory=tuple)
    next_concepts: tuple[str, ...] = field(default_factory=tuple)
    related: tuple[str, ...] = field(default_factory=tuple)
    source_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CanonicalDocument:
    """One human-readable source of truth for a topic."""

    metadata: DocumentMetadata
    body: str
    relative_path: str
    revision: str


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    """Read-only document representation used by replaceable search indexes."""

    document_id: str
    canonical_id: str | None
    title: str
    summary: str
    body: str
    relative_path: str
    revision: str
    source_type: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Search hit returned independently of a concrete index implementation."""

    document_id: str
    canonical_id: str | None
    title: str
    summary: str
    relative_path: str
    revision: str
    source_type: str
    chunk_id: str
    heading: str
    score: float
    snippet: str


@dataclass(frozen=True, slots=True)
class KnowledgeExcerpt:
    """Bounded section returned after a search hit is selected."""

    document_id: str
    relative_path: str
    revision: str
    source_type: str
    chunk_id: str
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class IndexStatistics:
    """Size measurements used to verify bounded retrieval behavior."""

    documents: int
    chunks: int
    total_chars: int
    max_chunk_chars: int


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Result of an optimistic canonical document write."""

    document: CanonicalDocument
    created: bool
    changed: bool


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """Git-backed recovery point for a canonical document."""

    revision: str
    authored_at: str
    subject: str
