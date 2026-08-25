"""Deterministic source-schema compiler for private LLM Wiki pages.

The compiler keeps its editable inputs outside ``wiki/``.  A compiled page is
therefore recoverable from a source record, accepted claim records, and a page
specification instead of becoming an untracked AI rewrite.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.domain import DocumentMetadata
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


class CompiledWiki:
    """Compile and audit one private source-schema Wiki without model calls."""

    def __init__(self, settings: CompiledWikiSettings) -> None:
        self._settings = settings
        self._last_input_state: tuple[tuple[str, int, int], ...] | None = None

    @property
    def enabled(self) -> bool:
        return True

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
            is_prior_curated_claim = claim.get("kind") == "curated-document" and claim.get(
                "source_ids"
            ) == [prior_source_id]
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

    def snapshot_inputs(self) -> dict[Path, bytes | None]:
        """Capture small compiler catalogs before a transactional canonical mutation."""

        return {
            path: path.read_bytes() if path.is_file() else None
            for path in (*self._input_paths(), self._settings.review_queue_path)
        }

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
    if kind not in {"source-body", "claims"}:
        raise WoonError("page render.kind must be source-body or claims")
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


def _validate_source(source: dict[str, Any]) -> None:
    _required_string(source, "source_id")
    _required_string(source, "kind")
    _required_string(source, "locator")
    _required_digest(source, "original_sha256")
    _required_digest(source, "normalized_sha256")
    if source.get("privacy") not in {"local-only", "private", "public", "external-private"}:
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


def _curated_body(value: str) -> str:
    """Keep the compiler-owned frontmatter and H1 outside generated prose."""

    if not isinstance(value, str) or not value.strip():
        raise WoonError("curated revision body must be a non-empty string")
    body = value.rstrip() + "\n"
    if body.startswith("---\n"):
        raise WoonError("curated revision body must not include frontmatter")
    if re.match(r"\s*#\s+", body):
        raise WoonError("curated revision body must not include an H1")
    return body


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
    else:
        body = "\n\n".join(
            record["markdown"].strip() for record in claims if record["markdown"].strip()
        )
    if not body.strip():
        raise WoonError("compiled page body must not be empty")
    body = _normalize_compiled_display_body(body)
    frontmatter = dict(page["frontmatter"])
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


def _normalize(value: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").split("\n")
    ).strip()
    return normalized + "\n"
