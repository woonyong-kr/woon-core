"""Application services for archive, retrieval, indexing, and recovery."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.compiled_wiki import (
    CompilationAudit,
    CompiledWiki,
    CompileReport,
    CuratedRevision,
    CuratedRevisionReport,
    MigrationReport,
    RetiredPageReport,
    RevisionReconciliationReport,
)
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
from woon_core.knowledge.identity import validate_canonical_id
from woon_core.knowledge.learning_checkpoint import (
    LearningCheckpoint,
    LearningCheckpointReport,
    upsert_learning_checkpoint,
    validate_learning_checkpoint,
)
from woon_core.knowledge.ports import (
    CanonicalDocumentRepository,
    KnowledgeHistory,
    KnowledgeSearchIndex,
    ReadOnlyKnowledgeCorpus,
)

DIFFICULTIES = {"foundation", "intermediate", "advanced"}


class KnowledgeService:
    """Coordinates ports while preserving one canonical file per concept."""

    def __init__(
        self,
        repository: CanonicalDocumentRepository,
        index: KnowledgeSearchIndex,
        history: KnowledgeHistory,
        corpus: ReadOnlyKnowledgeCorpus | None = None,
        *,
        compiled_wiki: CompiledWiki | None = None,
    ) -> None:
        self._repository = repository
        self._index = index
        self._history = history
        self._corpus = corpus
        self._compiled_wiki = compiled_wiki
        self._cached_state_token: tuple[object, ...] | None = None
        self._cached_generation: str | None = None

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
        *,
        archive_origin: str = "manual-reviewed",
        approved_review_id: str | None = None,
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
            compiler_snapshot = (
                self._compiled_wiki.snapshot_inputs() if self._compiled_wiki is not None else None
            )
            if self._compiled_wiki is None:
                result = self._repository.save(validated, normalized_body, expected_revision)
            else:
                try:
                    self._compiled_wiki.archive(
                        validated,
                        normalized_body,
                        metadata.source_ids,
                        archive_origin=archive_origin,
                        approved_review_id=approved_review_id,
                    )
                except Exception:
                    if compiler_snapshot is not None:
                        self._compiled_wiki.restore_inputs(compiler_snapshot)
                    self._repository.restore_snapshot(validated.canonical_id, snapshot)
                    raise
                saved = self._repository.get(validated.canonical_id)
                if saved is None:
                    raise WoonError("compiled archive did not create its canonical output")
                result = SaveResult(
                    document=saved,
                    created=current is None,
                    changed=saved.revision != (current.revision if current is not None else ""),
                )
            if result.changed:
                self._reindex_or_restore(validated.canonical_id, snapshot, compiler_snapshot)
            return result

    def reindex(self) -> int:
        with self._repository.exclusive():
            self._assert_compiled_current()
            return self._reindex_unlocked()

    def compile(self, *, force: bool = False) -> CompileReport:
        """Build changed LLM Wiki pages and keep the bounded search index aligned."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            report = self._compiled_wiki.compile(force=force)
            if report.compiled:
                self._reindex_unlocked()
            return report

    def migrate_compiled_wiki(self) -> MigrationReport:
        """Convert current Wiki pages once into source-schema compiler inputs."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            report = self._compiled_wiki.migrate()
            self._reindex_unlocked()
            return report

    def initialize_compiled_wiki_curation(self) -> int:
        """Create provisional present-use records for an already migrated Wiki."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            count = self._compiled_wiki.initialize_curation()
            return count

    def refresh_provisional_compiled_wiki_curation(self) -> int:
        """Refresh generated current-use text without touching reviewed records."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            return self._compiled_wiki.refresh_provisional_curation()

    def reconcile_superseded_compiled_wiki_revisions(self) -> RevisionReconciliationReport:
        """Preserve unreferenced conversation revisions as explicit inactive history."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            return self._compiled_wiki.reconcile_superseded_revisions()

    def curate_compiled_wiki_revisions(
        self, revisions: tuple[CuratedRevision, ...]
    ) -> CuratedRevisionReport:
        """Apply reviewed prose, compile it, and keep the search index in sync."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        if not revisions:
            raise WoonError("curated revision requires at least one page")
        page_ids = tuple(revision.page_id for revision in revisions)
        with self._repository.exclusive():
            snapshot = self._compiled_wiki.snapshot_inputs()
            try:
                report = self._compiled_wiki.curate_revisions(revisions)
                self._reindex_unlocked()
            except Exception as revision_error:
                try:
                    self._compiled_wiki.restore_inputs(snapshot)
                    self._compiled_wiki.compile(page_ids=page_ids)
                    self._reindex_unlocked()
                except Exception as recovery_error:
                    raise WoonError(
                        "curated revision failed and the previous compiler/index state could not "
                        f"be fully restored: revision={revision_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
            return report

    def revise_uncompiled_body(
        self,
        canonical_id: str,
        body: str,
        expected_revision: str,
    ) -> SaveResult:
        """Optimistically revise a canonical page not owned by the compiler."""

        normalized_id = self._validate_id(canonical_id)
        normalized_body = self._validate_body(body)
        with self._repository.exclusive():
            current = self._repository.get(normalized_id)
            if current is None:
                raise WoonError(f"canonical document not found: {normalized_id}")
            if current.revision != expected_revision:
                raise WoonError(
                    "canonical document changed after it was read; reload and merge before writing"
                )
            if self._compiled_wiki is not None and self._compiled_wiki.owns_page(normalized_id):
                raise WoonError("compiler-owned Wiki page requires a curated revision")
            snapshot = self._repository.snapshot(normalized_id)
            result = self._repository.save_body(normalized_id, normalized_body, expected_revision)
            if result.changed:
                self._reindex_or_restore(normalized_id, snapshot)
            return result

    def record_learning_checkpoint(
        self,
        checkpoint: LearningCheckpoint,
        expected_revision: str,
    ) -> LearningCheckpointReport:
        """Persist one structured resume point through the page's actual owner."""

        normalized_id = self._validate_id(checkpoint.canonical_id)
        validated = validate_learning_checkpoint(replace(checkpoint, canonical_id=normalized_id))
        with self._repository.exclusive():
            current = self._repository.get(normalized_id)
            if current is None:
                raise WoonError(f"canonical document not found: {normalized_id}")
            if current.revision != expected_revision:
                raise WoonError(
                    "canonical document changed after it was read; reload and merge before writing"
                )
            body = upsert_learning_checkpoint(current.body, validated)
            compiler_owned = bool(
                self._compiled_wiki is not None and self._compiled_wiki.owns_page(normalized_id)
            )
            if body == current.body:
                return LearningCheckpointReport(
                    canonical_id=normalized_id,
                    relative_path=current.relative_path,
                    revision=current.revision,
                    changed=False,
                    compiler_owned=compiler_owned,
                )
            snapshot = self._repository.snapshot(normalized_id)
            compiler_snapshot = None
            if compiler_owned:
                assert self._compiled_wiki is not None
                compiler_snapshot = self._compiled_wiki.snapshot_inputs()
                self._compiled_wiki.curate_revisions(
                    (
                        CuratedRevision(
                            page_id=normalized_id,
                            body=body,
                            statement=(
                                f"{validated.unit} 학습 체크포인트를 "
                                f"{validated.status} 상태로 갱신했다."
                            ),
                        ),
                    )
                )
                saved = self._repository.get(normalized_id)
                if saved is None:
                    raise WoonError("compiled learning checkpoint lost its canonical output")
                result = SaveResult(document=saved, created=False, changed=True)
            else:
                result = self._repository.save_body(normalized_id, body, expected_revision)
            if result.changed:
                self._reindex_or_restore(normalized_id, snapshot, compiler_snapshot)
            return LearningCheckpointReport(
                canonical_id=normalized_id,
                relative_path=result.document.relative_path,
                revision=result.document.revision,
                changed=result.changed,
                compiler_owned=compiler_owned,
            )

    def retire_compiled_wiki_pages(self, replacements: dict[str, str]) -> RetiredPageReport:
        """Merge obsolete compiled page identities and refresh the search index."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        if not replacements:
            raise WoonError("compiled page retirement requires at least one replacement")
        with self._repository.exclusive():
            report = self._compiled_wiki.retire_pages(replacements)
            self._reindex_unlocked()
            return report

    def compilation_audit(self) -> CompilationAudit:
        """Return source-schema receipt health separately from Markdown quality audit."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        return self._compiled_wiki.audit()

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
        if self._compiled_wiki is not None:
            errors.extend(self._compiled_wiki.audit().errors)
            errors.extend(self._compiled_wiki.navigation_issues())
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
            compiler_snapshot = (
                self._compiled_wiki.snapshot_inputs() if self._compiled_wiki is not None else None
            )
            if self._compiled_wiki is None:
                result = self._repository.save(
                    replace(historical.metadata), historical.body, expected_revision
                )
            else:
                historical_body_hash = hashlib.sha256(
                    historical.body.replace("\r\n", "\n").rstrip().encode("utf-8") + b"\n"
                ).hexdigest()
                self._compiled_wiki.restore_from_git(
                    replace(historical.metadata),
                    historical.body,
                    git_revision,
                    historical_body_hash,
                )
                saved = self._repository.get(canonical_id)
                if saved is None:
                    raise WoonError("compiled restore did not create its canonical output")
                result = SaveResult(document=saved, created=False, changed=True)
            if result.changed:
                self._reindex_or_restore(canonical_id, snapshot, compiler_snapshot)
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
        before = self._state_token()
        documents = self._index_documents()
        after = self._state_token()
        if before != after:
            raise WoonError("knowledge files changed while indexing; retry reindex")
        count = self._index.rebuild(documents)
        self._cached_state_token = after
        self._cached_generation = knowledge_generation(documents)
        return count

    def _reindex_or_restore(
        self,
        canonical_id: str,
        snapshot: bytes | None,
        compiler_snapshot: dict[Path, bytes | None] | None = None,
    ) -> None:
        try:
            self._reindex_unlocked()
        except Exception as index_error:
            try:
                self._repository.restore_snapshot(canonical_id, snapshot)
                if compiler_snapshot is not None and self._compiled_wiki is not None:
                    self._compiled_wiki.restore_inputs(compiler_snapshot)
                self._reindex_unlocked()
            except Exception as recovery_error:
                raise WoonError(
                    "knowledge index failed and the previous canonical/index state could not "
                    f"be fully restored: index={index_error}; recovery={recovery_error}"
                ) from recovery_error
            raise

    def _assert_index_current(self) -> None:
        self._assert_compiled_current()
        actual = self._index.generation()
        if actual is None:
            raise WoonError(
                "knowledge index does not exist or has no generation; call "
                "woon_knowledge_reindex and retry"
            )
        before = self._state_token()
        if before == self._cached_state_token and self._cached_generation is not None:
            expected = self._cached_generation
        else:
            documents = self._index_documents()
            after = self._state_token()
            if before != after:
                raise WoonError("knowledge files changed during freshness check; retry")
            expected = knowledge_generation(documents)
            self._cached_state_token = after
            self._cached_generation = expected
        if actual != expected:
            raise WoonError("knowledge index is stale; call woon_knowledge_reindex and retry")

    def _assert_compiled_current(self) -> None:
        if self._compiled_wiki is not None:
            self._compiled_wiki.assert_current()

    def _state_token(self) -> tuple[object, ...]:
        corpus = self._corpus.state_token() if self._corpus is not None else ()
        return (self._repository.state_token(), corpus)

    @staticmethod
    def _validate_id(canonical_id: str) -> str:
        return validate_canonical_id(canonical_id)

    def _validate_metadata(self, metadata: DocumentMetadata) -> DocumentMetadata:
        canonical_id = self._validate_id(metadata.canonical_id)
        domain = metadata.domain.strip()
        if canonical_id.split("/", 1)[0].casefold() != domain.casefold():
            raise WoonError("metadata domain must match the first canonical_id segment")
        title = " ".join(metadata.title.split())
        summary = " ".join(metadata.summary.split())
        purpose = " ".join(metadata.purpose.split())
        if not title or not summary or not purpose:
            raise WoonError("title, summary, and purpose must not be empty")
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
            purpose=purpose,
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
