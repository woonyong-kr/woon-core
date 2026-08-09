"""Application services for archive, retrieval, indexing, and recovery."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from woon_core.errors import WoonError
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
from woon_core.knowledge.generation import knowledge_generation
from woon_core.knowledge.ports import (
    CanonicalDocumentRepository,
    KnowledgeHistory,
    KnowledgeSearchIndex,
    ReadOnlyKnowledgeCorpus,
)

CANONICAL_ID = re.compile(r"^[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)+$")
DIFFICULTIES = {"foundation", "intermediate", "advanced"}


class KnowledgeService:
    """Coordinates ports while preserving one canonical file per concept."""

    def __init__(
        self,
        repository: CanonicalDocumentRepository,
        index: KnowledgeSearchIndex,
        history: KnowledgeHistory,
        corpus: ReadOnlyKnowledgeCorpus | None = None,
    ) -> None:
        self._repository = repository
        self._index = index
        self._history = history
        self._corpus = corpus

    def get(self, canonical_id: str) -> CanonicalDocument:
        normalized_id = self._validate_id(canonical_id)
        document = self._repository.get(normalized_id)
        if document is None:
            raise WoonError(f"canonical document not found: {normalized_id}")
        return document

    def archive(
        self,
        metadata: DocumentMetadata,
        body: str,
        expected_revision: str | None = None,
    ) -> SaveResult:
        validated = self._validate_metadata(metadata)
        normalized_body = self._validate_body(body)
        with self._repository.exclusive():
            current = self._repository.get(validated.canonical_id)
            if current is not None and expected_revision is None:
                raise WoonError(
                    "canonical document already exists; read it first and provide expected_revision"
                )
            self._ensure_unique_identity(validated)
            snapshot = self._repository.snapshot(validated.canonical_id)
            result = self._repository.save(validated, normalized_body, expected_revision)
            if result.changed:
                self._reindex_or_restore(validated.canonical_id, snapshot)
            return result

    def reindex(self) -> int:
        with self._repository.exclusive():
            return self._reindex_unlocked()

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise WoonError("search query must not be empty")
        if limit < 1 or limit > 20:
            raise WoonError("search limit must be between 1 and 20")
        self._assert_index_current()
        return self._index.search(normalized_query, limit)

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt:
        normalized_document_id = document_id.strip()
        normalized_chunk_id = chunk_id.strip()
        if not normalized_document_id or not normalized_chunk_id:
            raise WoonError("document_id and chunk_id must not be empty")
        self._assert_index_current()
        return self._index.read_excerpt(normalized_document_id, normalized_chunk_id)

    def index_statistics(self) -> IndexStatistics:
        self._assert_index_current()
        return self._index.statistics()

    def audit(self) -> list[str]:
        errors = self._repository.validate()
        try:
            documents = list(self._repository.list_documents())
        except WoonError as error:
            return sorted(set([*errors, str(error)]))
        ids = {document.metadata.canonical_id for document in documents}
        titles: dict[str, str] = {}
        sources: dict[str, str] = {}
        for document in documents:
            title = _fingerprint(document.metadata.title)
            if previous := titles.get(title):
                errors.append(
                    f"{document.metadata.canonical_id}: normalized title also used by {previous}"
                )
            titles[title] = document.metadata.canonical_id
            for source_id in document.metadata.source_ids:
                if previous := sources.get(source_id):
                    errors.append(
                        f"{document.metadata.canonical_id}: source_id {source_id!r} "
                        f"also used by {previous}"
                    )
                sources[source_id] = document.metadata.canonical_id
            references = (
                document.metadata.prerequisites
                + document.metadata.next_concepts
                + document.metadata.related
            )
            for reference in references:
                if reference not in ids:
                    errors.append(
                        f"{document.metadata.canonical_id}: unresolved relation {reference!r}"
                    )
        return sorted(set(errors))

    def history(self, canonical_id: str, limit: int = 20) -> list[HistoryEntry]:
        document = self.get(canonical_id)
        return self._history.list(document.relative_path, limit)

    def restore(
        self,
        canonical_id: str,
        git_revision: str,
        expected_revision: str,
        *,
        confirmed: bool,
    ) -> SaveResult:
        if not confirmed:
            raise WoonError("restore requires confirmed=true")
        current = self.get(canonical_id)
        historical_text = self._history.read(current.relative_path, git_revision)
        historical = self._repository.parse(current.relative_path, historical_text)
        if historical.metadata.canonical_id != current.metadata.canonical_id:
            raise WoonError("historical document identity does not match current identity")
        with self._repository.exclusive():
            latest = self._repository.get(canonical_id)
            if latest is None:
                raise WoonError(f"canonical document not found: {canonical_id}")
            if latest.revision != expected_revision:
                raise WoonError(
                    "canonical document changed after it was read; reload and merge before writing"
                )
            self._ensure_unique_identity(historical.metadata)
            snapshot = self._repository.snapshot(canonical_id)
            result = self._repository.save(
                replace(historical.metadata), historical.body, expected_revision
            )
            if result.changed:
                self._reindex_or_restore(canonical_id, snapshot)
            return result

    def _ensure_unique_identity(self, metadata: DocumentMetadata) -> None:
        candidate_sources = set(metadata.source_ids)
        for document in self._repository.list_documents():
            if document.metadata.canonical_id == metadata.canonical_id:
                continue
            if _fingerprint(document.metadata.title) == _fingerprint(metadata.title):
                raise WoonError(
                    "a canonical document with the same normalized title already exists: "
                    f"{document.metadata.canonical_id}"
                )
            duplicate_sources = candidate_sources.intersection(document.metadata.source_ids)
            if duplicate_sources:
                source_id = sorted(duplicate_sources)[0]
                raise WoonError(
                    f"source_id {source_id!r} is already owned by canonical document "
                    f"{document.metadata.canonical_id}"
                )

    def _index_documents(self) -> list[IndexedDocument]:
        canonical = [_indexed(document) for document in self._repository.list_documents()]
        documents = {document.document_id: document for document in canonical}
        if self._corpus is not None:
            for document in self._corpus.list_documents():
                documents.setdefault(document.document_id, document)
        return list(documents.values())

    def _reindex_unlocked(self) -> int:
        return self._index.rebuild(self._index_documents())

    def _reindex_or_restore(self, canonical_id: str, snapshot: bytes | None) -> None:
        try:
            self._reindex_unlocked()
        except Exception as index_error:
            try:
                self._repository.restore_snapshot(canonical_id, snapshot)
                self._reindex_unlocked()
            except Exception as recovery_error:
                raise WoonError(
                    "knowledge index failed and the previous canonical/index state could not "
                    f"be fully restored: index={index_error}; recovery={recovery_error}"
                ) from recovery_error
            raise

    def _assert_index_current(self) -> None:
        actual = self._index.generation()
        if actual is None:
            raise WoonError(
                "knowledge index does not exist or has no generation; call "
                "woon_knowledge_reindex and retry"
            )
        expected = knowledge_generation(self._index_documents())
        if actual != expected:
            raise WoonError("knowledge index is stale; call woon_knowledge_reindex and retry")

    @staticmethod
    def _validate_id(canonical_id: str) -> str:
        value = canonical_id.strip().lower()
        if not CANONICAL_ID.fullmatch(value):
            raise WoonError(
                "canonical_id must be a slash-separated lowercase path such as "
                "backend/ports-adapters"
            )
        return value

    def _validate_metadata(self, metadata: DocumentMetadata) -> DocumentMetadata:
        canonical_id = self._validate_id(metadata.canonical_id)
        domain = metadata.domain.strip().lower()
        if canonical_id.split("/", 1)[0] != domain:
            raise WoonError("metadata domain must match the first canonical_id segment")
        title = " ".join(metadata.title.split())
        summary = " ".join(metadata.summary.split())
        if not title or not summary:
            raise WoonError("title and summary must not be empty")
        if metadata.difficulty not in DIFFICULTIES:
            raise WoonError(f"unsupported difficulty: {metadata.difficulty!r}")
        relations = {
            "prerequisites": metadata.prerequisites,
            "next_concepts": metadata.next_concepts,
            "related": metadata.related,
        }
        normalized_relations: dict[str, tuple[str, ...]] = {}
        for name, values in relations.items():
            normalized = tuple(dict.fromkeys(self._validate_id(value) for value in values))
            if canonical_id in normalized:
                raise WoonError(f"{name} must not reference the document itself")
            normalized_relations[name] = normalized
        return replace(
            metadata,
            canonical_id=canonical_id,
            title=title,
            domain=domain,
            summary=summary,
            prerequisites=normalized_relations["prerequisites"],
            next_concepts=normalized_relations["next_concepts"],
            related=normalized_relations["related"],
            source_ids=tuple(
                dict.fromkeys(value.strip() for value in metadata.source_ids if value.strip())
            ),
        )

    @staticmethod
    def _validate_body(body: str) -> str:
        normalized = body.replace("\r\n", "\n").strip()
        if not normalized:
            raise WoonError("canonical document body must not be empty")
        if normalized.startswith("---"):
            raise WoonError("body must not include YAML frontmatter")
        if re.search(r"^#\s+", normalized, flags=re.MULTILINE):
            raise WoonError(
                "body must not include an H1; the repository renders the canonical title"
            )
        return normalized + "\n"


def _fingerprint(value: str) -> str:
    normalized = re.sub(r"[^0-9a-z가-힣]", "", value.lower())
    return hashlib.sha256(normalized.encode()).hexdigest()


def _indexed(document: CanonicalDocument) -> IndexedDocument:
    return IndexedDocument(
        document_id=document.relative_path,
        canonical_id=document.metadata.canonical_id,
        title=document.metadata.title,
        summary=document.metadata.summary,
        body=document.body,
        relative_path=document.relative_path,
        revision=document.revision,
        source_type="canonical",
    )
