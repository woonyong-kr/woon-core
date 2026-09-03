"""Deterministic source-schema compiler for private LLM Wiki pages.

The compiler keeps its editable inputs outside ``wiki/``.  A compiled page is
therefore recoverable from a source record, accepted claim records, and a page
specification instead of becoming an untracked AI rewrite.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from PIL import Image
from pypdf import PdfReader

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.book_contract import (
    BOOK_WORKFLOW_PHASES,
    book_reader_workflow_prose_violation,
    book_workflow_phase_index,
)
from woon_core.knowledge.book_coverage import (
    LEGACY_SCHEMA_VERSION as LEGACY_BOOK_COVERAGE_SCHEMA_VERSION,
)
from woon_core.knowledge.book_coverage import (
    SCHEMA_VERSION as BOOK_COVERAGE_SCHEMA_VERSION,
)
from woon_core.knowledge.book_coverage import audit_book_coverage, audit_book_coverage_scope
from woon_core.knowledge.book_intake import audit_book_intake
from woon_core.knowledge.book_rights import (
    BookRightsDemotion,
    BookRightsDemotionReport,
)
from woon_core.knowledge.domain import DocumentMetadata
from woon_core.knowledge.wiki_tree import (
    apply_wiki_tree_refresh,
    load_wiki_tree,
    prepare_wiki_tree_refresh,
    strip_generated_wiki_views,
)
from woon_core.knowledge.wiki_tree_migration import rewrite_retired_map_links
from woon_core.knowledge.woon_wiki import (
    compiled_wiki_contract,
    preserve_managed_context,
)

FRONTMATTER = re.compile(r"\A---\n(?P<yaml>[\s\S]*?)\n---\n?(?P<body>[\s\S]*)\Z")
H1 = re.compile(r"\A(?:\n)*#\s+(?P<title>.+?)\s*\n(?:\n)?")
COMPILED_KEY = "llm_wiki"
SCHEMA_VERSION = 1
MAX_COMPOSED_CLAIM_MARKDOWN_CHARS = 1_800
MANUAL_ARCHIVE_ORIGINS = {"manual-reviewed", "verified-source"}
GIT_RESTORE_ARCHIVE_ORIGIN = "git-restore"
MERMAID_COLOR_DIRECTIVE_RE = re.compile(
    r"^\s*(?:style\s+\S+|classDef\s+\S+|linkStyle\s+\S+)"
    r"[^\n]*(?:fill|stroke|color)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
MERMAID_THEME_COLOR_RE = re.compile(
    r"%%\{init:[^\n]*(?:themeVariables|themeCSS)",
    re.IGNORECASE,
)
MARKDOWN_ASSET_RE = re.compile(r"!\[[^\]]*\]\((?P<path>[^)]+)\)")
OBSIDIAN_ASSET_RE = re.compile(r"!\[\[(?P<path>[^\]|#]+)")


@dataclass(frozen=True, slots=True)
class CompiledWikiSettings:
    """Resolved repository-relative locations for the compiler inputs and outputs."""

    vault: Path
    output_root: Path
    sources_path: Path
    claims_path: Path
    pages_path: Path
    curation_path: Path
    relations_path: Path
    receipts_path: Path
    review_queue_path: Path


@dataclass(frozen=True, slots=True)
class CompileReport:
    """Observable result of one deterministic compiler invocation."""

    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Result of converting direct Markdown pages into compiler inputs."""

    migrated: int
    skipped: int
    compiled: int
    unchanged: int


@dataclass(frozen=True, slots=True)
class CompilationAudit:
    """Compiler health used by MCP, CLI, and the retrieval freshness gate."""

    pages: int
    receipts: int
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class RevisionReconciliationReport:
    """Non-destructive normalization result for repeated conversation archives."""

    archived_sources: int
    superseded_claims: int


@dataclass(frozen=True, slots=True)
class CuratedRevision:
    """One verified reader-facing rewrite derived from an existing Wiki page."""

    page_id: str
    body: str
    statement: str
    current_use: str | None = None


@dataclass(frozen=True, slots=True)
class CuratedRevisionReport:
    """Result of preserving raw inputs while promoting curated revisions."""

    curated: int
    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]
    staged_asset_count: int = 0
    unchanged_asset_count: int = 0


@dataclass(frozen=True, slots=True)
class VerifiedBookPage:
    """One source-bound book page approved for compiler-backed promotion."""

    page_id: str
    title: str
    body: str
    statement: str
    current_use: str
    source_locator: str
    source_sha256: str
    frontmatter: dict[str, Any]
    expected_revision: str | None = None


@dataclass(frozen=True, slots=True)
class BookCoverageManifestUpdate:
    """One optimistic full replacement or verified-scope fragment update.

    ``replace`` rewrites one schema-v2 full-book manifest. ``merge-scope``
    leaves the pre-existing full-book manifest byte-for-byte unchanged and
    writes one schema-v2 fragment below ``catalog/book-coverage-scopes``.  The
    base path and both hashes are pinned so a scoped review cannot be applied
    to a different table of contents or overwrite a newer scope review.
    """

    relative_path: str
    expected_sha256: str | None
    replacement: dict[str, Any]
    mode: str = "replace"
    base_relative_path: str | None = None
    base_expected_sha256: str | None = None
    scope_root_id: str | None = None


@dataclass(frozen=True, slots=True)
class StagedBookAsset:
    """One byte-pinned private source image staged beside a promotion payload."""

    staging_path: Path
    archive_relative_path: str
    sha256: str
    size: int
    provenance: str
    source_entry_locator: str


@dataclass(frozen=True, slots=True)
class RetiredPageReport:
    """Result of merging obsolete compiled pages into surviving canonical pages."""

    retired: int
    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]
    replacement_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerifiedBookUpdateReport:
    """One atomic verified-book promotion and zero-content page retirement."""

    curated: int
    retired: int
    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]
    retired_page_ids: tuple[str, ...]
    replacement_ids: tuple[str, ...]
    staged_asset_count: int = 0
    unchanged_asset_count: int = 0


@dataclass(frozen=True, slots=True)
class VerifiedBookPreflightReport:
    """Read-only optimistic validation before one verified-book mutation."""

    ready: bool
    page_count: int
    retirement_count: int
    coverage_mode: str
    coverage_path: str
    base_manifest_preserved: bool
    staged_asset_count: int = 0
    unchanged_asset_count: int = 0


@dataclass(frozen=True, slots=True)
class BookRightsDemotionSnapshot:
    """Byte-exact state needed to recover a rights demotion after index failure."""

    inputs: dict[Path, bytes | None]
    outputs: dict[Path, bytes | None]
    assets: dict[Path, bytes]
    quarantine_path: Path


@dataclass(frozen=True, slots=True)
class CompiledWikiTransaction:
    """Validated source/claim/page/curation upserts for one compiler transaction."""

    expected_revisions: dict[str, str | None]
    sources_upsert: tuple[dict[str, Any], ...]
    claims_upsert: tuple[dict[str, Any], ...]
    pages_upsert: tuple[dict[str, Any], ...]
    curations_upsert: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class CompiledWikiTransactionReport:
    """Observable result of one atomic compiled-Wiki catalog transaction."""

    sources_upserted: int
    claims_upserted: int
    pages_upserted: int
    curations_upserted: int
    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]


class CompiledWiki:
    """Compile and audit one private source-schema Wiki without model calls."""

    def __init__(self, settings: CompiledWikiSettings) -> None:
        self._settings = settings
        self._last_input_state: tuple[tuple[str, int, int], ...] | None = None

    @property
    def enabled(self) -> bool:
        return True

    @property
    def vault(self) -> Path:
        """Return the validated Vault root for cross-catalog policy checks."""

        return self._settings.vault

    def migrate(self) -> MigrationReport:
        """Capture existing Wiki Markdown as lossless source-backed page specs once."""

        if any(path.exists() for path in self._input_paths()):
            raise WoonError(
                "compiled Wiki inputs already exist; refuse to replace them during migration"
            )
        pages = self._discover_pages()
        if not pages:
            raise WoonError("compiled Wiki migration found no Markdown pages")

        source_records: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        specs: list[dict[str, Any]] = []
        curations: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for relative, text in pages:
            frontmatter, title, body = _parse_markdown(text, relative)
            page_id = relative.with_suffix("").as_posix()
            source_id = f"source://legacy-wiki/{quote(relative.as_posix(), safe='/._-')}"
            claim_id = f"claim://legacy-wiki/{quote(page_id, safe='/._-')}"
            source_records.append(
                {
                    "source_id": source_id,
                    "kind": "legacy-wiki",
                    "locator": relative.as_posix(),
                    "original_sha256": _sha256_text(text),
                    "normalized_sha256": _sha256_text(_normalize(body)),
                    "privacy": str(frontmatter.get("access", "local-only")),
                    "lifecycle": "compiled",
                    "title": title,
                    "body": body,
                }
            )
            claims.append(
                {
                    "claim_id": claim_id,
                    "kind": "legacy-document",
                    "status": "accepted",
                    "statement": title,
                    "source_ids": [source_id],
                    "markdown": "",
                }
            )
            specs.append(
                {
                    "page_id": page_id,
                    "output_path": relative.as_posix(),
                    "title": title,
                    "frontmatter": frontmatter,
                    "source_ids": [source_id],
                    "claim_ids": [claim_id],
                    "render": {"kind": "source-body", "source_id": source_id},
                }
            )
            curations.append(_initial_curation(page_id, title, frontmatter, source_records[-1:]))
            relations.extend(_relations_for(page_id, frontmatter))

        snapshot = self.snapshot_inputs()
        try:
            _write_yaml(
                self._settings.sources_path,
                {"version": SCHEMA_VERSION, "sources": source_records},
            )
            _write_yaml(self._settings.claims_path, {"version": SCHEMA_VERSION, "claims": claims})
            _write_yaml(self._settings.pages_path, {"version": SCHEMA_VERSION, "pages": specs})
            _write_yaml(
                self._settings.curation_path,
                {"version": SCHEMA_VERSION, "curations": curations},
            )
            _write_yaml(
                self._settings.relations_path,
                {"version": SCHEMA_VERSION, "relations": relations},
            )
            _write_yaml(
                self._settings.review_queue_path,
                {"version": SCHEMA_VERSION, "items": []},
            )
            _write_yaml(self._settings.receipts_path, {"version": SCHEMA_VERSION, "receipts": []})
            report = self.compile(force=True)
        except Exception:
            self.restore_inputs(snapshot)
            raise
        return MigrationReport(
            migrated=len(pages),
            skipped=0,
            compiled=report.compiled,
            unchanged=report.unchanged,
        )

    def compile(self, *, force: bool = False, page_ids: tuple[str, ...] = ()) -> CompileReport:
        """Compile stale or requested pages only after validating every input relation."""

        sources, claims, pages, curations, receipts = self._load_inputs()
        review_items = _load_yaml_list(self._settings.review_queue_path, "items")
        for source in sources.values():
            _validate_archive_review_binding(source, review_items)
        selected = set(page_ids)
        unknown = selected.difference(pages)
        if unknown:
            raise WoonError(f"compiled Wiki page spec not found: {sorted(unknown)[0]}")
        compiled = 0
        unchanged = 0
        changed_ids: list[str] = []
        # A page identity or output path may be migrated, but an old receipt
        # must never keep the retired page alive as a second canonical branch.
        updated_receipts = {
            page_id: receipt for page_id, receipt in receipts.items() if page_id in pages
        }
        receipt_changed = updated_receipts != receipts
        writes: list[tuple[Path, str]] = []

        for page_id in sorted(pages):
            if selected and page_id not in selected:
                continue
            page = pages[page_id]
            curation = self._page_curation(page, curations)
            source_records = self._page_sources(page, sources)
            claim_records = self._page_claims(page, claims)
            _validate_page(page, source_records, claim_records, curation)
            input_sha256 = _input_hash(page, source_records, claim_records, curation)
            output_path = _inside(
                self._settings.output_root, page["output_path"], "page output_path"
            )
            rendered = _render_page(page, source_records, claim_records, curation, input_sha256)
            compiler_projection_sha256 = _sha256_text(preserve_managed_context("", rendered))
            existing_text = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
            output = preserve_managed_context(existing_text, rendered)
            receipt = receipts.get(page_id)
            expected_hash = _sha256_text(output)
            if (
                not force
                and receipt is not None
                and receipt.get("input_sha256") == input_sha256
                and receipt.get("compiler_projection_sha256") == compiler_projection_sha256
                and receipt.get("output_sha256") == expected_hash
                and existing_text == output
            ):
                unchanged += 1
                continue
            writes.append((output_path, output))
            updated_receipts[page_id] = {
                "page_id": page_id,
                "compiler": "woon-core/llm-wiki-v1",
                "input_sha256": input_sha256,
                "output_sha256": expected_hash,
                "compiler_projection_sha256": compiler_projection_sha256,
                "source_ids": [record["source_id"] for record in source_records],
                "claim_ids": [record["claim_id"] for record in claim_records],
                "checks": [
                    "schema",
                    "source-provenance",
                    "accepted-claims",
                    "frontmatter-h1",
                    "privacy",
                ],
            }
            compiled += 1
            changed_ids.append(page_id)

        relation_records = _expected_relations(pages)
        try:
            current_relations = _load_yaml_list(self._settings.relations_path, "relations")
            relation_changed = current_relations != relation_records
        except WoonError:
            relation_changed = True
        if not writes and not relation_changed and not receipt_changed:
            self._last_input_state = None
            return CompileReport(compiled, unchanged, tuple(changed_ids))

        snapshots = [(path, path.read_bytes() if path.is_file() else None) for path, _ in writes]
        receipt_snapshot = (
            self._settings.receipts_path.read_bytes()
            if self._settings.receipts_path.is_file()
            else None
        )
        relation_snapshot = (
            self._settings.relations_path.read_bytes()
            if self._settings.relations_path.is_file()
            else None
        )
        try:
            for path, output in writes:
                atomic_write(path, output.encode("utf-8"))
            if relation_changed:
                _write_yaml(
                    self._settings.relations_path,
                    {"version": SCHEMA_VERSION, "relations": relation_records},
                )
            if writes or receipt_changed:
                _write_yaml(
                    self._settings.receipts_path,
                    {
                        "version": SCHEMA_VERSION,
                        "receipts": [updated_receipts[key] for key in sorted(updated_receipts)],
                    },
                )
        except Exception:
            for path, snapshot in snapshots:
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, snapshot)
            if receipt_snapshot is None:
                self._settings.receipts_path.unlink(missing_ok=True)
            else:
                atomic_write(self._settings.receipts_path, receipt_snapshot)
            if relation_snapshot is None:
                self._settings.relations_path.unlink(missing_ok=True)
            else:
                atomic_write(self._settings.relations_path, relation_snapshot)
            raise
        self._last_input_state = None
        return CompileReport(compiled, unchanged, tuple(changed_ids))

    def owns_page(self, page_id: str) -> bool:
        """Return whether this compiler owns the reader-facing page identity."""

        _, _, pages, _, _ = self._load_inputs()
        return page_id in pages

    def archive(
        self,
        metadata: DocumentMetadata,
        body: str,
        source_session_ids: tuple[str, ...],
        *,
        archive_origin: str = "manual-reviewed",
        approved_review_id: str | None = None,
    ) -> CompileReport:
        """Turn a manually reviewed body into compiler inputs.

        This is deliberately not an automation ingestion endpoint. Mail, chat,
        Novel, system, tool, and reasoning payloads require a separate
        privacy-minimizing projector and cannot be written through this method.
        """

        if archive_origin not in MANUAL_ARCHIVE_ORIGINS:
            raise WoonError(
                "compiled archive only accepts manual-reviewed or verified-source inputs"
            )

        return self._archive_compiled_input(
            metadata,
            body,
            source_session_ids,
            archive_origin=archive_origin,
            approved_review_id=approved_review_id,
        )

    def restore_from_git(
        self,
        metadata: DocumentMetadata,
        body: str,
        git_revision: str,
        expected_body_sha256: str,
    ) -> CompileReport:
        """Restore a human-confirmed Git revision without a mutable review-queue entry.

        This narrow path is intentionally separate from :meth:`archive`: the
        caller must already have read the body from the requested Git revision
        and bind that exact body hash to it.  It is for recovery only, never
        for mail/chat ingestion or normal archival.
        """

        revision = git_revision.strip()
        if not revision:
            raise WoonError("compiled Git restore requires a Git revision")
        body_hash = _sha256_text(_normalize(body))
        if body_hash != expected_body_sha256:
            raise WoonError("compiled Git restore body hash does not match the requested revision")
        return self._archive_compiled_input(
            metadata,
            body,
            (f"git:{revision}",),
            archive_origin=GIT_RESTORE_ARCHIVE_ORIGIN,
            approved_review_id=None,
        )

    def _archive_compiled_input(
        self,
        metadata: DocumentMetadata,
        body: str,
        source_session_ids: tuple[str, ...],
        *,
        archive_origin: str,
        approved_review_id: str | None,
    ) -> CompileReport:
        sources, claims, pages, curations, _ = self._load_inputs()
        # The compiler and the conversation archive share the same Wiki
        # identity.  A verification state is metadata, never a second output
        # root below or beside ``wiki/``.
        page_id = metadata.canonical_id
        body_hash = _sha256_text(_normalize(body))
        if archive_origin in MANUAL_ARCHIVE_ORIGINS:
            self._require_approved_archive_review(approved_review_id, body_hash)
        elif archive_origin == GIT_RESTORE_ARCHIVE_ORIGIN:
            if approved_review_id is not None:
                raise WoonError("compiled Git restore must not accept a review approval")
        else:
            raise WoonError("compiled archive_origin is invalid")
        source_id = f"source://conversation/{metadata.canonical_id}/{body_hash[:24]}"
        claim_id = f"claim://conversation/{metadata.canonical_id}/{body_hash[:24]}"
        previous_page = pages.get(page_id)
        sources[source_id] = {
            "source_id": source_id,
            "kind": "conversation",
            "locator": metadata.canonical_id,
            "original_sha256": body_hash,
            "normalized_sha256": body_hash,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": metadata.title,
            "purpose": metadata.purpose,
            "archive_origin": archive_origin,
            "approved_review_id": approved_review_id,
            "body": body.rstrip() + "\n",
            "source_session_ids": list(source_session_ids),
        }
        claims[claim_id] = {
            "claim_id": claim_id,
            "kind": "conversation-summary",
            "status": "accepted",
            "statement": metadata.summary,
            "source_ids": [source_id],
            "markdown": body.rstrip() + "\n",
        }
        if previous_page is not None:
            _supersede_replaced_conversation_revision(
                previous_page,
                pages,
                page_id,
                sources,
                claims,
                metadata.canonical_id,
                source_id,
                claim_id,
            )
        frontmatter = _canonical_frontmatter(metadata)
        # Preserve external session ownership in the rendered canonical document.
        # The compiler source id remains in the page spec and receipt provenance.
        frontmatter["source_ids"] = list(source_session_ids) or [source_id]
        pages[page_id] = {
            "page_id": page_id,
            "output_path": f"{metadata.canonical_id}.md",
            "title": metadata.title,
            "frontmatter": frontmatter,
            "source_ids": [source_id],
            "claim_ids": [claim_id],
            "render": {"kind": "claims"},
        }
        curations[page_id] = {
            "page_id": page_id,
            "current_use": metadata.purpose,
            "basis": "archive-request",
            "status": "confirmed",
        }
        self._write_inputs(sources, claims, pages, curations)
        return self.compile(page_ids=(page_id,))

    def _require_approved_archive_review(self, review_id: str | None, body_hash: str) -> None:
        """Require an immutable human approval bound to the exact archived body."""

        if not isinstance(review_id, str) or not review_id.strip():
            raise WoonError("compiled archive requires approved_review_id")
        for item in _load_yaml_list(self._settings.review_queue_path, "items"):
            if item.get("candidate_id") != review_id:
                continue
            if (
                item.get("status") == "approved"
                and item.get("kind") == "manual-archive"
                and item.get("input_sha256") == body_hash
                and isinstance(item.get("approved_by"), str)
                and item["approved_by"].strip()
            ):
                return
            break
        raise WoonError("compiled archive requires an approved review bound to the input hash")

    def curate_revisions(self, revisions: tuple[CuratedRevision, ...]) -> CuratedRevisionReport:
        """Promote reviewed prose without overwriting the legacy source it came from.

        A reader-facing revision is a new ``curated-wiki`` source.  The prior
        source and claim remain in the catalog for provenance, while the page
        renders the new source body.  Repeating the operation archives only a
        previous curated revision that is no longer referenced by another page.
        """

        if not revisions:
            raise WoonError("curated revision requires at least one page")
        sources, claims, pages, curations, _ = self._load_inputs()
        snapshot = self.snapshot_inputs()
        requested_ids = [revision.page_id for revision in revisions]
        if len(set(requested_ids)) != len(requested_ids):
            raise WoonError("curated revision contains a duplicate page_id")

        changed: list[str] = []
        try:
            for revision in sorted(revisions, key=lambda item: item.page_id):
                page_id = _required_string({"page_id": revision.page_id}, "page_id")
                page = pages.get(page_id)
                if page is None:
                    raise WoonError(f"compiled Wiki page spec not found: {page_id}")
                body = _curated_body(revision.body)
                statement = _required_string({"statement": revision.statement}, "statement")
                curation = self._page_curation(page, curations)
                current_use = (
                    _required_string({"current_use": revision.current_use}, "current_use")
                    if revision.current_use is not None
                    else curation["current_use"]
                )
                render = page.get("render")
                if not isinstance(render, dict):
                    raise WoonError("page render must be a mapping")
                render_kind = render.get("kind")
                if render_kind not in {"source-body", "claims"}:
                    raise WoonError("curated revision requires a supported page render")

                current_source_id = (
                    _required_string(render, "source_id") if render_kind == "source-body" else None
                )
                if current_source_id is not None and current_source_id not in sources:
                    raise WoonError("curated revision render source does not exist")

                normalized_hash = _sha256_text(_normalize(body))
                source_id = (
                    f"source://curated-wiki/{quote(page_id, safe='/._-')}/{normalized_hash[:24]}"
                )
                claim_id = (
                    f"claim://curated-wiki/{quote(page_id, safe='/._-')}/{normalized_hash[:24]}"
                )
                if source_id in sources or claim_id in claims:
                    raise WoonError("curated revision already exists in the compiler catalog")

                frontmatter = page.get("frontmatter")
                if not isinstance(frontmatter, dict):
                    raise WoonError("page frontmatter must be a mapping")
                title = _required_string(page, "title")
                privacy = "public" if frontmatter.get("access") == "public" else "local-only"
                sources[source_id] = {
                    "source_id": source_id,
                    "kind": "curated-wiki",
                    "locator": f"curation/{page_id}/{normalized_hash[:24]}",
                    "original_sha256": _sha256_text(body),
                    "normalized_sha256": normalized_hash,
                    "privacy": privacy,
                    "lifecycle": "compiled",
                    "title": title,
                    "purpose": current_use,
                    "body": body,
                }
                claims[claim_id] = {
                    "claim_id": claim_id,
                    "kind": "curated-document",
                    "status": "accepted",
                    "statement": statement,
                    "source_ids": [source_id],
                    "markdown": statement + "\n",
                }

                source_ids = _string_list(page.get("source_ids"), "page source_ids")
                claim_ids = _string_list(page.get("claim_ids"), "page claim_ids")
                if (
                    current_source_id is not None
                    and sources[current_source_id].get("kind") == "curated-wiki"
                ):
                    self._supersede_unshared_curated_source(
                        current_source_id, source_id, page_id, pages, sources
                    )
                    source_ids = [value for value in source_ids if value != current_source_id]
                    claim_ids = self._supersede_unshared_curated_claims(
                        claim_ids, current_source_id, claim_id, page_id, pages, claims
                    )
                page["source_ids"] = list(dict.fromkeys([*source_ids, source_id]))
                page["claim_ids"] = list(dict.fromkeys([*claim_ids, claim_id]))
                if privacy == "public":
                    # Historical local-only revisions remain in the catalog,
                    # but they are not current provenance for a newly reviewed
                    # public page. Keeping them in the page dependency list
                    # would make a safe public curation impossible forever.
                    removed_source_ids = [
                        value
                        for value in page["source_ids"]
                        if sources[value].get("privacy") != "public"
                    ]
                    page["source_ids"] = [
                        value
                        for value in page["source_ids"]
                        if sources[value].get("privacy") == "public"
                    ]
                    removed_claim_ids = [
                        value
                        for value in page["claim_ids"]
                        if not all(
                            sources[claim_source].get("privacy") == "public"
                            for claim_source in _string_list(
                                claims[value].get("source_ids"), "claim source_ids"
                            )
                        )
                    ]
                    page["claim_ids"] = [
                        value for value in page["claim_ids"] if value not in removed_claim_ids
                    ]
                    for prior_source_id in removed_source_ids:
                        self._supersede_unshared_curated_source(
                            prior_source_id, source_id, page_id, pages, sources
                        )
                    self._supersede_unshared_claims(
                        removed_claim_ids, claim_id, page_id, pages, claims
                    )
                page["render"] = {"kind": "source-body", "source_id": source_id}
                curation.update(
                    {
                        "current_use": current_use,
                        "basis": "curated-revision",
                        "status": "confirmed",
                    }
                )
                changed.append(page_id)

            self._write_inputs(sources, claims, pages, curations)
            compile_report = self.compile(page_ids=tuple(changed))
        except Exception:
            self.restore_inputs(snapshot)
            raise
        return CuratedRevisionReport(
            curated=len(changed),
            compiled=compile_report.compiled,
            unchanged=compile_report.unchanged,
            page_ids=tuple(changed),
        )

    def promote_verified_book_pages(
        self, records: tuple[VerifiedBookPage, ...]
    ) -> CuratedRevisionReport:
        """Promote non-empty, source-located book pages without creating shell outputs."""

        sources, claims, pages, curations, _ = self._load_inputs()
        snapshot = self.snapshot_inputs()
        try:
            changed = self._apply_verified_book_records(records, sources, claims, pages, curations)
            if changed:
                self._write_inputs(sources, claims, pages, curations)
            requested_ids = tuple(record.page_id for record in records)
            report = self.compile(page_ids=requested_ids)
        except Exception:
            self.restore_inputs(snapshot)
            raise
        return CuratedRevisionReport(
            curated=len(changed),
            compiled=report.compiled,
            unchanged=report.unchanged,
            page_ids=tuple(changed),
        )

    def apply_verified_book_update(
        self,
        records: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate | None = None,
        *,
        rights_restore_book_id: str | None = None,
    ) -> VerifiedBookUpdateReport:
        """Promote pages and retire obsolete wrappers in one compiler transaction.

        The method writes compiler inputs once, compiles the final topology once,
        and makes no intermediate tree observable. Inputs and every generated
        output are restored byte-for-byte if validation, compilation, deletion,
        or the final audit fails. Search index atomicity is owned by
        :class:`KnowledgeService`, which wraps this method under the repository
        lock and reindexes only after the compiler transaction succeeds.
        """

        if set(retirement_body_sha256) != set(replacements):
            raise WoonError("verified book update retirement_body_sha256 must match replacements")
        if any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in retirement_body_sha256.values()
        ):
            raise WoonError(
                "verified book update retirement_body_sha256 values must be lowercase SHA-256"
            )
        coverage_path: Path | None = None
        coverage_bytes: bytes | None = None
        coverage_errors_before: set[str] = set()
        coverage_book_id = ""
        if coverage_manifest is not None:
            coverage_path, coverage_bytes = self._validated_coverage_manifest_update(
                coverage_manifest
            )
            if coverage_manifest.mode == "replace":
                coverage_errors_before = set(audit_book_coverage(self._settings.vault).errors)
            replacement_book_id = coverage_manifest.replacement.get("book_id")
            if isinstance(replacement_book_id, str):
                coverage_book_id = replacement_book_id.strip()
        sources, claims, pages, curations, _ = self._load_inputs()
        carry_forward_ids: set[str] = set()
        active_records = records
        if rights_restore_book_id is not None:
            carry_forward_ids = self._validate_book_rights_restore_records(
                records,
                sources,
                pages,
                expected_book_id=rights_restore_book_id,
            )
            active_records = tuple(
                record for record in records if record.page_id not in carry_forward_ids
            )
        input_snapshot = self.snapshot_inputs(
            extra_paths=(coverage_path,) if coverage_path is not None else ()
        )
        output_snapshot = self.snapshot_outputs(
            extra_relative_paths=tuple(f"{record.page_id}.md" for record in records)
        )
        normalized = {
            _required_string({"page_id": page_id}, "page_id"): _required_string(
                {"replacement_id": replacement_id}, "replacement_id"
            )
            for page_id, replacement_id in replacements.items()
        }
        if any(page_id == replacement_id for page_id, replacement_id in normalized.items()):
            raise WoonError("compiled page cannot replace itself")
        retiring = set(normalized)
        if retiring.intersection(normalized.values()):
            raise WoonError("compiled page replacement must survive this retirement")
        promoted_ids = {record.page_id for record in records}
        overlap = promoted_ids.intersection(retiring)
        if overlap:
            raise WoonError(
                "verified book update cannot promote and retire the same page: "
                f"{sorted(overlap)[0]}"
            )
        unverified_survivors = set(normalized.values()).difference(promoted_ids)
        if unverified_survivors:
            raise WoonError(
                "verified book update replacement must be included in promoted pages: "
                f"{sorted(unverified_survivors)[0]}"
            )
        self._validate_verified_book_retirement_content(
            records,
            normalized,
            retirement_body_sha256,
            sources,
            pages,
        )

        # Validate against the current topology before promotion rewrites a
        # parent map's navigation_groups.  A structural wrapper may own its
        # children itself, through page-spec parent edges, or be the exact
        # navigation child selected by one parent map.
        structural_wrappers: set[str] = set()
        for page_id in retiring:
            page = pages.get(page_id)
            if not isinstance(page, dict):
                continue
            frontmatter = page.get("frontmatter")
            groups = frontmatter.get("navigation_groups") if isinstance(frontmatter, dict) else None
            has_group_children = isinstance(groups, list) and bool(groups)
            has_parent_children = any(
                isinstance(candidate.get("frontmatter"), dict)
                and _canonical_parent_id(candidate["frontmatter"].get("parent")) == page_id
                for candidate_id, candidate in pages.items()
                if candidate_id not in retiring and candidate_id != page_id
            )
            referenced_as_navigation_child = any(
                page_id in _navigation_group_children(candidate.get("frontmatter"))
                for candidate_id, candidate in pages.items()
                if candidate_id != page_id
            )
            if has_group_children or has_parent_children or referenced_as_navigation_child:
                structural_wrappers.add(page_id)
        invalid_wrappers = retiring.difference(structural_wrappers)
        if invalid_wrappers:
            raise WoonError(
                "verified book retirement is not a navigation wrapper: "
                f"{sorted(invalid_wrappers)[0]}"
            )

        changed: list[str] = []
        affected: set[str] = set()
        retired_outputs: list[Path] = []
        try:
            changed = self._apply_verified_book_records(
                active_records, sources, claims, pages, curations
            )
            if rights_restore_book_id is not None:
                self._retire_book_rights_decisions(
                    rights_restore_book_id,
                    records,
                    sources,
                    claims,
                    pages,
                    restored_page_ids={record.page_id for record in active_records},
                )
            unknown = retiring.difference(pages)
            if unknown:
                raise WoonError(f"compiled Wiki page spec not found: {sorted(unknown)[0]}")
            unknown_replacements = set(normalized.values()).difference(pages)
            if unknown_replacements:
                raise WoonError(
                    "compiled Wiki replacement page spec not found: "
                    f"{sorted(unknown_replacements)[0]}"
                )
            for page_id, page in pages.items():
                if page_id in retiring:
                    continue
                frontmatter = page.get("frontmatter")
                if not isinstance(frontmatter, dict):
                    raise WoonError("page frontmatter must be a mapping")
                if _redirect_frontmatter_relations(frontmatter, normalized):
                    affected.add(page_id)

            for page_id, replacement_id in sorted(normalized.items()):
                retired_page = pages[page_id]
                replacement_page = pages[replacement_id]
                replacement_sources = self._page_sources(replacement_page, sources)
                replacement_claims = self._page_claims(replacement_page, claims)
                successor_source_id = _current_source_id(replacement_page, replacement_sources)
                successor_claim_id = _current_claim_id(replacement_page, replacement_claims)
                remaining_pages = {
                    other_id: other_page
                    for other_id, other_page in pages.items()
                    if other_id not in retiring
                }
                for source_id in _string_list(retired_page.get("source_ids"), "page source_ids"):
                    used_elsewhere = any(
                        source_id in _string_list(other_page.get("source_ids"), "page source_ids")
                        for other_page in remaining_pages.values()
                    )
                    if not used_elsewhere and sources[source_id].get("lifecycle") == "compiled":
                        sources[source_id].update(
                            {"lifecycle": "archived", "superseded_by": successor_source_id}
                        )
                for claim_id in _string_list(retired_page.get("claim_ids"), "page claim_ids"):
                    used_elsewhere = any(
                        claim_id in _string_list(other_page.get("claim_ids"), "page claim_ids")
                        for other_page in remaining_pages.values()
                    )
                    if not used_elsewhere and claims[claim_id].get("status") == "accepted":
                        claims[claim_id].update(
                            {"status": "superseded", "superseded_by": successor_claim_id}
                        )
                retired_outputs.append(
                    _inside(
                        self._settings.output_root,
                        retired_page["output_path"],
                        "page output_path",
                    )
                )
                del pages[page_id]
                del curations[page_id]
                affected.add(replacement_id)

            affected.update(record.page_id for record in active_records)
            affected.difference_update(retiring)
            self._write_inputs(sources, claims, pages, curations)
            if coverage_path is not None and coverage_bytes is not None:
                atomic_write(coverage_path, coverage_bytes)
            compile_report = self.compile(page_ids=tuple(sorted(affected)))
            for output_path in retired_outputs:
                output_path.unlink(missing_ok=True)
            book_root_id = (
                coverage_manifest.scope_root_id
                if coverage_manifest is not None
                and coverage_manifest.mode == "merge-scope"
                and coverage_manifest.scope_root_id is not None
                else _verified_book_root_id(records, coverage_book_id)
            )
            tree_report = prepare_wiki_tree_refresh(
                self._settings.vault,
                canonical_prefix=book_root_id,
            )
            if tree_report.issues:
                raise WoonError(
                    f"verified book update could not refresh its Wiki tree: {tree_report.issues[0]}"
                )
            apply_wiki_tree_refresh(self._settings.vault, tree_report)
            self._refresh_generated_view_receipts(tuple(tree_report.pages))
            audit = self.audit()
            if not audit.complete:
                raise WoonError(f"verified book update left a stale catalog: {audit.errors[0]}")
            if coverage_manifest is not None:
                if coverage_manifest.mode == "merge-scope":
                    if coverage_path is None:  # pragma: no cover - validated above
                        raise WoonError("scoped book coverage path is missing")
                    scoped_relative = coverage_path.relative_to(self._settings.vault).as_posix()
                    coverage_audit = audit_book_coverage_scope(
                        self._settings.vault,
                        scoped_relative,
                    )
                    coverage_errors = set(coverage_audit.errors)
                    if coverage_errors:
                        first_error = _first_actionable_coverage_error(coverage_errors)
                        raise WoonError(
                            f"verified book update left stale scoped book coverage: {first_error}"
                        )
                    base_path = _inside(
                        self._settings.vault,
                        coverage_manifest.base_relative_path or "",
                        "book coverage base manifest path",
                    )
                    if _sha256_bytes(base_path.read_bytes()) != (
                        coverage_manifest.base_expected_sha256
                    ):
                        raise WoonError(
                            "verified book update changed its pinned base coverage manifest"
                        )
                    continue_coverage_audit = False
                else:
                    coverage_audit = audit_book_coverage(self._settings.vault)
                    new_coverage_errors = set(coverage_audit.errors).difference(
                        coverage_errors_before
                    )
                    target_markers = {
                        marker
                        for marker in (
                            coverage_book_id,
                            coverage_path.relative_to(self._settings.vault).as_posix()
                            if coverage_path is not None
                            else "",
                        )
                        if marker
                    }
                    target_errors = {
                        error
                        for error in coverage_audit.errors
                        if any(marker in error for marker in target_markers)
                    }
                    coverage_errors = new_coverage_errors or target_errors
                    continue_coverage_audit = True
                if continue_coverage_audit and coverage_errors:
                    first_error = _first_actionable_coverage_error(coverage_errors)
                    raise WoonError(f"verified book update left stale book coverage: {first_error}")
        except BaseException:
            self.restore_inputs(input_snapshot)
            self.restore_outputs(output_snapshot)
            raise

        return VerifiedBookUpdateReport(
            curated=len(changed),
            retired=len(normalized),
            compiled=compile_report.compiled,
            unchanged=compile_report.unchanged,
            page_ids=tuple(changed),
            retired_page_ids=tuple(sorted(normalized)),
            replacement_ids=tuple(sorted(set(normalized.values()))),
        )

    def dry_run_verified_book_update(
        self,
        records: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate,
        staged_assets: tuple[StagedBookAsset, ...] = (),
        *,
        rights_restore_book_id: str | None = None,
    ) -> VerifiedBookUpdateReport:
        """Execute the exact writer and post-write audits in an isolated Vault clone."""

        with tempfile.TemporaryDirectory(prefix="woon-book-preflight-") as temporary:
            dry_vault = (Path(temporary) / "vault").resolve()
            shutil.copytree(self._settings.vault / "catalog", dry_vault / "catalog")
            for source_path in self._settings.output_root.rglob("*.md"):
                if "_sources" in source_path.parts:
                    continue
                output_relative = source_path.relative_to(self._settings.vault)
                destination = dry_vault / output_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)

            replacement = coverage_manifest.replacement
            source_archive = replacement.get("source_archive")
            if isinstance(source_archive, dict):
                archive_relative = source_archive.get("relative_path")
                if isinstance(archive_relative, str):
                    self._copy_dry_run_source_file(archive_relative, dry_vault)
            asset_inventory = replacement.get("source_asset_inventory")
            if isinstance(asset_inventory, list):
                for item in asset_inventory:
                    if not isinstance(item, dict):
                        continue
                    asset_relative = item.get("archive_relative_path")
                    if isinstance(asset_relative, str):
                        self._copy_dry_run_source_file(asset_relative, dry_vault)

            dry_settings = CompiledWikiSettings(
                vault=dry_vault,
                output_root=dry_vault
                / self._settings.output_root.relative_to(self._settings.vault),
                sources_path=dry_vault
                / self._settings.sources_path.relative_to(self._settings.vault),
                claims_path=dry_vault
                / self._settings.claims_path.relative_to(self._settings.vault),
                pages_path=dry_vault
                / self._settings.pages_path.relative_to(self._settings.vault),
                curation_path=dry_vault
                / self._settings.curation_path.relative_to(self._settings.vault),
                relations_path=dry_vault
                / self._settings.relations_path.relative_to(self._settings.vault),
                receipts_path=dry_vault
                / self._settings.receipts_path.relative_to(self._settings.vault),
                review_queue_path=dry_vault
                / self._settings.review_queue_path.relative_to(self._settings.vault),
            )
            dry_compiler = CompiledWiki(dry_settings)
            dry_compiler.install_staged_book_assets(staged_assets)
            return dry_compiler.apply_verified_book_update(
                records,
                replacements,
                retirement_body_sha256,
                coverage_manifest,
                rights_restore_book_id=rights_restore_book_id,
            )

    def _copy_dry_run_source_file(self, relative: str, dry_vault: Path) -> None:
        """Copy one immutable source artifact needed by a cloned coverage audit."""

        source = _inside(self._settings.vault, relative, "dry-run source artifact")
        if not source.is_file():
            return
        destination = _inside(dry_vault, relative, "dry-run source artifact")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.hardlink_to(source)
        except OSError:
            shutil.copy2(source, destination)

    @staticmethod
    def _retire_book_rights_decisions(
        book_id: str,
        records: tuple[VerifiedBookPage, ...],
        sources: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
        *,
        restored_page_ids: set[str] | None = None,
    ) -> None:
        """Remove obsolete blocked-rights decisions from restored current pages."""

        source_prefix = f"source://book-rights/{book_id}/"
        claim_prefix = f"claim://book-rights/{book_id}/"
        promoted_ids = {record.page_id for record in records}
        live_rights_pages = {
            page_id
            for page_id, page in pages.items()
            if page_id == book_id or page_id.startswith(book_id + "/")
            if any(
                source_id.startswith(source_prefix)
                for source_id in _string_list(page.get("source_ids"), "page source_ids")
            )
        }
        omitted = live_rights_pages.difference(promoted_ids)
        if omitted:
            raise WoonError(
                "book rights restore must replace every surviving rights page: "
                f"{sorted(omitted)[0]}"
            )
        restored_ids = promoted_ids if restored_page_ids is None else restored_page_ids
        unknown_restored = restored_ids.difference(promoted_ids)
        if unknown_restored:
            raise WoonError(
                "book rights restore active scope must be declared in promoted pages: "
                f"{sorted(unknown_restored)[0]}"
            )
        for page_id in sorted(restored_ids):
            page = pages[page_id]
            current_source_id = _current_source_id(
                page, [sources[item] for item in page["source_ids"]]
            )
            current_claim_id = _current_claim_id(
                page, [claims[item] for item in page["claim_ids"]]
            )
            rights_sources = [
                item for item in page["source_ids"] if item.startswith(source_prefix)
            ]
            rights_claims = [
                item for item in page["claim_ids"] if item.startswith(claim_prefix)
            ]
            page["source_ids"] = [
                item for item in page["source_ids"] if item not in rights_sources
            ]
            page["claim_ids"] = [item for item in page["claim_ids"] if item not in rights_claims]
            for source_id in rights_sources:
                sources[source_id].update(
                    {"lifecycle": "archived", "superseded_by": current_source_id}
                )
            for claim_id in rights_claims:
                claims[claim_id].update(
                    {"status": "superseded", "superseded_by": current_claim_id}
                )

    def validate_verified_book_retirement_content(
        self,
        records: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_body_sha256: dict[str, str],
    ) -> None:
        """Fail a preflight if retiring reader prose is not copied exactly.

        This is intentionally the same strict check used by the writer.  A
        coverage manifest's relocated-span claim is not a substitute for the
        current wrapper body: the complete normalized reader body must occur in
        the promoted survivor before the wrapper may be removed.
        """

        sources, _, pages, _, _ = self._load_inputs()
        self._validate_verified_book_retirement_content(
            records,
            replacements,
            retirement_body_sha256,
            sources,
            pages,
        )

    def preflight_book_rights_demotion(
        self, request: BookRightsDemotion
    ) -> BookRightsDemotionReport:
        """Validate one rights demotion without mutating compiler inputs or outputs."""

        rights_source_ids, rights_claim_ids = self._validate_book_rights_demotion(request)
        return BookRightsDemotionReport(
            ready=True,
            applied=False,
            survivor_count=len(request.survivor_ids),
            retired_page_count=len(request.retire_page_ids),
            archived_source_count=len(request.affected_source_ids),
            superseded_claim_count=len(request.affected_claim_ids),
            quarantined_output_count=len(request.target_ids),
            quarantined_asset_count=len(request.expected_asset_sha256),
            quarantine_relative_path=request.quarantine_relative_path,
            rights_source_count=len(rights_source_ids),
            rights_claim_count=len(rights_claim_ids),
        )

    def snapshot_book_rights_demotion(
        self, request: BookRightsDemotion
    ) -> BookRightsDemotionSnapshot:
        """Capture every mutable path before the compiler and index transaction."""

        coverage_path = _inside(
            self._settings.vault,
            str(request.coverage["relative_path"]),
            "book rights coverage path",
        )
        intake_path = _inside(
            self._settings.vault,
            request.book_intake["relative_path"],
            "book rights intake path",
        )
        quarantine_path = _inside(
            self._settings.vault,
            request.quarantine_relative_path,
            "book rights quarantine path",
        )
        assets = {
            _inside(self._settings.vault, relative, "book rights asset path"): _inside(
                self._settings.vault, relative, "book rights asset path"
            ).read_bytes()
            for relative in request.expected_asset_sha256
        }
        return BookRightsDemotionSnapshot(
            inputs=self.snapshot_inputs(extra_paths=(coverage_path, intake_path)),
            outputs=self.snapshot_outputs(
                extra_relative_paths=tuple(f"{page_id}.md" for page_id in request.target_ids)
            ),
            assets=assets,
            quarantine_path=quarantine_path,
        )

    def restore_book_rights_demotion(self, snapshot: BookRightsDemotionSnapshot) -> None:
        """Restore compiler, generated, and asset bytes and remove new quarantine."""

        self.restore_inputs(snapshot.inputs)
        self.restore_outputs(snapshot.outputs)
        for path, content in snapshot.assets.items():
            atomic_write(path, content)
        if snapshot.quarantine_path.exists():
            shutil.rmtree(snapshot.quarantine_path)

    def apply_book_rights_demotion(
        self, request: BookRightsDemotion
    ) -> BookRightsDemotionReport:
        """Atomically replace restricted reader bodies with TOC-only page projections."""

        rights_source_ids, rights_claim_ids = self._validate_book_rights_demotion(request)
        coverage_path = _inside(
            self._settings.vault,
            str(request.coverage["relative_path"]),
            "book rights coverage path",
        )
        intake_path = _inside(
            self._settings.vault,
            request.book_intake["relative_path"],
            "book rights intake path",
        )
        quarantine_path = _inside(
            self._settings.vault,
            request.quarantine_relative_path,
            "book rights quarantine path",
        )
        archive_path = _inside(
            self._settings.vault,
            request.rights_evidence["source_archive_relative_path"],
            "book rights source archive path",
        )
        source_archive_before = _sha256_bytes(archive_path.read_bytes())
        input_snapshot = self.snapshot_inputs(extra_paths=(coverage_path, intake_path))
        output_snapshot = self.snapshot_outputs(
            extra_relative_paths=tuple(f"{page_id}.md" for page_id in request.target_ids)
        )
        asset_snapshot = {
            _inside(self._settings.vault, relative, "book rights asset path"): _inside(
                self._settings.vault, relative, "book rights asset path"
            ).read_bytes()
            for relative in request.expected_asset_sha256
        }
        sources, claims, pages, curations, _ = self._load_inputs()
        try:
            self._write_book_rights_quarantine(request, quarantine_path, pages)
            for page_id in request.survivor_ids:
                rights_source, rights_claim = _book_rights_records(
                    request,
                    page_id,
                    rights_source_ids[page_id],
                    rights_claim_ids[page_id],
                )
                sources[rights_source_ids[page_id]] = rights_source
                claims[rights_claim_ids[page_id]] = rights_claim

            survivors = set(request.survivor_ids)
            retiring = set(request.retire_page_ids)
            retired_parents = {
                page_id: _canonical_parent_id(pages[page_id]["frontmatter"].get("parent"))
                for page_id in retiring
            }
            relation_updated_ids: set[str] = set()
            for page_id, page in pages.items():
                if page_id in retiring or page_id in survivors:
                    continue
                frontmatter = page.get("frontmatter")
                if isinstance(frontmatter, dict):
                    changed = _redirect_frontmatter_relations(
                        frontmatter, request.retire_replacements
                    )
                    changed = (
                        _remove_retired_frontmatter_relations(frontmatter, retiring) or changed
                    )
                    if changed:
                        relation_updated_ids.add(page_id)

            for page_id in request.survivor_ids:
                page = pages[page_id]
                frontmatter = copy.deepcopy(page["frontmatter"])
                current_parent = _canonical_parent_id(frontmatter.get("parent"))
                if current_parent in retired_parents:
                    parent_id = retired_parents[current_parent]
                    parent_title = str(pages[parent_id]["title"])
                    frontmatter["parent"] = f"[[wiki/{parent_id}|{parent_title}]]"
                _redirect_frontmatter_relations(
                    frontmatter,
                    request.retire_replacements,
                    redirect_parent=False,
                )
                _remove_retired_frontmatter_relations(frontmatter, retiring)
                if page_id in request.survivor_navigation_groups:
                    groups = request.survivor_navigation_groups[page_id]
                    if groups:
                        frontmatter["navigation_groups"] = copy.deepcopy(groups)
                    else:
                        frontmatter.pop("navigation_groups", None)
                frontmatter.update(
                    {
                        "canonical_id": page_id,
                        "source_ids": [rights_source_ids[page_id]],
                        "status": "Planned",
                        "knowledge_state": "목차 확인됨",
                        "state_reason": "blocked-rights",
                    }
                )
                page.update(
                    {
                        "frontmatter": frontmatter,
                        "source_ids": [rights_source_ids[page_id]],
                        "claim_ids": [rights_claim_ids[page_id]],
                        "render": (
                            {
                                "kind": "source-body",
                                "source_id": rights_source_ids[page_id],
                            }
                            if request.survivor_bodies[page_id]
                            else {"kind": "toc-only"}
                        ),
                    }
                )
                curations[page_id] = {
                    "page_id": page_id,
                    "current_use": "원문 목차에서 이 절의 위치를 확인할 때 사용한다.",
                    "basis": "manual-review",
                    "status": "confirmed",
                }

            for source_id in request.affected_source_ids:
                sources[source_id].update(
                    {
                        "lifecycle": "archived",
                        "superseded_by": rights_source_ids[sorted(rights_source_ids)[0]],
                    }
                )
            for claim_id in request.affected_claim_ids:
                claims[claim_id].update(
                    {
                        "status": "superseded",
                        "superseded_by": rights_claim_ids[sorted(rights_claim_ids)[0]],
                    }
                )
            for page_id in retiring:
                del pages[page_id]
                del curations[page_id]

            intake = json.loads(intake_path.read_text(encoding="utf-8"))
            for bundle in intake["bundles"]:
                if bundle.get("id") == request.book_intake["bundle_id"]:
                    bundle["rights_status"] = "processing-prohibited"
                    bundle["processing_state"] = "blocked-rights"
                    bundle["rights_evidence"] = {
                        key: request.rights_evidence[key]
                        for key in (
                            "notice_locator",
                            "notice_sha256",
                            "decision",
                            "reviewed_on",
                        )
                    }
                    break

            self._write_inputs(sources, claims, pages, curations)
            atomic_write(
                coverage_path,
                (
                    json.dumps(
                        request.coverage["replacement"],
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            atomic_write(
                intake_path,
                (json.dumps(intake, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            self.compile(
                page_ids=tuple(sorted(survivors.union(relation_updated_ids)))
            )
            for page_id in retiring:
                _inside(
                    self._settings.output_root,
                    f"{page_id}.md",
                    "retired rights-blocked output",
                ).unlink(missing_ok=True)
            for asset_path in asset_snapshot:
                asset_path.unlink(missing_ok=True)

            tree_report = prepare_wiki_tree_refresh(
                self._settings.vault,
                canonical_prefix=request.book_id,
            )
            if tree_report.issues:
                raise WoonError(
                    "book rights demotion could not refresh its Wiki tree: "
                    + tree_report.issues[0]
                )
            tree_changed_paths = tuple(
                path
                for path, content in tree_report.pages.items()
                if not path.is_file() or path.read_bytes() != content
            )
            apply_wiki_tree_refresh(self._settings.vault, tree_report)
            self._refresh_generated_view_receipts(tree_changed_paths)
            audit = self.audit()
            if not audit.complete:
                raise WoonError(
                    "book rights demotion left stale compiler inputs: " + audit.errors[0]
                )
            intake_audit = audit_book_intake(self._settings.vault)
            if not intake_audit.complete:
                raise WoonError("book rights demotion left stale intake: " + intake_audit.errors[0])
            coverage_audit = audit_book_coverage(self._settings.vault)
            target_errors = [
                error for error in coverage_audit.errors if request.book_id in error
            ]
            if target_errors:
                raise WoonError("book rights demotion left stale coverage: " + target_errors[0])
            if request.book_id not in coverage_audit.pending_books:
                raise WoonError("rights-blocked book must remain pending TOC-only coverage")
            if _sha256_bytes(archive_path.read_bytes()) != source_archive_before:
                raise WoonError("book rights demotion changed the local-only source archive")
        except BaseException:
            self.restore_inputs(input_snapshot)
            self.restore_outputs(output_snapshot)
            for path, content in asset_snapshot.items():
                atomic_write(path, content)
            if quarantine_path.exists():
                shutil.rmtree(quarantine_path)
            raise

        return BookRightsDemotionReport(
            ready=True,
            applied=True,
            survivor_count=len(request.survivor_ids),
            retired_page_count=len(request.retire_page_ids),
            archived_source_count=len(request.affected_source_ids),
            superseded_claim_count=len(request.affected_claim_ids),
            quarantined_output_count=len(request.target_ids),
            quarantined_asset_count=len(request.expected_asset_sha256),
            quarantine_relative_path=request.quarantine_relative_path,
            rights_source_count=len(rights_source_ids),
            rights_claim_count=len(rights_claim_ids),
        )

    def _validate_book_rights_demotion(
        self, request: BookRightsDemotion
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Re-read every optimistic boundary used by a rights demotion."""

        sources, claims, pages, _, _ = self._load_inputs()
        target_ids = set(request.target_ids)
        if len(target_ids) != len(request.target_ids):
            raise WoonError("book rights demotion contains duplicate page targets")
        missing = target_ids.difference(pages)
        if missing:
            raise WoonError(f"book rights demotion page is missing: {sorted(missing)[0]}")
        retiring = set(request.retire_page_ids)
        survivors = set(request.survivor_ids)
        expected_navigation: dict[str, list[dict[str, Any]]] = {}
        for page_id in sorted(survivors):
            frontmatter = pages[page_id].get("frontmatter", {})
            current_groups = (
                copy.deepcopy(frontmatter.get("navigation_groups", []))
                if isinstance(frontmatter, dict)
                else []
            )
            demoted_groups = _flatten_navigation_groups(current_groups, pages, retiring)
            if demoted_groups != current_groups:
                expected_navigation[page_id] = demoted_groups
        if request.survivor_navigation_groups != expected_navigation:
            raise WoonError(
                "book rights navigation replacements are not the exact retired-link projection"
            )
        for page_id in sorted(survivors):
            body = request.survivor_bodies[page_id]
            frontmatter = pages[page_id].get("frontmatter", {})
            current_groups = (
                frontmatter.get("navigation_groups", [])
                if isinstance(frontmatter, dict)
                else []
            )
            after_groups = expected_navigation.get(page_id, current_groups)
            retired_descendants = [
                retired_id
                for retired_id in retiring
                if _nearest_surviving_ancestor(retired_id, pages, retiring) == page_id
            ]
            if body:
                if after_groups:
                    raise WoonError(
                        "book rights plain-text TOC body cannot duplicate live navigation: "
                        f"{page_id}"
                    )
                _validate_rights_toc_body(body, page_id, retiring)
                body_lines = body.splitlines()
                for child_id in retired_descendants:
                    child_groups = pages[child_id].get("frontmatter", {}).get(
                        "navigation_groups", []
                    )
                    prefix = "##" if child_groups else "-"
                    row = f"{prefix} {pages[child_id]['title']}"
                    if body_lines.count(row) != 1:
                        raise WoonError(
                            "book rights TOC body must preserve each retired descendant title "
                            "exactly once: "
                            f"{child_id}"
                        )
            elif current_groups and not after_groups:
                raise WoonError(
                    "book rights surviving map requires a plain-text TOC body after its "
                    f"children retire: {page_id}"
                )
        for page_id, page in pages.items():
            if page_id in target_ids:
                continue
            frontmatter = page.get("frontmatter")
            if not isinstance(frontmatter, dict):
                continue
            unresolved = _frontmatter_relation_targets(frontmatter).intersection(retiring)
            unresolved.difference_update(request.retire_replacements)
            if unresolved:
                raise WoonError(
                    "book rights retirement is referenced outside scope without replacement: "
                    + sorted(unresolved)[0]
                )
            navigation_refs = {
                child
                for group in frontmatter.get("navigation_groups", [])
                if isinstance(group, dict)
                for child in group.get("children", [])
                if isinstance(child, str)
            }
            retired_navigation_refs = navigation_refs.intersection(retiring)
            if retired_navigation_refs:
                raise WoonError(
                    "book rights retirement remains in an outside navigation map: "
                    + sorted(retired_navigation_refs)[0]
                )
        actual_sources = {
            source_id
            for page_id in target_ids
            for source_id in _string_list(pages[page_id].get("source_ids"), "page source_ids")
        }
        actual_claims = {
            claim_id
            for page_id in target_ids
            for claim_id in _string_list(pages[page_id].get("claim_ids"), "page claim_ids")
        }
        if actual_sources != set(request.affected_source_ids):
            raise WoonError("book rights demotion affected_source_ids changed after review")
        if actual_claims != set(request.affected_claim_ids):
            raise WoonError("book rights demotion affected_claim_ids changed after review")
        outside_source_ids = {
            source_id
            for page_id, page in pages.items()
            if page_id not in target_ids
            for source_id in _string_list(page.get("source_ids"), "page source_ids")
        }
        outside_claim_ids = {
            claim_id
            for page_id, page in pages.items()
            if page_id not in target_ids
            for claim_id in _string_list(page.get("claim_ids"), "page claim_ids")
        }
        shared_source_ids = actual_sources.intersection(outside_source_ids)
        shared_claim_ids = actual_claims.intersection(outside_claim_ids)
        if shared_source_ids:
            raise WoonError(
                "book rights demotion source is shared outside scope: "
                + sorted(shared_source_ids)[0]
            )
        if shared_claim_ids:
            raise WoonError(
                "book rights demotion claim is shared outside scope: "
                + sorted(shared_claim_ids)[0]
            )
        for source_id, digest in request.expected_source_body_sha256.items():
            source = sources.get(source_id)
            if source is None or source.get("lifecycle") != "compiled":
                raise WoonError(f"book rights demotion source is not current: {source_id}")
            if _sha256_text(_normalize(str(source.get("body", "")))) != digest:
                raise WoonError(f"book rights demotion source body changed: {source_id}")
        for claim_id in request.affected_claim_ids:
            claim = claims.get(claim_id)
            if claim is None or claim.get("status") != "accepted":
                raise WoonError(f"book rights demotion claim is not current: {claim_id}")

        asset_refs: set[str] = set()
        for page_id in target_ids:
            output = _inside(
                self._settings.output_root,
                str(pages[page_id]["output_path"]),
                "book rights output path",
            )
            if not output.is_file():
                raise WoonError(f"book rights demotion output is missing: {page_id}")
            content = output.read_bytes()
            if _sha256_bytes(content) != request.expected_output_sha256[page_id]:
                raise WoonError(f"book rights demotion output changed: {page_id}")
            asset_refs.update(_local_book_asset_refs(content.decode("utf-8")))
        if asset_refs != set(request.expected_asset_sha256):
            raise WoonError("book rights demotion affected asset inventory changed after review")
        for relative, digest in request.expected_asset_sha256.items():
            asset = _inside(self._settings.vault, relative, "book rights asset path")
            if not asset.is_file() or _sha256_bytes(asset.read_bytes()) != digest:
                raise WoonError(f"book rights demotion asset changed: {relative}")
            for page_id, page in pages.items():
                if page_id in target_ids:
                    continue
                other = _inside(
                    self._settings.output_root,
                    str(page["output_path"]),
                    "book rights outside output path",
                )
                if other.is_file() and relative in _local_book_asset_refs(
                    other.read_text(encoding="utf-8")
                ):
                    raise WoonError(
                        f"book rights demotion asset is shared outside scope: {relative}"
                    )

        archive_path = _inside(
            self._settings.vault,
            request.rights_evidence["source_archive_relative_path"],
            "book rights source archive path",
        )
        if (
            archive_path.is_symlink()
            or not archive_path.is_file()
            or _sha256_bytes(archive_path.read_bytes())
            != request.rights_evidence["source_archive_sha256"]
        ):
            raise WoonError("book rights demotion source archive changed after review")

        coverage_path = _inside(
            self._settings.vault,
            str(request.coverage["relative_path"]),
            "book rights coverage path",
        )
        coverage_bytes = coverage_path.read_bytes() if coverage_path.is_file() else b""
        if _sha256_bytes(coverage_bytes) != request.coverage["expected_sha256"]:
            raise WoonError("book rights demotion coverage manifest changed after review")
        current_coverage = json.loads(coverage_bytes)
        _validate_rights_coverage_replacement(current_coverage, request)

        intake_path = _inside(
            self._settings.vault,
            request.book_intake["relative_path"],
            "book rights intake path",
        )
        intake_bytes = intake_path.read_bytes() if intake_path.is_file() else b""
        if _sha256_bytes(intake_bytes) != request.book_intake["expected_sha256"]:
            raise WoonError("book rights demotion intake manifest changed after review")
        intake = json.loads(intake_bytes)
        bundles = intake.get("bundles") if isinstance(intake, dict) else None
        matches = [
            item
            for item in bundles or []
            if isinstance(item, dict) and item.get("id") == request.book_intake["bundle_id"]
        ]
        if len(matches) != 1 or matches[0].get("target") != request.book_id:
            raise WoonError("book rights demotion intake bundle is missing or ambiguous")

        quarantine_path = _inside(
            self._settings.vault,
            request.quarantine_relative_path,
            "book rights quarantine path",
        )
        if quarantine_path.exists() or quarantine_path.is_symlink():
            raise WoonError("book rights demotion quarantine target already exists")
        if quarantine_path.parent != archive_path.parent / "rights-quarantine":
            raise WoonError("book rights quarantine must be beside its source archive")

        rights_source_ids, rights_claim_ids = _book_rights_ids(request)
        if set(rights_source_ids.values()).intersection(sources) or set(
            rights_claim_ids.values()
        ).intersection(claims):
            raise WoonError("book rights demotion evidence identity already exists")
        return rights_source_ids, rights_claim_ids

    def _write_book_rights_quarantine(
        self,
        request: BookRightsDemotion,
        quarantine_path: Path,
        pages: dict[str, dict[str, Any]],
    ) -> None:
        """Create a byte-exact private recovery package before canonical mutation."""

        entries: list[dict[str, Any]] = []
        for page_id in sorted(request.target_ids):
            relative = str(pages[page_id]["output_path"])
            source = _inside(self._settings.output_root, relative, "book rights output")
            target_relative = f"generated/{relative}"
            target = _inside(quarantine_path, target_relative, "book rights quarantine output")
            content = source.read_bytes()
            atomic_write(target, content)
            entries.append(
                {
                    "kind": "generated-markdown",
                    "page_id": page_id,
                    "source_relative_path": f"wiki/{relative}",
                    "quarantine_relative_path": target_relative,
                    "sha256": _sha256_bytes(content),
                    "bytes": len(content),
                }
            )
        for relative in sorted(request.expected_asset_sha256):
            source = _inside(self._settings.vault, relative, "book rights asset")
            target_relative = f"assets/{relative}"
            target = _inside(quarantine_path, target_relative, "book rights quarantine asset")
            content = source.read_bytes()
            atomic_write(target, content)
            entries.append(
                {
                    "kind": "reader-asset",
                    "source_relative_path": relative,
                    "quarantine_relative_path": target_relative,
                    "sha256": _sha256_bytes(content),
                    "bytes": len(content),
                }
            )
        manifest = {
            "schema_version": 1,
            "book_id": request.book_id,
            "rights_evidence": request.rights_evidence,
            "entries": entries,
            "entry_count": len(entries),
            "rollback": {
                "restore_generated_from": "generated/",
                "restore_assets_from": "assets/",
                "catalog_history": "archived source and superseded claim records remain intact",
            },
        }
        atomic_write(
            quarantine_path / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )

    def _validate_verified_book_retirement_content(
        self,
        records: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_body_sha256: dict[str, str],
        sources: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
    ) -> None:
        """Validate optimistic body hashes and exact prose relocation."""

        promoted_by_id = {record.page_id: record for record in records}
        for page_id in sorted(replacements):
            page = pages.get(page_id)
            if page is None:
                raise WoonError(f"compiled Wiki page spec not found: {page_id}")
            source_id = _current_source_id(page, self._page_sources(page, sources))
            body = str(sources[source_id].get("body", ""))
            normalized_body = _retirement_body(body)
            actual_body_sha256 = _sha256_text(normalized_body)
            if actual_body_sha256 != retirement_body_sha256[page_id]:
                raise WoonError(f"verified book retirement body changed after review: {page_id}")
            if _navigation_only_body(normalized_body):
                continue
            replacement_id = replacements[page_id]
            replacement = promoted_by_id.get(replacement_id)
            if replacement is None:
                raise WoonError(
                    "verified book update replacement must be included in promoted pages: "
                    f"{replacement_id}"
                )
            replacement_body = _normalize(replacement.body)
            if normalized_body not in replacement_body:
                raise WoonError(
                    "verified book retirement reader content is not preserved in "
                    f"replacement: {page_id}"
                )

    def validate_book_coverage_manifest_update(self, update: BookCoverageManifestUpdate) -> Path:
        """Validate one coverage replacement and return its canonical local path."""

        path, _ = self._validated_coverage_manifest_update(update)
        return path

    def validate_staged_book_assets(
        self,
        assets: tuple[StagedBookAsset, ...],
        coverage_manifest: BookCoverageManifestUpdate,
    ) -> tuple[int, int]:
        """Validate source bytes, private destinations, and coverage bindings without writing.

        Embedded images retain their source-member bytes.  A PDF scan crop is
        instead bound to the byte-pinned source PDF, one numbered page, an
        explicit crop box, the rendered-page hash, and the staged output hash.
        """

        inventory = coverage_manifest.replacement.get("source_asset_inventory")
        if not isinstance(inventory, list):
            if assets:
                raise WoonError("staged book assets require a source_asset_inventory")
            return 0, 0
        inventory_by_path: dict[str, dict[str, Any]] = {}
        for item in inventory:
            if not isinstance(item, dict):
                continue
            relative = item.get("archive_relative_path")
            if isinstance(relative, str):
                if relative in inventory_by_path:
                    raise WoonError(f"duplicate source asset archive path: {relative}")
                inventory_by_path[relative] = item

        staged_by_path: dict[str, StagedBookAsset] = {}
        pdf_page_counts: dict[Path, int] = {}
        scan_crop_checks: list[
            tuple[
                StagedBookAsset,
                Path,
                int,
                int,
                tuple[int, int, int, int],
                str,
            ]
        ] = []
        staged_count = 0
        unchanged_count = 0
        for asset in assets:
            if asset.archive_relative_path in staged_by_path:
                raise WoonError(
                    f"duplicate staged book asset destination: {asset.archive_relative_path}"
                )
            staged_by_path[asset.archive_relative_path] = asset
            if asset.provenance not in {
                "embedded-original-byte-identical",
                "scan-crop-with-pinned-page-and-box",
            }:
                raise WoonError("staged book asset provenance is unsupported")
            if not asset.source_entry_locator.strip():
                raise WoonError("staged book asset source_entry_locator is required")
            if re.fullmatch(r"[0-9a-f]{64}", asset.sha256) is None:
                raise WoonError("staged book asset sha256 must be a lowercase SHA-256")
            if not isinstance(asset.size, int) or isinstance(asset.size, bool) or asset.size <= 0:
                raise WoonError("staged book asset size must be a positive integer")
            if (
                not asset.staging_path.is_absolute()
                or asset.staging_path.resolve() != asset.staging_path
                or asset.staging_path.is_symlink()
                or not asset.staging_path.is_file()
            ):
                raise WoonError("staged book asset source must be a regular non-symlink file")
            source_bytes = asset.staging_path.read_bytes()
            if len(source_bytes) != asset.size or _sha256_bytes(source_bytes) != asset.sha256:
                raise WoonError("staged book asset source size or hash does not match")

            item = inventory_by_path.get(asset.archive_relative_path)
            if item is None:
                raise WoonError(
                    "staged book asset is absent from source_asset_inventory: "
                    f"{asset.archive_relative_path}"
                )
            if asset.provenance == "embedded-original-byte-identical":
                if (
                    item.get("source_locator") != asset.source_entry_locator
                    or item.get("source_sha256") != asset.sha256
                    or item.get("archive_sha256") != asset.sha256
                    or item.get("extraction_kind") != "embedded-original"
                    or item.get("crop_provenance") is not None
                ):
                    raise WoonError(
                        "staged book asset does not match byte-identical inventory provenance: "
                        f"{asset.archive_relative_path}"
                    )
            else:
                scan_crop_checks.append(
                    self._validate_staged_scan_crop(
                        asset,
                        item,
                        coverage_manifest.replacement,
                        pdf_page_counts,
                    )
                )
            destination = self._book_asset_destination(asset.archive_relative_path)
            if destination.exists():
                if (
                    not destination.is_file()
                    or _sha256_bytes(destination.read_bytes()) != asset.sha256
                ):
                    raise WoonError(
                        "staged book asset destination exists with different bytes: "
                        f"{asset.archive_relative_path}"
                    )
                unchanged_count += 1
            else:
                staged_count += 1

        render_requests: dict[tuple[Path, int], set[int]] = {}
        for _, source_path, page_number, render_dpi, _, _ in scan_crop_checks:
            render_requests.setdefault((source_path, render_dpi), set()).add(page_number)
        rendered_pages: dict[tuple[Path, int, int], bytes] = {}
        for (source_path, render_dpi), page_numbers in render_requests.items():
            for page_number, rendered in _render_pdf_pages_png(
                source_path,
                page_numbers,
                render_dpi,
            ).items():
                rendered_pages[(source_path, page_number, render_dpi)] = rendered
        for asset, source_path, page_number, render_dpi, crop_box, page_sha256 in (
            scan_crop_checks
        ):
            rendered_page = rendered_pages[(source_path, page_number, render_dpi)]
            if _sha256_bytes(rendered_page) != page_sha256:
                raise WoonError("staged book scan crop rendered page hash does not match")
            reproduced_crop = _crop_png(rendered_page, crop_box)
            if (
                _sha256_bytes(reproduced_crop) != asset.sha256
                or reproduced_crop != asset.staging_path.read_bytes()
            ):
                raise WoonError("staged book scan crop output cannot be reproduced")

        for relative, item in inventory_by_path.items():
            archive_sha256 = item.get("archive_sha256")
            if not isinstance(archive_sha256, str):
                continue
            destination = self._book_asset_destination(relative)
            if destination.exists():
                if (
                    not destination.is_file()
                    or _sha256_bytes(destination.read_bytes()) != archive_sha256
                ):
                    raise WoonError(f"source asset archive bytes differ: {relative}")
            elif relative not in staged_by_path:
                raise WoonError(f"missing source asset requires a staged landing: {relative}")
        return staged_count, unchanged_count

    def _validate_staged_scan_crop(
        self,
        asset: StagedBookAsset,
        inventory_item: dict[str, Any],
        coverage_replacement: dict[str, Any],
        pdf_page_counts: dict[Path, int],
    ) -> tuple[StagedBookAsset, Path, int, int, tuple[int, int, int, int], str]:
        """Bind one staged crop to a pinned PDF page, crop box, and output hash."""

        if (
            inventory_item.get("extraction_kind") != "scan-crop"
            or inventory_item.get("source_locator") != asset.source_entry_locator
            or inventory_item.get("source_sha256") != asset.sha256
            or inventory_item.get("archive_sha256") != asset.sha256
        ):
            raise WoonError(
                "staged book scan crop does not match output inventory provenance: "
                f"{asset.archive_relative_path}"
            )

        source_archive = coverage_replacement.get("source_archive")
        if not isinstance(source_archive, dict):
            raise WoonError("staged book scan crop requires a pinned source PDF")
        source_relative = source_archive.get("relative_path")
        source_digest = source_archive.get("sha256")
        source_candidate = Path(source_relative) if isinstance(source_relative, str) else Path()
        if (
            not isinstance(source_relative, str)
            or source_candidate.suffix.lower() != ".pdf"
            or source_candidate.parts[:5]
            != ("wiki", "private", "_sources", "knowledge", "local-only")
            or re.fullmatch(r"[0-9a-f]{64}", str(source_digest)) is None
        ):
            raise WoonError("staged book scan crop requires a pinned source PDF")
        source_path = _inside(
            self._settings.vault,
            source_relative,
            "staged book scan crop source PDF",
        )
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or _sha256_bytes(source_path.read_bytes()) != source_digest
        ):
            raise WoonError("staged book scan crop source PDF hash does not match")
        edition = coverage_replacement.get("edition")
        if not isinstance(edition, dict) or edition.get("source_sha256") != source_digest:
            raise WoonError("staged book scan crop source PDF hash is not edition-pinned")

        crop = inventory_item.get("crop_provenance")
        expected_crop_fields = {
            "page_locator",
            "crop_box",
            "render_dpi",
            "source_page_sha256",
        }
        if not isinstance(crop, dict) or set(crop) != expected_crop_fields:
            raise WoonError("staged book scan crop provenance fields are invalid")
        page_locator = crop.get("page_locator")
        if not isinstance(page_locator, str):
            raise WoonError("staged book scan crop page locator is invalid")
        page_match = re.fullmatch(r".+#PDF-page-(?P<page>[1-9][0-9]*)", page_locator)
        if page_match is None:
            raise WoonError("staged book scan crop page locator is invalid")
        page_number = int(page_match.group("page"))
        if source_path not in pdf_page_counts:
            try:
                pdf_page_counts[source_path] = len(PdfReader(source_path).pages)
            except Exception as error:
                raise WoonError("staged book scan crop source PDF is unreadable") from error
        if page_number > pdf_page_counts[source_path]:
            raise WoonError("staged book scan crop page is outside the source PDF")
        crop_box = crop.get("crop_box")
        if (
            not isinstance(crop_box, list)
            or len(crop_box) != 4
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in crop_box
            )
            or crop_box[2] <= crop_box[0]
            or crop_box[3] <= crop_box[1]
        ):
            raise WoonError("staged book scan crop box is invalid")
        locator_match = re.fullmatch(
            rf"{re.escape(page_locator)}:crop-"
            r"(?P<x1>[0-9]+(?:\.[0-9]+)?),(?P<y1>[0-9]+(?:\.[0-9]+)?),"
            r"(?P<x2>[0-9]+(?:\.[0-9]+)?),(?P<y2>[0-9]+(?:\.[0-9]+)?)",
            asset.source_entry_locator,
        )
        if locator_match is None or [
            float(locator_match.group(name)) for name in ("x1", "y1", "x2", "y2")
        ] != [float(value) for value in crop_box]:
            raise WoonError("staged book scan crop locator and crop box do not match")
        render_dpi = crop.get("render_dpi")
        if (
            not isinstance(render_dpi, int)
            or isinstance(render_dpi, bool)
            or render_dpi <= 0
        ):
            raise WoonError("staged book scan crop render DPI is invalid")
        source_page_sha256 = crop.get("source_page_sha256")
        if re.fullmatch(r"[0-9a-f]{64}", str(source_page_sha256)) is None:
            raise WoonError("staged book scan crop rendered page hash is invalid")
        return (
            asset,
            source_path,
            page_number,
            render_dpi,
            tuple(crop_box),
            str(source_page_sha256),
        )

    def snapshot_staged_book_assets(
        self, assets: tuple[StagedBookAsset, ...]
    ) -> dict[Path, bytes | None]:
        """Capture exact destination bytes before an atomic source-image landing."""

        return {
            (path := self._book_asset_destination(asset.archive_relative_path)): (
                path.read_bytes() if path.is_file() else None
            )
            for asset in assets
        }

    def install_staged_book_assets(self, assets: tuple[StagedBookAsset, ...]) -> None:
        """Atomically install already validated source images into the private archive."""

        for asset in assets:
            source_bytes = asset.staging_path.read_bytes()
            if len(source_bytes) != asset.size or _sha256_bytes(source_bytes) != asset.sha256:
                raise WoonError("staged book asset changed after validation")
            destination = self._book_asset_destination(asset.archive_relative_path)
            if destination.is_file():
                if _sha256_bytes(destination.read_bytes()) != asset.sha256:
                    raise WoonError("staged book asset destination changed after validation")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, source_bytes)

    def restore_staged_book_assets(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore prior asset bytes and remove directories created only by a failed landing."""

        stop = self._settings.vault / "wiki/private/_sources/knowledge/local-only"
        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
                parent = path.parent
                while parent != stop and parent != self._settings.vault:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            else:
                atomic_write(path, content)

    def _book_asset_destination(self, relative: str) -> Path:
        candidate = Path(relative)
        if (
            not relative
            or "\\" in relative
            or candidate.is_absolute()
            or candidate.as_posix() != relative
            or candidate.parts[:5]
            != ("wiki", "private", "_sources", "knowledge", "local-only")
            or "images" not in candidate.parts[5:]
            or ".." in candidate.parts
        ):
            raise WoonError(
                "staged book asset destination must be under the private source image archive"
            )
        current = self._settings.vault
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise WoonError("staged book asset destination must not use symlinks")
        return _inside(self._settings.vault, relative, "staged book asset destination")

    def validate_book_workflow_pages(
        self,
        records: tuple[VerifiedBookPage, ...],
        workflow_phase: str,
    ) -> None:
        """Prevent concept-linking from silently regenerating reader prose."""

        for record in records:
            is_toc_only = _verified_book_toc_only(record.frontmatter)
            is_navigation_map = bool(record.frontmatter.get("navigation_groups"))
            candidate_body = _curated_body(
                record.body,
                allow_empty=is_toc_only or is_navigation_map,
            )
            if is_toc_only and candidate_body:
                raise WoonError(
                    f"toc-only verified book page must not contain authored prose: {record.page_id}"
                )
            workflow_violation = book_reader_workflow_prose_violation(candidate_body)
            if workflow_violation is not None:
                raise WoonError(
                    "book reader body must not contain generated learning workflow prose: "
                    f"{record.page_id}: {workflow_violation}"
                )
        sources, _, pages, _, _ = self._load_inputs()
        self._validate_book_rights_restore_records(records, sources, pages)
        if workflow_phase != "concept-linked":
            return
        for record in records:
            page = pages.get(record.page_id)
            if page is None:
                raise WoonError(
                    f"concept-linked book page must already exist: {record.page_id}"
                )
            render = page.get("render")
            if _verified_book_toc_only(record.frontmatter):
                if not isinstance(render, dict) or render.get("kind") != "toc-only":
                    raise WoonError(
                        f"concept-linked toc-only book page must remain toc-only: {record.page_id}"
                    )
                continue
            if not isinstance(render, dict) or render.get("kind") != "source-body":
                raise WoonError(
                    f"concept-linked book page must have a current source body: {record.page_id}"
                )
            source_id = _required_string(render, "source_id")
            source = sources.get(source_id)
            if source is None:
                raise WoonError(
                    f"concept-linked book page source does not exist: {record.page_id}"
                )
            is_toc_only = _verified_book_toc_only(record.frontmatter)
            is_navigation_map = bool(record.frontmatter.get("navigation_groups"))
            candidate_body = _curated_body(
                record.body,
                allow_empty=is_toc_only or is_navigation_map,
            )
            current_body = _curated_body(
                str(source.get("body", "")),
                allow_empty=is_toc_only or is_navigation_map,
            )
            if _normalize(candidate_body) != _normalize(current_body):
                raise WoonError(
                    "concept-linked workflow must not regenerate book reader body: "
                    f"{record.page_id}"
                )

    def _validate_book_rights_restore_records(
        self,
        records: tuple[VerifiedBookPage, ...],
        sources: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
        *,
        expected_book_id: str | None = None,
    ) -> set[str]:
        """Validate a one-scope rights restore and return exact carry-forward pages.

        A rights restore must explicitly pin every surviving blocked-rights page so
        reviewers cannot accidentally erase an older navigation shell.  Only the
        nearest blocked-rights ancestor of a newly landed descendant may change in
        this transaction.  Every other declared blocked-rights page is an exact
        carry-forward and is not rewritten by the writer.
        """

        records_by_id = {record.page_id: record for record in records}
        if len(records_by_id) != len(records):
            raise WoonError("verified book promotion contains a duplicate page_id")

        rights_pages_by_book: dict[str, set[str]] = {}
        for page_id, page in pages.items():
            for source_id in _string_list(page.get("source_ids"), "page source_ids"):
                if not source_id.startswith("source://book-rights/"):
                    continue
                tail = source_id.removeprefix("source://book-rights/")
                parts = tail.rsplit("/", 2)
                if len(parts) != 3:
                    raise WoonError(f"book rights source ID is malformed: {source_id}")
                rights_pages_by_book.setdefault(parts[0], set()).add(page_id)

        touched_books = {
            book_id
            for book_id, live_ids in rights_pages_by_book.items()
            if live_ids.intersection(records_by_id)
        }
        if expected_book_id is not None:
            if expected_book_id not in touched_books:
                raise WoonError(
                    "book rights restore does not declare a current rights page for: "
                    f"{expected_book_id}"
                )
            unexpected = touched_books.difference({expected_book_id})
            if unexpected:
                raise WoonError(
                    "book rights restore cannot mix books: " f"{sorted(unexpected)[0]}"
                )

        carry_forward_ids: set[str] = set()
        for book_id in sorted(touched_books):
            live_ids = rights_pages_by_book[book_id]
            omitted = live_ids.difference(records_by_id)
            if omitted:
                raise WoonError(
                    "book rights restore must replace every surviving rights page: "
                    f"{sorted(omitted)[0]}"
                )

            new_ids = {
                page_id
                for page_id in records_by_id
                if page_id.startswith(book_id + "/") and page_id not in pages
            }
            changed_scope_ids: set[str] = set()
            for new_id in new_ids:
                ancestors = {
                    page_id
                    for page_id in live_ids
                    if new_id.startswith(page_id + "/")
                }
                if ancestors:
                    changed_scope_ids.add(max(ancestors, key=len))

            book_carry_forward = live_ids.difference(changed_scope_ids)
            for page_id in sorted(book_carry_forward):
                record = records_by_id[page_id]
                page = pages[page_id]
                render = page.get("render")
                if not isinstance(render, dict):
                    raise WoonError(f"book carry-forward page render is invalid: {page_id}")
                render_kind = render.get("kind")
                if render_kind == "toc-only":
                    current_body = ""
                elif render_kind == "source-body":
                    source_id = _required_string(render, "source_id")
                    source = sources.get(source_id)
                    if source is None:
                        raise WoonError(
                            f"book carry-forward source does not exist: {page_id}"
                        )
                    current_body = str(source.get("body", ""))
                else:
                    raise WoonError(
                        f"book carry-forward page render is unsupported: {page_id}"
                    )
                if record.title != page.get("title"):
                    raise WoonError(
                        f"book rights carry-forward changed its title: {page_id}"
                    )
                if record.body != current_body:
                    raise WoonError(
                        f"book rights carry-forward changed its body: {page_id}"
                    )
                if record.frontmatter != page.get("frontmatter"):
                    raise WoonError(
                        f"book rights carry-forward changed its frontmatter: {page_id}"
                    )
                output_path = _inside(
                    self._settings.output_root,
                    _required_string(page, "output_path"),
                    "page output_path",
                )
                if record.expected_revision is None or not output_path.is_file():
                    raise WoonError(
                        f"book rights carry-forward requires its current revision: {page_id}"
                    )
                current_revision = _sha256_bytes(output_path.read_bytes())
                if record.expected_revision != current_revision:
                    raise WoonError(
                        "book rights carry-forward changed after review; reload before writing: "
                        f"{page_id}"
                    )
            carry_forward_ids.update(book_carry_forward)

        return carry_forward_ids

    def _validated_coverage_manifest_update(
        self, update: BookCoverageManifestUpdate
    ) -> tuple[Path, bytes]:
        if update.mode not in {"replace", "merge-scope"}:
            raise WoonError("book coverage manifest mode must be replace or merge-scope")
        if update.mode == "merge-scope":
            return self._validated_scoped_coverage_manifest_update(update)
        if any(
            value is not None
            for value in (
                update.base_relative_path,
                update.base_expected_sha256,
                update.scope_root_id,
            )
        ):
            raise WoonError("replace coverage update must not declare scoped merge fields")
        relative = update.relative_path
        candidate = Path(relative)
        if (
            not relative
            or "\\" in relative
            or candidate.is_absolute()
            or candidate.as_posix() != relative
            or candidate.parts[:2] != ("catalog", "book-coverage")
            or len(candidate.parts) != 3
            or candidate.suffix != ".json"
            or candidate.name in {".json", "..json"}
        ):
            raise WoonError(
                "book coverage manifest path must be one JSON file under catalog/book-coverage"
            )
        path = _inside(self._settings.vault, relative, "book coverage manifest path")
        current = self._settings.vault
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise WoonError("book coverage manifest path must not use symlinks")
        current_manifest: dict[str, Any] | None = None
        if path.exists():
            if not path.is_file():
                raise WoonError("book coverage manifest path must be a regular file")
            if update.expected_sha256 is None:
                raise WoonError("existing book coverage manifest requires expected_sha256")
            if re.fullmatch(r"[0-9a-f]{64}", update.expected_sha256) is None:
                raise WoonError(
                    "book coverage manifest expected_sha256 must be a lowercase SHA-256"
                )
            current_bytes = path.read_bytes()
            if _sha256_bytes(current_bytes) != update.expected_sha256:
                raise WoonError("book coverage manifest changed after review")
            try:
                loaded_current = json.loads(current_bytes)
            except json.JSONDecodeError as error:
                raise WoonError("current book coverage manifest is invalid JSON") from error
            if isinstance(loaded_current, dict):
                current_manifest = loaded_current
        elif update.expected_sha256 is not None:
            raise WoonError("new book coverage manifest requires expected_sha256 to be null")
        if not isinstance(update.replacement, dict):
            raise WoonError("book coverage manifest replacement must be an object")
        _validate_book_workflow_progression(
            current_manifest,
            update.replacement,
            "book coverage manifest",
            allow_source_landed_expansion=True,
        )
        try:
            replacement_bytes = (
                json.dumps(
                    update.replacement,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WoonError("book coverage manifest replacement must be JSON") from error
        return path, replacement_bytes

    def _validated_scoped_coverage_manifest_update(
        self, update: BookCoverageManifestUpdate
    ) -> tuple[Path, bytes]:
        """Validate a staged scope while proving the full manifest is unchanged."""

        target = Path(update.relative_path)
        if (
            not update.relative_path
            or "\\" in update.relative_path
            or target.is_absolute()
            or target.as_posix() != update.relative_path
            or target.parts[:2] != ("catalog", "book-coverage-scopes")
            or len(target.parts) != 4
            or target.suffix != ".json"
            or ".." in target.parts
        ):
            raise WoonError(
                "merge-scope coverage path must be one JSON file under "
                "catalog/book-coverage-scopes/<book>"
            )
        base_relative = update.base_relative_path
        base_candidate = Path(base_relative) if isinstance(base_relative, str) else Path()
        if (
            not isinstance(base_relative, str)
            or not base_relative
            or "\\" in base_relative
            or base_candidate.is_absolute()
            or base_candidate.as_posix() != base_relative
            or base_candidate.parts[:2] != ("catalog", "book-coverage")
            or len(base_candidate.parts) != 3
            or base_candidate.suffix != ".json"
            or ".." in base_candidate.parts
        ):
            raise WoonError(
                "merge-scope base_relative_path must be one JSON file under catalog/book-coverage"
            )
        if target.parts[2] != base_candidate.stem:
            raise WoonError("merge-scope coverage directory must match the base manifest name")
        if not isinstance(update.scope_root_id, str) or not update.scope_root_id.strip():
            raise WoonError("merge-scope coverage update requires scope_root_id")
        scope_root_id = update.scope_root_id.strip()
        if (
            not isinstance(update.base_expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", update.base_expected_sha256) is None
        ):
            raise WoonError("merge-scope base_expected_sha256 must be a lowercase SHA-256")

        base_path = _inside(
            self._settings.vault,
            base_relative,
            "book coverage base manifest path",
        )
        target_path = _inside(
            self._settings.vault,
            update.relative_path,
            "book coverage scoped manifest path",
        )
        for candidate in (base_candidate, target):
            current = self._settings.vault
            for part in candidate.parts:
                current = current / part
                if current.is_symlink():
                    raise WoonError("book coverage manifest path must not use symlinks")
        if not base_path.is_file():
            raise WoonError("merge-scope base manifest must be an existing regular file")
        base_bytes = base_path.read_bytes()
        if _sha256_bytes(base_bytes) != update.base_expected_sha256:
            raise WoonError("book coverage base manifest changed after scope review")
        current_scope: dict[str, Any] | None = None
        if target_path.exists():
            if not target_path.is_file():
                raise WoonError("scoped book coverage manifest path must be a regular file")
            if (
                not isinstance(update.expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", update.expected_sha256) is None
            ):
                raise WoonError("existing scoped book coverage manifest requires expected_sha256")
            current_scope_bytes = target_path.read_bytes()
            if _sha256_bytes(current_scope_bytes) != update.expected_sha256:
                raise WoonError("scoped book coverage manifest changed after review")
            try:
                loaded_scope = json.loads(current_scope_bytes)
            except json.JSONDecodeError as error:
                raise WoonError("current scoped book coverage manifest is invalid JSON") from error
            if isinstance(loaded_scope, dict):
                current_scope = loaded_scope
        elif update.expected_sha256 is not None:
            raise WoonError("new scoped book coverage manifest requires expected_sha256 to be null")

        try:
            base = json.loads(base_bytes)
        except json.JSONDecodeError as error:
            raise WoonError("merge-scope base manifest is invalid JSON") from error
        replacement = update.replacement
        if not isinstance(base, dict) or not isinstance(replacement, dict):
            raise WoonError("merge-scope base and replacement must be JSON objects")
        if replacement.get("schema_version") not in {
            LEGACY_BOOK_COVERAGE_SCHEMA_VERSION,
            BOOK_COVERAGE_SCHEMA_VERSION,
        }:
            raise WoonError(
                "merge-scope replacement schema_version must be "
                f"{LEGACY_BOOK_COVERAGE_SCHEMA_VERSION} or {BOOK_COVERAGE_SCHEMA_VERSION}"
            )
        _validate_book_workflow_progression(
            current_scope,
            replacement,
            "scoped book coverage manifest",
        )
        if replacement.get("book_id") != base.get("book_id"):
            raise WoonError("merge-scope replacement book_id must match the base manifest")
        scope = replacement.get("coverage_scope")
        expected_scope = {
            "root_id": scope_root_id,
            "base_relative_path": base_relative,
            "base_sha256": update.base_expected_sha256,
        }
        if scope != expected_scope:
            raise WoonError(
                "merge-scope replacement coverage_scope must exactly pin root, base path, "
                "and base hash"
            )
        nodes = replacement.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise WoonError("merge-scope replacement nodes must be a non-empty array")
        node_ids = {
            canonical_id
            for node in nodes
            if isinstance(node, dict) and isinstance(canonical_id := node.get("canonical_id"), str)
        }
        if len(node_ids) != len(nodes) or scope_root_id not in node_ids:
            raise WoonError(
                "merge-scope replacement nodes must be unique and include scope_root_id"
            )
        outside = sorted(
            node_id
            for node_id in node_ids
            if node_id != scope_root_id and not node_id.startswith(scope_root_id + "/")
        )
        if outside:
            raise WoonError(f"merge-scope replacement contains an out-of-scope node: {outside[0]}")
        base_nodes = base.get("nodes")
        base_node_ids = (
            {
                canonical_id
                for node in base_nodes
                if isinstance(node, dict)
                and isinstance(canonical_id := node.get("canonical_id"), str)
            }
            if isinstance(base_nodes, list)
            else set()
        )
        missing_from_base = sorted(node_ids.difference(base_node_ids))
        if missing_from_base:
            raise WoonError(
                "merge-scope replacement node is absent from the pinned full TOC: "
                f"{missing_from_base[0]}"
            )
        for sibling in (
            sorted(target_path.parent.glob("*.json")) if target_path.parent.exists() else ()
        ):
            if sibling == target_path or sibling.is_symlink() or not sibling.is_file():
                continue
            try:
                sibling_payload = json.loads(sibling.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WoonError(
                    f"existing scoped book coverage is invalid: {sibling.name}"
                ) from error
            sibling_scope = (
                sibling_payload.get("coverage_scope") if isinstance(sibling_payload, dict) else None
            )
            sibling_root = sibling_scope.get("root_id") if isinstance(sibling_scope, dict) else None
            if isinstance(sibling_root, str) and (
                sibling_root == scope_root_id
                or sibling_root.startswith(scope_root_id + "/")
                or scope_root_id.startswith(sibling_root + "/")
            ):
                raise WoonError(
                    f"merge-scope coverage overlaps an existing verified scope: {sibling_root}"
                )
        try:
            replacement_bytes = (
                json.dumps(replacement, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise WoonError("merge-scope replacement must be JSON") from error
        return target_path, replacement_bytes

    def retire_pages(self, replacements: dict[str, str]) -> RetiredPageReport:
        """Merge obsolete page identities into existing canonical pages.

        The retired page's source and claim records remain as inactive provenance.
        Frontmatter relations are redirected to the survivor, the obsolete output is
        removed, and receipts/relations are rebuilt in one rollback-safe operation.
        """

        if not replacements:
            raise WoonError("compiled page retirement requires at least one replacement")
        sources, claims, pages, curations, _ = self._load_inputs()
        normalized = {
            _required_string({"page_id": page_id}, "page_id"): _required_string(
                {"replacement_id": replacement_id}, "replacement_id"
            )
            for page_id, replacement_id in replacements.items()
        }
        if any(page_id == replacement_id for page_id, replacement_id in normalized.items()):
            raise WoonError("compiled page cannot replace itself")
        unknown = set(normalized).difference(pages)
        if unknown:
            raise WoonError(f"compiled Wiki page spec not found: {sorted(unknown)[0]}")
        unknown_replacements = set(normalized.values()).difference(pages)
        if unknown_replacements:
            raise WoonError(
                f"compiled Wiki replacement page spec not found: {sorted(unknown_replacements)[0]}"
            )
        retiring = set(normalized)
        if retiring.intersection(normalized.values()):
            raise WoonError("compiled page replacement must survive this retirement")

        snapshot = self.snapshot_inputs()
        output_snapshots: dict[Path, bytes] = {}
        affected: set[str] = set()
        try:
            for page_id, page in pages.items():
                if page_id in retiring:
                    continue
                frontmatter = page.get("frontmatter")
                if not isinstance(frontmatter, dict):
                    raise WoonError("page frontmatter must be a mapping")
                if _redirect_frontmatter_relations(frontmatter, normalized):
                    affected.add(page_id)

            for page_id, replacement_id in sorted(normalized.items()):
                retired_page = pages[page_id]
                replacement_page = pages[replacement_id]
                replacement_sources = self._page_sources(replacement_page, sources)
                replacement_claims = self._page_claims(replacement_page, claims)
                successor_source_id = _current_source_id(replacement_page, replacement_sources)
                successor_claim_id = _current_claim_id(replacement_page, replacement_claims)

                remaining_pages = {
                    other_id: other_page
                    for other_id, other_page in pages.items()
                    if other_id not in retiring
                }
                for source_id in _string_list(retired_page.get("source_ids"), "page source_ids"):
                    used_elsewhere = any(
                        source_id in _string_list(other_page.get("source_ids"), "page source_ids")
                        for other_page in remaining_pages.values()
                    )
                    if not used_elsewhere and sources[source_id].get("lifecycle") == "compiled":
                        sources[source_id].update(
                            {"lifecycle": "archived", "superseded_by": successor_source_id}
                        )
                for claim_id in _string_list(retired_page.get("claim_ids"), "page claim_ids"):
                    used_elsewhere = any(
                        claim_id in _string_list(other_page.get("claim_ids"), "page claim_ids")
                        for other_page in remaining_pages.values()
                    )
                    if not used_elsewhere and claims[claim_id].get("status") == "accepted":
                        claims[claim_id].update(
                            {"status": "superseded", "superseded_by": successor_claim_id}
                        )

                output_path = _inside(
                    self._settings.output_root,
                    retired_page["output_path"],
                    "page output_path",
                )
                if output_path.is_file():
                    output_snapshots[output_path] = output_path.read_bytes()
                del pages[page_id]
                del curations[page_id]
                affected.add(replacement_id)

            self._write_inputs(sources, claims, pages, curations)
            compile_report = self.compile(page_ids=tuple(sorted(affected)))
            for output_path in output_snapshots:
                output_path.unlink(missing_ok=True)
            audit = self.audit()
            if not audit.complete:
                raise WoonError(f"compiled page retirement left a stale catalog: {audit.errors[0]}")
        except Exception:
            self.restore_inputs(snapshot)
            for output_path, content in output_snapshots.items():
                atomic_write(output_path, content)
            self.compile(force=True)
            raise
        return RetiredPageReport(
            retired=len(normalized),
            compiled=compile_report.compiled,
            unchanged=compile_report.unchanged,
            page_ids=tuple(sorted(normalized)),
            replacement_ids=tuple(sorted(set(normalized.values()))),
        )

    def _apply_verified_book_records(
        self,
        records: tuple[VerifiedBookPage, ...],
        sources: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
        curations: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Validate and stage verified book pages without writing or compiling."""

        if not records:
            raise WoonError("verified book promotion requires at least one page")
        requested_ids = [record.page_id for record in records]
        if len(set(requested_ids)) != len(requested_ids):
            raise WoonError("verified book promotion contains a duplicate page_id")
        changed: list[str] = []
        for record in sorted(records, key=lambda item: item.page_id):
            page_id = _required_string({"page_id": record.page_id}, "page_id")
            title = _required_string({"title": record.title}, "title")
            statement = _required_string({"statement": record.statement}, "statement")
            current_use = _required_string({"current_use": record.current_use}, "current_use")
            locator = _required_string({"source_locator": record.source_locator}, "source_locator")
            if locator.startswith(("/", "~")) or ".." in Path(locator).parts:
                raise WoonError("verified book source_locator must be a stable safe locator")
            source_sha256 = record.source_sha256.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
                raise WoonError("verified book source_sha256 must be a lowercase SHA-256")
            frontmatter = copy.deepcopy(record.frontmatter)
            if not isinstance(frontmatter, dict):
                raise WoonError("verified book frontmatter must be a mapping")
            frontmatter["canonical_id"] = page_id
            frontmatter["title"] = title
            is_toc_only = _normalize_verified_book_toc_only(frontmatter)
            if frontmatter.get("access", "local-only") not in {"local-only", "private"}:
                raise WoonError("verified book promotion is private-only")
            if not isinstance(frontmatter.get("parent"), str) and page_id.count("/") > 1:
                raise WoonError("verified book child page requires a canonical parent")
            navigation_groups = frontmatter.get("navigation_groups")
            is_navigation_map = isinstance(navigation_groups, list) and bool(navigation_groups)
            body = _curated_body(
                record.body,
                allow_empty=is_toc_only or is_navigation_map,
            )
            if is_toc_only:
                if body:
                    raise WoonError(
                        f"toc-only verified book page must not contain authored prose: {page_id}"
                    )
                _validate_toc_only_navigation(page_id, navigation_groups)
                previous = pages.get(page_id)
                source_ids = list(previous.get("source_ids", [])) if previous else []
                claim_ids = list(previous.get("claim_ids", [])) if previous else []
                frontmatter["source_ids"] = source_ids
                replacement = {
                    "page_id": page_id,
                    "output_path": f"{page_id}.md",
                    "title": title,
                    "frontmatter": frontmatter,
                    "source_ids": source_ids,
                    "claim_ids": claim_ids,
                    "render": {"kind": "toc-only"},
                }
                replacement_curation = {
                    "page_id": page_id,
                    "current_use": current_use,
                    "basis": "manual-review",
                    "status": "confirmed",
                }
                if previous != replacement or curations.get(page_id) != replacement_curation:
                    changed.append(page_id)
                pages[page_id] = replacement
                curations[page_id] = replacement_curation
                continue

            body_hash = _sha256_text(_normalize(body))
            source_id = f"source://verified-book/{page_id}/{body_hash[:24]}"
            claim_id = f"claim://verified-book/{page_id}/{body_hash[:24]}"
            # The locator may be shared by every section on one official page,
            # but source ownership is page-bound.  Do not trust a builder's
            # frontmatter source_ids to turn that reusable locator into a
            # cross-page identity. Historical catalog sources are preserved
            # below on the page spec; reader metadata exposes only this
            # compiler-derived verified source.
            frontmatter["source_ids"] = [source_id]
            sources[source_id] = {
                "source_id": source_id,
                "kind": "verified-book",
                "locator": locator,
                "original_sha256": source_sha256,
                "normalized_sha256": body_hash,
                "privacy": "local-only",
                "lifecycle": "compiled",
                "title": title,
                "purpose": current_use,
                "body": body.rstrip() + "\n",
            }
            claims[claim_id] = {
                "claim_id": claim_id,
                "kind": "verified-book-summary",
                "status": "accepted",
                "statement": statement,
                "source_ids": [source_id],
                "markdown": body.rstrip() + "\n",
            }
            previous = pages.get(page_id)
            source_ids = list(previous.get("source_ids", [])) if previous else []
            claim_ids = list(previous.get("claim_ids", [])) if previous else []
            prior_book_source_ids = [
                value
                for value in source_ids
                if value.startswith(f"source://verified-book/{page_id}/") and value != source_id
            ]
            prior_book_claim_ids = [
                value
                for value in claim_ids
                if value.startswith(f"claim://verified-book/{page_id}/") and value != claim_id
            ]
            source_ids = [value for value in source_ids if value not in prior_book_source_ids]
            claim_ids = [value for value in claim_ids if value not in prior_book_claim_ids]
            source_ids = list(dict.fromkeys([*source_ids, source_id]))
            claim_ids = list(dict.fromkeys([*claim_ids, claim_id]))
            replacement = {
                "page_id": page_id,
                "output_path": f"{page_id}.md",
                "title": title,
                "frontmatter": frontmatter,
                "source_ids": source_ids,
                "claim_ids": claim_ids,
                "render": {"kind": "source-body", "source_id": source_id},
            }
            replacement_curation = {
                "page_id": page_id,
                "current_use": current_use,
                "basis": "verified-book-source",
                "status": "confirmed",
            }
            if previous != replacement or curations.get(page_id) != replacement_curation:
                changed.append(page_id)
            pages[page_id] = replacement
            curations[page_id] = replacement_curation
            for prior_source_id in prior_book_source_ids:
                self._supersede_unshared_curated_source(
                    prior_source_id, source_id, page_id, pages, sources
                )
            self._supersede_unshared_claims(prior_book_claim_ids, claim_id, page_id, pages, claims)

        known = self._known_relation_targets(pages)
        for record in records:
            parent = _canonical_parent_id(record.frontmatter.get("parent"))
            if parent and parent not in known:
                raise WoonError(
                    f"verified book parent does not exist for {record.page_id}: {parent}"
                )
        return changed

    @staticmethod
    def _supersede_unshared_curated_source(
        prior_source_id: str,
        successor_source_id: str,
        page_id: str,
        pages: dict[str, dict[str, Any]],
        sources: dict[str, dict[str, Any]],
    ) -> None:
        used_elsewhere = any(
            other_page_id != page_id
            and prior_source_id in _string_list(other_page.get("source_ids"), "page source_ids")
            for other_page_id, other_page in pages.items()
        )
        if not used_elsewhere:
            sources[prior_source_id].update(
                {"lifecycle": "archived", "superseded_by": successor_source_id}
            )

    @staticmethod
    def _supersede_unshared_curated_claims(
        claim_ids: list[str],
        prior_source_id: str,
        successor_claim_id: str,
        page_id: str,
        pages: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
    ) -> list[str]:
        retained: list[str] = []
        for prior_claim_id in claim_ids:
            claim = claims[prior_claim_id]
            claim_source_ids = _string_list(claim.get("source_ids"), "claim source_ids")
            is_prior_curated_claim = (
                claim.get("kind") == "curated-document" and prior_source_id in claim_source_ids
            )
            used_elsewhere = any(
                other_page_id != page_id
                and prior_claim_id in _string_list(other_page.get("claim_ids"), "page claim_ids")
                for other_page_id, other_page in pages.items()
            )
            if not is_prior_curated_claim or used_elsewhere:
                retained.append(prior_claim_id)
                continue
            claim.update({"status": "superseded", "superseded_by": successor_claim_id})
        return retained

    @staticmethod
    def _supersede_unshared_claims(
        claim_ids: list[str],
        successor_claim_id: str,
        page_id: str,
        pages: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
    ) -> None:
        for prior_claim_id in claim_ids:
            used_elsewhere = any(
                other_page_id != page_id
                and prior_claim_id in _string_list(other_page.get("claim_ids"), "page claim_ids")
                for other_page_id, other_page in pages.items()
            )
            if not used_elsewhere:
                claims[prior_claim_id].update(
                    {"status": "superseded", "superseded_by": successor_claim_id}
                )

    def reconcile_superseded_revisions(self) -> RevisionReconciliationReport:
        """Classify safe, unreferenced conversation revisions without discarding them.

        Repeated archive calls historically replaced a page spec but kept its prior
        generated source and claim as an orphan.  A record is reconciled only when
        the current page spec identifies one unambiguous successor with the same
        conversation locator; unrelated or ambiguous orphan records remain errors.
        """

        sources, claims, pages, curations, _ = self._load_inputs()
        referenced_sources = {
            source_id
            for page in pages.values()
            for source_id in _string_list(page.get("source_ids"), "page source_ids")
        }
        referenced_claims = {
            claim_id
            for page in pages.values()
            for claim_id in _string_list(page.get("claim_ids"), "page claim_ids")
        }
        active_sources_by_locator: dict[str, set[str]] = {}
        for source_id in referenced_sources:
            source = sources.get(source_id)
            if source is None or source.get("lifecycle") != "compiled":
                continue
            locator = source.get("locator")
            if isinstance(locator, str) and locator:
                active_sources_by_locator.setdefault(locator, set()).add(source_id)

        source_successors: dict[str, str] = {}
        for source_id, source in sources.items():
            if source_id in referenced_sources or source.get("lifecycle") != "compiled":
                continue
            if source.get("kind") != "conversation":
                continue
            locator = source.get("locator")
            successors = (
                active_sources_by_locator.get(locator, set()) if isinstance(locator, str) else set()
            )
            if len(successors) != 1:
                continue
            successor = next(iter(successors))
            if successor == source_id:
                continue
            source["lifecycle"] = "archived"
            source["superseded_by"] = successor
            source_successors[source_id] = successor

        active_claims_by_sources: dict[tuple[str, ...], set[str]] = {}
        for claim_id in referenced_claims:
            claim = claims.get(claim_id)
            if claim is None or claim.get("status") != "accepted":
                continue
            source_ids = tuple(_string_list(claim.get("source_ids"), "claim source_ids"))
            active_claims_by_sources.setdefault(source_ids, set()).add(claim_id)

        superseded_claims = 0
        for claim_id, claim in claims.items():
            if claim_id in referenced_claims or claim.get("status") != "accepted":
                continue
            source_ids = tuple(_string_list(claim.get("source_ids"), "claim source_ids"))
            successor_sources = tuple(
                source_successors.get(source_id, source_id) for source_id in source_ids
            )
            successors = active_claims_by_sources.get(successor_sources, set())
            if len(successors) != 1:
                continue
            successor = next(iter(successors))
            if successor == claim_id:
                continue
            claim["status"] = "superseded"
            claim["superseded_by"] = successor
            superseded_claims += 1

        if source_successors or superseded_claims:
            self._write_inputs(sources, claims, pages, curations)
            self._last_input_state = None
        return RevisionReconciliationReport(
            archived_sources=len(source_successors),
            superseded_claims=superseded_claims,
        )

    def initialize_curation(self) -> int:
        """Create one present-use record for every existing page spec.

        This deliberately derives only a *current* operating purpose.  It does
        not populate ``source.purpose`` for legacy material because that field
        represents historical collection intent, which cannot be reconstructed
        reliably from the rendered document.
        """

        if self._settings.curation_path.exists():
            raise WoonError(
                "compiled Wiki curation catalog already exists; "
                "edit its records instead of replacing them"
            )
        sources = _indexed(
            _load_yaml_list(self._settings.sources_path, "sources"), "source_id", "source"
        )
        pages = _indexed(_load_yaml_list(self._settings.pages_path, "pages"), "page_id", "page")
        curations = []
        for page_id, page in sorted(pages.items()):
            title = _required_string(page, "title")
            frontmatter = page.get("frontmatter")
            if not isinstance(frontmatter, dict):
                raise WoonError("page frontmatter must be a mapping")
            curations.append(
                _initial_curation(
                    page_id,
                    title,
                    frontmatter,
                    self._page_sources(page, sources),
                )
            )
        _write_yaml(
            self._settings.curation_path,
            {"version": SCHEMA_VERSION, "curations": curations},
        )
        self._last_input_state = None
        return len(curations)

    def refresh_provisional_curation(self) -> int:
        """Refresh generated legacy curation without overwriting manual records.

        A provisional record may predate a curated or newly archived page that
        was later added to its provenance.  Its source kinds are therefore
        rechecked so that an explicit current-use purpose is not mislabeled as
        inferred legacy metadata.
        """

        sources, _, pages, curations, _ = self._load_inputs()
        refreshed = 0
        for page_id, page in sorted(pages.items()):
            curation = self._page_curation(page, curations)
            if curation["basis"] != "legacy-page-metadata" or curation["status"] != "provisional":
                continue
            title = _required_string(page, "title")
            frontmatter = page.get("frontmatter")
            if not isinstance(frontmatter, dict):
                raise WoonError("page frontmatter must be a mapping")
            replacement = _initial_curation(
                page_id,
                title,
                frontmatter,
                self._page_sources(page, sources),
            )
            if any(
                curation[field] != replacement[field]
                for field in ("current_use", "basis", "status")
            ):
                curation.update(replacement)
                refreshed += 1
        if refreshed:
            _write_yaml(
                self._settings.curation_path,
                {
                    "version": SCHEMA_VERSION,
                    "curations": [curations[key] for key in sorted(curations)],
                },
            )
            self._last_input_state = None
        return refreshed

    def apply_compiled_wiki_transaction(
        self, transaction: CompiledWikiTransaction
    ) -> CompiledWikiTransactionReport:
        """Apply exact catalog upserts, compile affected pages, and audit the result.

        Source and claim identifiers are immutable: an existing identifier may
        be repeated only with byte-for-byte equivalent structured content.
        Pages and curations are optimistic upserts whose revisions are checked
        by :class:`KnowledgeService` while it holds the repository lock.
        """

        page_ids = _transaction_record_ids(transaction.pages_upsert, "page_id", "page")
        curation_ids = _transaction_record_ids(transaction.curations_upsert, "page_id", "curation")
        source_ids = _transaction_record_ids(transaction.sources_upsert, "source_id", "source")
        claim_ids = _transaction_record_ids(transaction.claims_upsert, "claim_id", "claim")
        if not page_ids:
            raise WoonError("compiled Wiki transaction requires at least one page upsert")
        if set(page_ids) != set(curation_ids):
            raise WoonError("compiled Wiki transaction curation page_ids must match page upserts")
        if set(page_ids) != set(transaction.expected_revisions):
            raise WoonError("compiled Wiki transaction expected_revisions must match page upserts")
        for page_id, revision in transaction.expected_revisions.items():
            _required_string({"page_id": page_id}, "page_id")
            if revision is not None and re.fullmatch(r"[0-9a-f]{64}", revision) is None:
                raise WoonError(
                    "compiled Wiki transaction expected revision must be lowercase SHA-256 or null"
                )

        sources, claims, pages, curations, _ = self._load_inputs()
        page_upserts = {str(record["page_id"]): record for record in transaction.pages_upsert}
        for page_id in page_ids:
            expected_revision = transaction.expected_revisions[page_id]
            current_page = pages.get(page_id)
            target_path = _inside(
                self._settings.output_root,
                _required_string(page_upserts[page_id], "output_path"),
                "page output_path",
            )
            if expected_revision is None:
                if current_page is not None or target_path.exists():
                    raise WoonError(
                        "compiled Wiki transaction expected a new page but its catalog or "
                        f"output already exists: {page_id}"
                    )
            elif current_page is None:
                raise WoonError(
                    "compiled Wiki transaction expected an existing page spec but it is missing: "
                    f"{page_id}"
                )
        for record in transaction.sources_upsert:
            _validate_source(record)
            source_id = str(record["source_id"])
            if source_id in sources and sources[source_id] != record:
                raise WoonError(
                    "compiled Wiki transaction source ID collision is not an exact upsert: "
                    f"{source_id}"
                )
            sources[source_id] = copy.deepcopy(record)
        for record in transaction.claims_upsert:
            _validate_claim_record(record)
            claim_id = str(record["claim_id"])
            if claim_id in claims and claims[claim_id] != record:
                raise WoonError(
                    "compiled Wiki transaction claim ID collision is not an exact upsert: "
                    f"{claim_id}"
                )
            claims[claim_id] = copy.deepcopy(record)
        for record in transaction.pages_upsert:
            pages[str(record["page_id"])] = copy.deepcopy(record)
        for record in transaction.curations_upsert:
            curations[str(record["page_id"])] = copy.deepcopy(record)

        for page_id in page_ids:
            page = pages[page_id]
            _validate_page(
                page,
                self._page_sources(page, sources),
                self._page_claims(page, claims),
                self._page_curation(page, curations),
            )

        input_snapshot = self.snapshot_inputs()
        output_snapshot = self.snapshot_outputs(
            extra_relative_paths=tuple(str(pages[page_id]["output_path"]) for page_id in page_ids)
        )
        try:
            self._write_inputs(sources, claims, pages, curations)
            compile_report = self.compile(page_ids=page_ids)
            tree_changed_paths: set[Path] = set()
            tree_page_ids = tuple(
                page_id
                for page_id in page_ids
                if page_id in pages
                and isinstance(pages[page_id].get("frontmatter"), dict)
                and pages[page_id]["frontmatter"].get("navigation_groups")
            )
            for page_id in tree_page_ids:
                tree_report = prepare_wiki_tree_refresh(
                    self._settings.vault,
                    canonical_prefix=page_id,
                )
                if tree_report.issues:
                    raise WoonError(
                        "compiled Wiki transaction could not refresh its Wiki tree: "
                        + tree_report.issues[0]
                    )
                tree_changed_paths.update(
                    path
                    for path, content in tree_report.pages.items()
                    if not path.is_file() or path.read_bytes() != content
                )
                apply_wiki_tree_refresh(self._settings.vault, tree_report)
            self._refresh_generated_view_receipts(tuple(sorted(tree_changed_paths)))
            audit = self.audit()
            if not audit.complete:
                raise WoonError(
                    "compiled Wiki transaction final audit failed: " + "; ".join(audit.errors)
                )
        except Exception:
            self.restore_inputs(input_snapshot)
            self.restore_outputs(output_snapshot)
            raise
        return CompiledWikiTransactionReport(
            sources_upserted=len(source_ids),
            claims_upserted=len(claim_ids),
            pages_upserted=len(page_ids),
            curations_upserted=len(curation_ids),
            compiled=compile_report.compiled,
            unchanged=compile_report.unchanged,
            page_ids=page_ids,
        )

    def snapshot_inputs(self, *, extra_paths: tuple[Path, ...] = ()) -> dict[Path, bytes | None]:
        """Capture small compiler catalogs before a transactional canonical mutation."""

        return {
            path: path.read_bytes() if path.is_file() else None
            for path in (
                *self._input_paths(),
                self._settings.review_queue_path,
                *extra_paths,
            )
        }

    def _refresh_generated_view_receipts(self, paths: tuple[Path, ...]) -> None:
        """Pin tree-derived output bytes after proving compiler projection stability."""

        sources, claims, pages, curations, receipts = self._load_inputs()
        by_output = {
            _inside(self._settings.output_root, page["output_path"], "page output_path"): (
                page_id,
                page,
            )
            for page_id, page in pages.items()
        }
        changed = False
        for path in paths:
            resolved = path.resolve()
            entry = by_output.get(resolved)
            if entry is None:
                raise WoonError("Wiki tree refresh changed an output not owned by the compiler")
            page_id, page = entry
            receipt = receipts.get(page_id)
            if receipt is None:
                raise WoonError(f"compiled receipt is missing after Wiki tree refresh: {page_id}")
            curation = self._page_curation(page, curations)
            source_records = self._page_sources(page, sources)
            claim_records = self._page_claims(page, claims)
            input_sha256 = _input_hash(page, source_records, claim_records, curation)
            rendered = _render_page(page, source_records, claim_records, curation, input_sha256)
            compiler_projection_sha256 = _sha256_text(preserve_managed_context("", rendered))
            actual = resolved.read_text(encoding="utf-8")
            if receipt.get("input_sha256") != input_sha256:
                raise WoonError(f"compiled input changed during Wiki tree refresh: {page_id}")
            if receipt.get("compiler_projection_sha256") != compiler_projection_sha256:
                raise WoonError(f"compiled projection changed during Wiki tree refresh: {page_id}")
            if preserve_managed_context(actual, rendered) != actual:
                raise WoonError(f"Wiki tree refresh changed compiler-owned content: {page_id}")
            output_sha256 = _sha256_text(actual)
            if receipt.get("output_sha256") != output_sha256:
                receipt["output_sha256"] = output_sha256
                changed = True
        if changed:
            _write_yaml(
                self._settings.receipts_path,
                {
                    "version": SCHEMA_VERSION,
                    "receipts": [receipts[key] for key in sorted(receipts)],
                },
            )

    def snapshot_outputs(
        self, *, extra_relative_paths: tuple[str, ...] = ()
    ) -> dict[Path, bytes | None]:
        """Capture all generated pages plus requested new paths for rollback."""

        _, _, pages, _, _ = self._load_inputs()
        relative_paths = {_required_string(page, "output_path") for page in pages.values()}
        relative_paths.update(extra_relative_paths)
        paths = {
            _inside(self._settings.output_root, relative, "page output_path")
            for relative in relative_paths
        }
        return {path: path.read_bytes() if path.is_file() else None for path in paths}

    def restore_outputs(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore generated output bytes captured by :meth:`snapshot_outputs`."""

        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content)

    def restore_inputs(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore a previously captured compiler catalog without touching raw sources."""

        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content)
        self._last_input_state = None

    def audit(self) -> CompilationAudit:
        """Check that every page can be reproduced from valid compiler inputs."""

        errors: list[str] = []
        try:
            sources, claims, pages, curations, receipts = self._load_inputs()
            review_items = _load_yaml_list(self._settings.review_queue_path, "items")
        except WoonError as error:
            return CompilationAudit(0, 0, (str(error),))
        outputs: set[str] = set()
        referenced_sources: set[str] = set()
        referenced_claims: set[str] = set()
        for source_id, source in sorted(sources.items()):
            try:
                _validate_source(source)
                _validate_archive_review_binding(source, review_items)
            except WoonError as error:
                errors.append(f"{source_id}: {error}")
        for claim_id, claim in sorted(claims.items()):
            try:
                _validate_claim_record(claim)
            except WoonError as error:
                errors.append(f"{claim_id}: {error}")
        for page_id, page in sorted(pages.items()):
            try:
                curation = self._page_curation(page, curations)
                source_records = self._page_sources(page, sources)
                claim_records = self._page_claims(page, claims)
                _validate_page(page, source_records, claim_records, curation)
                referenced_sources.update(record["source_id"] for record in source_records)
                referenced_claims.update(record["claim_id"] for record in claim_records)
                relative = page["output_path"]
                if relative in outputs:
                    raise WoonError(f"duplicate compiled output_path: {relative}")
                outputs.add(relative)
                path = _inside(self._settings.output_root, relative, "page output_path")
                if not path.is_file():
                    raise WoonError("compiled output is missing")
                input_sha256 = _input_hash(page, source_records, claim_records, curation)
                receipt = receipts.get(page_id)
                if receipt is None:
                    raise WoonError("compiled receipt is missing")
                if receipt.get("input_sha256") != input_sha256:
                    raise WoonError("compiled output is stale for its source or claims")
                actual = path.read_text(encoding="utf-8")
                if receipt.get("output_sha256") != _sha256_text(actual):
                    raise WoonError("compiled output bytes differ from its receipt")
                rendered = _render_page(page, source_records, claim_records, curation, input_sha256)
                compiler_projection_sha256 = _sha256_text(preserve_managed_context("", rendered))
                if receipt.get("compiler_projection_sha256") != compiler_projection_sha256:
                    raise WoonError("compiled projection differs from its receipt")
                expected = preserve_managed_context(actual, rendered)
                if expected != actual:
                    raise WoonError("compiled output cannot be reproduced with its Wiki context")
                _parse_markdown(actual, Path(relative))
            except (OSError, UnicodeError, WoonError) as error:
                errors.append(f"{page_id}: {error}")
        for page_id in sorted(set(receipts).difference(pages)):
            errors.append(f"{page_id}: receipt has no page spec")
        for source_id in sorted(set(sources).difference(referenced_sources)):
            inactive_error = _inactive_revision_error(
                source_id,
                sources[source_id],
                sources,
                referenced_sources,
                state_key="lifecycle",
                inactive_state="archived",
                record_label="source",
            )
            if inactive_error is not None:
                errors.append(f"{source_id}: {inactive_error}")
        for claim_id in sorted(set(claims).difference(referenced_claims)):
            inactive_error = _inactive_revision_error(
                claim_id,
                claims[claim_id],
                claims,
                referenced_claims,
                state_key="status",
                inactive_state="superseded",
                record_label="claim",
            )
            if inactive_error is not None:
                errors.append(f"{claim_id}: {inactive_error}")
        for page_id in sorted(set(curations).difference(pages)):
            errors.append(f"{page_id}: curation has no page spec")
        try:
            current_relations = _load_yaml_list(self._settings.relations_path, "relations")
            if current_relations != _expected_relations(pages):
                errors.append("relations catalog is stale for current page specs")
            else:
                known_targets = self._known_relation_targets(pages)
                for relation in current_relations:
                    target = _required_string(relation, "to_id")
                    if target not in known_targets:
                        errors.append(f"relation target does not resolve: {target}")
        except WoonError as error:
            errors.append(str(error))
        try:
            review_items = _load_yaml_list(self._settings.review_queue_path, "items")
            for position, item in enumerate(review_items, start=1):
                source_ids = item.get("source_ids", [])
                if source_ids == []:
                    continue
                for source_id in _string_list(source_ids, "review item source_ids"):
                    if source_id not in sources:
                        errors.append(
                            f"review item {position} references missing source_id {source_id!r}"
                        )
        except WoonError as error:
            errors.append(str(error))
        return CompilationAudit(len(pages), len(receipts), tuple(errors))

    def navigation_issues(self) -> tuple[str, ...]:
        """Return hierarchy and generated-navigation contract violations."""

        try:
            _, _, issues = load_wiki_tree(self._settings.vault)
        except WoonError as error:
            return (str(error),)
        return issues

    def assert_current(self) -> None:
        """Fail closed if compiler inputs changed without a matching build receipt."""

        state = self._input_state()
        if state == self._last_input_state:
            return
        audit = self.audit()
        if not audit.complete:
            raise WoonError(f"compiled Wiki is stale: {audit.errors[0]}")
        self._last_input_state = state

    def _discover_pages(self) -> list[tuple[Path, str]]:
        if not self._settings.output_root.is_dir():
            return []
        pages: list[tuple[Path, str]] = []
        for path in sorted(self._settings.output_root.rglob("*.md")):
            relative = path.relative_to(self._settings.output_root)
            if any(part.startswith("_") for part in relative.parts):
                continue
            pages.append((relative, path.read_text(encoding="utf-8")))
        return pages

    def _load_inputs(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        raw_sources = _load_yaml_list(self._settings.sources_path, "sources")
        raw_claims = _load_yaml_list(self._settings.claims_path, "claims")
        raw_pages = _load_yaml_list(self._settings.pages_path, "pages")
        raw_curations = _load_yaml_list(self._settings.curation_path, "curations")
        raw_receipts = _load_yaml_list(self._settings.receipts_path, "receipts")
        sources = _indexed(raw_sources, "source_id", "source")
        claims = _indexed(raw_claims, "claim_id", "claim")
        pages = _indexed(raw_pages, "page_id", "page")
        curations = _indexed(raw_curations, "page_id", "curation")
        receipts = _indexed(raw_receipts, "page_id", "receipt")
        return sources, claims, pages, curations, receipts

    def _write_inputs(
        self,
        sources: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
        curations: dict[str, dict[str, Any]],
    ) -> None:
        _write_yaml(
            self._settings.sources_path,
            {"version": SCHEMA_VERSION, "sources": [sources[key] for key in sorted(sources)]},
        )
        _write_yaml(
            self._settings.claims_path,
            {"version": SCHEMA_VERSION, "claims": [claims[key] for key in sorted(claims)]},
        )
        _write_yaml(
            self._settings.pages_path,
            {"version": SCHEMA_VERSION, "pages": [pages[key] for key in sorted(pages)]},
        )
        _write_yaml(
            self._settings.curation_path,
            {
                "version": SCHEMA_VERSION,
                "curations": [curations[key] for key in sorted(curations)],
            },
        )

    def _page_sources(
        self, page: dict[str, Any], sources: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        render = page.get("render")
        if (
            isinstance(render, dict)
            and render.get("kind") == "toc-only"
            and page.get("source_ids") == []
        ):
            return []
        identifiers = _string_list(page.get("source_ids"), "page source_ids")
        records: list[dict[str, Any]] = []
        for identifier in identifiers:
            record = sources.get(identifier)
            if record is None:
                raise WoonError(f"page references missing source_id {identifier!r}")
            records.append(record)
        return records

    def _page_curation(
        self, page: dict[str, Any], curations: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        page_id = _required_string(page, "page_id")
        curation = curations.get(page_id)
        if curation is None:
            raise WoonError(f"page has no current-use curation: {page_id}")
        _validate_curation(curation, page_id)
        return curation

    def _page_claims(
        self, page: dict[str, Any], claims: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        render = page.get("render")
        if (
            isinstance(render, dict)
            and render.get("kind") == "toc-only"
            and page.get("claim_ids") == []
        ):
            return []
        identifiers = _string_list(page.get("claim_ids"), "page claim_ids")
        records: list[dict[str, Any]] = []
        for identifier in identifiers:
            record = claims.get(identifier)
            if record is None:
                raise WoonError(f"page references missing claim_id {identifier!r}")
            records.append(record)
        return records

    def _input_paths(self) -> tuple[Path, ...]:
        return (
            self._settings.sources_path,
            self._settings.claims_path,
            self._settings.pages_path,
            self._settings.curation_path,
            self._settings.relations_path,
            self._settings.receipts_path,
        )

    def _input_state(self) -> tuple[tuple[str, int, int], ...]:
        state: list[tuple[str, int, int]] = []
        for path in self._input_paths():
            if path.is_file():
                stat = path.stat()
                state.append(
                    (
                        path.relative_to(self._settings.vault).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                )
            else:
                state.append((path.relative_to(self._settings.vault).as_posix(), -1, -1))
        return tuple(state)

    def _known_relation_targets(self, pages: dict[str, dict[str, Any]]) -> set[str]:
        """Resolve Wikilink-style targets without requiring every target to be a compiled page."""

        targets = set(pages)
        for page in pages.values():
            output = Path(page["output_path"])
            targets.add(output.with_suffix("").as_posix())
            targets.add(output.stem)
            frontmatter = page.get("frontmatter")
            if isinstance(frontmatter, dict):
                canonical_id = frontmatter.get("canonical_id")
                if isinstance(canonical_id, str) and canonical_id.strip():
                    targets.add(canonical_id.strip())
        for path in self._settings.vault.rglob("*.md"):
            if "_sources" in path.relative_to(self._settings.vault).parts:
                continue
            relative = path.relative_to(self._settings.vault).with_suffix("").as_posix()
            targets.add(relative)
            if relative.startswith("wiki/"):
                # ``_relation_target`` deliberately removes the human-facing
                # Wiki root. Non-compiled tree hubs are still valid canonical
                # relation targets and must be normalized the same way.
                targets.add(relative.removeprefix("wiki/"))
            targets.add(path.stem)
        return targets


def _validate_page(
    page: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    curation: dict[str, Any],
) -> None:
    page_id = _required_string(page, "page_id")
    output_path = _required_string(page, "output_path")
    if output_path.startswith("/") or ".." in Path(output_path).parts:
        raise WoonError("page output_path must be a safe relative Markdown path")
    if not output_path.endswith(".md"):
        raise WoonError("page output_path must end with .md")
    title = _required_string(page, "title")
    frontmatter = page.get("frontmatter")
    if not isinstance(frontmatter, dict):
        raise WoonError("page frontmatter must be a mapping")
    if str(frontmatter.get("title", "")).strip() != title:
        raise WoonError("page frontmatter title must match page title")
    _validate_curation(curation, page_id)
    render = page.get("render")
    if not isinstance(render, dict):
        raise WoonError("page render must be a mapping")
    kind = render.get("kind")
    if kind not in {"source-body", "claims", "toc-only"}:
        raise WoonError("page render.kind must be source-body, claims, or toc-only")
    if kind == "source-body":
        source_id = _required_string(render, "source_id")
        if source_id not in {record.get("source_id") for record in sources}:
            raise WoonError("source-body render source_id must be included in page source_ids")
    for source in sources:
        _validate_source(source)
        if source.get("lifecycle") != "compiled":
            raise WoonError("compiled page may only use compiled sources")
    for claim in claims:
        _validate_claim(claim, sources)
        if kind == "claims" and len(claim["markdown"].strip()) > MAX_COMPOSED_CLAIM_MARKDOWN_CHARS:
            raise WoonError(
                "composed claim markdown exceeds "
                f"{MAX_COMPOSED_CLAIM_MARKDOWN_CHARS} characters; split it by revisitable claim"
            )
    access = str(frontmatter.get("access", "local-only"))
    if access == "public" and any(str(source.get("privacy")) != "public" for source in sources):
        raise WoonError("public compiled page requires public source provenance")
    if not page_id.endswith(output_path.removesuffix(".md")) and not page_id.startswith("wiki/"):
        raise WoonError("page_id must identify a Wiki output")


def _transaction_record_ids(
    records: tuple[dict[str, Any], ...], key: str, label: str
) -> tuple[str, ...]:
    """Return deterministic identifiers while rejecting duplicate operations."""

    identifiers = tuple(_required_string(record, key) for record in records)
    if len(set(identifiers)) != len(identifiers):
        raise WoonError(f"compiled Wiki transaction contains a duplicate {label} ID")
    return tuple(sorted(identifiers))


def _validate_source(source: dict[str, Any]) -> None:
    _required_string(source, "source_id")
    _required_string(source, "kind")
    _required_string(source, "locator")
    _required_digest(source, "original_sha256")
    _required_digest(source, "normalized_sha256")
    if source.get("privacy") not in {"local-only", "private", "public"}:
        raise WoonError("source privacy is invalid")
    lifecycle = source.get("lifecycle")
    if lifecycle not in {"captured", "compiled", "archived"}:
        raise WoonError("source lifecycle is invalid")
    if lifecycle == "archived":
        _required_string(source, "superseded_by")
    elif "superseded_by" in source:
        raise WoonError("only archived source may declare superseded_by")
    if source.get("kind") != "legacy-wiki":
        _required_string(source, "purpose")
    archive_origin = source.get("archive_origin")
    if archive_origin is not None and archive_origin not in (
        *MANUAL_ARCHIVE_ORIGINS,
        GIT_RESTORE_ARCHIVE_ORIGIN,
    ):
        raise WoonError("source archive_origin is invalid")
    review_id = source.get("approved_review_id")
    if archive_origin in MANUAL_ARCHIVE_ORIGINS:
        _required_string(source, "approved_review_id")
    elif archive_origin == GIT_RESTORE_ARCHIVE_ORIGIN:
        if review_id is not None:
            raise WoonError("Git restore source must not declare approved_review_id")
        source_session_ids = source.get("source_session_ids")
        if (
            not isinstance(source_session_ids, list)
            or len(source_session_ids) != 1
            or not isinstance(source_session_ids[0], str)
            or not source_session_ids[0].startswith("git:")
            or not source_session_ids[0][4:].strip()
        ):
            raise WoonError("Git restore source must declare exactly one Git revision")
    elif review_id is not None:
        raise WoonError("source approved_review_id requires archive_origin")
    if not isinstance(source.get("body"), str):
        raise WoonError("source body must be a string")
    if _contains_mermaid_color_directive(source["body"]):
        raise WoonError(
            "source Mermaid must use renderer-owned neutral colors; "
            "encode meaning with labels, position, and line style"
        )
    if _sha256_text(_normalize(source["body"])) != source["normalized_sha256"]:
        raise WoonError("source normalized_sha256 does not match the normalized source body")


def _contains_mermaid_color_directive(markdown: str) -> bool:
    """Reject diagram-local colors so every renderer can own light/dark contrast."""

    in_mermaid = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not in_mermaid:
            in_mermaid = stripped.startswith("```mermaid")
            continue
        if stripped.startswith("```"):
            in_mermaid = False
            continue
        if MERMAID_COLOR_DIRECTIVE_RE.search(line) or MERMAID_THEME_COLOR_RE.search(line):
            return True
    return False


def _validate_archive_review_binding(
    source: dict[str, Any], review_items: list[dict[str, Any]]
) -> None:
    """Re-check review evidence during compile audit, not just at write time."""

    if source.get("archive_origin") not in MANUAL_ARCHIVE_ORIGINS:
        return
    body = source.get("body")
    if not isinstance(body, str):
        raise WoonError("approved archive source body must be a string")
    body_hash = _sha256_text(_normalize(body))
    review_id = source.get("approved_review_id")
    for item in review_items:
        if item.get("candidate_id") != review_id:
            continue
        if (
            item.get("status") == "approved"
            and item.get("kind") == "manual-archive"
            and item.get("input_sha256") == body_hash
            and isinstance(item.get("approved_by"), str)
            and item["approved_by"].strip()
        ):
            return
        break
    raise WoonError("approved archive source has no matching review evidence")


def _validate_curation(curation: dict[str, Any], page_id: str) -> None:
    if _required_string(curation, "page_id") != page_id:
        raise WoonError("current-use curation page_id must match page")
    _required_string(curation, "current_use")
    basis = _required_string(curation, "basis")
    if basis not in {
        "archive-request",
        "legacy-page-metadata",
        "manual-review",
        "curated-revision",
        "verified-book-source",
    }:
        raise WoonError("current-use curation basis is invalid")
    status = _required_string(curation, "status")
    if status not in {"provisional", "confirmed", "needs-review"}:
        raise WoonError("current-use curation status is invalid")


def _validate_claim(claim: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    _validate_claim_record(claim)
    if claim.get("status") != "accepted":
        raise WoonError("compiled page may only use accepted claims")
    source_ids = _string_list(claim.get("source_ids"), "claim source_ids")
    known = {str(source.get("source_id")) for source in sources}
    if not set(source_ids).issubset(known):
        raise WoonError("claim evidence source_id must belong to its page")


def _validate_claim_record(claim: dict[str, Any]) -> None:
    _required_string(claim, "claim_id")
    _required_string(claim, "kind")
    _required_string(claim, "statement")
    status = claim.get("status")
    if status not in {"accepted", "superseded"}:
        raise WoonError("claim status is invalid")
    if status == "superseded":
        _required_string(claim, "superseded_by")
    elif "superseded_by" in claim:
        raise WoonError("only superseded claim may declare superseded_by")
    _string_list(claim.get("source_ids"), "claim source_ids")
    if not isinstance(claim.get("markdown"), str):
        raise WoonError("claim markdown must be a string")
    if _contains_mermaid_color_directive(claim["markdown"]):
        raise WoonError(
            "claim Mermaid must use renderer-owned neutral colors; "
            "encode meaning with labels, position, and line style"
        )


def _curated_body(value: str, *, allow_empty: bool = False) -> str:
    """Keep the compiler-owned frontmatter and H1 outside generated prose."""

    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WoonError("curated revision body must be a non-empty string")
    if not value.strip():
        return ""
    body = value.rstrip() + "\n"
    if body.startswith("---\n"):
        raise WoonError("curated revision body must not include frontmatter")
    if re.match(r"\s*#\s+", body):
        raise WoonError("curated revision body must not include an H1")
    if re.search(r"[❶❷❸❹❺❻❼❽❾❿]", body):
        raise WoonError(
            "verified book textual callouts must use ① through ⑩; "
            "negative dingbat callouts are too low-contrast"
        )
    return body


def _verified_book_toc_only(frontmatter: object) -> bool:
    if not isinstance(frontmatter, dict):
        return False
    marker = frontmatter.get("book_toc_only")
    if marker is not None and marker is not True:
        raise WoonError("verified book book_toc_only marker must be true when present")
    return marker is True or frontmatter.get("content_state") == "toc-only"


def _normalize_verified_book_toc_only(frontmatter: dict[str, Any]) -> bool:
    is_toc_only = _verified_book_toc_only(frontmatter)
    frontmatter.pop("book_toc_only", None)
    if is_toc_only:
        frontmatter["content_state"] = "toc-only"
    return is_toc_only


def _validate_toc_only_navigation(page_id: str, navigation_groups: object) -> None:
    if navigation_groups is None:
        return
    if not isinstance(navigation_groups, list):
        raise WoonError("toc-only verified book navigation_groups must be an array")
    for group in navigation_groups:
        children = group.get("children") if isinstance(group, dict) else None
        if not isinstance(children, list):
            raise WoonError("toc-only verified book navigation group children must be an array")
        for child in children:
            child_id = _relation_target(child) if isinstance(child, str) else None
            if child_id == page_id:
                raise WoonError(f"toc-only verified book page must not link to itself: {page_id}")


def _retirement_body(value: str) -> str:
    """Remove derived Wiki views and normalize the reviewed retirement body."""

    authored = strip_generated_wiki_views(value)
    authored = re.sub(
        r"(?ms)^##\s+(?:이전과 다음|이전·다음)\s*$.*?(?=^##\s|\Z)",
        "",
        authored,
    )
    return _normalize(authored)


def _navigation_only_body(value: str) -> bool:
    """Return whether a wrapper body contains no reader prose or examples."""

    without_comments = re.sub(r"<!--[\s\S]*?-->", "", value)
    allowed_heading = re.compile(
        r"^#{2,6}\s+(?:목차|책 목차|책 전체 선형 이동|절 목차|하위 키워드|"
        r"최신 하위 문서|이전과 다음)\s*$"
    )
    allowed_link = re.compile(r"^\s*[-*]\s+(?:(?:이전|다음):\s+)?\[\[[^\]]+\]\]\s*$")
    for line in without_comments.splitlines():
        if not line.strip():
            continue
        if allowed_heading.fullmatch(line) or allowed_link.fullmatch(line):
            continue
        return False
    return True


def _supersede_replaced_conversation_revision(
    page: dict[str, Any],
    pages: dict[str, dict[str, Any]],
    page_id: str,
    sources: dict[str, dict[str, Any]],
    claims: dict[str, dict[str, Any]],
    canonical_id: str,
    successor_source_id: str,
    successor_claim_id: str,
) -> None:
    """Preserve the previous generated conversation revision as inactive evidence."""

    source_prefix = f"source://conversation/{canonical_id}/"
    claim_prefix = f"claim://conversation/{canonical_id}/"
    other_source_ids = {
        source_id
        for other_page_id, other_page in pages.items()
        if other_page_id != page_id
        for source_id in _string_list(other_page.get("source_ids"), "page source_ids")
    }
    other_claim_ids = {
        claim_id
        for other_page_id, other_page in pages.items()
        if other_page_id != page_id
        for claim_id in _string_list(other_page.get("claim_ids"), "page claim_ids")
    }
    for source_id in _string_list(page.get("source_ids"), "page source_ids"):
        if (
            source_id == successor_source_id
            or source_id in other_source_ids
            or not source_id.startswith(source_prefix)
        ):
            continue
        source = sources.get(source_id)
        if source is None:
            continue
        source["lifecycle"] = "archived"
        source["superseded_by"] = successor_source_id
    for claim_id in _string_list(page.get("claim_ids"), "page claim_ids"):
        if (
            claim_id == successor_claim_id
            or claim_id in other_claim_ids
            or not claim_id.startswith(claim_prefix)
        ):
            continue
        claim = claims.get(claim_id)
        if claim is None:
            continue
        claim["status"] = "superseded"
        claim["superseded_by"] = successor_claim_id


def _inactive_revision_error(
    record_id: str,
    record: dict[str, Any],
    records: dict[str, dict[str, Any]],
    referenced: set[str],
    *,
    state_key: str,
    inactive_state: str,
    record_label: str,
) -> str | None:
    """Ensure inactive history reaches a current record without a cycle."""

    if record.get(state_key) != inactive_state:
        return f"{record_label} has no page spec"
    successor = record.get("superseded_by")
    if not isinstance(successor, str) or not successor:
        return f"inactive {record_label} has no superseded_by"
    seen = {record_id}
    current = successor
    while current not in referenced:
        if current in seen:
            return f"{record_label} supersession chain contains a cycle"
        seen.add(current)
        successor_record = records.get(current)
        if successor_record is None:
            return f"{record_label} superseded_by does not exist"
        if successor_record.get(state_key) != inactive_state:
            return f"{record_label} supersession does not reach a current page"
        next_successor = successor_record.get("superseded_by")
        if not isinstance(next_successor, str) or not next_successor:
            return f"inactive {record_label} has no superseded_by"
        current = next_successor
    return None


def _render_page(
    page: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    curation: dict[str, Any],
    input_sha256: str,
) -> str:
    render = page["render"]
    if render["kind"] == "source-body":
        source_id = render["source_id"]
        body = next(record["body"] for record in sources if record["source_id"] == source_id)
    elif render["kind"] == "claims":
        body = "\n\n".join(
            record["markdown"].strip() for record in claims if record["markdown"].strip()
        )
    else:
        body = ""
    frontmatter = dict(page["frontmatter"])
    navigation_groups = frontmatter.get("navigation_groups")
    is_navigation_map = isinstance(navigation_groups, list) and bool(navigation_groups)
    if not body.strip() and not is_navigation_map and render["kind"] != "toc-only":
        raise WoonError("compiled page body must not be empty")
    body = _normalize_compiled_display_body(body)
    # This is a present operating purpose, not a reconstructed source intent.
    frontmatter["purpose"] = curation["current_use"]
    frontmatter["purpose_basis"] = curation["basis"]
    frontmatter[COMPILED_KEY] = {
        "schema_version": SCHEMA_VERSION,
        "build_id": input_sha256[:24],
        "page_id": page["page_id"],
    }
    provisional_yaml = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    provisional = f"---\n{provisional_yaml}---\n\n# {page['title']}\n\n{body.rstrip()}\n"
    frontmatter.update(compiled_wiki_contract(Path(page["output_path"]), provisional))
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{yaml_text}---\n\n# {page['title']}\n\n{body.rstrip()}\n"


def _normalize_compiled_display_body(body: str) -> str:
    """Remove retired generated views without rewriting source knowledge.

    Mermaid labels are evidence-bearing content. Truncated historical labels
    must be repaired in their canonical source record, never guessed or
    deleted while compiling a view.
    """

    body = re.sub(
        r"\n?<!-- recent-docs:start -->.*?<!-- recent-docs:end -->\n?",
        "\n",
        body,
        flags=re.DOTALL,
    )
    body = re.sub(
        r"\n?<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->\n?",
        "\n",
        body,
        flags=re.DOTALL,
    )
    return rewrite_retired_map_links(body)


def _input_hash(
    page: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    curation: dict[str, Any],
) -> str:
    payload = {
        "version": SCHEMA_VERSION,
        "page": page,
        "sources": sources,
        "claims": claims,
        # Review state is operational metadata.  Confirming it must not make
        # every prose-quality review stale when the rendered Markdown is unchanged.
        "curation": {
            "page_id": curation["page_id"],
            "current_use": curation["current_use"],
            "basis": curation["basis"],
        },
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return _sha256_text(serialized)


def _canonical_frontmatter(metadata: DocumentMetadata) -> dict[str, Any]:
    return {
        "type": "Wiki",
        "canonical_id": metadata.canonical_id,
        "title": metadata.title,
        "domain": metadata.domain,
        "summary": metadata.summary,
        "purpose": metadata.purpose,
        "status": "Canonical",
        "publish": False,
        "access": "local-only",
        "difficulty": metadata.difficulty,
        "prerequisites": list(metadata.prerequisites),
        "next_concepts": list(metadata.next_concepts),
        "related": list(metadata.related),
    }


def _canonical_parent_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    match = re.fullmatch(r"\[\[(?P<target>[^\]|#]+)(?:\|[^\]]+)?\]\]", value.strip())
    if match is None:
        raise WoonError("verified book parent must be one canonical wikilink")
    target = match.group("target")
    if target.startswith("wiki/"):
        target = target[5:]
    return target.removesuffix(".md")


def _nearest_surviving_ancestor(
    page_id: str,
    pages: dict[str, dict[str, Any]],
    retiring: set[str],
) -> str:
    current = page_id
    seen = {current}
    while current in pages:
        frontmatter = pages[current].get("frontmatter", {})
        parent = _canonical_parent_id(
            frontmatter.get("parent") if isinstance(frontmatter, dict) else None
        )
        if not parent or parent in seen:
            return ""
        if parent not in retiring:
            return parent
        seen.add(parent)
        current = parent
    return ""


def _verified_book_root_id(
    records: tuple[VerifiedBookPage, ...],
    manifest_book_id: str,
) -> str:
    """Resolve the single book subtree that must be refreshed transactionally."""

    roots = {
        record.page_id
        for record in records
        if record.frontmatter.get("entity_kind") == "book"
        or record.frontmatter.get("content_kind") == "book"
    }
    if manifest_book_id:
        if roots and roots != {manifest_book_id}:
            raise WoonError("verified book coverage book_id does not match promoted book root")
        return manifest_book_id
    if len(roots) != 1:
        raise WoonError(
            "verified book update requires exactly one promoted book root or coverage book_id"
        )
    return next(iter(roots))


def _first_actionable_coverage_error(errors: set[str]) -> str:
    """Prefer a concrete contract violation over a derived incomplete marker."""

    ordered = sorted(errors)
    return next(
        (error for error in ordered if "audit is incomplete because" not in error),
        ordered[0],
    )


def _navigation_group_children(value: object) -> set[str]:
    """Return exact canonical child ids from one page's navigation groups."""

    if not isinstance(value, dict):
        return set()
    groups = value.get("navigation_groups")
    if not isinstance(groups, list):
        return set()
    result: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        children = group.get("children")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, str) or not child.strip():
                continue
            text = child.strip()
            match = re.fullmatch(r"\[\[(?P<target>[^\]|#]+)(?:\|[^\]]+)?\]\]", text)
            target = match.group("target") if match is not None else text
            if target.startswith("wiki/"):
                target = target[5:]
            result.add(target.removesuffix(".md"))
    return result


def _initial_curation(
    page_id: str,
    title: str,
    frontmatter: dict[str, Any],
    source_records: list[dict[str, Any]],
) -> dict[str, str]:
    """Derive current-use provenance without fabricating legacy intent."""

    kinds = {_required_string(source, "kind") for source in source_records}
    explicit_purposes = [
        _required_string(source, "purpose")
        for source in source_records
        if _required_string(source, "kind") != "legacy-wiki"
    ]
    distinct_purposes = list(dict.fromkeys(explicit_purposes))
    existing = frontmatter.get("purpose")
    if len(distinct_purposes) == 1:
        current_use = distinct_purposes[0]
    elif isinstance(existing, str) and existing.strip():
        current_use = existing.strip()
    elif frontmatter.get("index_role") == "folder-index" or title.endswith("지도"):
        current_use = f"{title} 관련 문서를 찾고 학습 순서를 잡을 때, 탐색의 출발점으로 사용한다."
    else:
        current_use = (
            f"{title} 내용을 다시 학습하거나 설명할 때, 관련 개념과 자료를 찾는 기준으로 사용한다."
        )
    if kinds == {"legacy-wiki"}:
        basis = "legacy-page-metadata"
        status = "provisional"
    elif "legacy-wiki" in kinds or "curated-wiki" in kinds:
        basis = "manual-review"
        status = "confirmed"
    else:
        basis = "archive-request"
        status = "confirmed"
    return {
        "page_id": page_id,
        "current_use": current_use,
        "basis": basis,
        "status": status,
    }


def _relations_for(page_id: str, frontmatter: dict[str, Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for field, relation_type in (
        ("prerequisites", "requires"),
        ("next_concepts", "next"),
        ("related", "related"),
        ("related_to", "related"),
    ):
        raw = frontmatter.get(field)
        if not isinstance(raw, list):
            continue
        for target in raw:
            target_id = _relation_target(target) if isinstance(target, str) else None
            if target_id:
                relations.append(
                    {"from_page_id": page_id, "type": relation_type, "to_id": target_id}
                )
    return relations


def _current_source_id(page: dict[str, Any], source_records: list[dict[str, Any]]) -> str:
    render = page.get("render")
    if isinstance(render, dict) and render.get("kind") == "source-body":
        return _required_string(render, "source_id")
    if not source_records:
        raise WoonError("replacement page has no current source")
    return _required_string(source_records[-1], "source_id")


def _book_rights_ids(
    request: BookRightsDemotion,
) -> tuple[dict[str, str], dict[str, str]]:
    suffix = request.rights_evidence["notice_sha256"][:24]
    source_ids: dict[str, str] = {}
    claim_ids: dict[str, str] = {}
    for page_id in request.survivor_ids:
        page_suffix = _sha256_text(page_id)[:16]
        source_ids[page_id] = (
            f"source://book-rights/{request.book_id}/{suffix}/{page_suffix}"
        )
        claim_ids[page_id] = f"claim://book-rights/{request.book_id}/{suffix}/{page_suffix}"
    return source_ids, claim_ids


def _book_rights_records(
    request: BookRightsDemotion,
    page_id: str,
    source_id: str,
    claim_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = request.rights_evidence["notice_summary"]
    body = request.survivor_bodies[page_id]
    source = {
        "source_id": source_id,
        "kind": "book-rights-decision",
        "locator": request.rights_evidence["notice_locator"],
        "original_sha256": request.rights_evidence["source_archive_sha256"],
        "normalized_sha256": _sha256_text(_normalize(body)),
        "privacy": "local-only",
        "lifecycle": "compiled",
        "title": f"{page_id} 권리 제한 목차",
        "purpose": "권리 제한이 해소되기 전까지 책 페이지를 목차 전용으로 유지한다.",
        "body": body,
    }
    claim = {
        "claim_id": claim_id,
        "kind": "book-rights-decision",
        "status": "accepted",
        "statement": f"{page_id}: {summary}",
        "source_ids": [source_id],
        "markdown": "",
    }
    return source, claim


def _flatten_navigation_groups(
    raw_groups: object,
    pages: dict[str, dict[str, Any]],
    retiring: set[str],
) -> list[dict[str, Any]]:
    """Expand retired wrappers and drop retired terminals from surviving maps."""

    if not isinstance(raw_groups, list):
        return []
    result: list[dict[str, Any]] = []
    for group in raw_groups:
        if not isinstance(group, dict) or not isinstance(group.get("children"), list):
            continue
        direct: list[str] = []
        for child in group["children"]:
            if not isinstance(child, str) or child not in retiring:
                if isinstance(child, str):
                    direct.append(child)
                continue
            retired = pages[child]
            retired_groups = retired.get("frontmatter", {}).get("navigation_groups", [])
            retired_children = [
                item
                for retired_group in retired_groups
                if isinstance(retired_group, dict)
                for item in retired_group.get("children", [])
                if isinstance(item, str) and item not in retiring
            ]
            if not retired_children:
                continue
            if direct:
                result.append({"label": str(group.get("label", "목차")), "children": direct})
                direct = []
            result.append(
                {
                    "label": str(retired.get("title", group.get("label", "목차"))),
                    "children": list(dict.fromkeys(retired_children)),
                }
            )
        if direct:
            result.append({"label": str(group.get("label", "목차")), "children": direct})
    return result


def _local_book_asset_refs(markdown: str) -> set[str]:
    refs: set[str] = set()
    for pattern in (MARKDOWN_ASSET_RE, OBSIDIAN_ASSET_RE):
        for match in pattern.finditer(markdown):
            value = match.group("path").strip().strip("<>")
            if value.startswith("assets/") and ".." not in Path(value).parts:
                refs.add(value)
    return refs


def _validate_rights_toc_body(body: str, page_id: str, retiring: set[str]) -> None:
    """Require rights-safe maps to retain only keyword headings and plain TOC bullets."""

    if "[[" in body or re.search(r"\[[^\]]+\]\([^)]+\)", body):
        raise WoonError(f"book rights TOC body must not contain links: {page_id}")
    if any(retired_id in body for retired_id in retiring):
        raise WoonError(f"book rights TOC body must not contain retired canonical IDs: {page_id}")
    group_open = False
    group_has_bullet = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"##\s+\S.*", stripped):
            if group_open and not group_has_bullet:
                raise WoonError(
                    f"book rights TOC keyword has no plain-text bullet: {page_id}"
                )
            group_open = True
            group_has_bullet = False
            continue
        if re.fullmatch(r"-\s+[^\[].*", stripped) and group_open:
            group_has_bullet = True
            continue
        raise WoonError(
            "book rights TOC body must contain only H2 keywords and plain-text bullets: "
            f"{page_id}"
        )
    if not group_open or not group_has_bullet:
        raise WoonError(f"book rights TOC body requires H2 keywords with bullets: {page_id}")


def _validate_rights_coverage_replacement(
    current: object,
    request: BookRightsDemotion,
) -> None:
    replacement = request.coverage["replacement"]
    if not isinstance(current, dict) or not isinstance(replacement, dict):
        raise WoonError("book rights coverage manifests must be objects")
    if current.get("schema_version") != 1 or replacement.get("schema_version") != 1:
        raise WoonError("book rights demotion preserves the pending schema-v1 TOC manifest")
    if current.get("book_id") != request.book_id or replacement.get("book_id") != request.book_id:
        raise WoonError("book rights demotion coverage book_id mismatch")
    expected = copy.deepcopy(current)
    retiring = set(request.retire_page_ids)
    survivors = set(request.survivor_ids)
    nodes = []
    zero_coverage = {
        name: {"covered": 0, "expected": 0}
        for name in ("cautions", "claims", "code", "examples", "figures")
    }
    wrapper_parents = {
        node.get("canonical_id"): node.get("parent_id")
        for node in current.get("nodes", [])
        if isinstance(node, dict) and node.get("canonical_id") in retiring
    }
    for node in current.get("nodes", []):
        if not isinstance(node, dict) or node.get("canonical_id") in retiring:
            continue
        item = copy.deepcopy(node)
        canonical_id = item.get("canonical_id")
        if item.get("parent_id") in wrapper_parents:
            item["parent_id"] = wrapper_parents[item["parent_id"]]
        if canonical_id in survivors:
            item["state"] = "toc-only"
            item["has_direct_content"] = False
            item["coverage"] = copy.deepcopy(zero_coverage)
            item["runnable"] = {"expected": 0, "verified": 0}
            item["korean_prose_reviewed"] = False
        nodes.append(item)
    expected["nodes"] = nodes
    expected["toc_node_count"] = len(nodes)
    expected["toc_leaf_count"] = sum(node.get("leaf") is True for node in nodes)
    expected["rights_status"] = "blocked-rights"
    expected["rights_evidence"] = request.rights_evidence
    if replacement != expected:
        raise WoonError("book rights coverage replacement is not the exact TOC-only demotion")


def _current_claim_id(page: dict[str, Any], claim_records: list[dict[str, Any]]) -> str:
    if not claim_records:
        raise WoonError("replacement page has no current claim")
    render = page.get("render")
    source_id = (
        _required_string(render, "source_id")
        if isinstance(render, dict) and render.get("kind") == "source-body"
        else None
    )
    if source_id is not None:
        for claim in reversed(claim_records):
            if source_id in _string_list(claim.get("source_ids"), "claim source_ids"):
                return _required_string(claim, "claim_id")
    return _required_string(claim_records[-1], "claim_id")


def _redirect_frontmatter_relations(
    frontmatter: dict[str, Any],
    replacements: dict[str, str],
    *,
    redirect_parent: bool = True,
) -> bool:
    """Redirect relation fields while preserving their existing link syntax."""

    changed = False
    for field in ("prerequisites", "next_concepts", "related", "related_to"):
        values = frontmatter.get(field)
        if not isinstance(values, list):
            continue
        redirected = [_redirect_relation_value(value, replacements) for value in values]
        deduplicated = list(dict.fromkeys(redirected))
        if deduplicated != values:
            frontmatter[field] = deduplicated
            changed = True
    parent = frontmatter.get("parent")
    if redirect_parent and isinstance(parent, str):
        redirected_parent = _redirect_relation_value(parent, replacements)
        if redirected_parent != parent:
            frontmatter["parent"] = redirected_parent
            changed = True
    return changed


def _remove_retired_frontmatter_relations(
    frontmatter: dict[str, Any], retiring: set[str]
) -> bool:
    """Remove relation-array references that cannot be redirected safely."""

    changed = False
    for field in ("prerequisites", "next_concepts", "related", "related_to"):
        values = frontmatter.get(field)
        if not isinstance(values, list):
            continue
        kept = [
            value
            for value in values
            if not isinstance(value, str) or _relation_target(value) not in retiring
        ]
        if kept != values:
            frontmatter[field] = kept
            changed = True
    return changed


def _frontmatter_relation_targets(frontmatter: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    parent = frontmatter.get("parent")
    if isinstance(parent, str) and (target := _relation_target(parent)) is not None:
        targets.add(target)
    for field in ("prerequisites", "next_concepts", "related", "related_to"):
        values = frontmatter.get(field)
        if not isinstance(values, list):
            continue
        targets.update(
            target
            for value in values
            if isinstance(value, str) and (target := _relation_target(value)) is not None
        )
    return targets


def _redirect_relation_value(value: Any, replacements: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    target = _relation_target(value)
    if target is None:
        return value
    replacement = replacements.get(target)
    if replacement is None and "/" not in target:
        matches = [new for old, new in replacements.items() if old.rsplit("/", 1)[-1] == target]
        if len(matches) == 1:
            replacement = matches[0]
    if replacement is None:
        return value

    if value.startswith("[[") and value.endswith("]]"):
        inner = value[2:-2]
        link, separator, label = inner.partition("|")
        anchor_separator = "#" if "#" in link else ""
        anchor = link.partition("#")[2] if anchor_separator else ""
        path = link.partition("#")[0]
        if path.startswith("wiki/"):
            new_path = f"wiki/{replacement}"
        elif "/" in path:
            new_path = replacement
        else:
            new_path = replacement.rsplit("/", 1)[-1]
        new_link = new_path + (f"#{anchor}" if anchor_separator else "")
        return f"[[{new_link}{separator}{label}]]"
    if value.startswith("wiki/"):
        return f"wiki/{replacement}"
    if "/" in value:
        return replacement
    return replacement.rsplit("/", 1)[-1]


def _relation_target(value: str) -> str | None:
    """Normalize a frontmatter relation while keeping the rendered link untouched."""

    target = value.strip()
    if target.startswith("[[") and target.endswith("]]"):
        target = target[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
    target = target.removesuffix(".md")
    if target.startswith("wiki/"):
        target = target.removeprefix("wiki/")
    return target or None


def _expected_relations(pages: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Derive the review graph from page specs instead of trusting a manual copy."""

    relations: list[dict[str, str]] = []
    for page_id in sorted(pages):
        frontmatter = pages[page_id].get("frontmatter")
        if isinstance(frontmatter, dict):
            relations.extend(_relations_for(page_id, frontmatter))
    return sorted(relations, key=lambda item: (item["from_page_id"], item["type"], item["to_id"]))


def _parse_markdown(text: str, relative: Path) -> tuple[dict[str, Any], str, str]:
    match = FRONTMATTER.fullmatch(text)
    if match is None:
        raise WoonError(f"{relative.as_posix()}: Wiki source is missing YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as error:
        raise WoonError(f"{relative.as_posix()}: invalid YAML frontmatter") from error
    if not isinstance(frontmatter, dict):
        raise WoonError(f"{relative.as_posix()}: frontmatter must be a mapping")
    h1 = H1.match(match.group("body"))
    if h1 is None:
        raise WoonError(f"{relative.as_posix()}: Wiki source is missing H1")
    title = str(frontmatter.get("title", "")).strip()
    if not title or title != h1.group("title").strip():
        raise WoonError(f"{relative.as_posix()}: frontmatter title must match H1")
    return frontmatter, title, match.group("body")[h1.end() :].rstrip() + "\n"


def _load_yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise WoonError(f"compiled Wiki {key} catalog is missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise WoonError(f"load compiled Wiki {key}: {error}") from error
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        raise WoonError(f"compiled Wiki {key} catalog has unsupported version")
    records = raw.get(key)
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise WoonError(f"compiled Wiki {key} catalog must contain a mapping list")
    return [dict(item) for item in records]


def _indexed(records: list[dict[str, Any]], key: str, name: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = _required_string(record, key)
        if identifier in indexed:
            raise WoonError(f"duplicate compiled Wiki {name} identifier: {identifier}")
        indexed[identifier] = record
    return indexed


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"compiled Wiki record requires non-empty {key}")
    return value.strip()


def _string_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise WoonError(f"{field} must be a non-empty string list")
    return [item.strip() for item in value]


def _required_digest(record: dict[str, Any], key: str) -> None:
    value = record.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise WoonError(f"compiled Wiki record requires SHA-256 {key}")


def _inside(root: Path, relative: str, field: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise WoonError(f"{field} must be a safe relative path")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise WoonError(f"{field} escapes the compiler output root") from error
    return resolved


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    data = yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).encode("utf-8")
    atomic_write(path, data)


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _render_pdf_page_png(
    source_pdf: Path,
    page_number: int,
    render_dpi: int,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bytes:
    """Render one PDF page, optionally cropping its deterministic PNG bytes."""

    rendered = _render_pdf_pages_png(source_pdf, {page_number}, render_dpi)[page_number]
    return rendered if crop_box is None else _crop_png(rendered, crop_box)


def _render_pdf_pages_png(
    source_pdf: Path,
    page_numbers: set[int],
    render_dpi: int,
) -> dict[int, bytes]:
    """Render requested pages in compact Poppler ranges and return their PNG bytes."""

    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise WoonError("staged book scan crop validation requires pdftoppm")
    requested = sorted(page_numbers)
    if not requested:
        return {}
    ranges: list[tuple[int, int]] = []
    start = previous = requested[0]
    for page_number in requested[1:]:
        if page_number - previous > 2:
            ranges.append((start, previous))
            start = page_number
        previous = page_number
    ranges.append((start, previous))

    with tempfile.TemporaryDirectory(prefix="woon-book-scan-crop-") as temporary:
        prefix = Path(temporary) / "page"
        for first, last in ranges:
            command = [
                renderer,
                "-png",
                "-r",
                str(render_dpi),
                "-f",
                str(first),
                "-l",
                str(last),
                str(source_pdf),
                str(prefix),
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=180,
                )
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                raise WoonError("staged book scan crop PDF render failed") from error
        outputs: dict[int, bytes] = {}
        for output in prefix.parent.glob(f"{prefix.name}-*.png"):
            suffix = output.stem.removeprefix(f"{prefix.name}-")
            if suffix.isdigit() and int(suffix) in page_numbers:
                outputs[int(suffix)] = output.read_bytes()
        missing = page_numbers.difference(outputs)
        if missing:
            raise WoonError(
                "staged book scan crop PDF render produced no PNG for page "
                f"{min(missing)}"
            )
        return outputs


def _crop_png(
    rendered_page: bytes,
    crop_box: tuple[int, int, int, int],
) -> bytes:
    """Crop a rendered page and serialize one deterministic RGB PNG."""

    try:
        with Image.open(BytesIO(rendered_page)) as image:
            if crop_box[2] > image.width or crop_box[3] > image.height:
                raise WoonError("staged book scan crop box exceeds rendered page")
            cropped = image.convert("RGB").crop(crop_box)
            output = BytesIO()
            cropped.save(output, format="PNG")
            return output.getvalue()
    except WoonError:
        raise
    except OSError as error:
        raise WoonError("staged book scan crop rendered page is invalid") from error


def _normalize(value: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").split("\n")
    ).strip()
    return normalized + "\n"


def _validate_book_workflow_progression(
    current: dict[str, Any] | None,
    replacement: dict[str, Any],
    label: str,
    *,
    allow_source_landed_expansion: bool = False,
) -> None:
    """Reject phase rollback and loss of immutable source/translation coverage."""

    replacement_schema = replacement.get("schema_version")
    if replacement_schema == LEGACY_BOOK_COVERAGE_SCHEMA_VERSION:
        if current is not None and current.get("schema_version") == BOOK_COVERAGE_SCHEMA_VERSION:
            raise WoonError(f"{label} cannot roll back from Book Contract v7 to legacy schema")
        return
    if replacement_schema != BOOK_COVERAGE_SCHEMA_VERSION:
        raise WoonError(
            f"{label} schema_version must be {LEGACY_BOOK_COVERAGE_SCHEMA_VERSION} "
            f"or {BOOK_COVERAGE_SCHEMA_VERSION}"
        )
    phase = replacement.get("workflow_phase")
    phase_rank = book_workflow_phase_index(phase)
    if phase_rank < 0:
        raise WoonError(
            f"{label} workflow_phase must be one of: {', '.join(BOOK_WORKFLOW_PHASES)}"
        )
    translation_required = replacement.get("translation_required")
    if not isinstance(translation_required, bool):
        raise WoonError(f"{label} translation_required must be true or false")

    if current is None or current.get("schema_version") != BOOK_COVERAGE_SCHEMA_VERSION:
        if phase != "source-landed":
            raise WoonError(f"{label} must begin at workflow_phase=source-landed")
        return

    current_phase = current.get("workflow_phase")
    current_rank = book_workflow_phase_index(current_phase)
    if current_rank < 0:
        raise WoonError(f"current {label} workflow_phase is invalid")
    if phase_rank < current_rank:
        raise WoonError(
            f"{label} workflow phase cannot roll back: current={current_phase} next={phase}"
        )
    if current.get("translation_required") is not translation_required:
        raise WoonError(f"{label} translation_required cannot change after source landing")

    current_evidence = current.get("phase_evidence")
    replacement_evidence = replacement.get("phase_evidence")
    if not isinstance(current_evidence, dict) or not isinstance(replacement_evidence, dict):
        raise WoonError(f"{label} phase_evidence must be preserved across updates")
    for reached_phase in BOOK_WORKFLOW_PHASES[: current_rank + 1]:
        if replacement_evidence.get(reached_phase) != current_evidence.get(reached_phase):
            raise WoonError(
                f"{label} phase evidence cannot change after {reached_phase} is verified"
            )

    if (
        allow_source_landed_expansion
        and current_phase == "source-landed"
        and phase == "source-landed"
    ):
        extensible_fields = {
            "nodes",
            "source_structure_elements",
            "source_structure_assignments",
            "source_elements",
            "source_asset_inventory",
            "source_element_assignments",
        }
        refreshable_evidence_fields = {
            "toc_evidence",
            "source_structure_inventory_evidence",
            "source_element_inventory_evidence",
            "source_asset_inventory_evidence",
        }
        count_fields = {"toc_node_count", "toc_leaf_count"}
        mutable_fields = (
            extensible_fields | refreshable_evidence_fields | count_fields
        )
        for field in set(current) | set(replacement):
            if field not in mutable_fields and replacement.get(field) != current.get(field):
                raise WoonError(
                    f"{label} immutable {field} cannot change during source landing"
                )
        for field, identity_field in (
            ("nodes", "canonical_id"),
            ("source_structure_elements", "structure_id"),
            ("source_structure_assignments", "structure_id"),
            ("source_elements", "element_id"),
            ("source_asset_inventory", "asset_id"),
            ("source_element_assignments", "element_id"),
        ):
            _validate_ordered_supersequence(
                current.get(field),
                replacement.get(field),
                field=field,
                identity_field=identity_field,
                label=label,
            )
        _validate_source_asset_inventory_evidence(
            replacement.get("source_asset_inventory"),
            replacement.get("source_asset_inventory_evidence"),
            label,
        )
        return

    for field in (
        "edition",
        "source_archive",
        "source_asset_inventory",
        "source_asset_inventory_evidence",
        "source_structure_elements",
        "source_elements",
    ):
        if replacement.get(field) != current.get(field):
            raise WoonError(f"{label} immutable {field} cannot change after source landing")
    if _source_owner_bindings(replacement) != _source_owner_bindings(current):
        raise WoonError(f"{label} source element leaf ownership cannot change")

    translated_rank = book_workflow_phase_index("translated")
    if (
        current_rank >= translated_rank
        and phase_rank >= translated_rank
        and replacement.get("source_element_assignments")
        != current.get("source_element_assignments")
    ):
        raise WoonError(
            f"{label} translated reader delivery cannot decrease or be regenerated"
        )


def _validate_ordered_supersequence(
    current: Any,
    replacement: Any,
    *,
    field: str,
    identity_field: str,
    label: str,
) -> None:
    """Allow insertion while preserving every existing identified object and its order."""

    if not isinstance(current, list) or not isinstance(replacement, list):
        raise WoonError(f"{label} {field} must remain an array during source landing")

    def indexed(items: list[Any], version: str) -> dict[str, tuple[int, bytes]]:
        result: dict[str, tuple[int, bytes]] = {}
        for index, item in enumerate(items):
            identity = item.get(identity_field) if isinstance(item, dict) else None
            if not isinstance(identity, str) or not identity:
                raise WoonError(
                    f"{label} {field}[{index}] must have a non-empty {identity_field}"
                )
            if identity in result:
                raise WoonError(
                    f"{label} {field} contains duplicate {identity_field}: {identity}"
                )
            try:
                canonical = json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (TypeError, ValueError) as error:
                raise WoonError(
                    f"{label} {version} {field}[{index}] must be JSON"
                ) from error
            result[identity] = (index, canonical)
        return result

    current_by_id = indexed(current, "current")
    replacement_by_id = indexed(replacement, "replacement")
    previous_index = -1
    for identity, (_, current_bytes) in current_by_id.items():
        replacement_item = replacement_by_id.get(identity)
        if replacement_item is None:
            raise WoonError(
                f"{label} {field} cannot delete existing {identity_field}: {identity}"
            )
        replacement_index, replacement_bytes = replacement_item
        if replacement_bytes != current_bytes:
            raise WoonError(
                f"{label} {field} cannot change existing {identity_field}: {identity}"
            )
        if replacement_index <= previous_index:
            raise WoonError(
                f"{label} {field} cannot reorder existing {identity_field}: {identity}"
            )
        previous_index = replacement_index


def _validate_source_asset_inventory_evidence(
    inventory: Any,
    evidence: Any,
    label: str,
) -> None:
    """Keep mutable source-landing asset evidence synchronized with its inventory."""

    if not isinstance(inventory, list) or not isinstance(evidence, dict):
        raise WoonError(
            f"{label} source_asset_inventory_evidence must describe the replacement inventory"
        )
    if evidence.get("expected_asset_count") != len(inventory):
        raise WoonError(
            f"{label} source_asset_inventory_evidence expected_asset_count is stale"
        )
    inventory_sha256 = hashlib.sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if evidence.get("inventory_sha256") != inventory_sha256:
        raise WoonError(
            f"{label} source_asset_inventory_evidence inventory_sha256 is stale"
        )


def _source_owner_bindings(manifest: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    assignments = manifest.get("source_element_assignments")
    if not isinstance(assignments, list):
        return ()
    return tuple(
        sorted(
            (
                str(item.get("element_id", "")),
                str(item.get("owner_id", "")),
            )
            for item in assignments
            if isinstance(item, dict)
        )
    )
