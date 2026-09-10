"""Validate a complete, replay-safe Wiki restructure manifest.

The manifest is an instruction for a one-time physical migration; it is not a
second knowledge graph.  Keeping validation separate from mutation makes it
possible to reject an incomplete or stale migration before any canonical page,
catalog, or receipt is touched.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.compiled_wiki import (
    CompiledWikiPageRetirement,
    CompiledWikiTransaction,
    CompiledWikiWikilinkRewrite,
)
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.identity import validate_canonical_id
from woon_core.knowledge.service import ManualWikiWrite
from woon_core.knowledge.wiki_tree import (
    iter_wiki_pages,
    split_markdown,
    strip_generated_wiki_views,
)

_DISPOSITIONS = {"keep", "merge", "move", "retire", "review"}
_V2_ACTIONS = _DISPOSITIONS | {"create"}
_V3_ACTIONS = _DISPOSITIONS | {"update"}
_V3_PUBLICATION_STATES = {"private", "review", "publish"}
# ``content_class`` governs which tree/schema safeguards are relevant.  It
# intentionally does not say whether a page is good learning prose or whether
# its code has run; those are separately receipt-backed quality concerns.
_V3_CONTENT_CLASSES = {
    "map",
    "topic",
    "book-reader",
    "project",
    "person",
    "schedule",
    "career",
    "creative",
    "operations",
    "resource",
}
_V3_SCHEMA_IDS = {
    "navigation-map",
    "book-reader",
    "project-entity",
    "person-entity",
    "schedule-record",
    "career-record",
    "creative-project",
    "operations-record",
    "resource-index",
}
_V3_REQUIRED_SCHEMAS = {
    "map": frozenset({"navigation-map"}),
    "topic": frozenset(),
    "book-reader": frozenset({"book-reader"}),
    "project": frozenset({"project-entity"}),
    "person": frozenset({"person-entity"}),
    "schedule": frozenset({"schedule-record"}),
    "career": frozenset({"career-record"}),
    "creative": frozenset({"creative-project"}),
    "operations": frozenset({"operations-record"}),
    "resource": frozenset({"resource-index"}),
}
_V3_RECORD_STATUSES = {"ready", "review", "pending-successor"}
_V3_MANIFEST_FIELDS = {
    "kind",
    "version",
    "base_inventory",
    "target_contract",
    "map_id_policy",
    "records",
    "nodes",
    "link_rewrites",
}
_V3_RECORD_FIELDS = {
    "action",
    "current_path",
    "current_sha256",
    "canonical_id",
    "source_owner",
    "page_id",
    "page_spec_sha256",
    "publication_state",
    "status",
    "content_class",
    "schemas_required",
    "required_checks",
    "target_path",
    "target_parent",
    "target_parent_canonical_id",
    "target_sequence",
    "target_sha256",
    "manual_payload",
    "target_page_spec",
    "target_page_spec_sha256",
    "target_curation",
    "link_successor",
    "successor_canonical_id",
    "successor_page_id",
}
_V3_NODE_FIELDS = {
    "action",
    "node_key",
    "canonical_id",
    "page_id",
    "source_owner",
    "page_spec_sha256",
    "target_path",
    "target_parent",
    "target_parent_canonical_id",
    "target_sequence",
    "publication_state",
    "status",
    "content_class",
    "schemas_required",
    "required_checks",
    "page_spec",
    "curation",
}
_V3_REWRITE_FIELDS = {
    "current_target",
    "replacement_target",
    "expected_source_occurrences",
    "expected_claim_occurrences",
    "compiler_referrers",
    "manual_referrers",
}
_V3_COMPILER_REFERRER_FIELDS = {"current_path", "page_id", "current_sha256"}
_V3_MANUAL_REFERRER_FIELDS = {"current_path", "current_sha256", "expected_occurrences"}
_BODY_WIKILINK_RE = re.compile(r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


@dataclass(frozen=True, slots=True)
class WikiRestructurePreflight:
    """Read-only result for one complete Wiki restructure instruction."""

    document_count: int
    disposition_counts: dict[str, int]
    target_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WikiRestructureClassification:
    """A complete, non-mutating assignment to the approved target tree."""

    document_count: int
    disposition_counts: dict[str, int]
    scope_counts: dict[str, int]
    records: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class WikiRestructureApplyReport:
    """One compiler/manual restructure mutation completed atomically."""

    manifest_path: str
    created_pages: tuple[str, ...]
    moved_pages: tuple[str, ...]
    compiled: int
    unchanged: int
    manual_written: int = 0
    source_revisions: int = 0
    claim_revisions: int = 0
    pages_retired: int = 0


@dataclass(frozen=True, slots=True)
class _V3TransactionPlan:
    """Prepared exact bytes and compiler catalog mutation for a v3 apply."""

    transaction: CompiledWikiTransaction
    manual_writes: tuple[ManualWikiWrite, ...]
    created_pages: tuple[str, ...]
    moved_pages: tuple[str, ...]


def render_wiki_restructure_inventory(vault: Path) -> bytes:
    """Render the complete file-level migration inventory without assigning paths.

    The approved target tree first classifies legacy pages to one allowed
    branch.  That is not yet a relocation instruction: a page still needs an
    exact parent, output path, successor when merged, and link plan.  This
    local inventory preserves the current source/claim/page/receipt ownership
    and link surface required to make those decisions without re-reading a
    moving worktree.
    """

    root = vault.expanduser().resolve()
    compiler_pages, _compiler_curations = _compiler_catalog_records(root)
    compiler_by_path = {
        (Path("wiki") / str(page["output_path"])).as_posix(): (page_id, page)
        for page_id, page in compiler_pages.items()
    }
    receipt_ids = _compiler_receipt_ids(root)
    paths = iter_wiki_pages(root / "wiki")
    relative_paths = {path.relative_to(root).as_posix() for path in paths}
    outbound_by_path: dict[str, tuple[str, ...]] = {}
    inbound_by_path: dict[str, set[str]] = {relative: set() for relative in relative_paths}
    texts: dict[str, str] = {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        texts[relative] = text
        outbound = tuple(sorted(_wiki_link_targets(text)))
        outbound_by_path[relative] = outbound
        for target in outbound:
            if target in inbound_by_path:
                inbound_by_path[target].add(relative)

    records: list[dict[str, object]] = []
    for relative in sorted(relative_paths):
        path = root / relative
        metadata, _body = split_markdown(texts[relative])
        scope, disposition, rationale = _approved_scope_for_legacy_path(
            relative, canonical_id=str(metadata.get("canonical_id", ""))
        )
        compiler_record = compiler_by_path.get(relative)
        record: dict[str, object] = {
            "current_path": relative,
            "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "canonical_id": metadata.get("canonical_id"),
            "current_parent": metadata.get("parent"),
            # Older canonical pages expressed this boundary as the paired
            # ``publish``/``access`` fields.  The v3 manifest still needs one
            # explicit, pinned value, but it must not require a content-wide
            # frontmatter rewrite merely to describe an already-public or
            # already-private page.
            "publication_state": _effective_publication_state(metadata) or "unset",
            "source_owner": "compiler" if compiler_record is not None else "manual",
            "disposition": disposition,
            "target_scope": scope,
            "rationale": rationale,
            "inbound_wikilinks": sorted(inbound_by_path[relative]),
            "outbound_wikilinks": list(outbound_by_path[relative]),
            "required_checks": [
                "frontmatter",
                "links",
                "privacy",
                "structure",
                "deterministic-output",
            ],
        }
        if compiler_record is not None:
            page_id, page = compiler_record
            record.update(
                {
                    "page_id": page_id,
                    "page_spec_sha256": _canonical_record_sha256(page),
                    "source_ids": list(page.get("source_ids", [])),
                    "claim_ids": list(page.get("claim_ids", [])),
                    "receipt_id": page_id if page_id in receipt_ids else None,
                }
            )
        if disposition == "merge":
            record["status"] = "pending-successor"
        elif disposition == "keep":
            record["status"] = "classified"
        else:
            record["status"] = "pending-target"
        records.append(record)
    payload = {
        "kind": "wiki-restructure-inventory",
        "version": 1,
        "document_count": len(records),
        "records": records,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_inventory(vault: Path, output_path: Path) -> Path:
    """Write one private inventory snapshot and never overwrite a reviewer copy."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure inventory must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure inventory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_inventory(root), mode=0o600)
    return output


def render_wiki_restructure_template(vault: Path) -> bytes:
    """Render a complete local baseline without assigning final destinations.

    Every record starts as ``review``.  This is intentionally not an apply
    manifest: a reviewer must assign a destination or a merge successor before
    the preflight can describe the transaction as ready.
    """

    root = vault.expanduser().resolve()
    compiler_owned = _compiler_owned_paths(root)
    records: list[dict[str, str]] = []
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        canonical_id = metadata.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise WoonError(f"Wiki template requires canonical_id: {relative}")
        records.append(
            {
                "current_path": relative,
                "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "canonical_id": canonical_id,
                "source_owner": "compiler" if relative in compiler_owned else "manual",
                "disposition": "review",
            }
        )
    payload = {"version": 1, "records": records}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_template(vault: Path, output_path: Path) -> Path:
    """Create one local baseline manifest without overwriting prior review work."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure template must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure template already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_template(root), mode=0o600)
    return output


def render_wiki_restructure_classification(vault: Path) -> bytes:
    """Classify every active page before any path or catalog mutation.

    A scope names the sole branch approved by the user.  It is intentionally
    not a target path: compiler-owned pages must still move through their
    source/claim/page-spec transaction, never through a Markdown rename.
    """

    root = vault.expanduser().resolve()
    records: list[dict[str, str]] = []
    dispositions: dict[str, int] = {}
    scopes: dict[str, int] = {}
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        scope, disposition, rationale = _approved_scope_for_legacy_path(
            relative, canonical_id=str(metadata.get("canonical_id", ""))
        )
        records.append(
            {
                "current_path": relative,
                "target_scope": scope,
                "disposition": disposition,
                "rationale": rationale,
            }
        )
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
        scopes[scope] = scopes.get(scope, 0) + 1
    payload = {
        "version": 1,
        "document_count": len(records),
        "disposition_counts": dispositions,
        "scope_counts": dict(sorted(scopes.items())),
        "records": records,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_classification(vault: Path, output_path: Path) -> Path:
    """Write the complete local-only classification without touching Wiki pages."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure classification must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure classification already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_classification(root), mode=0o600)
    return output


def prepare_wiki_restructure_preflight(
    vault: Path, manifest_path: Path
) -> WikiRestructurePreflight:
    """Validate that a manifest accounts for every active human Wiki page.

    A later writer may consume this result, but this function deliberately does
    not rename files or rewrite metadata.  In particular, raw evidence below
    ``wiki/private/_sources`` is excluded because its movement is owned by the
    source resolver rather than the human Wiki tree.
    """

    root = vault.expanduser().resolve()
    manifest_file = manifest_path.expanduser().resolve()
    if not manifest_file.is_file():
        raise WoonError(f"Wiki restructure manifest is missing: {manifest_file}")
    try:
        payload = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise WoonError("Wiki restructure manifest must be a mapping")
    if payload.get("kind") == "wiki-restructure-inventory":
        raise WoonError(
            "Wiki restructure inventory is read-only; complete target_path, target_parent, "
            "and merge successor fields in a separate apply manifest"
        )
    if payload.get("version") == 3:
        return _prepare_wiki_restructure_v3_preflight(root, payload, manifest_file)
    if payload.get("version") == 2:
        return _prepare_wiki_restructure_v2_preflight(root, payload)
    if payload.get("version") != 1:
        raise WoonError("Wiki restructure manifest must use version: 1, version: 2, or version: 3")
    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("Wiki restructure manifest requires a records list")

    active = {path.relative_to(root).as_posix(): path for path in iter_wiki_pages(root / "wiki")}
    compiler_owned = _compiler_owned_paths(root)
    issues: list[str] = []
    seen: set[str] = set()
    targets: dict[str, str] = {}
    final_paths: set[str] = set()
    move_parents: list[tuple[str, str]] = []
    counts = {disposition: 0 for disposition in sorted(_DISPOSITIONS)}

    for index, record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        current: str | None = _relative_path(
            record.get("current_path"), label, "current_path", issues
        )
        disposition = record.get("disposition")
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            issues.append(f"{label}: unsupported disposition {disposition!r}")
            continue
        counts[disposition] += 1
        if current is None:
            continue
        if current in seen:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active Wiki page: {current}")
            continue
        expected_hash = record.get("current_sha256")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            issues.append(f"{label}: current_sha256 does not match: {current}")
        try:
            metadata, _ = split_markdown(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, WoonError) as error:
            issues.append(f"{label}: cannot read frontmatter for {current}: {error}")
            continue
        if record.get("canonical_id") != metadata.get("canonical_id"):
            issues.append(f"{label}: canonical_id does not match: {current}")
        expected_owner = "compiler" if current in compiler_owned else "manual"
        if record.get("source_owner") != expected_owner:
            issues.append(f"{label}: source_owner must be {expected_owner!r} for {current}")

        target = record.get("target_path")
        if disposition == "move":
            target_path = _relative_path(target, label, "target_path", issues)
            if target_path is None:
                continue
            if not target_path.startswith("wiki/"):
                issues.append(f"{label}: target_path must stay below wiki/: {target_path}")
                continue
            previous = targets.setdefault(target_path, current)
            if previous != current:
                issues.append(f"{label}: target_path collision {target_path} with {previous}")
            final_paths.add(target_path)
            target_parent = _relative_path(
                record.get("target_parent"), label, "target_parent", issues
            )
            if target_parent is not None:
                move_parents.append((label, target_parent))
        elif disposition == "keep":
            final_paths.add(current)
        elif target not in {None, ""}:
            issues.append(f"{label}: only move records may define target_path")
        if disposition in {"merge", "retire"}:
            successor = record.get("link_successor")
            if not isinstance(successor, str) or not successor.strip():
                issues.append(f"{label}: {disposition} requires link_successor")

    missing = sorted(set(active) - seen)
    extra = sorted(seen - set(active))
    if missing:
        issues.append(f"manifest omits {len(missing)} active Wiki pages")
    if extra:
        issues.append(f"manifest names {len(extra)} non-active Wiki pages")
    for label, target_parent in move_parents:
        if target_parent not in final_paths:
            issues.append(f"{label}: target_parent is not a final Wiki page: {target_parent}")
    return WikiRestructurePreflight(
        document_count=len(active),
        disposition_counts={key: value for key, value in counts.items() if value},
        target_count=len(targets),
        issues=tuple(issues),
    )


def apply_wiki_restructure(vault: Path, manifest_path: Path) -> WikiRestructureApplyReport:
    """Apply a reviewed v2 or v3 Wiki restructure transaction.

    The manifest is deliberately not a Markdown rename plan.  Existing pages
    retain their source and claim records while their page specification moves;
    source-free hubs are introduced as explicit ``toc-only`` compiler specs.
    The compiler owns both generated writes and removal of superseded output
    paths, and its snapshots restore every catalog and generated file if a
    compile, tree refresh, audit, or index update fails.
    """

    root = vault.expanduser().resolve()
    manifest = manifest_path.expanduser().resolve()
    payload = _load_wiki_restructure_manifest(manifest)
    if payload.get("version") == 3:
        return _apply_wiki_restructure_v3(root, manifest, payload)
    if payload.get("version") != 2:
        raise WoonError("Wiki restructure apply requires a version: 2 or version: 3 manifest")
    preflight = _prepare_wiki_restructure_v2_preflight(root, payload)
    if preflight.issues:
        raise WoonError(
            "Wiki restructure preflight failed; refusing to change compiler inputs: "
            + "; ".join(preflight.issues[:12])
        )
    if preflight.disposition_counts.get("review", 0):
        raise WoonError("Wiki restructure apply requires every record to leave review")
    unsupported = {
        action for action in ("merge", "retire") if preflight.disposition_counts.get(action, 0)
    }
    if unsupported:
        raise WoonError(
            "Wiki restructure apply currently supports only compiler create and move; found: "
            + ", ".join(sorted(unsupported))
        )

    transaction, created_pages, moved_pages = _v2_compiler_transaction(root, payload)
    if not (*created_pages, *moved_pages):
        return WikiRestructureApplyReport(
            manifest_path=manifest.as_posix(),
            created_pages=(),
            moved_pages=(),
            compiled=0,
            unchanged=0,
        )
    _settings, service = build_knowledge_service(root)
    report = service.apply_compiled_wiki_transaction(transaction)
    return WikiRestructureApplyReport(
        manifest_path=manifest.as_posix(),
        created_pages=created_pages,
        moved_pages=moved_pages,
        compiled=report.compiled,
        unchanged=report.unchanged,
    )


def _apply_wiki_restructure_v3(
    root: Path, manifest: Path, payload: dict[str, Any]
) -> WikiRestructureApplyReport:
    """Apply a full ownership-aware v3 tree relocation.

    Version 3 does not loosen version 2's compiler boundary.  It adds an
    exact-byte bridge for manual Markdown and delegates compiler input link
    relocation to the immutable-provenance transaction primitive.  A compiler
    predecessor can retire only when its exact survivor page upsert and
    declared link successor are present in this same transaction.  Manual
    predecessors use exact service-level deletion, never an inferred body.
    """

    preflight = _prepare_wiki_restructure_v3_preflight(root, payload, manifest)
    if preflight.issues:
        raise WoonError(
            "Wiki restructure v3 preflight failed; refusing to change the Vault: "
            + "; ".join(preflight.issues[:12])
        )
    pending = {"review"}.intersection(preflight.disposition_counts)
    if pending:
        raise WoonError(
            "Wiki restructure v3 apply requires every record to leave review; found: "
            + ", ".join(sorted(pending))
        )
    plan = _v3_transaction_plan(root, payload, manifest)
    if not (
        plan.created_pages
        or plan.moved_pages
        or plan.manual_writes
        or plan.transaction.pages_upsert
    ):
        return WikiRestructureApplyReport(
            manifest_path=manifest.as_posix(),
            created_pages=(),
            moved_pages=(),
            compiled=0,
            unchanged=0,
        )
    _settings, service = build_knowledge_service(root)
    report = service.apply_wiki_restructure_transaction(plan.transaction, plan.manual_writes)
    return WikiRestructureApplyReport(
        manifest_path=manifest.as_posix(),
        created_pages=plan.created_pages,
        moved_pages=plan.moved_pages,
        compiled=report.compiler.compiled,
        unchanged=report.compiler.unchanged,
        manual_written=report.manual_written,
        source_revisions=report.compiler.source_revisions,
        claim_revisions=report.compiler.claim_revisions,
        pages_retired=report.compiler.pages_retired,
    )


def _prepare_wiki_restructure_v3_preflight(
    root: Path, payload: dict[str, Any], manifest: Path
) -> WikiRestructurePreflight:
    """Fail closed unless a v3 manifest pins every structural input.

    A v3 manifest is intentionally a *tree* mutation.  Learning modality,
    runnable-code assertions, quality judgements, and deployment decisions
    are rejected here because they need their own evidence/approval receipts.
    """

    issues: list[str] = []
    if payload.get("kind") != "wiki-restructure-apply":
        issues.append("v3 manifest kind must be wiki-restructure-apply")
    _validate_v3_field_set(payload, _V3_MANIFEST_FIELDS, "v3 manifest", issues)
    _validate_v3_base_snapshot(root, payload.get("base_inventory"), issues)
    _validate_v3_target_contract(root, payload.get("target_contract"), issues)
    _validate_v3_policy(payload.get("map_id_policy"), issues)

    records = payload.get("records")
    nodes = payload.get("nodes", [])
    rewrites = payload.get("link_rewrites", [])
    if not isinstance(records, list):
        issues.append("v3 manifest records must be a list")
        records = []
    if not isinstance(nodes, list):
        issues.append("v3 manifest nodes must be a list")
        nodes = []
    if not isinstance(rewrites, list):
        issues.append("v3 manifest link_rewrites must be a list")
        rewrites = []

    active = {path.relative_to(root).as_posix(): path for path in iter_wiki_pages(root / "wiki")}
    compiler_pages, _compiler_curations = _compiler_catalog_records(root)
    compiler_by_path = {
        (Path("wiki") / str(page["output_path"])).as_posix(): (page_id, page)
        for page_id, page in compiler_pages.items()
    }
    metadata_by_path: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    for current, path in active.items():
        try:
            text = path.read_text(encoding="utf-8")
            metadata, _body = split_markdown(text)
        except (OSError, UnicodeError, WoonError) as error:
            issues.append(f"cannot read active Wiki page {current}: {error}")
            continue
        texts[current] = text
        metadata_by_path[current] = metadata

    seen: set[str] = set()
    counts = {action: 0 for action in sorted(_V3_ACTIONS)}
    final_paths: dict[str, tuple[str, str]] = {}
    final_canonical_ids: dict[str, str] = {}
    canonical_final_paths: dict[str, tuple[str, str]] = {}
    final_compiler_pages: dict[str, tuple[str, str, str]] = {}
    parent_requests: list[tuple[str, str, str]] = []
    merge_successors: list[tuple[str, str, dict[str, Any], str]] = []
    record_by_current: dict[str, dict[str, Any]] = {}
    manual_payloads: dict[str, bytes] = {}

    def declare_final(path: str, canonical_id: str, label: str) -> None:
        key = _normalized_path_key(path)
        previous = final_paths.setdefault(key, (path, label))
        if previous != (path, label):
            issues.append(f"{label}: final target collision {path} with {previous[1]}")
            return
        final_canonical_ids[path] = canonical_id
        previous_canonical = canonical_final_paths.setdefault(canonical_id, (path, label))
        if previous_canonical != (path, label):
            issues.append(
                f"{label}: final canonical_id collision {canonical_id} with {previous_canonical[1]}"
            )

    def declare_final_compiler_page(page_id: str, path: str, canonical_id: str, label: str) -> None:
        previous = final_compiler_pages.setdefault(page_id, (path, canonical_id, label))
        if previous != (path, canonical_id, label):
            issues.append(f"{label}: final compiler page_id collision {page_id} with {previous[2]}")

    for index, raw_record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(raw_record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        record = raw_record
        _validate_v3_field_set(record, _V3_RECORD_FIELDS, label, issues)
        _reject_v3_non_tree_fields(record, label, issues)
        action = record.get("action")
        if not isinstance(action, str) or action not in _V3_ACTIONS:
            issues.append(f"{label}: unsupported action {action!r}")
            continue
        counts[action] += 1
        _validate_v3_action_fields(record, label, action, issues)
        _validate_v3_content_classification(record, label, issues)
        _validate_v3_common_record_fields(record, label, issues)
        current_path = _relative_path(record.get("current_path"), label, "current_path", issues)
        if current_path is None:
            continue
        if current_path in seen:
            issues.append(f"{label}: duplicate current_path {current_path}")
            continue
        seen.add(current_path)
        record_by_current[current_path] = record
        source = active.get(current_path)
        if source is None:
            issues.append(f"{label}: current_path is not an active Wiki page: {current_path}")
            continue
        if source.is_symlink() or not source.is_file():
            issues.append(f"{label}: current_path is not a regular Wiki file: {current_path}")
        if record.get("current_sha256") != _sha256_file(source):
            issues.append(f"{label}: current_sha256 does not match: {current_path}")
        metadata = metadata_by_path.get(current_path, {})
        canonical_id = _required_manifest_string(record, "canonical_id", label, issues)
        if canonical_id is not None and canonical_id != metadata.get("canonical_id"):
            issues.append(f"{label}: canonical_id does not match: {current_path}")
        owner = record.get("source_owner")
        actual_compiler = compiler_by_path.get(current_path)
        expected_owner = "compiler" if actual_compiler is not None else "manual"
        if owner != expected_owner:
            issues.append(f"{label}: source_owner must be {expected_owner!r} for {current_path}")
        _validate_v3_publication_state(record, metadata, action, label, issues)

        if expected_owner == "compiler":
            if actual_compiler is None:  # pragma: no cover - narrowed above
                continue
            actual_page_id, page = actual_compiler
            if record.get("page_id") != actual_page_id:
                issues.append(f"{label}: page_id does not match compiler ownership: {current_path}")
            if record.get("page_spec_sha256") != _canonical_record_sha256(page):
                issues.append(f"{label}: page_spec_sha256 does not match: {current_path}")
        elif record.get("page_id") is not None:
            issues.append(f"{label}: manual page_id must be null")

        if action in {"move", "update"}:
            target = _v3_target_path(record, label, issues)
            _validate_v3_target_sequence(record, label, issues)
            if target is not None and canonical_id is not None:
                declare_final(target, canonical_id, label)
                _collect_v3_parent_request(record, label, target, parent_requests, issues)
                if expected_owner == "compiler" and actual_compiler is not None:
                    declare_final_compiler_page(actual_compiler[0], target, canonical_id, label)
            if action == "update" and target is not None and target != current_path:
                issues.append(f"{label}: update target_path must equal current_path")
            if expected_owner == "manual":
                payload_bytes = _validate_v3_manual_payload(
                    record, label, manifest, metadata, target, issues
                )
                if payload_bytes is not None:
                    manual_payloads[current_path] = payload_bytes
            elif action == "update":
                _validate_v3_compiler_update_record(
                    record, label, current_path, actual_compiler, issues
                )
        elif action == "keep":
            if canonical_id is not None:
                declare_final(current_path, canonical_id, label)
        elif action in {"merge", "retire"}:
            successor = _v3_successor_path(record, label, issues)
            successor_id = _required_manifest_string(
                record, "successor_canonical_id", label, issues
            )
            if successor_id is not None:
                _validate_v3_canonical_id(successor_id, label, issues)
            if successor is not None and successor_id is not None:
                parent_requests.append((label, successor, successor_id))
            if expected_owner == "compiler":
                successor_page_id = _required_manifest_string(
                    record, "successor_page_id", label, issues
                )
                replacement = (
                    _validate_v3_compiler_merge_record(record, label, compiler_pages, issues)
                    if action == "merge"
                    else None
                )
                if (
                    replacement is not None
                    and successor is not None
                    and successor_id is not None
                    and successor_page_id is not None
                ):
                    declare_final(successor, successor_id, label)
                    declare_final_compiler_page(successor_page_id, successor, successor_id, label)
                    frontmatter = replacement.get("frontmatter")
                    if isinstance(frontmatter, dict):
                        merge_successors.append((successor, successor_id, frontmatter, label))
            elif record.get("successor_page_id") not in {None, ""}:
                issues.append(f"{label}: manual merge/retire must not define successor_page_id")
        elif action == "review":
            if any(record.get(key) not in {None, ""} for key in ("target_path", "manual_payload")):
                issues.append(f"{label}: review must not define a target or manual payload")

    for index, raw_node in enumerate(nodes, start=1):
        label = f"nodes[{index}]"
        if not isinstance(raw_node, dict):
            issues.append(f"{label}: node must be a mapping")
            continue
        _validate_v3_field_set(raw_node, _V3_NODE_FIELDS, label, issues)
        _reject_v3_non_tree_fields(raw_node, label, issues)
        canonical_id = _required_manifest_string(raw_node, "canonical_id", label, issues)
        node_key = _required_manifest_string(raw_node, "node_key", label, issues)
        page_id = _required_manifest_string(raw_node, "page_id", label, issues)
        target = _v3_target_path(raw_node, label, issues)
        _validate_v3_content_classification(raw_node, label, issues)
        _validate_v3_required_checks(raw_node, label, issues)
        if raw_node.get("status") != "ready":
            issues.append(f"{label}: new Map status must be ready")
        _validate_v3_publication_value(raw_node.get("publication_state"), label, issues)
        _validate_v3_target_sequence(raw_node, label, issues)
        if canonical_id is not None:
            _validate_v3_canonical_id(canonical_id, label, issues)
        if canonical_id is not None and node_key is not None:
            if node_key == "README" or not _is_nfc(node_key):
                issues.append(f"{label}: node_key must be an NFC non-root target label hierarchy")
            if page_id is not None and page_id != f"{canonical_id}/README":
                issues.append(f"{label}: page_id must be canonical_id plus /README")
            expected = f"wiki/{node_key}/README.md"
            if target is not None and target != expected:
                issues.append(f"{label}: target_path must follow the Map path convention")
        _validate_v3_create_node(raw_node, label, canonical_id, page_id, target, issues)
        if target is not None and canonical_id is not None:
            declare_final(target, canonical_id, label)
            _collect_v3_parent_request(raw_node, label, target, parent_requests, issues)
            if page_id is not None:
                declare_final_compiler_page(page_id, target, canonical_id, label)

    missing = sorted(set(active).difference(seen))
    if missing:
        issues.append(f"manifest omits {len(missing)} active Wiki pages")
    extra = sorted(seen.difference(active))
    if extra:
        issues.append(f"manifest names {len(extra)} non-active Wiki pages")
    for label, parent, parent_id in parent_requests:
        actual_id = final_canonical_ids.get(parent)
        if actual_id is None:
            issues.append(f"{label}: referenced final Wiki page is missing: {parent}")
        elif actual_id != parent_id:
            issues.append(f"{label}: referenced canonical_id does not match: {parent}")

    for current, record in record_by_current.items():
        if record.get("source_owner") != "compiler" or record.get("action") not in {
            "merge",
            "retire",
        }:
            continue
        label = f"record {current}"
        successor_id = record.get("successor_page_id")
        successor_path = _v3_successor_path(record, label, issues)
        successor_canonical_id = record.get("successor_canonical_id")
        if not isinstance(successor_id, str):
            continue
        final = final_compiler_pages.get(successor_id)
        if final is None:
            issues.append(
                f"{label}: compiler retirement successor must be a final exact move/update "
                "or new merge upsert"
            )
            continue
        if (
            successor_path is not None
            and successor_canonical_id is not None
            and final[:2] != (successor_path, successor_canonical_id)
        ):
            issues.append(
                f"{label}: compiler retirement successor does not match final exact upsert"
            )

    _validate_v3_final_tree_edges(
        records=record_by_current,
        nodes=nodes,
        metadata_by_path=metadata_by_path,
        final_paths=set(final_canonical_ids),
        merge_successors=tuple(merge_successors),
        issues=issues,
    )

    retired_compiler_page_ids = {
        compiler_by_path[current][0]
        for current, record in record_by_current.items()
        if record.get("source_owner") == "compiler"
        and record.get("action") in {"merge", "retire"}
        and current in compiler_by_path
    }
    _validate_v3_link_rewrites(
        root,
        rewrites,
        record_by_current,
        manual_payloads,
        texts,
        compiler_by_path,
        retired_compiler_page_ids,
        issues,
    )
    if rewrites:
        _validate_v3_live_compiler_record_states(
            root,
            compiler_pages,
            retired_compiler_page_ids,
            issues,
        )
    return WikiRestructurePreflight(
        document_count=len(active),
        disposition_counts={key: value for key, value in counts.items() if value},
        target_count=len(final_paths),
        issues=tuple(issues),
    )


def _validate_v3_base_snapshot(root: Path, value: object, issues: list[str]) -> None:
    if not isinstance(value, dict):
        issues.append("v3 manifest base_inventory must be a mapping")
        return
    _validate_v3_field_set(
        value,
        {"path", "sha256", "document_count", "active_tree_sha256"},
        "base_inventory",
        issues,
    )
    path = _v3_regular_root_file(root, value.get("path"), "base_inventory.path", issues)
    expected = value.get("sha256")
    if not _is_sha256(expected):
        issues.append("base_inventory.sha256 must be a SHA-256 digest")
    elif path is not None and _sha256_file(path) != expected:
        issues.append("base_inventory.sha256 does not match")
    if value.get("document_count") != len(tuple(iter_wiki_pages(root / "wiki"))):
        issues.append("base_inventory.document_count does not match active Wiki pages")
    if value.get("active_tree_sha256") != _v3_active_tree_sha256(root):
        issues.append("base_inventory.active_tree_sha256 does not match")


def _validate_v3_target_contract(root: Path, value: object, issues: list[str]) -> None:
    if not isinstance(value, dict):
        issues.append("v3 manifest target_contract must be a mapping")
        return
    _validate_v3_field_set(value, {"path", "sha256"}, "target_contract", issues)
    if value.get("path") != "docs/wiki-information-architecture.md":
        issues.append("target_contract.path must pin docs/wiki-information-architecture.md")
    path = _v3_regular_root_file(root, value.get("path"), "target_contract.path", issues)
    expected = value.get("sha256")
    if not _is_sha256(expected):
        issues.append("target_contract.sha256 must be a SHA-256 digest")
    elif path is not None and _sha256_file(path) != expected:
        issues.append("target_contract.sha256 does not match")


def _validate_v3_policy(value: object, issues: list[str]) -> None:
    expected = {
        "normalization": "NFC",
        "map_key": "<NFC target label hierarchy>",
        "map_path": "wiki/<node_key>/README.md",
        "map_page_id": "<canonical_id>/README",
    }
    if value != expected:
        issues.append("v3 manifest map_id_policy must declare the approved NFC Map convention")


def _validate_v3_field_set(
    value: dict[str, Any], allowed: set[str], label: str, issues: list[str]
) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        issues.append(f"{label}: unsupported fields: " + ", ".join(unknown))


def _v3_regular_root_file(root: Path, value: object, field: str, issues: list[str]) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{field} must be a non-empty Vault-relative file")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{field} escapes the Vault: {value!r}")
        return None
    path = (root / candidate).resolve()
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        issues.append(f"{field} must name a regular file below the Vault")
        return None
    return path


def _v3_active_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_v3_common_record_fields(
    record: dict[str, Any], label: str, issues: list[str]
) -> None:
    for field in ("current_sha256", "canonical_id", "source_owner", "publication_state", "status"):
        if field not in record:
            issues.append(f"{label}: {field} is required")
    if not _is_sha256(record.get("current_sha256")):
        issues.append(f"{label}: current_sha256 must be a SHA-256 digest")
    _validate_v3_canonical_id(record.get("canonical_id"), label, issues)
    status = record.get("status")
    if status not in _V3_RECORD_STATUSES:
        issues.append(f"{label}: status must be ready, review, or pending-successor")
    else:
        action = record.get("action")
        expected_status = (
            "review"
            if action == "review"
            else "pending-successor"
            if action in {"merge", "retire"}
            else "ready"
        )
        if status != expected_status:
            issues.append(f"{label}: status is inconsistent with action {action!r}")
    _validate_v3_required_checks(record, label, issues)


def _validate_v3_action_fields(
    record: dict[str, Any], label: str, action: str, issues: list[str]
) -> None:
    target_fields = {
        "target_path",
        "target_parent",
        "target_parent_canonical_id",
        "target_sequence",
    }
    manual_fields = {"manual_payload", "target_sha256"}
    compiler_update_fields = {
        "target_page_spec",
        "target_page_spec_sha256",
        "target_curation",
    }
    successor_fields = {"link_successor", "successor_canonical_id", "successor_page_id"}
    present = {
        field for field in record if record.get(field) is not None and record.get(field) != ""
    }
    if action in {"keep", "review"}:
        forbidden = present.intersection(
            target_fields | manual_fields | compiler_update_fields | successor_fields
        )
    elif action in {"merge", "retire"}:
        forbidden = present.intersection(target_fields)
        if record.get("source_owner") == "manual" or action == "retire":
            forbidden |= present.intersection(manual_fields | compiler_update_fields)
        else:
            forbidden |= present.intersection(manual_fields)
    else:
        forbidden = present.intersection(successor_fields)
        if record.get("source_owner") == "compiler" and action == "move":
            forbidden |= present.intersection(manual_fields | compiler_update_fields)
        elif record.get("source_owner") == "manual":
            forbidden |= present.intersection(compiler_update_fields)
    if forbidden:
        issues.append(
            f"{label}: fields are not allowed for action {action!r}: "
            + ", ".join(sorted(forbidden))
        )


def _validate_v3_required_checks(record: dict[str, Any], label: str, issues: list[str]) -> None:
    checks = record.get("required_checks")
    expected = {"frontmatter", "links", "privacy", "structure", "deterministic-output"}
    if not isinstance(checks, list) or set(checks) != expected or len(checks) != len(expected):
        issues.append(f"{label}: required_checks must contain the exact structural gate set")


def _validate_v3_content_classification(
    record: dict[str, Any], label: str, issues: list[str]
) -> None:
    content_class = record.get("content_class")
    if content_class not in _V3_CONTENT_CLASSES:
        issues.append(f"{label}: content_class must be one of the approved structural classes")
    schemas = record.get("schemas_required")
    if not isinstance(schemas, list) or any(item not in _V3_SCHEMA_IDS for item in schemas):
        issues.append(f"{label}: schemas_required must be an array of approved schema IDs")
    elif len(schemas) != len(set(schemas)):
        issues.append(f"{label}: schemas_required must not repeat schema IDs")
    elif isinstance(content_class, str) and set(schemas) != _V3_REQUIRED_SCHEMAS[content_class]:
        issues.append(f"{label}: schemas_required must match content_class structural schema")


def _validate_v3_canonical_id(value: object, label: str, issues: list[str]) -> None:
    if not isinstance(value, str) or not _is_nfc(value):
        issues.append(f"{label}: canonical_id must be an NFC stable canonical path")
        return
    try:
        validate_canonical_id(value)
    except WoonError as error:
        issues.append(f"{label}: {error}")


def _reject_v3_non_tree_fields(record: dict[str, Any], label: str, issues: list[str]) -> None:
    forbidden = {
        "learning_mode",
        "code_contract",
        "execution_contract",
        "deployment",
        "deploy",
        "quality_status",
    }.intersection(record)
    if forbidden:
        issues.append(
            f"{label}: quality, execution, and deployment fields belong to separate receipts: "
            + ", ".join(sorted(forbidden))
        )


def _validate_v3_publication_value(value: object, label: str, issues: list[str]) -> None:
    if value not in _V3_PUBLICATION_STATES:
        issues.append(f"{label}: publication_state must be private, review, or publish")


def _effective_publication_state(metadata: dict[str, Any]) -> str | None:
    """Return the existing publication boundary without silently widening it.

    ``publication_state`` is the current v3 representation.  Existing Wiki
    pages predate it and use only the equivalent public projection pair:
    ``publish: true`` + ``access: public`` or the local pair ``false`` +
    ``local-only``.  Any mixed or unknown legacy pair remains invalid for a
    structural transaction instead of being guessed into a broader state.
    """

    explicit = metadata.get("publication_state")
    if explicit is not None:
        return explicit if explicit in _V3_PUBLICATION_STATES else None
    if metadata.get("publish") is True and metadata.get("access") == "public":
        return "publish"
    if metadata.get("publish") is False and metadata.get("access") == "local-only":
        return "private"
    return None


def _validate_v3_publication_state(
    record: dict[str, Any], metadata: dict[str, Any], action: str, label: str, issues: list[str]
) -> None:
    state = record.get("publication_state")
    _validate_v3_publication_value(state, label, issues)
    current_state = _effective_publication_state(metadata)
    if current_state is None:
        issues.append(
            f"{label}: current publication boundary is not a valid publication_state "
            "or publish/access pair"
        )
        return
    if action in {"keep", "review", "merge", "retire"} and current_state != state:
        issues.append(f"{label}: non-mutating action must pin the current publication_state")


def _v3_target_path(record: dict[str, Any], label: str, issues: list[str]) -> str | None:
    target = _relative_path(record.get("target_path"), label, "target_path", issues)
    if target is None:
        return None
    if not target.startswith("wiki/"):
        issues.append(f"{label}: target_path must stay below wiki/: {target}")
    elif not _is_nfc(target):
        issues.append(f"{label}: target_path must use NFC normalization")
    return target


def _validate_v3_target_sequence(record: dict[str, Any], label: str, issues: list[str]) -> None:
    sequence = record.get("target_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, (int, float)) or sequence <= 0:
        issues.append(f"{label}: target_sequence must be a positive number")


def _collect_v3_parent_request(
    record: dict[str, Any],
    label: str,
    target: str,
    requests: list[tuple[str, str, str]],
    issues: list[str],
) -> None:
    if target == "wiki/README.md":
        if record.get("target_parent") not in {None, ""} or record.get(
            "target_parent_canonical_id"
        ) not in {None, ""}:
            issues.append(f"{label}: root README must not define target_parent")
        return
    parent = _relative_path(record.get("target_parent"), label, "target_parent", issues)
    parent_id = _required_manifest_string(record, "target_parent_canonical_id", label, issues)
    if parent is not None and parent_id is not None:
        if target == parent:
            issues.append(f"{label}: target_parent must not be the page itself")
        requests.append((label, parent, parent_id))


def _v3_successor_path(record: dict[str, Any], label: str, issues: list[str]) -> str | None:
    atom = _v3_wikilink_atom(record.get("link_successor"), label, "link_successor", issues)
    return f"{atom}.md" if atom is not None else None


def _validate_v3_manual_payload(
    record: dict[str, Any],
    label: str,
    manifest: Path,
    current_metadata: dict[str, Any],
    target: str | None,
    issues: list[str],
) -> bytes | None:
    value = record.get("manual_payload")
    if not isinstance(value, dict):
        issues.append(f"{label}: manual move/update requires manual_payload")
        return None
    _validate_v3_field_set(value, {"path", "sha256"}, f"{label}.manual_payload", issues)
    raw_path = value.get("path")
    expected = value.get("sha256")
    if not isinstance(raw_path, str) or not raw_path.strip() or not _is_sha256(expected):
        issues.append(f"{label}: manual_payload requires path and SHA-256")
        return None
    candidate = Path(raw_path)
    root = manifest.parent.resolve()
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: manual_payload.path escapes the manifest directory")
        return None
    payload_path = (root / candidate).resolve()
    if (
        not payload_path.is_relative_to(root)
        or payload_path.is_symlink()
        or not payload_path.is_file()
    ):
        issues.append(f"{label}: manual_payload.path must be a regular manifest-relative file")
        return None
    content = payload_path.read_bytes()
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected or record.get("target_sha256") != expected:
        issues.append(f"{label}: manual payload SHA-256 does not match target_sha256")
        return None
    try:
        metadata, _body = split_markdown(content.decode("utf-8"))
    except (UnicodeError, WoonError) as error:
        issues.append(f"{label}: manual payload is not valid Markdown frontmatter: {error}")
        return None
    if metadata.get("canonical_id") != record.get("canonical_id"):
        issues.append(f"{label}: manual payload canonical_id does not match")
    if metadata.get("publication_state") != record.get("publication_state"):
        issues.append(f"{label}: manual payload publication_state does not match")
    if target is not None and target != "wiki/README.md":
        parent = record.get("target_parent")
        if (
            isinstance(parent, str)
            and _normalized_wikilink_target(metadata.get("parent")) != parent
        ):
            issues.append(f"{label}: manual payload parent does not match target_parent")
        if metadata.get("sequence") != record.get("target_sequence"):
            issues.append(f"{label}: manual payload sequence does not match target_sequence")
    elif current_metadata.get("canonical_id") != metadata.get("canonical_id"):
        issues.append(f"{label}: manual payload changed stable identity")
    return content


def _validate_v3_compiler_update_record(
    record: dict[str, Any],
    label: str,
    current: str,
    compiler_record: tuple[str, dict[str, Any]] | None,
    issues: list[str],
) -> None:
    if compiler_record is None:  # pragma: no cover - narrowed by caller
        return
    page_id, _page = compiler_record
    replacement = record.get("target_page_spec")
    curation = record.get("target_curation")
    if not isinstance(replacement, dict) or not isinstance(curation, dict):
        issues.append(f"{label}: compiler update requires target_page_spec and target_curation")
        return
    if record.get("target_page_spec_sha256") != _canonical_record_sha256(replacement):
        issues.append(f"{label}: target_page_spec_sha256 does not match target_page_spec")
    if replacement.get("page_id") != page_id:
        issues.append(f"{label}: target_page_spec.page_id must preserve compiler page_id")
    if replacement.get("output_path") != current.removeprefix("wiki/"):
        issues.append(f"{label}: compiler update must preserve output_path")
    if curation.get("page_id") != page_id:
        issues.append(f"{label}: target_curation.page_id must preserve compiler page_id")


def _validate_v3_compiler_merge_record(
    record: dict[str, Any],
    label: str,
    compiler_pages: dict[str, dict[str, Any]],
    issues: list[str],
) -> dict[str, Any] | None:
    """Validate an explicit new survivor without inventing its page boundary.

    A merge record may either nominate an existing *separately upserted*
    survivor, or carry one complete new survivor spec/curation pair.  Existing
    pages are deliberately not silently copied: the reviewed manifest must
    include their ordinary move/update exact upsert as well.
    """

    successor_id = _required_manifest_string(record, "successor_page_id", label, issues)
    replacement = record.get("target_page_spec")
    curation = record.get("target_curation")
    digest = record.get("target_page_spec_sha256")
    present = [value is not None and value != "" for value in (replacement, curation, digest)]
    if any(present) and not all(present):
        issues.append(
            f"{label}: compiler merge survivor requires target_page_spec, "
            "target_page_spec_sha256, and target_curation together"
        )
        return None
    if not any(present):
        return None
    if not isinstance(replacement, dict) or not isinstance(curation, dict):
        issues.append(f"{label}: compiler merge survivor must be page spec and curation mappings")
        return None
    if digest != _canonical_record_sha256(replacement):
        issues.append(f"{label}: target_page_spec_sha256 does not match target_page_spec")
    if successor_id is not None and replacement.get("page_id") != successor_id:
        issues.append(f"{label}: target_page_spec.page_id must match successor_page_id")
    if successor_id is not None and curation.get("page_id") != successor_id:
        issues.append(f"{label}: target_curation.page_id must match successor_page_id")
    if successor_id is not None and successor_id in compiler_pages:
        issues.append(
            f"{label}: an existing compiler survivor must be supplied by its own move/update record"
        )
    successor = _v3_successor_path(record, label, issues)
    if successor is not None and replacement.get("output_path") != successor.removeprefix("wiki/"):
        issues.append(f"{label}: target_page_spec.output_path must match link_successor")
    frontmatter = replacement.get("frontmatter")
    if not isinstance(frontmatter, dict):
        issues.append(f"{label}: target_page_spec requires frontmatter")
    else:
        if frontmatter.get("canonical_id") != record.get("successor_canonical_id"):
            issues.append(f"{label}: survivor frontmatter canonical_id must match successor")
        if frontmatter.get("publication_state") not in _V3_PUBLICATION_STATES:
            issues.append(f"{label}: survivor frontmatter publication_state is invalid")
    return replacement


def _validate_v3_create_node(
    node: dict[str, Any],
    label: str,
    canonical_id: str | None,
    page_id: str | None,
    target: str | None,
    issues: list[str],
) -> None:
    if node.get("action") != "create" or node.get("source_owner") != "compiler":
        issues.append(f"{label}: new Map node must be a compiler create")
    if node.get("content_class") != "map" or node.get("schemas_required") != ["navigation-map"]:
        issues.append(f"{label}: new Map must use map content_class and navigation-map schema")
    page = node.get("page_spec")
    curation = node.get("curation")
    if not isinstance(page, dict) or not isinstance(curation, dict):
        issues.append(f"{label}: new Map node requires page_spec and curation")
        return
    if node.get("page_spec_sha256") != _canonical_record_sha256(page):
        issues.append(f"{label}: page_spec_sha256 does not match page_spec")
    if page_id is not None and page.get("page_id") != page_id:
        issues.append(f"{label}: page_spec.page_id does not match page_id")
    if target is not None and page.get("output_path") != target.removeprefix("wiki/"):
        issues.append(f"{label}: page_spec.output_path does not match target_path")
    if (
        page_id is not None
        and page.get("output_path") != f"{page_id}.md"
        and page.get("output_path_migration") is not True
    ):
        issues.append(
            f"{label}: display-label Map output differs from page_id; "
            "output_path_migration is required"
        )
    frontmatter = page.get("frontmatter")
    if not isinstance(frontmatter, dict) or frontmatter.get("canonical_id") != canonical_id:
        issues.append(f"{label}: page_spec frontmatter canonical_id does not match")
    elif frontmatter.get("publication_state") != node.get("publication_state"):
        issues.append(f"{label}: page_spec publication_state does not match")
    elif frontmatter.get("node_kind") != "hub":
        issues.append(f"{label}: new Map page_spec frontmatter node_kind must be hub")
    elif (
        isinstance(node.get("node_key"), str)
        and frontmatter.get("title") != node["node_key"].rsplit("/", 1)[-1]
    ):
        issues.append(f"{label}: Map title must match the final node_key label")
    elif target != "wiki/README.md":
        parent = node.get("target_parent")
        if (
            isinstance(parent, str)
            and _normalized_wikilink_target(frontmatter.get("parent")) != parent
        ):
            issues.append(f"{label}: page_spec parent does not match target_parent")
        if frontmatter.get("sequence") != node.get("target_sequence"):
            issues.append(f"{label}: page_spec sequence does not match target_sequence")
    if (
        page.get("render") != {"kind": "toc-only"}
        or page.get("source_ids") != []
        or page.get("claim_ids") != []
    ):
        issues.append(f"{label}: new Map must be source-free toc-only")
    if page_id is not None and curation.get("page_id") != page_id:
        issues.append(f"{label}: curation.page_id does not match page_id")


def _validate_v3_final_tree_edges(
    *,
    records: dict[str, dict[str, Any]],
    nodes: list[object],
    metadata_by_path: dict[str, dict[str, Any]],
    final_paths: set[str],
    merge_successors: tuple[tuple[str, str, dict[str, Any], str], ...],
    issues: list[str],
) -> None:
    """Require an explicit final parent for every live non-root page.

    The compiler will rebuild generated child views, but this preflight must
    first prove that a move did not leave a kept child pointing at an archived
    parent.  Each newly created Map must also have a real direct child: empty
    category placeholders are outside the approved target contract.
    """

    edges: list[tuple[str, str, str]] = []
    final_canonical_ids: dict[str, str] = {}
    for current, record in records.items():
        action = record.get("action")
        if action in {"merge", "retire", "review"}:
            continue
        target = current if action == "keep" else record.get("target_path")
        if not isinstance(target, str) or target == "wiki/README.md":
            if isinstance(target, str) and isinstance(record.get("canonical_id"), str):
                final_canonical_ids[target] = record["canonical_id"]
            continue
        if isinstance(record.get("canonical_id"), str):
            final_canonical_ids[target] = record["canonical_id"]
        parent = (
            record.get("target_parent")
            if action in {"move", "update"}
            else _normalized_wikilink_target(metadata_by_path.get(current, {}).get("parent"))
        )
        if not isinstance(parent, str):
            issues.append(f"{current}: final non-root page must have an exact parent")
            continue
        if parent not in final_paths:
            issues.append(f"{current}: final parent is not a final Wiki page: {parent}")
        edges.append((target, parent, current))
    for target, canonical_id, frontmatter, label in merge_successors:
        final_canonical_ids[target] = canonical_id
        if target == "wiki/README.md":
            continue
        parent = _normalized_wikilink_target(frontmatter.get("parent"))
        if parent is None:
            issues.append(f"{label}: merged survivor must define an exact final parent")
            continue
        if parent not in final_paths:
            issues.append(f"{label}: merged survivor parent is not a final Wiki page: {parent}")
        edges.append((target, parent, label))
    map_targets: set[str] = set()
    for index, raw_node in enumerate(nodes, start=1):
        if not isinstance(raw_node, dict):
            continue
        target = raw_node.get("target_path")
        if not isinstance(target, str):
            continue
        map_targets.add(target)
        node_canonical_id = raw_node.get("canonical_id")
        if isinstance(node_canonical_id, str):
            final_canonical_ids[target] = node_canonical_id
        parent = raw_node.get("target_parent")
        if target == "wiki/README.md":
            continue
        if not isinstance(parent, str):
            issues.append(f"nodes[{index}]: final Map must have an exact parent")
            continue
        if parent not in final_paths:
            issues.append(f"nodes[{index}]: final parent is not a final Wiki page: {parent}")
        edges.append((target, parent, f"nodes[{index}]"))
    for map_target in sorted(map_targets):
        if not any(parent == map_target for _child, parent, _label in edges):
            issues.append(f"new Map must have at least one direct child: {map_target}")
    direct_ids: dict[str, set[str]] = {}
    for child, parent, _label in edges:
        child_canonical_id = final_canonical_ids.get(child)
        if child_canonical_id is not None:
            direct_ids.setdefault(parent, set()).add(child_canonical_id)
    for index, raw_node in enumerate(nodes, start=1):
        if not isinstance(raw_node, dict):
            continue
        target = raw_node.get("target_path")
        page = raw_node.get("page_spec")
        node_frontmatter = page.get("frontmatter") if isinstance(page, dict) else None
        groups = (
            node_frontmatter.get("navigation_groups")
            if isinstance(node_frontmatter, dict)
            else None
        )
        if not isinstance(target, str) or not isinstance(groups, list):
            continue
        listed_values = [
            child_id
            for group in groups
            if isinstance(group, dict) and isinstance(group.get("children"), list)
            for child_id in group["children"]
            if isinstance(child_id, str)
        ]
        listed = set(listed_values)
        if listed != direct_ids.get(target, set()) or len(listed) != len(listed_values):
            issues.append(
                f"nodes[{index}]: navigation_groups must list each direct child exactly once"
            )


def _validate_v3_link_rewrites(
    root: Path,
    rewrites: list[object],
    records: dict[str, dict[str, Any]],
    manual_payloads: dict[str, bytes],
    texts: dict[str, str],
    compiler_by_path: dict[str, tuple[str, dict[str, Any]]],
    retired_compiler_page_ids: set[str],
    issues: list[str],
) -> None:
    """Validate every explicit successor atom without title-based inference."""

    expected_targets: dict[str, str] = {}
    for current, record in records.items():
        action = record.get("action")
        if action in {"move", "update"} and record.get("target_path") != current:
            target = record.get("target_path")
            if isinstance(target, str):
                expected_targets[_path_to_wikilink_atom(current)] = _path_to_wikilink_atom(target)
        elif action in {"merge", "retire"}:
            successor = record.get("link_successor")
            if isinstance(successor, str):
                expected_targets[_path_to_wikilink_atom(current)] = _path_to_wikilink_atom(
                    successor
                )

    expected_referrers: dict[tuple[str, str], dict[str, set[str]]] = {}
    for path, text in texts.items():
        owner = "compiler" if path in compiler_by_path else "manual"
        if owner == "compiler" and compiler_by_path[path][0] in retired_compiler_page_ids:
            continue
        if owner == "manual" and records.get(path, {}).get("action") in {"merge", "retire"}:
            continue
        for atom in _authored_wikilink_atoms(text):
            replacement = expected_targets.get(atom)
            if replacement is None:
                continue
            bucket = expected_referrers.setdefault(
                (atom, replacement), {"compiler": set(), "manual": set()}
            )
            bucket[owner].add(path)

    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(rewrites, start=1):
        label = f"link_rewrites[{index}]"
        if not isinstance(raw, dict):
            issues.append(f"{label}: rewrite must be a mapping")
            continue
        _validate_v3_field_set(raw, _V3_REWRITE_FIELDS, label, issues)
        _reject_v3_non_tree_fields(raw, label, issues)
        current_atom = _v3_wikilink_atom(raw.get("current_target"), label, "current_target", issues)
        replacement_atom = _v3_wikilink_atom(
            raw.get("replacement_target"), label, "replacement_target", issues
        )
        if current_atom is None or replacement_atom is None:
            continue
        key = (current_atom, replacement_atom)
        if key in seen:
            issues.append(f"{label}: duplicate exact link rewrite atom")
            continue
        seen.add(key)
        if expected_targets.get(current_atom) != replacement_atom:
            issues.append(f"{label}: replacement_target is not the declared final successor")
        expected_source = raw.get("expected_source_occurrences")
        expected_claim = raw.get("expected_claim_occurrences")
        if not _nonnegative_int(expected_source) or not _nonnegative_int(expected_claim):
            issues.append(f"{label}: compiler occurrence counts must be non-negative integers")
        else:
            actual_source, actual_claim = _v3_catalog_wikilink_counts(
                root, current_atom, retired_compiler_page_ids
            )
            if (expected_source, expected_claim) != (actual_source, actual_claim):
                issues.append(
                    f"{label}: compiler occurrence counts do not match active source/claims"
                )
        expected = expected_referrers.get(key, {"compiler": set(), "manual": set()})
        compiler_paths = _validate_v3_compiler_referrers(
            raw.get("compiler_referrers"), label, compiler_by_path, texts, current_atom, issues
        )
        manual_paths = _validate_v3_manual_referrers(
            raw.get("manual_referrers"),
            label,
            records,
            manual_payloads,
            texts,
            current_atom,
            replacement_atom,
            issues,
        )
        if compiler_paths != expected["compiler"]:
            issues.append(f"{label}: compiler_referrers are not the exact authored inbound set")
        if manual_paths != expected["manual"]:
            issues.append(f"{label}: manual_referrers are not the exact authored inbound set")

    missing = sorted(set(expected_referrers).difference(seen))
    if missing:
        issues.append(
            "link_rewrites omit "
            + ", ".join(f"{current} -> {replacement}" for current, replacement in missing[:3])
        )
    unexpected = sorted(seen.difference(expected_referrers))
    if unexpected:
        issues.append(
            "link_rewrites name an atom without authored inbound references: "
            + ", ".join(f"{current} -> {replacement}" for current, replacement in unexpected[:3])
        )


def _validate_v3_compiler_referrers(
    value: object,
    label: str,
    compiler_by_path: dict[str, tuple[str, dict[str, Any]]],
    texts: dict[str, str],
    atom: str,
    issues: list[str],
) -> set[str]:
    if not isinstance(value, list):
        issues.append(f"{label}: compiler_referrers must be a list")
        return set()
    result: set[str] = set()
    for index, raw in enumerate(value, start=1):
        location = f"{label}.compiler_referrers[{index}]"
        if not isinstance(raw, dict):
            issues.append(f"{location} must be a mapping")
            continue
        _validate_v3_field_set(raw, _V3_COMPILER_REFERRER_FIELDS, location, issues)
        path = _relative_path(raw.get("current_path"), location, "current_path", issues)
        if path is None:
            continue
        compiler = compiler_by_path.get(path)
        if compiler is None:
            issues.append(f"{location}: current_path is not compiler-owned")
            continue
        if raw.get("page_id") != compiler[0]:
            issues.append(f"{location}: page_id does not match compiler ownership")
        if raw.get("current_sha256") != _sha256_text(texts.get(path, "")):
            issues.append(f"{location}: current_sha256 does not match")
        if atom not in _authored_wikilink_atoms(texts.get(path, "")):
            issues.append(f"{location}: does not contain the exact current_target atom")
        result.add(path)
    return result


def _validate_v3_manual_referrers(
    value: object,
    label: str,
    records: dict[str, dict[str, Any]],
    manual_payloads: dict[str, bytes],
    texts: dict[str, str],
    atom: str,
    replacement: str,
    issues: list[str],
) -> set[str]:
    if not isinstance(value, list):
        issues.append(f"{label}: manual_referrers must be a list")
        return set()
    result: set[str] = set()
    for index, raw in enumerate(value, start=1):
        location = f"{label}.manual_referrers[{index}]"
        if not isinstance(raw, dict):
            issues.append(f"{location} must be a mapping")
            continue
        _validate_v3_field_set(raw, _V3_MANUAL_REFERRER_FIELDS, location, issues)
        path = _relative_path(raw.get("current_path"), location, "current_path", issues)
        if path is None:
            continue
        record = records.get(path)
        if record is None or record.get("source_owner") != "manual":
            issues.append(f"{location}: current_path is not manual-owned")
            continue
        if raw.get("current_sha256") != _sha256_text(texts.get(path, "")):
            issues.append(f"{location}: current_sha256 does not match")
        expected = raw.get("expected_occurrences")
        actual = _wikilink_atom_count(texts.get(path, ""), atom)
        if not _nonnegative_int(expected) or expected != actual or actual == 0:
            issues.append(f"{location}: expected_occurrences does not match the current exact atom")
        payload = manual_payloads.get(path)
        if payload is None:
            issues.append(f"{location}: manual referrer requires a hash-pinned move/update payload")
        elif _wikilink_atom_count(payload.decode("utf-8"), replacement) != actual:
            issues.append(f"{location}: manual payload does not replace the exact atom count")
        result.add(path)
    return result


def _v3_catalog_wikilink_counts(
    root: Path, atom: str, retired_page_ids: set[str]
) -> tuple[int, int]:
    sources, claims = _v3_source_claim_catalog(root)
    pages, _curations = _compiler_catalog_records(root)
    live_source_ids = {
        source_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for source_id in page.get("source_ids", [])
        if isinstance(source_id, str)
    }
    live_claim_ids = {
        claim_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for claim_id in page.get("claim_ids", [])
        if isinstance(claim_id, str)
    }
    source_count = sum(
        _wikilink_atom_count(str(record.get("body", "")), atom)
        for source_id, record in sources.items()
        if source_id in live_source_ids and record.get("lifecycle") == "compiled"
    )
    claim_count = sum(
        _wikilink_atom_count(str(record.get("statement", "")), atom)
        + _wikilink_atom_count(str(record.get("markdown", "")), atom)
        for claim_id, record in claims.items()
        if claim_id in live_claim_ids and record.get("status") == "accepted"
    )
    return source_count, claim_count


def _validate_v3_live_compiler_record_states(
    root: Path, pages: dict[str, dict[str, Any]], retired_page_ids: set[str], issues: list[str]
) -> None:
    """Mirror the compiler primitive's live-record lifecycle precondition."""

    try:
        sources, claims = _v3_source_claim_catalog(root)
    except WoonError as error:
        issues.append(str(error))
        return
    live_source_ids = {
        source_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for source_id in page.get("source_ids", [])
        if isinstance(source_id, str)
    }
    live_claim_ids = {
        claim_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for claim_id in page.get("claim_ids", [])
        if isinstance(claim_id, str)
    }
    invalid_sources = sorted(
        source_id
        for source_id in live_source_ids
        if source_id not in sources or sources[source_id].get("lifecycle") != "compiled"
    )
    invalid_claims = sorted(
        claim_id
        for claim_id in live_claim_ids
        if claim_id not in claims or claims[claim_id].get("status") != "accepted"
    )
    if invalid_sources:
        issues.append(
            "link rewrites require all live compiler sources to be lifecycle compiled: "
            + invalid_sources[0]
        )
    if invalid_claims:
        issues.append(
            "link rewrites require all live compiler claims to be status accepted: "
            + invalid_claims[0]
        )


def _v3_source_claim_catalog(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        source_payload = yaml.safe_load(
            (root / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
        )
        claim_payload = yaml.safe_load(
            (root / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki compiler source/claim catalog is unreadable: {error}") from error
    source_rows = source_payload.get("sources") if isinstance(source_payload, dict) else None
    claim_rows = claim_payload.get("claims") if isinstance(claim_payload, dict) else None
    if not isinstance(source_rows, list) or not isinstance(claim_rows, list):
        raise WoonError("Wiki compiler source/claim catalogs require lists")
    sources = _v3_index_catalog_records(source_rows, "source_id", "sources")
    claims = _v3_index_catalog_records(claim_rows, "claim_id", "claims")
    return sources, claims


def _v3_index_catalog_records(
    rows: list[object], key: str, label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get(key), str) or not raw[key].strip():
            raise WoonError(f"Wiki compiler {label}[{index}] has no {key}")
        identifier = raw[key]
        if identifier in result:
            raise WoonError(f"Wiki compiler {label} duplicate {key}: {identifier}")
        result[identifier] = raw
    return result


def _authored_wikilink_atoms(text: str) -> set[str]:
    try:
        _metadata, body = split_markdown(text)
    except WoonError:
        body = text
    result: set[str] = set()
    for match in _BODY_WIKILINK_RE.finditer(strip_generated_wiki_views(body)):
        atom = _normalize_wikilink_atom(match.group("target"))
        if atom is not None:
            result.add(atom)
    return result


def _wikilink_atom_count(text: str, expected: str) -> int:
    try:
        _metadata, body = split_markdown(text)
    except WoonError:
        body = text
    return sum(
        _normalize_wikilink_atom(match.group("target")) == expected
        for match in _BODY_WIKILINK_RE.finditer(strip_generated_wiki_views(body))
    )


def _v3_wikilink_atom(value: object, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str):
        issues.append(f"{label}: {field} must be an extensionless wiki/... wikilink atom")
        return None
    atom = _normalize_wikilink_atom(value)
    if atom is None or not value.strip().startswith("wiki/") or value.strip().endswith(".md"):
        issues.append(f"{label}: {field} must be an extensionless wiki/... wikilink atom")
        return None
    return atom


def _normalize_wikilink_atom(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value.strip().removesuffix(".md"))
    if not value.strip() or candidate.is_absolute() or ".." in candidate.parts:
        return None
    normalized = candidate.as_posix()
    if not normalized.startswith("wiki/") or not _is_nfc(normalized):
        return None
    return normalized


def _normalized_wikilink_target(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _BODY_WIKILINK_RE.fullmatch(value.strip())
    if match is None:
        return None
    atom = _normalize_wikilink_atom(match.group("target"))
    return f"{atom}.md" if atom is not None else None


def _path_to_wikilink_atom(path: str) -> str:
    return path.removesuffix(".md")


def _normalized_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _is_nfc(value: str) -> bool:
    return value == unicodedata.normalize("NFC", value)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _v3_transaction_plan(root: Path, payload: dict[str, Any], manifest: Path) -> _V3TransactionPlan:
    """Materialize preflight-pinned v3 records without inferring any target."""

    raw_records = payload.get("records")
    raw_nodes = payload.get("nodes", [])
    raw_rewrites = payload.get("link_rewrites", [])
    if (
        not isinstance(raw_records, list)
        or not isinstance(raw_nodes, list)
        or not isinstance(raw_rewrites, list)
    ):
        raise WoonError("v3 manifest shape changed after preflight")
    records = [record for record in raw_records if isinstance(record, dict)]
    nodes = [node for node in raw_nodes if isinstance(node, dict)]
    compiler_pages, compiler_curations = _compiler_catalog_records(root)
    compiler_by_path = {
        (Path("wiki") / str(page["output_path"])).as_posix(): (page_id, page)
        for page_id, page in compiler_pages.items()
    }
    active_titles = _active_wiki_titles(root)
    final_titles = dict(active_titles)
    for record in records:
        action = record.get("action")
        current = str(record.get("current_path", ""))
        if action in {"move", "update"}:
            target = str(record.get("target_path", ""))
            if record.get("source_owner") == "manual":
                payload_bytes = _v3_manual_payload_bytes(record, manifest)
                metadata, _body = split_markdown(payload_bytes.decode("utf-8"))
                final_titles.pop(current, None)
                final_titles[target] = str(metadata["title"])
            elif action == "move":
                final_titles.pop(current, None)
                final_titles[target] = active_titles[current]
            elif action == "update":
                replacement = record["target_page_spec"]
                if isinstance(replacement, dict):
                    final_titles[target] = str(replacement["title"])
        elif action in {"merge", "retire"}:
            final_titles.pop(current, None)
            replacement = record.get("target_page_spec")
            successor = _v3_successor_path(record, "v3 transaction", [])
            if action == "merge" and isinstance(replacement, dict) and successor is not None:
                final_titles[successor] = str(replacement["title"])
    for node in nodes:
        page = node.get("page_spec")
        node_target = node.get("target_path")
        if isinstance(page, dict) and isinstance(node_target, str):
            final_titles[node_target] = str(page["title"])

    pages_upsert: dict[str, dict[str, Any]] = {}
    curations_upsert: dict[str, dict[str, Any]] = {}
    expected_revisions: dict[str, str | None] = {}
    expected_page_specs: dict[str, str | None] = {}
    retired_outputs: list[str] = []
    created: list[str] = []
    moved: list[str] = []
    manual_writes: list[ManualWikiWrite] = []
    page_retirements: list[CompiledWikiPageRetirement] = []

    def upsert_existing(page_id: str, page: dict[str, Any], *, current_path: str) -> None:
        pages_upsert[page_id] = page
        curation = compiler_curations.get(page_id)
        if curation is None:  # pragma: no cover - compiler catalog preflight rejects this
            raise WoonError(f"compiler curation is missing: {page_id}")
        curations_upsert[page_id] = deepcopy(curation)
        expected_revisions[page_id] = _sha256_file(root / current_path)
        expected_page_specs[page_id] = _canonical_record_sha256(compiler_pages[page_id])

    def upsert_new(page_id: str, page: dict[str, Any], curation: dict[str, Any]) -> None:
        existing = pages_upsert.get(page_id)
        if existing is not None:
            if existing != page or curations_upsert.get(page_id) != curation:
                raise WoonError(f"v3 merge survivor changed after preflight: {page_id}")
            return
        pages_upsert[page_id] = page
        curations_upsert[page_id] = curation
        expected_revisions[page_id] = None
        expected_page_specs[page_id] = None
        created.append(page_id)

    for record in records:
        action = record.get("action")
        owner = record.get("source_owner")
        if action in {"merge", "retire"} and owner == "manual":
            manual_writes.append(
                ManualWikiWrite(
                    current_path=str(record["current_path"]),
                    current_sha256=str(record["current_sha256"]),
                    target_path=None,
                    target_sha256=None,
                    content=None,
                )
            )
            continue
        if action == "merge" and owner == "compiler":
            replacement = record.get("target_page_spec")
            curation = record.get("target_curation")
            successor_page_id = record.get("successor_page_id")
            if (
                isinstance(replacement, dict)
                and isinstance(curation, dict)
                and isinstance(successor_page_id, str)
            ):
                upsert_new(successor_page_id, deepcopy(replacement), deepcopy(curation))
            continue
        if action not in {"move", "update"}:
            continue
        current = str(record["current_path"])
        target = str(record["target_path"])
        if owner == "manual":
            body = _v3_manual_payload_bytes(record, manifest)
            manual_writes.append(
                ManualWikiWrite(
                    current_path=current,
                    current_sha256=str(record["current_sha256"]),
                    target_path=target,
                    target_sha256=str(record["target_sha256"]),
                    content=body,
                )
            )
            continue
        page_id, current_page = compiler_by_path[current]
        if action == "move":
            replacement = deepcopy(current_page)
            replacement["output_path"] = target.removeprefix("wiki/")
            replacement["output_path_migration"] = True
            frontmatter = _v3_page_frontmatter(replacement, page_id)
            _v3_apply_structural_frontmatter(record, frontmatter, final_titles)
            retired_outputs.append(str(current_page["output_path"]))
            moved.append(page_id)
        else:
            replacement = deepcopy(record["target_page_spec"])
            frontmatter = _v3_page_frontmatter(replacement, page_id)
            _v3_apply_structural_frontmatter(record, frontmatter, final_titles)
            curation = record.get("target_curation")
            if not isinstance(curation, dict):  # pragma: no cover - v3 preflight rejects this
                raise WoonError(f"compiler update curation is invalid: {page_id}")
        upsert_existing(page_id, replacement, current_path=current)
        if action == "update":
            curations_upsert[page_id] = deepcopy(record["target_curation"])

    for node in nodes:
        page = deepcopy(node["page_spec"])
        curation = deepcopy(node["curation"])
        page_id = str(node["page_id"])
        upsert_new(page_id, page, curation)

    retired_page_ids = {
        compiler_by_path[str(record["current_path"])][0]
        for record in records
        if record.get("source_owner") == "compiler"
        and record.get("action") in {"merge", "retire"}
        and str(record.get("current_path", "")) in compiler_by_path
    }
    typed_rewrites, source_hashes, claim_hashes, affected_page_ids = _v3_compiler_wikilink_plan(
        root, raw_rewrites, compiler_pages, retired_page_ids
    )
    for page_id in sorted(affected_page_ids):
        if page_id in pages_upsert:
            continue
        page = deepcopy(compiler_pages[page_id])
        current_path = (Path("wiki") / str(page["output_path"])).as_posix()
        upsert_existing(page_id, page, current_path=current_path)

    receipts = _compiler_receipt_records(root)
    for record in records:
        if record.get("source_owner") != "compiler" or record.get("action") not in {
            "merge",
            "retire",
        }:
            continue
        current = str(record["current_path"])
        page_id, _page = compiler_by_path[current]
        receipt = receipts.get(page_id)
        if receipt is None:  # pragma: no cover - compiler audit/preflight rejects this state
            raise WoonError(f"compiler retirement receipt is missing: {page_id}")
        page_retirements.append(
            CompiledWikiPageRetirement(
                page_id=page_id,
                successor_page_id=str(record["successor_page_id"]),
                current_wikilink_target=_path_to_wikilink_atom(current),
                expected_output_sha256=str(record["current_sha256"]),
                expected_page_spec_sha256=str(record["page_spec_sha256"]),
                expected_receipt_sha256=_canonical_record_sha256(receipt),
            )
        )

    transaction = CompiledWikiTransaction(
        expected_revisions=expected_revisions,
        sources_upsert=(),
        claims_upsert=(),
        pages_upsert=tuple(pages_upsert[page_id] for page_id in sorted(pages_upsert)),
        curations_upsert=tuple(curations_upsert[page_id] for page_id in sorted(curations_upsert)),
        expected_page_spec_sha256=expected_page_specs,
        retired_output_paths=tuple(sorted(set(retired_outputs))),
        refresh_wiki_tree=True,
        wikilink_rewrites=typed_rewrites,
        expected_source_record_sha256=source_hashes,
        expected_claim_record_sha256=claim_hashes,
        page_retirements=tuple(page_retirements),
    )
    return _V3TransactionPlan(
        transaction=transaction,
        manual_writes=tuple(manual_writes),
        created_pages=tuple(sorted(created)),
        moved_pages=tuple(sorted(moved)),
    )


def _v3_page_frontmatter(page: dict[str, Any], page_id: str) -> dict[str, Any]:
    frontmatter = page.get("frontmatter")
    if not isinstance(frontmatter, dict):  # pragma: no cover - v3 preflight rejects this
        raise WoonError(f"compiler page frontmatter is invalid: {page_id}")
    return frontmatter


def _v3_apply_structural_frontmatter(
    record: dict[str, Any], frontmatter: dict[str, Any], final_titles: dict[str, str]
) -> None:
    target = str(record["target_path"])
    if target == "wiki/README.md":
        frontmatter.pop("parent", None)
        frontmatter.pop("sequence", None)
    else:
        parent = str(record["target_parent"])
        title = final_titles.get(parent)
        if title is None:  # pragma: no cover - v3 preflight checks topology
            raise WoonError(f"target parent title is missing: {parent}")
        frontmatter["parent"] = _wiki_link(parent, title)
        frontmatter["sequence"] = record["target_sequence"]
    frontmatter["publication_state"] = record["publication_state"]
    if target.startswith("wiki/Wiki/") and frontmatter.get("public_slug") is not None:
        # Keep an explicitly approved stable URL; no tree migration may derive
        # a new public slug from a Korean label or a changed filename.
        frontmatter["public_slug"] = frontmatter["public_slug"]


def _v3_manual_payload_bytes(record: dict[str, Any], manifest: Path) -> bytes:
    payload = record.get("manual_payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("path"), str):
        raise WoonError("v3 manual payload changed after preflight")
    path = (manifest.parent.resolve() / Path(payload["path"])).resolve()
    return path.read_bytes()


def _v3_compiler_wikilink_plan(
    root: Path,
    rewrites: list[object],
    pages: dict[str, dict[str, Any]],
    retired_page_ids: set[str],
) -> tuple[
    tuple[CompiledWikiWikilinkRewrite, ...],
    dict[str, str],
    dict[str, str],
    frozenset[str],
]:
    if not rewrites:
        return (), {}, {}, frozenset()
    sources, claims = _v3_source_claim_catalog(root)
    typed: list[CompiledWikiWikilinkRewrite] = []
    replacement_atoms: set[str] = set()
    for raw in rewrites:
        if not isinstance(raw, dict):  # pragma: no cover - v3 preflight rejects this
            raise WoonError("v3 link rewrite changed after preflight")
        current = str(raw["current_target"])
        replacement = str(raw["replacement_target"])
        if replacement in replacement_atoms:  # defensive mirror of compiler primitive
            raise WoonError("v3 link rewrite replacement atom is duplicated")
        replacement_atoms.add(replacement)
        typed.append(
            CompiledWikiWikilinkRewrite(
                current_target=current,
                replacement_target=replacement,
                expected_source_occurrences=int(raw["expected_source_occurrences"]),
                expected_claim_occurrences=int(raw["expected_claim_occurrences"]),
            )
        )
    atoms = {rewrite.current_target for rewrite in typed}
    live_source_ids = {
        source_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for source_id in page.get("source_ids", [])
        if isinstance(source_id, str)
    }
    live_claim_ids = {
        claim_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        for claim_id in page.get("claim_ids", [])
        if isinstance(claim_id, str)
    }
    affected_sources = {
        source_id
        for source_id in live_source_ids
        if source_id in sources
        and sources[source_id].get("lifecycle") == "compiled"
        and any(
            _wikilink_atom_count(str(sources[source_id].get("body", "")), atom) for atom in atoms
        )
    }
    affected_claims = {
        claim_id
        for claim_id in live_claim_ids
        if claim_id in claims
        and claims[claim_id].get("status") == "accepted"
        and (
            any(
                _wikilink_atom_count(str(claims[claim_id].get("statement", "")), atom)
                for atom in atoms
            )
            or any(
                _wikilink_atom_count(str(claims[claim_id].get("markdown", "")), atom)
                for atom in atoms
            )
            or any(
                source_id in affected_sources
                for source_id in claims[claim_id].get("source_ids", [])
            )
        )
    }
    source_hashes = {
        source_id: _canonical_record_sha256(sources[source_id]) for source_id in affected_sources
    }
    claim_hashes = {
        claim_id: _canonical_record_sha256(claims[claim_id]) for claim_id in affected_claims
    }
    affected_pages = frozenset(
        page_id
        for page_id, page in pages.items()
        if page_id not in retired_page_ids
        if any(source_id in affected_sources for source_id in page.get("source_ids", []))
        or any(claim_id in affected_claims for claim_id in page.get("claim_ids", []))
    )
    return tuple(typed), source_hashes, claim_hashes, affected_pages


def _prepare_wiki_restructure_v2_preflight(
    root: Path, payload: dict[str, Any]
) -> WikiRestructurePreflight:
    """Verify every v2 record pins both generated output and compiler ownership."""

    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("Wiki restructure manifest requires a records list")
    active = {path.relative_to(root).as_posix(): path for path in iter_wiki_pages(root / "wiki")}
    compiler_pages, _compiler_curations = _compiler_catalog_records(root)
    compiler_by_path: dict[str, tuple[str, dict[str, Any]]] = {}
    for compiler_page_id, page in compiler_pages.items():
        output_path = _required_manifest_string(page, "output_path", "compiler page")
        if output_path is None:  # pragma: no cover - helper raises without an issue sink
            raise WoonError(f"compiler page has no output_path: {compiler_page_id}")
        compiler_by_path[(Path("wiki") / output_path).as_posix()] = (compiler_page_id, page)
    metadata_by_path: dict[str, dict[str, Any]] = {}
    for relative, path in active.items():
        try:
            metadata, _body = split_markdown(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, WoonError) as error:
            raise WoonError(f"cannot read active Wiki frontmatter: {relative}: {error}") from error
        metadata_by_path[relative] = metadata

    issues: list[str] = []
    seen_current: set[str] = set()
    final_paths: dict[str, tuple[str, str]] = {}
    final_canonical_ids: dict[str, str] = {}
    parent_requests: list[tuple[str, str, str]] = []
    counts = {action: 0 for action in sorted(_V2_ACTIONS)}

    def declare_final(path: str, owner: str, canonical_id: str, label: str) -> None:
        previous = final_paths.setdefault(path, (owner, label))
        if previous != (owner, label):
            issues.append(f"{label}: final target collision {path} with {previous[1]}")
            return
        final_canonical_ids[path] = canonical_id

    for index, raw_record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(raw_record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        record = raw_record
        action = record.get("action")
        if not isinstance(action, str) or action not in _V2_ACTIONS:
            issues.append(f"{label}: unsupported action {action!r}")
            continue
        counts[action] += 1
        canonical_id = _required_manifest_string(record, "canonical_id", label, issues)
        page_id = _required_manifest_string(record, "page_id", label, issues)
        owner = record.get("source_owner")
        if owner not in {"compiler", "manual"}:
            issues.append(f"{label}: source_owner must be compiler or manual")
        if action == "create":
            _validate_v2_create_record(record, label, canonical_id, page_id, owner, issues)
            target = _v2_target_path(record, label, issues)
            if target is not None and canonical_id is not None:
                declare_final(target, page_id or "", canonical_id, label)
                _collect_v2_parent_request(record, label, target, parent_requests, issues)
            continue

        current = _relative_path(record.get("current_path"), label, "current_path", issues)
        if current is None:
            continue
        if current in seen_current:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen_current.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active Wiki page: {current}")
            continue
        expected_hash = record.get("current_sha256")
        if expected_hash != hashlib.sha256(source.read_bytes()).hexdigest():
            issues.append(f"{label}: current_sha256 does not match: {current}")
        metadata = metadata_by_path[current]
        if canonical_id is not None and canonical_id != metadata.get("canonical_id"):
            issues.append(f"{label}: canonical_id does not match: {current}")
        actual_compiler = compiler_by_path.get(current)
        expected_owner = "compiler" if actual_compiler is not None else "manual"
        if owner != expected_owner:
            issues.append(f"{label}: source_owner must be {expected_owner!r} for {current}")
        if expected_owner == "compiler":
            if actual_compiler is None:  # pragma: no cover - narrowed above
                issues.append(f"{label}: compiler page is missing: {current}")
                continue
            actual_page_id, page = actual_compiler
            if page_id != actual_page_id:
                issues.append(f"{label}: page_id does not match compiler ownership: {current}")
            if record.get("page_spec_sha256") != _canonical_record_sha256(page):
                issues.append(f"{label}: page_spec_sha256 does not match: {current}")
        elif record.get("page_spec_sha256") not in {None, ""}:
            issues.append(f"{label}: manual page must not define page_spec_sha256")
        if action == "move":
            if owner != "compiler":
                issues.append(f"{label}: move is currently supported only for compiler pages")
                continue
            target = _v2_target_path(record, label, issues)
            _validate_v2_target_sequence(record, label, issues)
            if target is not None and canonical_id is not None:
                declare_final(target, page_id or "", canonical_id, label)
                _collect_v2_parent_request(record, label, target, parent_requests, issues)
        elif action == "keep":
            if canonical_id is not None:
                declare_final(current, page_id or "", canonical_id, label)
        elif action in {"merge", "retire"}:
            successor = record.get("link_successor")
            if not isinstance(successor, str) or not successor.strip():
                issues.append(f"{label}: {action} requires link_successor")
        elif action == "review":
            if record.get("target_path") not in {None, ""}:
                issues.append(f"{label}: review record must not define target_path")

    missing = sorted(set(active).difference(seen_current))
    if missing:
        issues.append(f"manifest omits {len(missing)} active Wiki pages")
    extra = sorted(seen_current.difference(active))
    if extra:
        issues.append(f"manifest names {len(extra)} non-active Wiki pages")
    for label, target_parent, parent_canonical_id in parent_requests:
        actual_canonical_id = final_canonical_ids.get(target_parent)
        if actual_canonical_id is None:
            issues.append(f"{label}: target_parent is not a final Wiki page: {target_parent}")
        elif actual_canonical_id != parent_canonical_id:
            issues.append(f"{label}: target_parent_canonical_id does not match: {target_parent}")
    return WikiRestructurePreflight(
        document_count=len(active),
        disposition_counts={key: value for key, value in counts.items() if value},
        target_count=sum(1 for action in ("create", "move") for _ in range(counts[action])),
        issues=tuple(issues),
    )


def _v2_compiler_transaction(
    root: Path, payload: dict[str, Any]
) -> tuple[CompiledWikiTransaction, tuple[str, ...], tuple[str, ...]]:
    """Build one exact compiler transaction only after v2 preflight succeeds."""

    records = payload.get("records")
    if not isinstance(records, list):  # pragma: no cover - validated by preflight
        raise WoonError("Wiki restructure manifest requires a records list")
    compiler_pages, compiler_curations = _compiler_catalog_records(root)
    active_titles = _active_wiki_titles(root)
    final_titles = dict(active_titles)
    for record in records:
        if not isinstance(record, dict):  # pragma: no cover - validated by preflight
            raise WoonError("Wiki restructure manifest contains an invalid record")
        action = record.get("action")
        if action == "move":
            current = str(record["current_path"])
            final_titles.pop(current, None)
            title = active_titles.get(current)
            if title is None:  # pragma: no cover - v2 preflight checks active current_path
                raise WoonError(f"migrated Wiki page title is missing: {current}")
            final_titles[str(record["target_path"])] = title
        elif action == "create":
            page = record.get("page_spec")
            if isinstance(page, dict):
                final_titles[str(record["target_path"])] = str(page["title"])

    pages_upsert: list[dict[str, Any]] = []
    curations_upsert: list[dict[str, Any]] = []
    expected_revisions: dict[str, str | None] = {}
    expected_page_spec_sha256: dict[str, str | None] = {}
    retired_output_paths: list[str] = []
    created_pages: list[str] = []
    moved_pages: list[str] = []
    for record in records:
        if not isinstance(record, dict):  # pragma: no cover - validated by preflight
            raise WoonError("Wiki restructure manifest contains an invalid record")
        action = record.get("action")
        if action == "create":
            page = deepcopy(record["page_spec"])
            curation = deepcopy(record["curation"])
            page_id = str(record["page_id"])
            pages_upsert.append(page)
            curations_upsert.append(curation)
            expected_revisions[page_id] = None
            expected_page_spec_sha256[page_id] = None
            created_pages.append(page_id)
        elif action == "move":
            page_id = str(record["page_id"])
            current_page = compiler_pages[page_id]
            current_curation = compiler_curations[page_id]
            moved = deepcopy(current_page)
            moved["output_path"] = str(record["target_path"])[len("wiki/") :]
            # A compiler page ID is stable provenance, while its generated Wiki
            # location may change.  Keep this explicit so accidental unrelated
            # ID/path mismatches remain invalid in the compiler.
            moved["output_path_migration"] = True
            frontmatter = moved.get("frontmatter")
            if not isinstance(
                frontmatter, dict
            ):  # pragma: no cover - compiler catalog validated later
                raise WoonError(f"compiler page frontmatter is invalid: {page_id}")
            parent_path = str(record["target_parent"])
            parent_title = final_titles.get(parent_path)
            if parent_title is None:  # pragma: no cover - v2 preflight checks final topology
                raise WoonError(f"target parent title is missing: {parent_path}")
            frontmatter["parent"] = _wiki_link(parent_path, parent_title)
            frontmatter["sequence"] = record["target_sequence"]
            pages_upsert.append(moved)
            curations_upsert.append(deepcopy(current_curation))
            current_path = root / str(record["current_path"])
            expected_revisions[page_id] = hashlib.sha256(current_path.read_bytes()).hexdigest()
            expected_page_spec_sha256[page_id] = _canonical_record_sha256(current_page)
            retired_output_paths.append(str(current_page["output_path"]))
            moved_pages.append(page_id)
    return (
        CompiledWikiTransaction(
            expected_revisions=expected_revisions,
            sources_upsert=(),
            claims_upsert=(),
            pages_upsert=tuple(pages_upsert),
            curations_upsert=tuple(curations_upsert),
            expected_page_spec_sha256=expected_page_spec_sha256,
            retired_output_paths=tuple(sorted(retired_output_paths)),
            refresh_wiki_tree=True,
        ),
        tuple(sorted(created_pages)),
        tuple(sorted(moved_pages)),
    )


def _validate_v2_create_record(
    record: dict[str, Any],
    label: str,
    canonical_id: str | None,
    page_id: str | None,
    owner: object,
    issues: list[str],
) -> None:
    if record.get("current_path") not in {None, ""} or record.get("current_sha256") not in {
        None,
        "",
    }:
        issues.append(f"{label}: create record must not define current_path or current_sha256")
    if owner != "compiler":
        issues.append(f"{label}: create is currently supported only for compiler pages")
    page = record.get("page_spec")
    curation = record.get("curation")
    if not isinstance(page, dict) or not isinstance(curation, dict):
        issues.append(f"{label}: create requires page_spec and curation mappings")
        return
    if record.get("page_spec_sha256") != _canonical_record_sha256(page):
        issues.append(f"{label}: page_spec_sha256 does not match page_spec")
    target = record.get("target_path")
    if (
        isinstance(target, str)
        and target.startswith("wiki/")
        and page.get("output_path") != target[len("wiki/") :]
    ):
        issues.append(f"{label}: page_spec.output_path must match target_path")
    if page_id is not None and page.get("page_id") != page_id:
        issues.append(f"{label}: page_spec.page_id must match page_id")
    frontmatter = page.get("frontmatter")
    if not isinstance(frontmatter, dict) or frontmatter.get("canonical_id") != canonical_id:
        issues.append(f"{label}: page_spec frontmatter canonical_id must match canonical_id")
    if page.get("render") != {"kind": "toc-only"}:
        issues.append(f"{label}: create page_spec must be source-free toc-only")
    if page.get("source_ids") != [] or page.get("claim_ids") != []:
        issues.append(f"{label}: create page_spec must not introduce source or claim ownership")
    if page_id is not None and curation.get("page_id") != page_id:
        issues.append(f"{label}: curation page_id must match page_id")
    _validate_v2_target_sequence(record, label, issues, frontmatter=frontmatter)


def _validate_v2_target_sequence(
    record: dict[str, Any],
    label: str,
    issues: list[str],
    *,
    frontmatter: dict[str, Any] | None = None,
) -> None:
    sequence = record.get("target_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, (int, float)) or sequence <= 0:
        issues.append(f"{label}: target_sequence must be a positive number")
        return
    if frontmatter is not None and frontmatter.get("sequence") != sequence:
        issues.append(f"{label}: page_spec frontmatter sequence must match target_sequence")


def _v2_target_path(record: dict[str, Any], label: str, issues: list[str]) -> str | None:
    target = _relative_path(record.get("target_path"), label, "target_path", issues)
    if target is not None and not target.startswith("wiki/"):
        issues.append(f"{label}: target_path must stay below wiki/: {target}")
    return target


def _collect_v2_parent_request(
    record: dict[str, Any],
    label: str,
    target: str,
    requests: list[tuple[str, str, str]],
    issues: list[str],
) -> None:
    parent = _relative_path(record.get("target_parent"), label, "target_parent", issues)
    canonical_id = _required_manifest_string(record, "target_parent_canonical_id", label, issues)
    if parent is not None and canonical_id is not None:
        if parent == target:
            issues.append(f"{label}: target_parent must not be the page itself")
        requests.append((label, parent, canonical_id))


def _active_wiki_titles(root: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    for path in iter_wiki_pages(root / "wiki"):
        metadata, _body = split_markdown(path.read_text(encoding="utf-8"))
        title = metadata.get("title")
        if not isinstance(title, str) or not title.strip():
            raise WoonError(f"Wiki page has no title: {path.relative_to(root).as_posix()}")
        titles[path.relative_to(root).as_posix()] = title.strip()
    return titles


def _wiki_link(path: str, title: str) -> str:
    return f"[[{path.removesuffix('.md')}|{title}]]"


def _load_wiki_restructure_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise WoonError(f"Wiki restructure manifest is missing or not a regular file: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict):
        raise WoonError("Wiki restructure manifest must be a mapping")
    return payload


def _compiler_catalog_records(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    pages_path = root / "catalog/llm-wiki/pages.yaml"
    curations_path = root / "catalog/llm-wiki/curation.yaml"
    try:
        page_payload = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
        curation_payload = yaml.safe_load(curations_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki compiler catalog is unreadable: {error}") from error
    raw_pages = page_payload.get("pages") if isinstance(page_payload, dict) else None
    raw_curations = (
        curation_payload.get("curations") if isinstance(curation_payload, dict) else None
    )
    if not isinstance(raw_pages, list) or not isinstance(raw_curations, list):
        raise WoonError("Wiki compiler catalog requires pages and curations lists")
    pages: dict[str, dict[str, Any]] = {}
    curations: dict[str, dict[str, Any]] = {}
    for record in raw_pages:
        if not isinstance(record, dict):
            raise WoonError("Wiki compiler catalog pages must contain mappings")
        page_id = _required_manifest_string(record, "page_id", "compiler page")
        if page_id is None or page_id in pages:
            raise WoonError("Wiki compiler catalog page_id is missing or duplicated")
        pages[page_id] = record
    for record in raw_curations:
        if not isinstance(record, dict):
            raise WoonError("Wiki compiler catalog curations must contain mappings")
        page_id = _required_manifest_string(record, "page_id", "compiler curation")
        if page_id is None or page_id in curations:
            raise WoonError("Wiki compiler catalog curation page_id is missing or duplicated")
        curations[page_id] = record
    if set(pages) != set(curations):
        raise WoonError("Wiki compiler catalog pages and curations must have identical IDs")
    return pages, curations


def _compiler_receipt_ids(root: Path) -> frozenset[str]:
    """Return receipt identities without treating receipt content as page input."""

    return frozenset(_compiler_receipt_records(root))


def _compiler_receipt_records(root: Path) -> dict[str, dict[str, Any]]:
    """Load exact receipt rows for destructive compiler retirement pins."""

    path = root / "catalog/llm-wiki/receipts.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki compiler receipts are unreadable: {error}") from error
    records = payload.get("receipts") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise WoonError("Wiki compiler receipts require a receipts list")
    result: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise WoonError("Wiki compiler receipts must contain mappings")
        page_id = _required_manifest_string(record, "page_id", f"compiler receipt {index}")
        if page_id is None or page_id in result:
            raise WoonError("Wiki compiler receipt page_id is missing or duplicated")
        result[page_id] = record
    return result


def _wiki_link_targets(text: str) -> set[str]:
    """Resolve only local Wiki wikilinks to their current Markdown locations."""

    targets: set[str] = set()
    for match in _BODY_WIKILINK_RE.finditer(text):
        raw = match.group("target").strip()
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        normalized = candidate.as_posix().removesuffix(".md")
        if not normalized.startswith("wiki/"):
            continue
        targets.add(f"{normalized}.md")
    return targets


def _required_manifest_string(
    record: dict[str, Any], field: str, label: str, issues: list[str] | None = None
) -> str | None:
    value = record.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    message = f"{label}: {field} must be a non-empty string"
    if issues is not None:
        issues.append(message)
        return None
    raise WoonError(message)


def _canonical_record_sha256(value: dict[str, Any]) -> str:
    try:
        payload = json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise WoonError("Wiki restructure page spec must be canonical JSON") from error
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _relative_path(value: Any, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()


def _compiler_owned_paths(root: Path) -> frozenset[str]:
    """Read compiler page ownership without treating a missing catalog as an error."""

    catalog = root / "catalog/llm-wiki/pages.yaml"
    if not catalog.is_file():
        return frozenset()
    try:
        payload = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki page catalog is unreadable: {error}") from error
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        raise WoonError("Wiki page catalog requires a pages list")
    result: set[str] = set()
    for index, page in enumerate(pages, start=1):
        output = page.get("output_path") if isinstance(page, dict) else None
        if not isinstance(output, str) or not output.strip():
            raise WoonError(f"Wiki page catalog pages[{index}] has invalid output_path")
        result.add((Path("wiki") / output).as_posix())
    return frozenset(result)


def _approved_scope_for_legacy_path(
    relative: str, *, canonical_id: str = ""
) -> tuple[str, str, str]:
    """Assign legacy areas to one branch of the fixed, approved tree."""

    if canonical_id == "personal/projects/private-novel-writing":
        return "창작 > 창작 프로젝트", "move", "creative-project"

    exact = {
        "wiki/common/README.md": ("개인 운영 > 지식 운영", "merge", "legacy-topic-map"),
        "wiki/common/aws-cost-cleanup-guardrails.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/aws-immersion-day-retry-roadmap.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/aws-well-architected-review.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/c-pointer-and-array.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > C·C++",
            "move",
            "topic",
        ),
        "wiki/common/cpu-vs-gpu.md": (
            "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
            "move",
            "topic",
        ),
        "wiki/common/floating-point-fixed-point.md": (
            "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
            "move",
            "topic",
        ),
        "wiki/common/garbage-collection.md": (
            "Wiki > 프로그래밍 언어·런타임 > 런타임·빌드",
            "move",
            "topic",
        ),
        "wiki/common/jpg-png-gif-image-formats.md": (
            "Wiki > 프런트엔드·클라이언트 > 웹 플랫폼",
            "move",
            "topic",
        ),
        "wiki/common/python-call-by-object-reference.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > Python",
            "move",
            "topic",
        ),
        "wiki/common/software-design-complexity.md": (
            "Wiki > 소프트웨어 설계·아키텍처 > 코드 설계·리팩터링",
            "move",
            "topic",
        ),
        "wiki/common/technical-writing.md": ("개인 운영 > 지식 운영", "move", "operating"),
        "wiki/concepts/README.md": ("개인 운영 > 지식 운영", "merge", "legacy-topic-map"),
        "wiki/concepts/kotlin.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > Kotlin",
            "move",
            "topic",
        ),
        "wiki/concepts/programming-languages.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 공통",
            "move",
            "topic",
        ),
        "wiki/concepts/refactoring.md": (
            "Wiki > 소프트웨어 설계·아키텍처 > 코드 설계·리팩터링",
            "move",
            "topic",
        ),
        "wiki/nodes/aice-associate-30일-학습-계획.md": (
            "Wiki > AI·머신러닝 > 머신러닝 기초",
            "move",
            "learning-plan",
        ),
        "wiki/nodes/이메일-별칭.md": ("개인 운영 > 생활", "move", "personal-operating"),
        "wiki/personal/ai-서비스-만들기.md": (
            "Wiki > AI·머신러닝 > AI 애플리케이션",
            "move",
            "topic",
        ),
        "wiki/personal/projects/(미정)소설-집필.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스.md": (
            "Wiki > 프로젝트 > K8s Clue",
            "move",
            "project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스-런타임.md": (
            "Wiki > 프로젝트 > K8s Clue > 아키텍처",
            "move",
            "project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스-이벤트-계약.md": (
            "Wiki > 프로젝트 > K8s Clue > 아키텍처",
            "move",
            "project",
        ),
        "wiki/personal/projects/minidb-데이터베이스-엔진.md": (
            "Wiki > 데이터·저장소",
            "move",
            "learning-implementation",
        ),
        "wiki/personal/projects/private-ai-animation-production.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/private-ai-animation-production/creator-research.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/private-ai-animation-production/production-learning-path.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/return-evidence-camera.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/temporal-k8s-ops-port.md": (
            "Wiki > 플랫폼·전달·운영 > 컨테이너·Kubernetes",
            "move",
            "learning-implementation",
        ),
        "wiki/personal/projects/woon-지식-운영-시스템.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/projects/README.md": (
            "개인 운영 > 지식 운영",
            "merge",
            "legacy-project-map",
        ),
        "wiki/personal/aice-associate-준비.md": (
            "Wiki > AI·머신러닝 > 머신러닝 기초",
            "move",
            "learning-plan",
        ),
        "wiki/personal/codex-대화는-완료된-경계부터-누적-정리한다.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/context-calendar.md": ("일정", "move", "schedule"),
        "wiki/personal/linked-graph.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/personal/woon-obsidian-테마.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/personal/이력서-복원은-검증-문장-우선.md": ("커리어 > 지원 자료", "move", "career"),
        "wiki/personal/이민정-ai-서비스-구현-분석.md": ("인물 > 이민정", "move", "person"),
        "wiki/personal/자동화는-실제-산출물로-검증한다.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/테스트-실패-원인을-조건별로-분리한다.md": (
            "Wiki > 품질·보안·신뢰성 > 테스트·검증",
            "move",
            "topic",
        ),
        "wiki/personal/플러그인-가치는-연결된-원문-탐색-경험에-둔다.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/private/이민정.md": ("인물 > 이민정", "move", "person"),
        "wiki/private/이민정-데이터-ai-커리어-전환-자료.md": (
            "인물 > 이민정",
            "move",
            "person-evidence",
        ),
        "wiki/private/이민정-크래프톤-입사-주거-보증금대출.md": (
            "인물 > 이민정",
            "move",
            "person-evidence",
        ),
    }
    if relative in exact:
        return exact[relative]
    if relative.startswith("wiki/personal/리모트ai-"):
        return "Wiki > AI·머신러닝 > AI 애플리케이션", "move", "private-design-topic"
    if relative.startswith("wiki/personal/") and relative.rsplit("/", 1)[-1] in {
        "강승렬.md",
        "김정선.md",
        "김희준.md",
        "신다영.md",
        "최우녕.md",
        "홍윤기.md",
    }:
        return "인물", "move", "person"
    if relative.startswith("wiki/hubs/"):
        return _legacy_hub_scope(relative)
    if relative.startswith("wiki/resources/"):
        return _legacy_resource_scope(relative)
    prefixes = (
        ("wiki/personal/kotlin-in-action", "Wiki > 책 > 프로그래밍 언어·설계", "book"),
        ("wiki/personal/컴퓨터-시스템-3판", "Wiki > 책 > 시스템·플랫폼", "book"),
        ("wiki/personal/밑바닥부터-시작하는-딥러닝-1", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/personal/밑바닥부터-만들면서-배우는-llm", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/private/novel", "창작 > 창작 프로젝트", "creative-project"),
        ("wiki/personal/career", "커리어", "career"),
        ("wiki/personal/interview", "커리어 > 지원 자료 > 면접 준비", "career"),
        ("wiki/ai", "Wiki > AI·머신러닝", "domain"),
        ("wiki/algorithm", "Wiki > 컴퓨터 과학 기초", "domain"),
        ("wiki/backend", "Wiki > 백엔드·서비스", "domain"),
        ("wiki/database", "Wiki > 데이터·저장소", "domain"),
        ("wiki/network", "Wiki > 컴퓨터 시스템·네트워크 > 네트워크", "domain"),
        ("wiki/os", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "domain"),
        ("wiki/pintos", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "learning-implementation"),
        ("wiki/security", "Wiki > 품질·보안·신뢰성", "domain"),
        ("wiki/books", "Wiki > 책", "book-map"),
        ("wiki/tools", "개인 운영 > 지식 운영", "operating"),
        ("wiki/knowledge", "개인 운영 > 지식 운영", "operating"),
        ("wiki/people", "인물", "people-map"),
    )
    for prefix, scope, rationale in prefixes:
        if relative == f"{prefix}.md" or relative.startswith(prefix + "/"):
            return scope, "move", rationale
    if relative == "wiki/README.md":
        return "Vault root", "keep", "vault-root"
    if relative.startswith(("wiki/common/", "wiki/concepts/", "wiki/nodes/")):
        return "review", "review", "mixed-legacy-topic"
    if relative.startswith("wiki/private/"):
        return "review", "review", "private-legacy-boundary"
    if relative.startswith("wiki/personal/"):
        return "review", "review", "personal-legacy-boundary"
    return "review", "review", "unclassified-legacy-path"


def _legacy_hub_scope(relative: str) -> tuple[str, str, str]:
    """Retire only the wrapper, merging its links into one final Map."""

    stem = Path(relative).stem
    scopes = {
        "ai-concept-to-code": "Wiki > AI·머신러닝",
        "ai-llm": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "ai-neural-network": "Wiki > AI·머신러닝 > 딥러닝",
        "algorithm-data-structure": "Wiki > 컴퓨터 과학 기초",
        "aws-immersion-day": "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
        "backend-runtime": "Wiki > 백엔드·서비스",
        "cnn-vision": "Wiki > AI·머신러닝 > 딥러닝",
        "concept-to-code": "개인 운영 > 지식 운영",
        "cpu-execution-program-loading": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "cs-basics": "Wiki > 컴퓨터 과학 기초",
        "database-storage": "Wiki > 데이터·저장소",
        "file-system-storage": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "knowledge-operations": "개인 운영 > 지식 운영",
        "llm-alignment-finetuning": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "llm-inference-serving": "Wiki > AI·머신러닝 > AI 애플리케이션",
        "llm-pretraining-scaling": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "local-private-index": "개인 운영 > 지식 운영",
        "math-statistics-foundations": "Wiki > AI·머신러닝 > 머신러닝 기초",
        "network-protocol": "Wiki > 컴퓨터 시스템·네트워크 > 네트워크",
        "neural-network-fundamentals": "Wiki > AI·머신러닝 > 딥러닝",
        "os-responsibility-boundary": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "os": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-alarm-clock-question-navigation": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-process-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-user-program-execution-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-vm-implementation-readiness": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-vm-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "process-lifecycle": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "qemu-hardware-byte-debugging": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "rag-agent-application": "Wiki > AI·머신러닝 > AI 애플리케이션",
        "security-web": "Wiki > 품질·보안·신뢰성 > 애플리케이션 보안",
        "sequence-model-rnn": "Wiki > AI·머신러닝 > 딥러닝",
        "threads-execution-model": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "tools-obsidian-pkm": "개인 운영 > 지식 운영",
        "transformer-attention": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "user-program-execution-boundary": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "vault-taxonomy": "개인 운영 > 지식 운영",
        "virtual-memory-translation": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "web-server-proxy": "Wiki > 백엔드·서비스 > 웹 애플리케이션",
    }
    return (
        scopes.get(stem, "review"),
        "merge" if stem in scopes else "review",
        "legacy-navigation-wrapper",
    )


def _legacy_resource_scope(relative: str) -> tuple[str, str, str]:
    """Retire resource link wrappers after their catalog relations move."""

    scopes = {
        "README": "개인 운영 > 지식 운영",
        "ai": "Wiki > AI·머신러닝",
        "algorithm": "Wiki > 컴퓨터 과학 기초",
        "aws": "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
        "books": "Wiki > 책",
        "c-memory": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "career": "커리어",
        "developer-references": "개인 운영 > 지식 운영",
        "learning-repositories": "개인 운영 > 지식 운영",
        "legacy-vault-2026": "개인 운영 > 지식 운영",
        "obsidian": "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
        "operating-system": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "programming-language": "Wiki > 프로그래밍 언어·런타임",
        "writing": "개인 운영 > 지식 운영",
    }
    stem = Path(relative).stem
    return (
        scopes.get(stem, "review"),
        "merge" if stem in scopes else "review",
        "legacy-resource-wrapper",
    )
