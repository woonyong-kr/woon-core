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
    SaveResult,
    SearchResult,
)
from woon_core.knowledge.ports import (
    CanonicalDocumentRepository,
    KnowledgeHistory,
    KnowledgeSearchIndex,
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
    ) -> None:
        self._repository = repository
        self._index = index
        self._history = history

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
        current = self._repository.get(validated.canonical_id)
        if current is not None and expected_revision is None:
            raise WoonError(
                "canonical document already exists; read it first and provide expected_revision"
            )
        for document in self._repository.list_documents():
            if document.metadata.canonical_id != validated.canonical_id and _fingerprint(
                document.metadata.title
            ) == _fingerprint(validated.title):
                raise WoonError(
                    "a canonical document with the same normalized title already exists: "
                    f"{document.metadata.canonical_id}"
                )
        result = self._repository.save(validated, normalized_body, expected_revision)
        if result.changed:
            self.reindex()
        return result

    def reindex(self) -> int:
        return self._index.rebuild(self._repository.list_documents())

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        normalized_query = " ".join(query.split())
        if not normalized_query:
            raise WoonError("search query must not be empty")
        if limit < 1 or limit > 20:
            raise WoonError("search limit must be between 1 and 20")
        return self._index.search(normalized_query, limit)

    def audit(self) -> list[str]:
        errors = self._repository.validate()
        ids = {document.metadata.canonical_id for document in self._repository.list_documents()}
        for document in self._repository.list_documents():
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
        result = self._repository.save(
            replace(historical.metadata), historical.body, expected_revision
        )
        if result.changed:
            self.reindex()
        return result

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
