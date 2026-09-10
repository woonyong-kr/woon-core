"""Application services for archive, retrieval, indexing, and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.book_intake import (
    audit_book_intake,
    validate_book_promotion_rights,
)
from woon_core.knowledge.book_rights import (
    PRIVATE_AUTHORIZATION_DECISION,
    BookRightsDemotion,
    BookRightsDemotionReport,
    BookRightsRestoration,
    BookRightsRestorationReport,
)
from woon_core.knowledge.compiled_wiki import (
    WIKILINK_RE,
    BookCoverageManifestUpdate,
    CompilationAudit,
    CompiledWiki,
    CompiledWikiTransaction,
    CompiledWikiTransactionReport,
    CompileReport,
    CuratedRevision,
    CuratedRevisionReport,
    LegacyPageAdoption,
    MigrationReport,
    RetiredPageReport,
    RevisionReconciliationReport,
    StagedBookAsset,
    VerifiedBookPage,
    VerifiedBookPreflightReport,
    VerifiedBookUpdateReport,
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
from woon_core.knowledge.wiki_tree import (
    iter_wiki_pages,
    load_wiki_tree,
    normalize_identity,
    split_markdown,
)
from woon_core.people.records import validate_record_collection, validate_record_metadata_update

DIFFICULTIES = {"foundation", "intermediate", "advanced"}


@dataclass(frozen=True, slots=True)
class ManualWikiWrite:
    """One hash-pinned manual Wiki rewrite, relocation, or retirement.

    A mixed Wiki restructure never asks the service to infer a filename,
    frontmatter change, or link replacement.  The reviewed planner supplies
    the complete next bytes, pinned both to the source it read and to the
    bytes it intends to write.  ``current_path == target_path`` represents a
    manual metadata/link update without a physical relocation.  A retirement
    uses ``target_path=target_sha256=content=None``; no empty synthetic page
    is written in place of the reviewed predecessor.
    """

    current_path: str
    current_sha256: str
    target_path: str | None
    target_sha256: str | None
    content: bytes | None


@dataclass(frozen=True, slots=True)
class ResourceFileRename:
    """A reviewed same-directory private resource filename change; bytes stay intact."""

    current_path: str
    target_path: str
    current_sha256: str


@dataclass(frozen=True, slots=True)
class WikiRestructureTransactionReport:
    """Observable outcome for one compiler-and-manual Wiki transaction."""

    compiler: CompiledWikiTransactionReport
    manual_written: int
    indexed_documents: int
    resources_renamed: int = 0
    resource_references_written: int = 0


def _manual_wikilink_atom(value: str) -> str:
    """Normalize the path part of an Obsidian link without accepting traversal."""

    if not isinstance(value, str) or not value.strip():
        raise WoonError("manual Wiki rewrite target must be a non-empty Wiki path")
    candidate = Path(value.strip().replace("\\", "/").removesuffix(".md"))
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise WoonError("manual Wiki rewrite target must be a safe relative Wiki path")
    normalized = candidate.as_posix()
    if not normalized.startswith("wiki/"):
        raise WoonError("manual Wiki rewrite target must start with wiki/")
    return normalized


def _manual_wikilink_count(text: str, target: str) -> int:
    count = 0
    for match in WIKILINK_RE.finditer(text):
        try:
            candidate = _manual_wikilink_atom(match.group("target").strip())
        except WoonError:
            continue
        if candidate == target:
            count += 1
    return count


def _transaction_refreshes_tree(transaction: CompiledWikiTransaction) -> bool:
    """A tree refresh can update Core-owned views on native Markdown pages too."""

    return transaction.refresh_wiki_tree or any(
        isinstance(page.get("frontmatter"), dict) and page["frontmatter"].get("navigation_groups")
        for page in transaction.pages_upsert
    )


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
            if snapshot and "record_kind" in split_markdown(snapshot.decode("utf-8"))[0]:
                raise WoonError(
                    "record metadata is owned by its native/source writer; "
                    "use apply_native_wiki_transaction with resolved frontmatter"
                )
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

    def compile(self, *, force: bool = False, page_ids: tuple[str, ...] = ()) -> CompileReport:
        """Build changed LLM Wiki pages and keep the bounded search index aligned."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            report = self._compiled_wiki.compile(force=force, page_ids=page_ids)
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

    def promote_verified_book_pages(
        self,
        pages: tuple[VerifiedBookPage, ...],
        coverage_manifest: BookCoverageManifestUpdate | None = None,
        staged_assets: tuple[StagedBookAsset, ...] = (),
    ) -> CuratedRevisionReport:
        """Atomically promote book pages with their optimistic coverage replacement."""

        report = self.apply_verified_book_update(
            pages,
            {},
            {},
            {},
            coverage_manifest,
            staged_assets,
        )
        return CuratedRevisionReport(
            curated=report.curated,
            compiled=report.compiled,
            unchanged=report.unchanged,
            page_ids=report.page_ids,
            staged_asset_count=report.staged_asset_count,
            unchanged_asset_count=report.unchanged_asset_count,
        )

    def apply_compiled_wiki_transaction(
        self, transaction: CompiledWikiTransaction
    ) -> CompiledWikiTransactionReport:
        """Apply one optimistic compiler catalog transaction and reindex atomically."""

        if _transaction_refreshes_tree(transaction):
            return self.apply_wiki_restructure_transaction(transaction, ()).compiler

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        page_ids = tuple(str(page.get("page_id", "")) for page in transaction.pages_upsert)
        if not page_ids:
            raise WoonError("compiled Wiki transaction requires at least one page upsert")
        with self._repository.exclusive():
            for page_id in page_ids:
                expected_revision = transaction.expected_revisions.get(page_id)
                current = self._repository.get(page_id)
                if expected_revision is None:
                    if current is not None:
                        raise WoonError(
                            "compiled Wiki transaction expected a new page but it already exists: "
                            f"{page_id}"
                        )
                    continue
                if current is None:
                    raise WoonError(
                        "compiled Wiki transaction expected an existing page but it is missing: "
                        f"{page_id}"
                    )
                if current.revision != expected_revision:
                    raise WoonError(
                        "compiled Wiki transaction page changed after it was read; "
                        f"reload and merge before writing: {page_id}"
                    )

            input_snapshot = self._compiled_wiki.snapshot_inputs()
            output_snapshot = self._compiled_wiki.snapshot_outputs(
                extra_relative_paths=tuple(
                    str(page.get("output_path", "")) for page in transaction.pages_upsert
                )
            )
            try:
                report = self._compiled_wiki.apply_compiled_wiki_transaction(transaction)
                self._reindex_unlocked()
            except Exception as transaction_error:
                try:
                    self._compiled_wiki.restore_inputs(input_snapshot)
                    self._compiled_wiki.restore_outputs(output_snapshot)
                    self._reindex_unlocked()
                except Exception as recovery_error:
                    raise WoonError(
                        "compiled Wiki transaction failed and recovery was incomplete: "
                        f"transaction={transaction_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
            return report

    def apply_legacy_page_adoptions(
        self, transaction: CompiledWikiTransaction, adoptions: tuple[LegacyPageAdoption, ...]
    ) -> CompiledWikiTransactionReport:
        """Atomically archive only preflight-pinned raw pages, then compile their takeover."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            self._compiled_wiki.preflight_legacy_page_adoptions(transaction, adoptions)
            adopted = {item.page_id for item in adoptions}
            for page in transaction.pages_upsert:
                page_id = str(page.get("page_id", ""))
                current = self._repository.get(page_id)
                expected = transaction.expected_revisions.get(page_id)
                if page_id in adopted:
                    if expected is not None or current is None:
                        raise WoonError(
                            "legacy adoption requires an existing raw page and null revision"
                        )
                elif current is None or current.revision != expected:
                    raise WoonError("compiled Wiki transaction changed after it was read")
            input_snapshot = self._compiled_wiki.snapshot_inputs()
            output_snapshot = self._compiled_wiki.snapshot_outputs(
                extra_relative_paths=tuple(
                    str(page.get("output_path", "")) for page in transaction.pages_upsert
                )
            )
            archives = [self._compiled_wiki.vault / item.archive_path for item in adoptions]
            try:
                for item, archive in zip(adoptions, archives, strict=True):
                    raw = self._compiled_wiki.vault / "wiki" / item.output_path
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(raw, archive)
                report = self._compiled_wiki.apply_compiled_wiki_transaction(transaction)
                self._reindex_unlocked()
                return report
            except Exception:
                self._compiled_wiki.restore_inputs(input_snapshot)
                self._compiled_wiki.restore_outputs(output_snapshot)
                for archive in archives:
                    archive.unlink(missing_ok=True)
                self._reindex_unlocked()
                raise

    def apply_verified_book_update(
        self,
        pages: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_expected_revisions: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate | None = None,
        staged_assets: tuple[StagedBookAsset, ...] = (),
        *,
        retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        retirement_content_relocations: dict[str, tuple[str, ...]] | None = None,
    ) -> VerifiedBookUpdateReport:
        """Atomically promote pages, retire wrappers, and rebuild search once.

        Every promoted and retired identity is re-read under the repository lock.
        A compiler, generated-output, or index failure restores the single input
        snapshot and all generated outputs before rebuilding the previous index.
        """

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            coverage_path = self._validate_verified_book_update_request(
                pages,
                replacements,
                retirement_expected_revisions,
                retirement_body_sha256,
                coverage_manifest,
                retirement_image_replacements=retirement_image_replacements,
                retirement_content_relocations=retirement_content_relocations,
            )
            if staged_assets and coverage_manifest is None:
                raise WoonError("staged book assets require a coverage manifest")
            asset_counts = (
                compiler.validate_staged_book_assets(staged_assets, coverage_manifest)
                if coverage_manifest is not None
                else (0, 0)
            )
            materialized_scope_paths = (
                compiler.materialized_book_coverage_scope_paths(coverage_manifest)
                if coverage_manifest is not None and coverage_manifest.mode == "materialize-scopes"
                else ()
            )
            input_snapshot = compiler.snapshot_inputs(
                extra_paths=(
                    *((coverage_path,) if coverage_path is not None else ()),
                    *materialized_scope_paths,
                )
            )
            output_snapshot = compiler.snapshot_outputs(
                extra_relative_paths=tuple(f"{page.page_id}.md" for page in pages)
            )
            asset_snapshot = compiler.snapshot_staged_book_assets(staged_assets)
            try:
                compiler.install_staged_book_assets(staged_assets)
                report = compiler.apply_verified_book_update(
                    pages,
                    replacements,
                    retirement_body_sha256,
                    coverage_manifest,
                    retirement_image_replacements=retirement_image_replacements,
                    retirement_content_relocations=retirement_content_relocations,
                )
                self._reindex_unlocked()
            except BaseException as update_error:
                try:
                    compiler.restore_inputs(input_snapshot)
                    compiler.restore_outputs(output_snapshot)
                    compiler.restore_staged_book_assets(asset_snapshot)
                    self._reindex_unlocked()
                except BaseException as recovery_error:
                    raise WoonError(
                        "verified book update failed and recovery was incomplete: "
                        f"update={update_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
            return replace(
                report,
                staged_asset_count=asset_counts[0],
                unchanged_asset_count=asset_counts[1],
            )

    def preflight_verified_book_update(
        self,
        pages: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_expected_revisions: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate,
        staged_assets: tuple[StagedBookAsset, ...] = (),
        *,
        retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        retirement_content_relocations: dict[str, tuple[str, ...]] | None = None,
    ) -> VerifiedBookPreflightReport:
        """Validate revisions and coverage hashes without mutating the Vault."""

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            coverage_path = self._validate_verified_book_update_request(
                pages,
                replacements,
                retirement_expected_revisions,
                retirement_body_sha256,
                coverage_manifest,
                retirement_image_replacements=retirement_image_replacements,
                retirement_content_relocations=retirement_content_relocations,
            )
            asset_counts = compiler.validate_staged_book_assets(staged_assets, coverage_manifest)
            compiler.dry_run_verified_book_update(
                pages,
                replacements,
                retirement_body_sha256,
                coverage_manifest,
                staged_assets,
                retirement_image_replacements=retirement_image_replacements,
                retirement_content_relocations=retirement_content_relocations,
            )
        if coverage_path is None:  # pragma: no cover - public preflight requires coverage
            raise WoonError("verified book preflight requires a coverage manifest")
        return VerifiedBookPreflightReport(
            ready=True,
            page_count=len(pages),
            retirement_count=len(replacements),
            coverage_mode=coverage_manifest.mode,
            coverage_path=coverage_manifest.relative_path,
            base_manifest_preserved=coverage_manifest.mode == "merge-scope",
            staged_asset_count=asset_counts[0],
            unchanged_asset_count=asset_counts[1],
        )

    def preflight_book_rights_restoration(
        self,
        request: BookRightsRestoration,
        pages: tuple[VerifiedBookPage, ...],
        coverage_manifest: BookCoverageManifestUpdate,
        staged_assets: tuple[StagedBookAsset, ...] = (),
    ) -> BookRightsRestorationReport:
        """Validate a private-only authorization and source restore without writing."""

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            coverage_path = self._validate_verified_book_update_request(
                pages,
                {},
                {},
                {},
                coverage_manifest,
                allow_blocked_restore=True,
            )
            if coverage_path is None:  # pragma: no cover - restore requires coverage
                raise WoonError("book rights restore requires a coverage manifest")
            self._validate_book_rights_restoration(request, pages, coverage_manifest)
            asset_counts = compiler.validate_staged_book_assets(staged_assets, coverage_manifest)
            compiler.dry_run_verified_book_update(
                pages,
                {},
                {},
                coverage_manifest,
                staged_assets,
                rights_restore_book_id=request.book_id,
            )
        return BookRightsRestorationReport(
            ready=True,
            applied=False,
            page_count=len(pages),
            coverage_mode=coverage_manifest.mode,
            coverage_path=coverage_manifest.relative_path,
            intake_relative_path=request.book_intake["relative_path"],
            quarantine_manifest_count=len(request.quarantine_manifests),
            staged_asset_count=asset_counts[0],
            unchanged_asset_count=asset_counts[1],
        )

    def apply_book_rights_restoration(
        self,
        request: BookRightsRestoration,
        pages: tuple[VerifiedBookPage, ...],
        coverage_manifest: BookCoverageManifestUpdate,
        staged_assets: tuple[StagedBookAsset, ...] = (),
    ) -> BookRightsRestorationReport:
        """Atomically authorize private use, restore pages, assets, and the search index."""

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            coverage_path = self._validate_verified_book_update_request(
                pages,
                {},
                {},
                {},
                coverage_manifest,
                allow_blocked_restore=True,
            )
            if coverage_path is None:  # pragma: no cover - restore requires coverage
                raise WoonError("book rights restore requires a coverage manifest")
            intake_path, intake_bytes = self._validate_book_rights_restoration(
                request, pages, coverage_manifest
            )
            asset_counts = compiler.validate_staged_book_assets(staged_assets, coverage_manifest)
            materialized_scope_paths = (
                compiler.materialized_book_coverage_scope_paths(coverage_manifest)
                if coverage_manifest.mode == "materialize-scopes"
                else ()
            )
            input_snapshot = compiler.snapshot_inputs(
                extra_paths=(coverage_path, intake_path, *materialized_scope_paths)
            )
            output_snapshot = compiler.snapshot_outputs(
                extra_relative_paths=tuple(f"{page.page_id}.md" for page in pages)
            )
            asset_snapshot = compiler.snapshot_staged_book_assets(staged_assets)
            try:
                atomic_write(intake_path, intake_bytes)
                compiler.install_staged_book_assets(staged_assets)
                compiler.apply_verified_book_update(
                    pages,
                    {},
                    {},
                    coverage_manifest,
                    rights_restore_book_id=request.book_id,
                )
                intake_audit = audit_book_intake(compiler.vault, intake_path.stem)
                if not intake_audit.complete:
                    raise WoonError(
                        "book rights restore left stale intake: " + intake_audit.errors[0]
                    )
                self._reindex_unlocked()
            except BaseException as update_error:
                try:
                    compiler.restore_inputs(input_snapshot)
                    compiler.restore_outputs(output_snapshot)
                    compiler.restore_staged_book_assets(asset_snapshot)
                    self._reindex_unlocked()
                except BaseException as recovery_error:
                    raise WoonError(
                        "book rights restore failed and recovery was incomplete: "
                        f"update={update_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
        return BookRightsRestorationReport(
            ready=True,
            applied=True,
            page_count=len(pages),
            coverage_mode=coverage_manifest.mode,
            coverage_path=coverage_manifest.relative_path,
            intake_relative_path=request.book_intake["relative_path"],
            quarantine_manifest_count=len(request.quarantine_manifests),
            staged_asset_count=asset_counts[0],
            unchanged_asset_count=asset_counts[1],
        )

    def _validate_book_rights_restoration(
        self,
        request: BookRightsRestoration,
        pages: tuple[VerifiedBookPage, ...],
        coverage_manifest: BookCoverageManifestUpdate,
    ) -> tuple[Path, bytes]:
        """Bind approval, archive, quarantine, intake, and source hashes exactly."""

        compiler = self._compiled_wiki
        if compiler is None:  # pragma: no cover - callers guard this
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        if any(
            page.page_id != request.book_id and not page.page_id.startswith(request.book_id + "/")
            for page in pages
        ):
            raise WoonError("book rights restore page is outside book_id")
        source_hashes = {page.source_sha256 for page in pages}
        authorized_hash = request.rights_evidence["source_archive_sha256"]
        if source_hashes != {authorized_hash}:
            raise WoonError("book rights restore page source hash is not authorized")
        replacement = coverage_manifest.replacement
        if replacement.get("book_id") != request.book_id:
            raise WoonError("book rights restore coverage book_id mismatch")
        workflow_phase = replacement.get("workflow_phase")
        if workflow_phase not in {"toc-indexed", "source-landed"}:
            raise WoonError("book rights restore must begin at toc-indexed or source-landed")
        if workflow_phase == "toc-indexed":
            forbidden = {
                "source_archive",
                "source_asset_inventory",
                "source_asset_inventory_evidence",
                "source_element_inventory_evidence",
                "source_elements",
                "source_element_assignments",
            }
            present = sorted(forbidden.intersection(replacement))
            if present:
                raise WoonError(
                    "book rights restore toc-indexed coverage must not claim source archive, "
                    f"asset, or semantic coverage fields: {present!r}"
                )
        if replacement.get("translation_required") is not False:
            raise WoonError("book rights restore for a Korean source requires translation false")
        edition = replacement.get("edition")
        if not isinstance(edition, dict) or edition.get("source_sha256") != authorized_hash:
            raise WoonError("book rights restore coverage source hash is not authorized")

        archive_path = self._private_rights_path(
            compiler.vault,
            str(request.rights_evidence["source_archive_relative_path"]),
            "source archive",
        )
        if archive_path.is_symlink() or not archive_path.is_file():
            raise WoonError("book rights restore source archive is missing")
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != authorized_hash:
            raise WoonError("book rights restore source archive hash changed")

        intake_path = self._private_rights_path(
            compiler.vault,
            request.book_intake["relative_path"],
            "book intake",
        )
        intake_content = intake_path.read_bytes() if intake_path.is_file() else b""
        if hashlib.sha256(intake_content).hexdigest() != request.book_intake["expected_sha256"]:
            raise WoonError("book rights restore intake changed after authorization review")
        try:
            intake = json.loads(intake_content)
        except json.JSONDecodeError as error:
            raise WoonError("book rights restore intake is invalid JSON") from error
        bundles = intake.get("bundles") if isinstance(intake, dict) else None
        if not isinstance(bundles, list):
            raise WoonError("book rights restore intake bundles are invalid")
        matches = [
            bundle
            for bundle in bundles
            if isinstance(bundle, dict)
            and bundle.get("id") == request.book_intake["bundle_id"]
            and bundle.get("target") == request.book_id
        ]
        if len(matches) != 1:
            raise WoonError("book rights restore intake bundle is missing or ambiguous")
        bundle = matches[0]
        if (
            bundle.get("rights_status")
            not in {
                "processing-prohibited",
                "unverified-commercial",
            }
            or bundle.get("processing_state") != "blocked-rights"
        ):
            raise WoonError("book rights restore requires one currently blocked intake bundle")
        if (
            bundle.get("rights_status") == "processing-prohibited"
            and not request.quarantine_manifests
        ):
            raise WoonError("book rights restore of a demoted book requires quarantine evidence")

        for item in request.quarantine_manifests:
            manifest_path = self._private_rights_path(
                compiler.vault, item["relative_path"], "quarantine manifest"
            )
            manifest_bytes = manifest_path.read_bytes() if manifest_path.is_file() else b""
            if hashlib.sha256(manifest_bytes).hexdigest() != item["expected_sha256"]:
                raise WoonError("book rights restore quarantine manifest hash changed")
            try:
                manifest = json.loads(manifest_bytes)
            except json.JSONDecodeError as error:
                raise WoonError(
                    "book rights restore quarantine manifest is invalid JSON"
                ) from error
            if manifest.get("book_id") != request.book_id:
                raise WoonError("book rights restore quarantine book_id mismatch")
            prior_rights = manifest.get("rights_evidence")
            if (
                not isinstance(prior_rights, dict)
                or prior_rights.get("source_archive_sha256") != authorized_hash
            ):
                raise WoonError("book rights restore quarantine source hash mismatch")
            if manifest_path.parent.parent != archive_path.parent / "rights-quarantine":
                raise WoonError("book rights restore quarantine is not beside its source archive")
            entries = manifest.get("entries")
            if not isinstance(entries, list) or manifest.get("entry_count") != len(entries):
                raise WoonError("book rights restore quarantine entries are invalid")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise WoonError("book rights restore quarantine entry is invalid")
                relative = entry.get("quarantine_relative_path")
                if not isinstance(relative, str):
                    raise WoonError("book rights restore quarantine entry path is invalid")
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
                    raise WoonError("book rights restore quarantine entry path is unsafe")
                entry_path = (manifest_path.parent / Path(*pure.parts)).resolve()
                try:
                    entry_path.relative_to(manifest_path.parent.resolve())
                except ValueError as error:
                    raise WoonError("book rights restore quarantine entry escapes") from error
                if entry_path.is_symlink() or not entry_path.is_file():
                    raise WoonError("book rights restore quarantine entry is missing")
                content = entry_path.read_bytes()
                if hashlib.sha256(content).hexdigest() != entry.get("sha256") or len(
                    content
                ) != entry.get("bytes"):
                    raise WoonError("book rights restore quarantine entry changed")

        bundle.pop("private_processing_authorized", None)
        bundle["rights_status"] = PRIVATE_AUTHORIZATION_DECISION
        bundle["processing_state"] = "content-in-progress"
        bundle["rights_evidence"] = request.rights_evidence
        intake_bytes = (json.dumps(intake, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return intake_path, intake_bytes

    @staticmethod
    def _private_rights_path(vault: Path, relative: str, label: str) -> Path:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
            raise WoonError(f"book rights restore {label} path is unsafe")
        path = (vault / Path(*pure.parts)).resolve()
        try:
            path.relative_to(vault.resolve())
        except ValueError as error:
            raise WoonError(f"book rights restore {label} path escapes Vault") from error
        return path

    def preflight_book_rights_demotion(
        self, request: BookRightsDemotion
    ) -> BookRightsDemotionReport:
        """Validate an exact rights demotion under the canonical repository lock."""

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            self._validate_book_rights_revisions(request)
            return compiler.preflight_book_rights_demotion(request)

    def apply_book_rights_demotion(self, request: BookRightsDemotion) -> BookRightsDemotionReport:
        """Apply and index one rights demotion with byte-exact outer rollback."""

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        with self._repository.exclusive():
            self._validate_book_rights_revisions(request)
            compiler.preflight_book_rights_demotion(request)
            snapshot = compiler.snapshot_book_rights_demotion(request)
            try:
                report = compiler.apply_book_rights_demotion(request)
                self._reindex_unlocked()
            except BaseException as update_error:
                try:
                    compiler.restore_book_rights_demotion(snapshot)
                    self._reindex_unlocked()
                except BaseException as recovery_error:
                    raise WoonError(
                        "book rights demotion failed and recovery was incomplete: "
                        f"update={update_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
            return report

    def _validate_book_rights_revisions(self, request: BookRightsDemotion) -> None:
        if set(request.expected_revisions) != set(request.target_ids):
            raise WoonError("book rights demotion revisions must match exact page targets")
        for page_id, expected_revision in request.expected_revisions.items():
            current = self._repository.get(page_id)
            if current is None:
                raise WoonError(f"book rights demotion page does not exist: {page_id}")
            if current.revision != expected_revision:
                raise WoonError(
                    "book rights demotion page changed after review; reload before writing: "
                    f"{page_id}"
                )

    def _validate_verified_book_update_request(
        self,
        pages: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_expected_revisions: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate | None,
        *,
        allow_blocked_restore: bool = False,
        retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        retirement_content_relocations: dict[str, tuple[str, ...]] | None = None,
    ) -> Path | None:
        """Validate one request while the repository lock is held."""

        if self._compiled_wiki is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        if not pages:
            raise WoonError("verified book update requires at least one promoted page")
        if set(retirement_expected_revisions) != set(replacements):
            raise WoonError(
                "verified book update retirement_expected_revisions must match replacements"
            )
        if set(retirement_body_sha256) != set(replacements):
            raise WoonError("verified book update retirement_body_sha256 must match replacements")
        promoted_ids = {page.page_id for page in pages}
        overlap = promoted_ids.intersection(replacements)
        if overlap:
            raise WoonError(
                "verified book update cannot promote and retire the same page: "
                f"{sorted(overlap)[0]}"
            )
        unverified_survivors = set(replacements.values()).difference(promoted_ids)
        if unverified_survivors:
            raise WoonError(
                "verified book update replacement must be included in promoted pages: "
                f"{sorted(unverified_survivors)[0]}"
            )
        relocation_survivors = {
            survivor
            for survivors in (retirement_content_relocations or {}).values()
            for survivor in survivors
        }.difference(promoted_ids)
        if relocation_survivors:
            raise WoonError(
                "verified book content relocation must target promoted pages: "
                f"{sorted(relocation_survivors)[0]}"
            )
        for page in pages:
            current = self._repository.get(page.page_id)
            if current is None:
                if page.expected_revision is not None:
                    raise WoonError(
                        f"verified book page does not exist but expected a revision: {page.page_id}"
                    )
            elif page.expected_revision != current.revision:
                raise WoonError(
                    "verified book page changed after it was read; reload and merge before writing"
                )
        for page_id, expected_revision in retirement_expected_revisions.items():
            current = self._repository.get(page_id)
            if current is None:
                raise WoonError(f"retired book page does not exist: {page_id}")
            if current.revision != expected_revision:
                raise WoonError(
                    "retired book page changed after it was read; reload and merge before writing"
                )
        self._compiled_wiki.validate_verified_book_retirement_content(
            pages,
            replacements,
            retirement_body_sha256,
            coverage_manifest,
            retirement_image_replacements=retirement_image_replacements,
            retirement_content_relocations=retirement_content_relocations,
        )
        workflow_phase = "source-landed"
        if coverage_manifest is not None:
            candidate_phase = coverage_manifest.replacement.get("workflow_phase")
            if isinstance(candidate_phase, str):
                workflow_phase = candidate_phase
        self._compiled_wiki.validate_book_workflow_pages(
            pages,
            workflow_phase,
            allow_legacy_toc_normalization=(
                coverage_manifest is not None
                and coverage_manifest.mode == "replace"
                and (not allow_blocked_restore or workflow_phase == "toc-indexed")
            ),
            replacement_survivor_ids=set(replacements.values()),
            retired_page_ids=set(replacements),
        )
        if coverage_manifest is not None:
            book_id = coverage_manifest.replacement.get("book_id")
            if isinstance(book_id, str) and book_id:
                validate_book_promotion_rights(
                    self._compiled_wiki.vault,
                    book_id,
                    {page.source_sha256 for page in pages},
                    allow_blocked_restore=allow_blocked_restore,
                )
        if coverage_manifest is None:
            return None
        # Keep the established validator call shape for ordinary promotions.
        # The relocation argument is meaningful only for a wrapper retirement;
        # passing an empty value would unnecessarily break compatible adapters.
        if retirement_content_relocations:
            return self._compiled_wiki.validate_book_coverage_manifest_update(
                coverage_manifest,
                records=pages,
                retirement_replacements=replacements,
                retirement_content_relocations=retirement_content_relocations,
            )
        if replacements:
            return self._compiled_wiki.validate_book_coverage_manifest_update(
                coverage_manifest,
                records=pages,
                retirement_replacements=replacements,
            )
        return self._compiled_wiki.validate_book_coverage_manifest_update(coverage_manifest)

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
        book_roots = {
            document.metadata.canonical_id
            for document in documents
            if document.metadata.entity_kind == "book"
        }
        titles: dict[tuple[str, str], str] = {}
        sources: dict[str, str] = {}
        for document in documents:
            is_book_descendant = any(
                document.metadata.canonical_id.startswith(f"{book_root}/")
                for book_root in book_roots
            )
            if not is_book_descendant:
                title = (document.metadata.identity_scope, _fingerprint(document.metadata.title))
                if previous := titles.get(title):
                    errors.append(
                        f"{document.metadata.canonical_id}: normalized title also used by "
                        f"{previous}"
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
            if document.metadata.identity_scope == metadata.identity_scope and _fingerprint(
                document.metadata.title
            ) == _fingerprint(metadata.title):
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
        if not isinstance(metadata.identity_scope, str):
            raise WoonError("canonical document identity_scope must be a string")
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
            identity_scope=metadata.identity_scope.strip(),
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

    def apply_wiki_restructure_transaction(
        self,
        compiler_transaction: CompiledWikiTransaction,
        manual_writes: tuple[ManualWikiWrite, ...],
        *,
        resource_renames: tuple[ResourceFileRename, ...] = (),
        resource_reference_writes: tuple[ManualWikiWrite, ...] = (),
    ) -> WikiRestructureTransactionReport:
        """Apply reviewed manual relocations with one compiler transaction.

        Generated Wiki pages continue to be changed only through
        :class:`CompiledWikiTransaction`.  Manual pages are supplied as exact,
        hash-pinned bytes and are snapshotted together with every existing
        manual page because a complete tree refresh can update their generated
        navigation blocks.  Thus a compiler, audit, tree, or index failure
        restores both ownership domains before the old index is rebuilt.
        Optional resource renames preserve bytes, directory, extension and
        mode. Their existing source catalog files accept only
        the reviewed locator substitutions; compiler and native references
        still belong to the two existing transaction inputs.
        """

        compiler = self._compiled_wiki
        if compiler is None:
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        if (
            not manual_writes
            and not compiler_transaction.page_retirements
            and not resource_renames
            and not resource_reference_writes
            and not _transaction_refreshes_tree(compiler_transaction)
        ):
            report = self.apply_compiled_wiki_transaction(compiler_transaction)
            return WikiRestructureTransactionReport(
                compiler=report,
                manual_written=0,
                indexed_documents=0,
            )
        with self._repository.exclusive():
            self._validate_compiled_wiki_transaction_revisions(compiler_transaction)
            manual_snapshot, writes = self._prepare_manual_wiki_writes(
                compiler_transaction, manual_writes
            )
            self._validate_manual_wikilink_retirements(compiler_transaction, writes)
            resource_moves, reference_writes = self._prepare_resource_renames(
                resource_renames, resource_reference_writes
            )
            input_snapshot = compiler.snapshot_inputs()
            output_snapshot = compiler.snapshot_outputs(
                extra_relative_paths=tuple(
                    sorted(
                        {
                            str(page.get("output_path", ""))
                            for page in compiler_transaction.pages_upsert
                        }
                        | set(compiler_transaction.retired_output_paths)
                    )
                )
            )
            moved_resources: list[tuple[Path, Path, bytes, int]] = []
            written_references: list[tuple[Path, bytes, bytes, int]] = []
            native_writes: dict[Path, bytes | None] = {}
            page_overrides: dict[Path, bytes | None] = {
                source: None for source, _target, _content, _mode in writes
            }
            page_overrides.update(
                {
                    target: content
                    for _source, target, content, _mode in writes
                    if target is not None
                }
            )
            native_survivors = {}
            successor_ids = {
                item.successor_page_id for item in compiler_transaction.page_retirements
            }
            for source, target, content, _mode in writes:
                if target is None or content is None:
                    continue
                before = split_markdown(source.read_text(encoding="utf-8"))[0]
                after = split_markdown(content.decode("utf-8"))[0]
                canonical_id = after.get("canonical_id")
                if canonical_id not in successor_ids:
                    continue
                if before.get("canonical_id") != canonical_id or canonical_id in native_survivors:
                    raise WoonError(
                        "native retirement survivor must preserve one existing identity"
                    )
                native_survivors[canonical_id] = {
                    "current_path": source.relative_to(compiler.output_root).as_posix(),
                    "output_path": target.relative_to(compiler.output_root).as_posix(),
                    "before_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "after_sha256": hashlib.sha256(content).hexdigest(),
                }
            mutation_started = False

            def before_write() -> None:
                nonlocal mutation_started
                self._validate_compiled_wiki_transaction_revisions(compiler_transaction)
                for source, _target, _content, _mode in writes:
                    if source.read_bytes() != manual_snapshot[source][0]:
                        raise WoonError("manual Wiki source changed before write; replan")
                mutation_started = True
                for source, target, content, mode in resource_moves:
                    if source.is_symlink() or source.read_bytes() != content:
                        raise WoonError(f"resource changed before rename: {source}")
                    # link/unlink provides no-clobber rename semantics on POSIX.
                    os.link(source, target, follow_symlinks=False)
                    moved_resources.append((source, target, content, mode))
                    source.unlink()
                for path, before, after, mode in reference_writes:
                    if path.is_symlink() or path.read_bytes() != before:
                        raise WoonError(f"resource reference changed before write: {path}")
                    atomic_write(path, after, mode=mode)
                    written_references.append((path, before, after, mode))
                self._apply_manual_wiki_writes(writes, native_writes)

            try:
                compiler_report = compiler.apply_compiled_wiki_transaction(
                    compiler_transaction,
                    _page_overrides=page_overrides,
                    _before_write=before_write,
                    _native_writes=native_writes,
                    _native_survivors=native_survivors,
                )
                indexed_documents = self._reindex_unlocked()
                for source, target, content, mode in moved_resources:
                    if (
                        source.exists()
                        or target.is_symlink()
                        or target.read_bytes() != content
                        or target.stat().st_mode & 0o777 != mode
                    ):
                        raise WoonError(f"resource rename verification failed: {target}")
            except BaseException as transaction_error:
                if not mutation_started:
                    raise
                try:
                    compiler.restore_inputs(input_snapshot)
                    compiler.restore_outputs(output_snapshot)
                    self._restore_manual_wiki_snapshot(manual_snapshot, native_writes)
                    self._restore_resource_renames(moved_resources, written_references)
                    self._reindex_unlocked()
                except BaseException as recovery_error:
                    raise WoonError(
                        "mixed Wiki restructure failed and recovery was incomplete: "
                        f"transaction={transaction_error}; recovery={recovery_error}"
                    ) from recovery_error
                raise
            return WikiRestructureTransactionReport(
                compiler=compiler_report,
                manual_written=len(writes),
                indexed_documents=indexed_documents,
                resources_renamed=len(moved_resources),
                resource_references_written=len(written_references),
            )

    def apply_native_wiki_transaction(
        self, manual_writes: tuple[ManualWikiWrite, ...]
    ) -> dict[str, object]:
        """Update existing private native pages without compiling or rewriting the catalog.

        The caller supplies complete, reviewed bytes. Creation, relocation, deletion,
        identity changes and public promotion are outside this transaction's ownership.
        """

        if self._compiled_wiki is None:
            raise WoonError("native Wiki transaction requires a configured Wiki vault")
        if not manual_writes:
            raise WoonError("native Wiki transaction requires at least one write")
        vault = self._compiled_wiki.vault
        with self._repository.exclusive():
            writes: dict[Path, tuple[bytes, bytes, int]] = {}
            for write in manual_writes:
                if write.current_path != write.target_path or not isinstance(write.content, bytes):
                    raise WoonError("native Wiki transaction only updates existing paths")
                path = self._manual_wiki_path(vault, vault / "wiki", write.current_path, "native")
                lexical = vault / write.current_path
                if any(part.is_symlink() for part in (lexical, *lexical.parents)):
                    raise WoonError("native Wiki transaction does not follow symlinks")
                if path in writes or not path.is_file():
                    raise WoonError("native Wiki target must be a distinct existing regular page")
                before = path.read_bytes()
                if hashlib.sha256(before).hexdigest() != write.current_sha256:
                    raise WoonError(f"native Wiki source changed; replan: {write.current_path}")
                if hashlib.sha256(write.content).hexdigest() != write.target_sha256:
                    raise WoonError(f"native Wiki target hash does not match: {write.target_path}")
                previous, _ = split_markdown(before.decode("utf-8"))
                desired, _ = split_markdown(write.content.decode("utf-8"))
                identity = previous.get("canonical_id")
                if not isinstance(identity, str) or desired.get("canonical_id") != identity:
                    raise WoonError("native Wiki transaction must preserve canonical identity")
                if self._compiled_wiki.owns_page(
                    identity, output_path=path.relative_to(vault / "wiki").as_posix()
                ):
                    raise WoonError("compiler-owned Wiki page requires a compiler transaction")
                if any(
                    header.get("access") != "local-only" or header.get("publish") is not False
                    for header in (previous, desired)
                ):
                    raise WoonError("native Wiki transaction must preserve private publication")
                validate_record_metadata_update(previous, desired)
                writes[path] = (before, write.content, path.stat().st_mode & 0o777)
            validate_record_collection(
                (path.relative_to(vault).as_posix(), split_markdown(after.decode("utf-8"))[0])
                for path, (_before, after, _mode) in writes.items()
            )
            changed = {path: values for path, values in writes.items() if values[0] != values[1]}
            result: dict[str, object] = {
                "action": "native-wiki-transaction",
                "written": len(changed),
                "compiler_written": 0,
                "catalog_written": 0,
                "ui_verified": False,
            }
            if not changed:
                return result
            _, _, baseline_issues = load_wiki_tree(vault)
            operation_id = "native-wiki-" + uuid.uuid4().hex
            local = vault
            for part in (".local", "woon-knowledge", "native-wiki-transactions", operation_id):
                local /= part
                if local.is_symlink() or (local.exists() and not local.is_dir()):
                    raise WoonError("native Wiki transaction state must use regular directories")
                local.mkdir(exist_ok=True, mode=0o700)
            receipt_path = local / "receipt.json"
            records = []
            for index, (path, (before, after, _mode)) in enumerate(changed.items()):
                backup = local / f"before-{index}.md"
                atomic_write(backup, before, mode=0o600)
                records.append(
                    {
                        "path": path.relative_to(vault).as_posix(),
                        "before_sha256": hashlib.sha256(before).hexdigest(),
                        "after_sha256": hashlib.sha256(after).hexdigest(),
                        "backup": backup.relative_to(vault).as_posix(),
                    }
                )
            owned: dict[Path, bytes] = {}
            try:
                for path, (before, after, mode) in changed.items():
                    if path.read_bytes() != before:
                        raise WoonError("native Wiki source changed before write; replan")
                    atomic_write(path, after, mode=mode)
                    owned[path] = after
                _, _, issues = load_wiki_tree(vault)
                targets = tuple(record["path"] + ":" for record in records)
                blocking = [
                    issue
                    for issue in issues
                    if issue not in baseline_issues or issue.startswith(targets)
                ]
                if blocking:
                    raise WoonError("native Wiki tree validation failed: " + "; ".join(blocking))
                indexed = self._reindex_unlocked()
                if any(path.read_bytes() != after for path, after in owned.items()):
                    raise WoonError(
                        "native Wiki changed during verification; preserve concurrent edit"
                    )
                result.update(
                    operation_id=operation_id,
                    indexed_documents=indexed,
                    changes=records,
                    verification="target-tree-and-index-reread; UI-not-checked",
                )
                content = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
                atomic_write(receipt_path, content, mode=0o600)
                if receipt_path.read_bytes() != content:
                    raise WoonError("native Wiki transaction receipt reread failed")
                result["receipt"] = receipt_path.relative_to(vault).as_posix()
                return result
            except BaseException as error:
                conflicts = []
                for path, after in reversed(tuple(owned.items())):
                    if not path.is_file() or path.read_bytes() != after:
                        conflicts.append(path.relative_to(vault).as_posix())
                        continue
                    before, _after, mode = changed[path]
                    atomic_write(path, before, mode=mode)
                receipt_path.unlink(missing_ok=True)
                try:
                    self._reindex_unlocked()
                except BaseException as recovery:
                    raise WoonError(
                        "native Wiki transaction failed; index recovery failed: "
                        f"{error}; {recovery}"
                    ) from recovery
                if conflicts:
                    raise WoonError(
                        "native Wiki transaction failed; concurrent edits preserved; backups at "
                        f"{local.relative_to(vault)}: " + ", ".join(conflicts)
                    ) from error
                raise

    def _validate_compiled_wiki_transaction_revisions(
        self, transaction: CompiledWikiTransaction
    ) -> None:
        """Re-read compiler output revisions while the repository lock is held."""

        page_ids = tuple(str(page.get("page_id", "")) for page in transaction.pages_upsert)
        if not page_ids:
            raise WoonError("compiled Wiki transaction requires at least one page upsert")
        for page_id in page_ids:
            expected_revision = transaction.expected_revisions.get(page_id)
            current = self._repository.get(page_id)
            if expected_revision is None:
                if current is not None:
                    raise WoonError(
                        "compiled Wiki transaction expected a new page but it already exists: "
                        f"{page_id}"
                    )
                continue
            if current is None:
                raise WoonError(
                    "compiled Wiki transaction expected an existing page but it is missing: "
                    f"{page_id}"
                )
            if current.revision != expected_revision:
                raise WoonError(
                    "compiled Wiki transaction page changed after it was read; "
                    f"reload and merge before writing: {page_id}"
                )
        for retirement in transaction.page_retirements:
            page_id = retirement.page_id
            current = self._repository.get(page_id)
            if current is None or current.revision != retirement.expected_output_sha256:
                raise WoonError(
                    "compiled Wiki retirement page changed after it was read; "
                    f"reload and merge before writing: {page_id}"
                )

    def _prepare_resource_renames(
        self,
        renames: tuple[ResourceFileRename, ...],
        references: tuple[ManualWikiWrite, ...],
    ) -> tuple[
        tuple[tuple[Path, Path, bytes, int], ...],
        tuple[tuple[Path, bytes, bytes, int], ...],
    ]:
        """Validate exact private filenames and active catalog locator replacements."""

        if not renames:
            if references:
                raise WoonError("resource reference writes require resource renames")
            return (), ()
        assert self._compiled_wiki is not None
        vault = self._compiled_wiki.vault.resolve()

        def exact_path(relative: str) -> Path:
            candidate = Path(relative)
            path = vault / candidate
            if (
                not relative
                or candidate.is_absolute()
                or ".." in candidate.parts
                or path.resolve() != path
                or not path.is_relative_to(vault)
            ):
                raise WoonError("resource path must be relative and contain no symlinks")
            return path

        moves: list[tuple[Path, Path, bytes, int]] = []
        touched: set[Path] = set()
        replacements: list[tuple[bytes, bytes]] = []
        for item in renames:
            source, target = exact_path(item.current_path), exact_path(item.target_path)
            if (
                not item.current_path.startswith(
                    ("private/knowledge/local-only/", "private/knowledge/private/career/")
                )
                or source.parent != target.parent
                or source == target
                or source.suffix.lower() not in {".pdf", ".html"}
                or source.suffix != target.suffix
            ):
                raise WoonError("resource rename must preserve its private folder and extension")
            if source in touched or target in touched:
                raise WoonError("resource renames overlap")
            touched.update((source, target))
            if not source.is_file() or target.exists():
                raise WoonError("resource source must exist and target must be absent")
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != item.current_sha256:
                raise WoonError("resource source SHA-256 changed after review")
            moves.append((source, target, content, source.stat().st_mode & 0o777))
            replacements.append((item.current_path.encode(), item.target_path.encode()))

        writes: list[tuple[Path, bytes, bytes, int]] = []
        for reference in references:
            path = exact_path(reference.current_path)
            if (
                not reference.current_path.startswith("catalog/sources/")
                or path.suffix not in {".yaml", ".yml"}
                or reference.target_path != reference.current_path
                or not path.is_file()
                or path in touched
            ):
                raise WoonError("resource reference write must update one existing source catalog")
            touched.add(path)
            before = path.read_bytes()
            after = before
            for old_locator, new_locator in replacements:
                after = after.replace(old_locator, new_locator)
            if (
                before == after
                or reference.content != after
                or hashlib.sha256(before).hexdigest() != reference.current_sha256
                or hashlib.sha256(after).hexdigest() != reference.target_sha256
            ):
                raise WoonError("resource reference write must match reviewed locator changes")
            writes.append((path, before, after, path.stat().st_mode & 0o777))
        return tuple(moves), tuple(writes)

    @staticmethod
    def _restore_resource_renames(
        moves: list[tuple[Path, Path, bytes, int]],
        references: list[tuple[Path, bytes, bytes, int]],
    ) -> None:
        """Restore owned bytes while preserving concurrent edits at either filename."""

        conflicts: list[str] = []
        for path, before, after, mode in reversed(references):
            if path.is_symlink() or not path.is_file() or path.read_bytes() != after:
                conflicts.append(str(path))
                continue
            atomic_write(path, before, mode=mode)
        for source, target, content, mode in reversed(moves):
            if not source.exists() and not source.is_symlink():
                try:
                    with source.open("xb") as stream:
                        os.fchmod(stream.fileno(), mode)
                        stream.write(content)
                except FileExistsError:
                    pass
            if source.is_symlink() or not source.is_file() or source.read_bytes() != content:
                conflicts.append(str(source))
                continue
            if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
                conflicts.append(str(target))
                continue
            target.unlink()
        if conflicts:
            raise WoonError("concurrent resource edits preserved: " + ", ".join(conflicts))

    def _prepare_manual_wiki_writes(
        self,
        compiler_transaction: CompiledWikiTransaction,
        manual_writes: tuple[ManualWikiWrite, ...],
    ) -> tuple[
        dict[Path, tuple[bytes | None, int | None]],
        tuple[tuple[Path, Path | None, bytes | None, int], ...],
    ]:
        """Fail closed before a mixed transaction changes any manual page."""

        compiler = self._compiled_wiki
        if compiler is None:  # pragma: no cover - checked by public caller
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        vault = compiler.vault
        wiki_root = (vault / "wiki").resolve()
        current_compiler_paths = set(compiler.snapshot_outputs())
        future_compiler_paths = {
            (compiler.output_root / str(page.get("output_path", ""))).resolve()
            for page in compiler_transaction.pages_upsert
        }
        protected_paths = current_compiler_paths | future_compiler_paths
        current_paths: set[Path] = set()
        target_paths: set[Path] = set()
        prepared: list[tuple[Path, Path | None, bytes | None, int]] = []

        for index, write in enumerate(manual_writes, start=1):
            label = f"manual write {index}"
            source = self._manual_wiki_path(vault, wiki_root, write.current_path, label)
            retiring = write.target_path is None
            target_path = write.target_path
            target = (
                None
                if retiring
                else self._manual_wiki_path(
                    vault, wiki_root, target_path if target_path is not None else "", label
                )
            )
            if source in current_paths:
                raise WoonError(f"{label} repeats current_path: {write.current_path}")
            if target is not None and target in target_paths:
                raise WoonError(f"{label} repeats target_path: {write.target_path}")
            current_paths.add(source)
            if target is not None:
                target_paths.add(target)
            if source in protected_paths or (target is not None and target in protected_paths):
                raise WoonError(
                    f"{label} touches a compiler-owned Wiki output; use a compiler transaction"
                )
            if source.is_symlink() or not source.is_file():
                raise WoonError(f"{label} current_path is not a regular manual Wiki page")
            actual_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            if write.current_sha256 != actual_sha256:
                raise WoonError(f"{label} current_sha256 does not match: {write.current_path}")
            if retiring:
                if write.target_sha256 is not None or write.content is not None:
                    raise WoonError(
                        f"{label} manual retirement must omit target_sha256 and content"
                    )
                prepared.append((source, None, None, source.stat().st_mode & 0o777))
                continue
            if not isinstance(write.content, bytes):
                raise WoonError(f"{label} content must be bytes")
            if not isinstance(write.target_sha256, str):
                raise WoonError(f"{label} target_sha256 must be a SHA-256 digest")
            target_sha256 = hashlib.sha256(write.content).hexdigest()
            if write.target_sha256 != target_sha256:
                raise WoonError(f"{label} target_sha256 does not match: {write.target_path}")
            validate_record_metadata_update(
                split_markdown(source.read_text(encoding="utf-8"))[0],
                split_markdown(write.content.decode("utf-8"))[0],
            )
            if target is None:  # pragma: no cover - narrowed by the retirement branch
                raise WoonError(f"{label} target_path is required for a manual write")
            prepared.append((source, target, write.content, source.stat().st_mode & 0o777))

        for index, (_source, target, _content, _mode) in enumerate(prepared, start=1):
            if target is not None and target.exists() and target not in current_paths:
                raise WoonError(
                    f"manual write {index} target_path already exists and is not moving in this "
                    f"transaction: {target.relative_to(vault).as_posix()}"
                )

        all_existing_manual_paths = {
            path.resolve()
            for path in iter_wiki_pages(wiki_root)
            if path.resolve() not in current_compiler_paths
        }
        snapshot_paths = all_existing_manual_paths | current_paths | target_paths
        snapshot: dict[Path, tuple[bytes | None, int | None]] = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777)
            if path.is_file()
            else (None, None)
            for path in snapshot_paths
        }
        return snapshot, tuple(prepared)

    def _validate_manual_wikilink_retirements(
        self,
        compiler_transaction: CompiledWikiTransaction,
        writes: tuple[tuple[Path, Path | None, bytes | None, int], ...],
    ) -> None:
        """Require hash-pinned manual rewrites before a compiler target disappears."""

        if not compiler_transaction.page_retirements:
            return
        compiler = self._compiled_wiki
        if compiler is None:  # pragma: no cover - checked by public caller
            raise WoonError("compiled Wiki is not enabled for this knowledge vault")
        vault = compiler.vault
        wiki_root = (vault / "wiki").resolve()
        compiler_paths = set(compiler.snapshot_outputs())
        final_manual: dict[Path, bytes] = {
            path.resolve(): path.read_bytes()
            for path in iter_wiki_pages(wiki_root)
            if path.resolve() not in compiler_paths
        }
        for source, target, content, _mode in writes:
            final_manual.pop(source, None)
            if target is not None and content is not None:
                final_manual[target] = content
        replacements = {
            _manual_wikilink_atom(rewrite.current_target): _manual_wikilink_atom(
                rewrite.replacement_target
            )
            for rewrite in compiler_transaction.wikilink_rewrites
        }
        for retirement in compiler_transaction.page_retirements:
            current = _manual_wikilink_atom(retirement.current_wikilink_target)
            rewritten = replacements.get(current)
            for path, content in final_manual.items():
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError as error:
                    relative = path.relative_to(vault).as_posix()
                    raise WoonError(
                        "manual Wiki page is not UTF-8 while checking retired inbound links: "
                        + relative
                    ) from error
                if not _manual_wikilink_count(text, current):
                    continue
                relative = path.relative_to(vault).as_posix()
                if rewritten is None:
                    raise WoonError(
                        "manual Wiki inbound link to a retired compiler page has no explicit "
                        f"rewrite: {relative}"
                    )
                raise WoonError(
                    "manual Wiki inbound link to a retired compiler page remains after its "
                    f"explicit rewrite: {relative}"
                )

    @staticmethod
    def _manual_wiki_path(vault: Path, wiki_root: Path, relative: str, label: str) -> Path:
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts:
            raise WoonError(f"{label} path escapes the Vault: {relative!r}")
        if any((vault / part).is_symlink() for part in (candidate, *candidate.parents)):
            raise WoonError(f"{label} path must not use symlinks: {relative}")
        resolved = (vault / candidate).resolve()
        if not resolved.is_relative_to(wiki_root):
            raise WoonError(f"{label} path must stay below wiki/: {relative}")
        return resolved

    @staticmethod
    def _apply_manual_wiki_writes(
        writes: tuple[tuple[Path, Path | None, bytes | None, int], ...],
        written: dict[Path, bytes | None],
    ) -> None:
        target_paths = {target for _source, target, _content, _mode in writes if target is not None}
        source_paths = {source for source, _target, _content, _mode in writes}
        for _source, target, content, mode in sorted(
            writes,
            key=lambda item: item[1].as_posix() if item[1] is not None else "",
        ):
            if target is not None and content is not None:
                atomic_write(target, content, mode=mode)
                written[target] = content
        removals = sorted(source_paths.difference(target_paths), key=lambda path: path.as_posix())
        for source in removals:
            source.unlink(missing_ok=True)
            written[source] = None

    @staticmethod
    def _restore_manual_wiki_snapshot(
        snapshot: dict[Path, tuple[bytes | None, int | None]],
        written: dict[Path, bytes | None],
    ) -> None:
        for path, owned in sorted(written.items(), key=lambda item: item[0].as_posix()):
            actual = path.read_bytes() if path.is_file() else None
            if path.is_symlink() or actual != owned:
                continue  # Preserve untouched paths and later user edits.
            content, mode = snapshot[path]
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content, mode=mode or 0o644)


def _fingerprint(value: str) -> str:
    normalized = normalize_identity(value)
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
