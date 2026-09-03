"""Verify that book-shaped Wiki pages cover one verified edition without shells."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from woon_core.knowledge.book_contract import (
    BOOK_CONTRACT_SHA256,
    BOOK_CONTRACT_VERSION,
    BOOK_WORKFLOW_PHASES,
    book_reader_workflow_prose_violation,
    book_workflow_phase_index,
)
from woon_core.knowledge.wiki_tree import (
    CHILDREN_END,
    CHILDREN_START,
    split_markdown,
    strip_generated_wiki_views,
)

SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSION = 2
STATES = {"toc-only", "drafted", "source-covered", "code-verified", "reviewed"}
COVERED_STATES = {"source-covered", "code-verified", "reviewed"}
NODE_KINDS = {
    "part",
    "chapter",
    "section",
    "subsection",
    "front-matter",
    "back-matter",
    "appendix",
    "bibliography",
    "index",
}
SOURCE_STRUCTURE_KINDS = NODE_KINDS | {"copyright"}
COVERAGE_KINDS = ("claims", "examples", "cautions", "figures", "code")
SOURCE_ELEMENT_KINDS = {"claim", "example", "caution", "figure", "code"}
COVERAGE_KIND_BY_ELEMENT = {
    "claim": "claims",
    "example": "examples",
    "caution": "cautions",
    "figure": "figures",
    "code": "code",
}
SEMANTIC_UNITS = {
    "paragraph",
    "definition",
    "theorem",
    "table",
    "procedure",
    "equation",
    "list",
    "worked-example",
    "exercise",
    "scenario",
    "caution",
    "figure",
    "code-block",
}
INVENTORY_EXTRACTION_METHODS = {
    "manual-semantic-review",
    "structured-source-parser",
    "hybrid-semantic-review",
}
RUNNABLE_SUPPORT = {"supported", "static-exception", "not-applicable"}
STATIC_EXCEPTION_REASON_CODES = {
    "fragment",
    "dependency",
    "intentional-error",
    "placeholder",
}
_WIKILINK_TARGET = re.compile(r"^\[\[(?P<target>[^\]|#]+)")
_FENCE_OPEN = re.compile(r"(?m)^```(?P<language>[A-Za-z0-9_+-]+)[ \t]*$")
_FENCE_BLOCK = re.compile(r"(?ms)^```(?P<language>[A-Za-z0-9_+-]+)[ \t]*\n(?P<body>.*?)^```[ \t]*$")
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
_MANAGED_MAP_LINK = re.compile(r"(?m)^- \[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]\s*$")
_MANAGED_MAP_H2 = re.compile(r"(?m)^##\s+(?P<label>\S.*?)\s*$")
_READER_OPERATIONAL_PROSE = (
    re.compile(r"(?m)^## (?:근거와 )?학습 상태\s*$"),
    re.compile(r"(?m)^## 다시 열었을 때(?: .*)?\s*$"),
    re.compile(r"(?m)^## 목차 근거\s*$"),
    re.compile(r"상위 `학습 체크포인트`"),
    re.compile(r"이 페이지가 존재한다는 사실은 (?:숙달|학습 완료) 증거가 아니다"),
    re.compile(r"현재 학습 상태는 `?(?:Planned|Active|Completed)`?"),
    re.compile(r"Obsidian 실행 경계"),
    re.compile(r"각 (?:절|장|문서)(?:이|가) .*소유한다"),
    re.compile(r"Run을 눌렀다는 이유만으로 학습 완료"),
    re.compile(r"\b(?:claim|example|caution|figure|code) semantic unit\b", re.IGNORECASE),
    re.compile(r"unnumbered source code segment", re.IGNORECASE),
    re.compile(r"(?:한다|된다|줄인다|남긴다|계산한다)이다"),
    re.compile(r"(?:사용|적용|계산|표시|분류)를 사용을"),
)


@dataclass(frozen=True, slots=True)
class BookCoverageLaneAudit:
    """One independently reported book verification lane."""

    scope: str
    complete: bool
    error_count: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BookCoverageAudit:
    contract_version: int
    contract_sha256: str
    book_count: int
    manifest_count: int
    verified_scope_count: int
    pending_book_count: int
    pending_books: tuple[str, ...]
    blocked_book_count: int
    blocked_books: tuple[str, ...]
    expected_node_count: int
    covered_leaf_count: int
    structure: BookCoverageLaneAudit
    source: BookCoverageLaneAudit
    runnable: BookCoverageLaneAudit
    quality: BookCoverageLaneAudit
    ui: BookCoverageLaneAudit
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return (
            not self.errors
            and not self.pending_books
            and all(
                lane.complete
                for lane in (self.structure, self.source, self.runnable, self.quality, self.ui)
            )
        )


def audit_book_coverage(vault: Path) -> BookCoverageAudit:
    """Audit every current book entity against its source coverage manifest."""

    return _audit_book_coverage(vault)


def audit_book_coverage_scope(vault: Path, relative_path: str) -> BookCoverageAudit:
    """Audit one staged schema-v2 scope without judging unfinished sibling chapters."""

    candidate = Path(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or candidate.is_absolute()
        or candidate.as_posix() != relative_path
        or candidate.parts[:2] != ("catalog", "book-coverage-scopes")
        or len(candidate.parts) != 4
        or candidate.suffix != ".json"
        or ".." in candidate.parts
    ):
        raise ValueError(
            "scoped book coverage path must be one JSON file under "
            "catalog/book-coverage-scopes/<book>"
        )
    return _audit_book_coverage(vault, scope_manifest_path=candidate)


def _audit_book_coverage(
    vault: Path,
    *,
    scope_manifest_path: Path | None = None,
) -> BookCoverageAudit:
    """Shared full-book and independently verified-scope audit implementation."""

    vault = vault.expanduser().resolve()
    wiki = vault / "wiki"
    pages: dict[str, tuple[Path, dict[str, Any], str]] = {}
    books: set[str] = set()
    errors: list[str] = []
    if wiki.exists():
        for path in sorted(wiki.rglob("*.md")):
            if "_sources" in path.parts:
                continue
            try:
                metadata, body = split_markdown(path.read_text(encoding="utf-8"))
            except Exception as error:  # pragma: no cover - tree audit owns detail
                errors.append(f"{path.relative_to(vault)}: invalid Markdown: {error}")
                continue
            canonical_id = metadata.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                continue
            canonical_id = canonical_id.strip()
            pages[canonical_id] = (path, metadata, body)
            if metadata.get("content_kind") == "book" or metadata.get("entity_kind") == "book":
                books.add(canonical_id)

    manifest_root = vault / "catalog/book-coverage"
    manifests: dict[str, tuple[Path, dict[str, Any]]] = {}
    manifest_paths = (
        (vault / scope_manifest_path,)
        if scope_manifest_path is not None
        else tuple(sorted(manifest_root.glob("*.json")))
        if manifest_root.exists()
        else ()
    )
    for path in manifest_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"{path.relative_to(vault)}: invalid JSON: {error}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path.relative_to(vault)}: manifest must be an object")
            continue
        book_id = payload.get("book_id")
        if not isinstance(book_id, str) or not book_id.strip():
            errors.append(f"{path.relative_to(vault)}: book_id is required")
            continue
        book_id = book_id.strip()
        if book_id in manifests:
            errors.append(f"{path.relative_to(vault)}: duplicate manifest for {book_id}")
            continue
        manifests[book_id] = (path, payload)

    blocked_books: set[str] = set()
    intake_root = vault / "catalog/book-intake"
    if intake_root.exists():
        for path in sorted(intake_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # book-intake-audit owns malformed intake detail. Do not let a
                # broken registry silently waive a missing coverage manifest.
                continue
            bundles = payload.get("bundles") if isinstance(payload, dict) else None
            if not isinstance(bundles, list):
                continue
            for bundle in bundles:
                if not isinstance(bundle, dict):
                    continue
                target = _text(bundle.get("target"))
                if (
                    bundle.get("kind") == "book"
                    and bundle.get("processing_state") == "blocked-rights"
                    and target
                ):
                    blocked_books.add(target)

    if scope_manifest_path is None:
        for book_id in sorted(books - manifests.keys() - blocked_books):
            errors.append(f"{book_id}: book coverage manifest is missing")
    for book_id in sorted(manifests.keys() - books):
        path, _ = manifests[book_id]
        errors.append(f"{path.relative_to(vault)}: manifest book does not exist: {book_id}")

    expected_node_count = 0
    covered_leaf_count = 0
    verified_scope_count = 0
    ui_scope_ids: dict[str, set[str]] = {}
    pending_books: set[str] = set()
    for book_id in sorted(books & manifests.keys()):
        path, manifest = manifests[book_id]
        prefix = path.relative_to(vault).as_posix()
        base_node_ids_for_scope: set[str] = set()
        if scope_manifest_path is not None:
            scope = manifest.get("coverage_scope")
            if not isinstance(scope, dict) or set(scope) != {
                "root_id",
                "base_relative_path",
                "base_sha256",
            }:
                errors.append(
                    f"{prefix}: coverage_scope requires root_id, base_relative_path, "
                    "and base_sha256"
                )
            else:
                scope_root_id = _text(scope.get("root_id"))
                base_relative_path = _text(scope.get("base_relative_path"))
                base_sha256 = _text(scope.get("base_sha256"))
                if not scope_root_id:
                    errors.append(f"{prefix}: coverage_scope.root_id is required")
                if not base_relative_path.startswith("catalog/book-coverage/"):
                    errors.append(
                        f"{prefix}: coverage_scope.base_relative_path must name a full "
                        "book coverage manifest"
                    )
                if _LOWER_SHA256.fullmatch(base_sha256) is None:
                    errors.append(
                        f"{prefix}: coverage_scope.base_sha256 must be a lowercase SHA-256"
                    )
                base_candidate = Path(base_relative_path)
                if (
                    base_candidate.is_absolute()
                    or base_candidate.as_posix() != base_relative_path
                    or base_candidate.parts[:2] != ("catalog", "book-coverage")
                    or len(base_candidate.parts) != 3
                    or base_candidate.suffix != ".json"
                    or ".." in base_candidate.parts
                ):
                    errors.append(f"{prefix}: coverage_scope.base_relative_path is not canonical")
                else:
                    base_path = vault / base_candidate
                    if base_path.is_symlink() or not base_path.is_file():
                        errors.append(
                            f"{prefix}: coverage_scope base manifest is missing or unsafe"
                        )
                    else:
                        base_bytes = base_path.read_bytes()
                        if hashlib.sha256(base_bytes).hexdigest() != base_sha256:
                            errors.append(
                                f"{prefix}: coverage_scope base manifest changed after review"
                            )
                        try:
                            base_payload = json.loads(base_bytes)
                        except json.JSONDecodeError:
                            errors.append(f"{prefix}: coverage_scope base manifest is invalid JSON")
                        else:
                            if (
                                not isinstance(base_payload, dict)
                                or base_payload.get("book_id") != book_id
                            ):
                                errors.append(
                                    f"{prefix}: coverage_scope base manifest book_id mismatch"
                                )
                            base_nodes = (
                                base_payload.get("nodes")
                                if isinstance(base_payload, dict)
                                else None
                            )
                            if isinstance(base_nodes, list):
                                base_node_ids_for_scope = {
                                    canonical_id
                                    for node in base_nodes
                                    if isinstance(node, dict)
                                    and isinstance(canonical_id := node.get("canonical_id"), str)
                                }
        manifest_schema = manifest.get("schema_version")
        if scope_manifest_path is None and manifest_schema == 1:
            pending_books.add(book_id)
            legacy_nodes = manifest.get("nodes")
            if isinstance(legacy_nodes, list):
                expected_node_count += len(legacy_nodes)
            else:
                errors.append(f"{prefix}: legacy pending manifest nodes must be an array")
            continue
        if manifest_schema not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            errors.append(
                f"{prefix}: schema_version must be {SCHEMA_VERSION} "
                f"(stored v{LEGACY_SCHEMA_VERSION} scopes remain audit-compatible)"
            )
        workflow_phase = ""
        translation_required = True
        if manifest_schema == SCHEMA_VERSION:
            workflow_phase, translation_required = _audit_workflow_contract(
                prefix,
                manifest,
                vault,
                pages,
                errors,
            )
        edition = manifest.get("edition")
        if not isinstance(edition, dict) or not _text(edition.get("label")):
            errors.append(f"{prefix}: edition.label is required")
        source_sha256 = edition.get("source_sha256") if isinstance(edition, dict) else None
        if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            errors.append(f"{prefix}: edition.source_sha256 must be a lowercase SHA-256")
        toc_evidence = manifest.get("toc_evidence")
        if not isinstance(toc_evidence, list) or not toc_evidence:
            errors.append(f"{prefix}: toc_evidence must contain a verified locator")
        else:
            for index, item in enumerate(toc_evidence):
                if (
                    not isinstance(item, dict)
                    or not _text(item.get("locator"))
                    or not _text(item.get("verified_on"))
                ):
                    errors.append(
                        f"{prefix}: toc_evidence[{index}] requires locator and verified_on"
                    )
        nodes = manifest.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            errors.append(f"{prefix}: nodes must contain the complete verified TOC")
            continue
        expected_node_count += len(nodes)
        declared_count = manifest.get("toc_node_count")
        if declared_count != len(nodes):
            errors.append(f"{prefix}: toc_node_count does not match nodes")
        seen: set[str] = set()
        leaf_count = 0
        node_ids = {
            canonical_id
            for node in nodes
            if isinstance(node, dict) and (canonical_id := _text(node.get("canonical_id")))
        }
        if scope_manifest_path is not None:
            scope = manifest.get("coverage_scope")
            scope_root_id = _text(scope.get("root_id")) if isinstance(scope, dict) else ""
            if scope_root_id not in node_ids:
                errors.append(f"{prefix}: coverage_scope.root_id must be present in nodes")
            for node_id in sorted(node_ids):
                if (
                    scope_root_id
                    and node_id != scope_root_id
                    and not node_id.startswith(scope_root_id + "/")
                ):
                    errors.append(
                        f"{prefix}: scoped node is outside coverage_scope.root_id: {node_id}"
                    )
            missing_from_base = sorted(node_ids.difference(base_node_ids_for_scope))
            if missing_from_base:
                errors.append(
                    f"{prefix}: scoped node is absent from the pinned full TOC: "
                    f"{missing_from_base[0]}"
                )
            ui_scope_ids[book_id] = set(node_ids)
        node_order = [
            canonical_id
            for node in nodes
            if isinstance(node, dict) and (canonical_id := _text(node.get("canonical_id")))
        ]
        leaf_ids = {
            canonical_id
            for node in nodes
            if isinstance(node, dict)
            and node.get("leaf") is True
            and (canonical_id := _text(node.get("canonical_id")))
        }
        _audit_source_structure_contract(
            prefix,
            manifest,
            node_ids,
            node_order,
            pages,
            errors,
        )
        _audit_retired_source_section_wrappers(
            prefix,
            manifest,
            node_ids,
            leaf_ids,
            pages,
            errors,
        )
        element_counts, element_contract_valid = _audit_source_element_contract(
            prefix,
            manifest,
            node_ids,
            leaf_ids,
            pages,
            vault,
            errors,
        )
        if not element_contract_valid:
            errors.append(
                f"{prefix}: runnable audit is incomplete because the source element "
                "inventory or assignments are invalid"
            )
        for index, node in enumerate(nodes):
            label = f"{prefix}: nodes[{index}]"
            if not isinstance(node, dict):
                errors.append(f"{label} must be an object")
                continue
            canonical_id = _text(node.get("canonical_id"))
            parent_id = _text(node.get("parent_id"))
            kind = _text(node.get("kind"))
            state = _text(node.get("state"))
            locator = _text(node.get("source_locator"))
            leaf = node.get("leaf") is True
            has_direct_content = node.get("has_direct_content")
            if not canonical_id:
                errors.append(f"{label}.canonical_id is required")
                continue
            if canonical_id in seen:
                errors.append(f"{label}: duplicate canonical_id {canonical_id}")
            seen.add(canonical_id)
            if kind not in NODE_KINDS:
                errors.append(
                    f"{label}.kind must be part, chapter, section, subsection, "
                    "front-matter, back-matter, appendix, bibliography, or index"
                )
            if state not in STATES:
                errors.append(f"{label}.state is invalid")
            if not locator:
                errors.append(f"{label}.source_locator is required")
            if not isinstance(has_direct_content, bool):
                errors.append(f"{label}.has_direct_content must be true or false")
            if leaf and has_direct_content is not True:
                errors.append(f"{label}: a leaf must declare has_direct_content=true")
            reader_language = _text(node.get("reader_language"))
            source_prose_verified = node.get("source_prose_verified")
            if manifest_schema == SCHEMA_VERSION:
                if re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", reader_language) is None:
                    errors.append(f"{label}.reader_language must be a BCP-47 language tag")
                if not isinstance(source_prose_verified, bool):
                    errors.append(f"{label}.source_prose_verified must be true or false")
                elif has_direct_content is True and source_prose_verified is not True:
                    errors.append(
                        f"{label}: direct source content requires source_prose_verified=true"
                    )
            actual_runnable_count: int | None = None
            reader_body = ""
            actual = pages.get(canonical_id)
            if actual is None:
                errors.append(f"{label}: canonical page does not exist: {canonical_id}")
            else:
                page_path, metadata, body = actual
                reader_body = _reader_body(body)
                actual_runnable_count = len(
                    re.findall(r"(?m)^```run-[A-Za-z0-9_-]+[ \t]*$", reader_body)
                )
                actual_parent = _canonical_parent(metadata.get("parent"))
                if not parent_id or actual_parent != parent_id:
                    errors.append(
                        f"{label}: parent mismatch expected={parent_id or '<missing>'} "
                        f"actual={actual_parent or '<missing>'}"
                    )
                if leaf and reader_body.strip() == "":
                    errors.append(f"{page_path.relative_to(vault)}: leaf body is empty")
                workflow_violation = book_reader_workflow_prose_violation(reader_body)
                if workflow_violation is not None:
                    errors.append(
                        f"{page_path.relative_to(vault)}: reader body contains generated "
                        f"learning workflow prose: {workflow_violation}"
                    )
                for pattern in _READER_OPERATIONAL_PROSE:
                    if pattern.search(reader_body):
                        errors.append(
                            f"{page_path.relative_to(vault)}: reader body contains "
                            "workflow or completion metadata"
                        )
                        break
            if state == "toc-only":
                if node.get("leaf") is not False:
                    errors.append(f"{label}: a toc-only node must declare leaf=false")
                if has_direct_content is not False:
                    errors.append(
                        f"{label}: a toc-only node must declare has_direct_content=false"
                    )
                if actual is not None:
                    _, metadata, _ = actual
                    if metadata.get("content_state") != "toc-only":
                        errors.append(
                            f"{label}: toc-only page must declare content_state=toc-only"
                        )
                    if reader_body.strip():
                        errors.append(f"{label}: toc-only page contains authored prose")
                continue
            if not leaf:
                if has_direct_content is not True:
                    continue
            else:
                leaf_count += 1
            coverage = node.get("coverage")
            if not isinstance(coverage, dict):
                errors.append(f"{label}.coverage is required for direct source content")
                continue
            fully_covered = True
            for coverage_kind in COVERAGE_KINDS:
                counts = coverage.get(coverage_kind)
                if not isinstance(counts, dict):
                    errors.append(f"{label}.coverage.{coverage_kind} is required")
                    fully_covered = False
                    continue
                expected = counts.get("expected")
                covered = counts.get("covered")
                if not isinstance(expected, int) or expected < 0:
                    errors.append(f"{label}.coverage.{coverage_kind}.expected is invalid")
                    fully_covered = False
                    continue
                if not isinstance(covered, int) or covered < 0 or covered > expected:
                    errors.append(f"{label}.coverage.{coverage_kind}.covered is invalid")
                    fully_covered = False
                    continue
                if covered != expected:
                    fully_covered = False
                if canonical_id:
                    assigned = element_counts.get((canonical_id, coverage_kind), 0)
                    if expected != assigned or covered != assigned:
                        errors.append(
                            f"{label}.coverage.{coverage_kind} expected={expected} "
                            f"covered={covered} does not match derived exact source element "
                            f"assignments={assigned}"
                        )
                        fully_covered = False
            claims = coverage.get("claims")
            if isinstance(claims, dict) and claims.get("expected") == 0:
                errors.append(f"{label}: a source-covered leaf must declare at least one claim")
                fully_covered = False
            runnable = node.get("runnable")
            if not isinstance(runnable, dict):
                errors.append(f"{label}.runnable is required")
                fully_covered = False
            else:
                expected = runnable.get("expected")
                verified = runnable.get("verified")
                if (
                    not isinstance(expected, int)
                    or expected < 0
                    or not isinstance(verified, int)
                    or verified < 0
                    or verified > expected
                ):
                    errors.append(f"{label}.runnable counts are invalid")
                    fully_covered = False
                elif expected != verified:
                    fully_covered = False
                elif actual_runnable_count is not None and expected != actual_runnable_count:
                    errors.append(
                        f"{label}: runnable.expected={expected} does not match "
                        f"reader run-* blocks={actual_runnable_count}"
                    )
                    fully_covered = False
                elif expected > 0 and state not in {"code-verified", "reviewed"}:
                    errors.append(f"{label}: verified runnable code requires code-verified state")
                    fully_covered = False
            if state in COVERED_STATES and fully_covered:
                reader_quality_verified = True
                if manifest_schema == LEGACY_SCHEMA_VERSION:
                    if node.get("korean_prose_reviewed") is not True:
                        errors.append(f"{label}: korean_prose_reviewed must be true")
                        reader_quality_verified = False
                elif manifest_schema == SCHEMA_VERSION:
                    phase_rank = book_workflow_phase_index(workflow_phase)
                    translated_rank = book_workflow_phase_index("translated")
                    if has_direct_content is True and source_prose_verified is not True:
                        reader_quality_verified = False
                    if phase_rank >= translated_rank:
                        if reader_language != "ko":
                            errors.append(
                                f"{label}: translated-or-later reader_language must be ko"
                            )
                            reader_quality_verified = False
                        if node.get("korean_prose_reviewed") is not True:
                            errors.append(
                                f"{label}: translated-or-later korean_prose_reviewed must be true"
                            )
                            reader_quality_verified = False
                        if (
                            has_direct_content is True
                            and actual is not None
                            and re.search(r"[가-힣]", _reader_body(actual[2])) is None
                        ):
                            errors.append(
                                f"{label}: translated reader body must contain Korean prose"
                            )
                            reader_quality_verified = False
                    elif (
                        workflow_phase == "source-landed"
                        and translation_required is False
                        and has_direct_content is True
                        and reader_language != "ko"
                    ):
                        errors.append(
                            f"{label}: Korean source with translation_required=false "
                            "must declare reader_language=ko"
                        )
                        reader_quality_verified = False
                if leaf and reader_quality_verified:
                    covered_leaf_count += 1
            else:
                errors.append(f"{label}: leaf is not fully source-covered")
        if manifest.get("toc_leaf_count") != leaf_count:
            errors.append(f"{prefix}: toc_leaf_count does not match leaf nodes")

    if scope_manifest_path is None:
        scope_root = vault / "catalog/book-coverage-scopes"
        if scope_root.exists():
            for scope_path in sorted(scope_root.glob("*/*.json")):
                relative_scope = scope_path.relative_to(vault).as_posix()
                try:
                    scope_audit = audit_book_coverage_scope(vault, relative_scope)
                except ValueError as error:
                    errors.append(f"{relative_scope}: {error}")
                    continue
                verified_scope_count += 1
                covered_leaf_count += scope_audit.covered_leaf_count
                errors.extend(scope_audit.errors)

    audited_books = (
        books.difference(pending_books) if scope_manifest_path is None else set(manifests)
    )
    for book_id in sorted(audited_books):
        _audit_book_map_ui(
            book_id,
            pages,
            errors,
            allowed_ids=ui_scope_ids.get(book_id),
        )

    lanes = _lane_audits(errors)

    return BookCoverageAudit(
        contract_version=BOOK_CONTRACT_VERSION,
        contract_sha256=BOOK_CONTRACT_SHA256,
        book_count=len(books),
        manifest_count=len(manifests),
        verified_scope_count=1 if scope_manifest_path is not None else verified_scope_count,
        pending_book_count=len(pending_books),
        pending_books=tuple(sorted(pending_books)),
        blocked_book_count=len(books & blocked_books),
        blocked_books=tuple(sorted(books & blocked_books)),
        expected_node_count=expected_node_count,
        covered_leaf_count=covered_leaf_count,
        structure=lanes["structure"],
        source=lanes["source"],
        runnable=lanes["runnable"],
        quality=lanes["quality"],
        ui=lanes["ui"],
        errors=tuple(errors),
    )


def _audit_workflow_contract(
    prefix: str,
    manifest: dict[str, Any],
    vault: Path,
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    errors: list[str],
) -> tuple[str, bool]:
    """Validate the four-phase v7 book workflow and its immutable evidence."""

    workflow_phase = _text(manifest.get("workflow_phase"))
    phase_rank = book_workflow_phase_index(workflow_phase)
    if phase_rank < 0:
        errors.append(
            f"{prefix}: workflow_phase must be one of: {', '.join(BOOK_WORKFLOW_PHASES)}"
        )
    translation_required = manifest.get("translation_required")
    if not isinstance(translation_required, bool):
        errors.append(f"{prefix}: translation_required must be true or false")
        translation_required = True

    source_archive = manifest.get("source_archive")
    expected_archive_fields = {"relative_path", "actual_title", "sha256", "privacy"}
    if not isinstance(source_archive, dict) or set(source_archive) != expected_archive_fields:
        errors.append(
            f"{prefix}: source_archive must contain relative_path, actual_title, sha256, "
            "and privacy"
        )
    else:
        relative_path = _text(source_archive.get("relative_path"))
        actual_title = _text(source_archive.get("actual_title"))
        archive_sha256 = _text(source_archive.get("sha256"))
        candidate = Path(relative_path)
        valid_path = (
            bool(relative_path)
            and not candidate.is_absolute()
            and candidate.as_posix() == relative_path
            and candidate.parts[:5]
            == ("wiki", "private", "_sources", "knowledge", "local-only")
            and ".." not in candidate.parts
        )
        if not valid_path:
            errors.append(
                f"{prefix}: source_archive.relative_path must be under "
                "wiki/private/_sources/knowledge/local-only"
            )
        if not actual_title or Path(actual_title).name != actual_title:
            errors.append(f"{prefix}: source_archive.actual_title must be one file title")
        elif valid_path and candidate.stem != actual_title:
            errors.append(
                f"{prefix}: source archive filename must use actual_title exactly"
            )
        if _LOWER_SHA256.fullmatch(archive_sha256) is None:
            errors.append(f"{prefix}: source_archive.sha256 must be a lowercase SHA-256")
        edition = manifest.get("edition")
        edition_sha256 = _text(edition.get("source_sha256")) if isinstance(edition, dict) else ""
        if archive_sha256 and edition_sha256 and archive_sha256 != edition_sha256:
            errors.append(f"{prefix}: source archive hash must match edition.source_sha256")
        if source_archive.get("privacy") != "local-only":
            errors.append(f"{prefix}: source_archive.privacy must be local-only")
        if valid_path:
            archive_path = vault / candidate
            if archive_path.is_symlink() or not archive_path.is_file():
                errors.append(f"{prefix}: source_archive file is missing or unsafe")
            elif _LOWER_SHA256.fullmatch(archive_sha256) is not None:
                actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                if actual_sha256 != archive_sha256:
                    errors.append(f"{prefix}: source_archive file hash does not match")

    asset_inventory = manifest.get("source_asset_inventory")
    if not isinstance(asset_inventory, list):
        errors.append(f"{prefix}: source_asset_inventory must be an array")
        asset_inventory = []
    seen_asset_ids: set[str] = set()
    for index, item in enumerate(asset_inventory):
        label = f"{prefix}: source_asset_inventory[{index}]"
        expected_fields = {
            "asset_id",
            "source_locator",
            "source_sha256",
            "archive_relative_path",
            "archive_sha256",
            "extraction_kind",
            "crop_provenance",
        }
        if not isinstance(item, dict) or set(item) != expected_fields:
            errors.append(f"{label} fields are invalid")
            continue
        asset_id = _text(item.get("asset_id"))
        if not asset_id:
            errors.append(f"{label}.asset_id is required")
        elif asset_id in seen_asset_ids:
            errors.append(f"{label}: duplicate asset_id {asset_id}")
        seen_asset_ids.add(asset_id)
        _audit_pinned_evidence(label, item, "source_locator", "source_sha256", errors)
        archive_relative_path = _text(item.get("archive_relative_path"))
        archive_candidate = Path(archive_relative_path)
        valid_archive_path = (
            bool(archive_relative_path)
            and not archive_candidate.is_absolute()
            and archive_candidate.as_posix() == archive_relative_path
            and archive_candidate.parts[:5]
            == ("wiki", "private", "_sources", "knowledge", "local-only")
            and ".." not in archive_candidate.parts
        )
        if not valid_archive_path:
            errors.append(
                f"{label}.archive_relative_path must be under "
                "wiki/private/_sources/knowledge/local-only"
            )
        archive_sha256 = _text(item.get("archive_sha256"))
        if _LOWER_SHA256.fullmatch(archive_sha256) is None:
            errors.append(f"{label}.archive_sha256 must be a lowercase SHA-256")
        elif valid_archive_path:
            archive_path = vault / archive_candidate
            if archive_path.is_symlink() or not archive_path.is_file():
                errors.append(f"{label}: archived source image is missing or unsafe")
            elif hashlib.sha256(archive_path.read_bytes()).hexdigest() != archive_sha256:
                errors.append(f"{label}: archived source image hash does not match")
        extraction_kind = item.get("extraction_kind")
        crop_provenance = item.get("crop_provenance")
        if extraction_kind == "embedded-original":
            if _text(item.get("source_sha256")) != archive_sha256:
                errors.append(f"{label}: embedded image bytes must be preserved exactly")
            if crop_provenance is not None:
                errors.append(f"{label}: embedded image crop_provenance must be null")
        elif extraction_kind == "scan-crop":
            crop_fields = {"page_locator", "crop_box", "render_dpi", "source_page_sha256"}
            if not isinstance(crop_provenance, dict) or set(crop_provenance) != crop_fields:
                errors.append(f"{label}: scan crop provenance fields are invalid")
            else:
                if not _stable_locator(_text(crop_provenance.get("page_locator"))):
                    errors.append(f"{label}: scan crop page_locator must be stable")
                crop_box = crop_provenance.get("crop_box")
                if not isinstance(crop_box, list) or len(crop_box) != 4 or not all(
                    isinstance(value, (int, float)) and value >= 0 for value in crop_box
                ):
                    errors.append(f"{label}: scan crop crop_box must contain four numbers")
                if not isinstance(crop_provenance.get("render_dpi"), int) or (
                    crop_provenance.get("render_dpi", 0) <= 0
                ):
                    errors.append(f"{label}: scan crop render_dpi must be positive")
                if _LOWER_SHA256.fullmatch(
                    _text(crop_provenance.get("source_page_sha256"))
                ) is None:
                    errors.append(
                        f"{label}: scan crop source_page_sha256 must be a lowercase SHA-256"
                    )
        else:
            errors.append(
                f"{label}.extraction_kind must be embedded-original or scan-crop"
            )

    asset_evidence = manifest.get("source_asset_inventory_evidence")
    expected_asset_fields = {
        "locator",
        "sha256",
        "verified_on",
        "embedded_original_bytes",
        "scan_crop_provenance",
        "expected_asset_count",
        "inventory_sha256",
    }
    if not isinstance(asset_evidence, dict) or set(asset_evidence) != expected_asset_fields:
        errors.append(
            f"{prefix}: source_asset_inventory_evidence fields are invalid"
        )
    else:
        _audit_pinned_evidence(
            f"{prefix}: source_asset_inventory_evidence",
            asset_evidence,
            "locator",
            "sha256",
            errors,
        )
        if not _text(asset_evidence.get("verified_on")):
            errors.append(
                f"{prefix}: source_asset_inventory_evidence.verified_on is required"
            )
        if asset_evidence.get("embedded_original_bytes") is not True:
            errors.append(
                f"{prefix}: embedded source images must preserve original bytes"
            )
        if not isinstance(asset_evidence.get("scan_crop_provenance"), bool):
            errors.append(
                f"{prefix}: source_asset_inventory_evidence.scan_crop_provenance "
                "must be true or false"
            )
        if asset_evidence.get("expected_asset_count") != len(asset_inventory):
            errors.append(
                f"{prefix}: source asset expected count does not match inventory"
            )
        expected_inventory_sha256 = hashlib.sha256(
            json.dumps(
                asset_inventory,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if asset_evidence.get("inventory_sha256") != expected_inventory_sha256:
            errors.append(f"{prefix}: source asset inventory hash does not match")

    evidence = manifest.get("phase_evidence")
    expected_phase_keys = (
        set(BOOK_WORKFLOW_PHASES[: phase_rank + 1]) if phase_rank >= 0 else set()
    )
    if not isinstance(evidence, dict) or set(evidence) != expected_phase_keys:
        errors.append(
            f"{prefix}: phase_evidence must contain exactly the reached workflow phases"
        )
        evidence = {}
    phase_fields = {
        "source-landed": {"locator", "sha256"},
        "translated": {"locator", "sha256"},
        "concept-linked": {
            "locator",
            "sha256",
            "book_content_sha256",
            "relation_ids",
        },
        "understanding-enriched": {
            "locator",
            "sha256",
            "source_coverage_sha256",
            "translation_coverage_sha256",
            "source_session_ids",
        },
    }
    for phase in BOOK_WORKFLOW_PHASES[: phase_rank + 1]:
        item = evidence.get(phase)
        if not isinstance(item, dict) or set(item) != phase_fields[phase]:
            errors.append(f"{prefix}: phase_evidence.{phase} fields are invalid")
            continue
        _audit_pinned_evidence(
            f"{prefix}: phase_evidence.{phase}",
            item,
            "locator",
            "sha256",
            errors,
        )
        for digest_field in (
            "book_content_sha256",
            "source_coverage_sha256",
            "translation_coverage_sha256",
        ):
            if digest_field in item and _LOWER_SHA256.fullmatch(_text(item[digest_field])) is None:
                errors.append(
                    f"{prefix}: phase_evidence.{phase}.{digest_field} must be a "
                    "lowercase SHA-256"
                )
        if phase == "understanding-enriched":
            sessions = item.get("source_session_ids")
            if not isinstance(sessions, list) or not sessions or not all(
                isinstance(value, str) and value.strip() for value in sessions
            ):
                errors.append(
                    f"{prefix}: phase_evidence.understanding-enriched.source_session_ids "
                    "must be non-empty"
                )
            source_coverage_sha256 = hashlib.sha256(
                json.dumps(
                    {
                        "source_elements": manifest.get("source_elements"),
                        "owner_bindings": _source_owner_bindings(manifest),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if item.get("source_coverage_sha256") != source_coverage_sha256:
                errors.append(
                    f"{prefix}: understanding-enriched source coverage hash does not match"
                )
            translation_coverage_sha256 = hashlib.sha256(
                json.dumps(
                    manifest.get("source_element_assignments"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if item.get("translation_coverage_sha256") != translation_coverage_sha256:
                errors.append(
                    f"{prefix}: understanding-enriched translation coverage hash does not match"
                )
        if phase == "concept-linked":
            relation_ids = item.get("relation_ids")
            if (
                not isinstance(relation_ids, list)
                or not relation_ids
                or not all(isinstance(value, str) and value.strip() for value in relation_ids)
                or len(set(relation_ids)) != len(relation_ids)
            ):
                errors.append(
                    f"{prefix}: phase_evidence.concept-linked.relation_ids "
                    "must be non-empty and unique"
                )
            reader_entries: list[dict[str, str]] = []
            for node in manifest.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                canonical_id = _text(node.get("canonical_id"))
                actual = pages.get(canonical_id)
                if not canonical_id or actual is None:
                    continue
                reader_entries.append(
                    {
                        "canonical_id": canonical_id,
                        "reader_body": _reader_body(actual[2]),
                    }
                )
            actual_book_content_sha256 = hashlib.sha256(
                json.dumps(
                    reader_entries,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if item.get("book_content_sha256") != actual_book_content_sha256:
                errors.append(
                    f"{prefix}: concept-linked book content hash does not match reader pages"
                )
    return workflow_phase, translation_required


def _source_owner_bindings(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    assignments = manifest.get("source_element_assignments")
    if not isinstance(assignments, list):
        return []
    return sorted(
        (
            _text(item.get("element_id")),
            _text(item.get("owner_id")),
        )
        for item in assignments
        if isinstance(item, dict)
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _canonical_parent(value: object) -> str:
    if not isinstance(value, str):
        return ""
    match = _WIKILINK_TARGET.match(value.strip())
    if match is None:
        return ""
    target = match.group("target")
    if target.startswith("wiki/"):
        target = target[5:]
    return target.removesuffix(".md")


def _reader_body(body: str) -> str:
    body = strip_generated_wiki_views(body)
    return re.sub(r"(?m)^# .+?\s*$", "", body, count=1)


def _audit_source_structure_contract(
    prefix: str,
    manifest: dict[str, Any],
    node_ids: set[str],
    node_order: list[str],
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    errors: list[str],
) -> None:
    """Require exact source-order ownership for front/body/back matter and appendices."""

    evidence = manifest.get("source_structure_inventory_evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"locator", "sha256", "verified_on"}:
        errors.append(
            f"{prefix}: source_structure_inventory_evidence requires locator, sha256, "
            "and verified_on"
        )
    else:
        if not _stable_locator(_text(evidence.get("locator"))):
            errors.append(f"{prefix}: source_structure_inventory_evidence.locator must be stable")
        if _LOWER_SHA256.fullmatch(_text(evidence.get("sha256"))) is None:
            errors.append(
                f"{prefix}: source_structure_inventory_evidence.sha256 must be a lowercase SHA-256"
            )
        if not _text(evidence.get("verified_on")):
            errors.append(f"{prefix}: source_structure_inventory_evidence.verified_on is required")
    raw_elements = manifest.get("source_structure_elements")
    raw_assignments = manifest.get("source_structure_assignments")
    if not isinstance(raw_elements, list) or not raw_elements:
        errors.append(
            f"{prefix}: source_structure_elements must inventory front matter, body, "
            "back matter, appendices, and copyright metadata"
        )
        raw_elements = []
    if not isinstance(raw_assignments, list):
        errors.append(
            f"{prefix}: source_structure_assignments must classify every source structure"
        )
        raw_assignments = []

    elements: dict[str, dict[str, Any]] = {}
    for index, element in enumerate(raw_elements):
        label = f"{prefix}: source_structure_elements[{index}]"
        if not isinstance(element, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(element) != {
            "structure_id",
            "kind",
            "title",
            "source_locator",
            "source_sha256",
        }:
            errors.append(f"{label} fields are invalid")
        structure_id = _text(element.get("structure_id"))
        kind = _text(element.get("kind"))
        title = _text(element.get("title"))
        locator = _text(element.get("source_locator"))
        digest = _text(element.get("source_sha256"))
        if kind not in SOURCE_STRUCTURE_KINDS:
            errors.append(f"{label}.kind is invalid")
        if not title:
            errors.append(f"{label}.title is required")
        if not _stable_locator(locator):
            errors.append(f"{label}.source_locator must be a stable non-machine locator")
        if _LOWER_SHA256.fullmatch(digest) is None:
            errors.append(f"{label}.source_sha256 must be a lowercase SHA-256")
        expected_id = _source_structure_id(kind, title, locator, digest)
        if structure_id != expected_id:
            errors.append(
                f"{label}.structure_id is not the stable source structure identity; "
                f"expected={expected_id}"
            )
        if structure_id in elements:
            errors.append(f"{label}: duplicate source structure {structure_id}")
        elements[structure_id] = element

    assignment_counts: dict[str, int] = {}
    canonical_nodes: set[str] = set()
    canonical_by_structure: dict[str, str] = {}
    for index, assignment in enumerate(raw_assignments):
        label = f"{prefix}: source_structure_assignments[{index}]"
        if not isinstance(assignment, dict):
            errors.append(f"{label} must be an object")
            continue
        structure_id = _text(assignment.get("structure_id"))
        disposition = _text(assignment.get("disposition"))
        assignment_counts[structure_id] = assignment_counts.get(structure_id, 0) + 1
        if assignment_counts[structure_id] > 1:
            errors.append(f"{label}: source structure assigned more than once: {structure_id}")
            continue
        element = elements.get(structure_id)
        if element is None:
            errors.append(f"{label}: assignment references unknown source structure")
            continue
        if disposition == "canonical-node":
            _audit_exact_fields(
                label,
                assignment,
                {"structure_id", "disposition", "canonical_id"},
                errors,
            )
            canonical_id = _text(assignment.get("canonical_id"))
            canonical_by_structure[structure_id] = canonical_id
            if canonical_id not in node_ids:
                errors.append(
                    f"{label}: canonical source structure node is missing: {canonical_id}"
                )
            if canonical_id in canonical_nodes:
                errors.append(f"{label}: multiple structures reuse canonical node: {canonical_id}")
            canonical_nodes.add(canonical_id)
            page = pages.get(canonical_id)
            if page is not None and _text(page[1].get("title")) != _text(element.get("title")):
                errors.append(
                    f"{label}: canonical node title does not match source structure title"
                )
        elif disposition == "metadata-only":
            _audit_exact_fields(
                label,
                assignment,
                {"structure_id", "disposition", "metadata_field", "reason"},
                errors,
            )
            if _text(element.get("kind")) not in {"copyright", "bibliography", "index"}:
                errors.append(
                    f"{label}: only copyright, bibliography, or index may be metadata-only; "
                    "meaningful front/back matter requires a source-order canonical leaf"
                )
            if not _text(assignment.get("metadata_field")) or not _text(assignment.get("reason")):
                errors.append(f"{label}: metadata_field and reason are required")
        else:
            errors.append(f"{label}.disposition must be canonical-node or metadata-only")

    for structure_id in sorted(elements.keys() - assignment_counts.keys()):
        errors.append(f"{prefix}: source structure has no disposition: {structure_id}")
    extra_nodes = node_ids - canonical_nodes
    if extra_nodes:
        errors.append(
            f"{prefix}: manifest nodes lack exact source structure ownership: "
            f"{sorted(extra_nodes)!r}"
        )
    source_order = [
        canonical_by_structure[structure_id]
        for element in raw_elements
        if isinstance(element, dict)
        and (structure_id := _text(element.get("structure_id"))) in canonical_by_structure
    ]
    if source_order != node_order:
        errors.append(
            f"{prefix}: manifest node order does not match source structure order: "
            f"expected={source_order!r} actual={node_order!r}"
        )


def _source_structure_id(kind: str, title: str, locator: str, source_sha256: str) -> str:
    identity = json.dumps(
        {
            "kind": kind,
            "source_locator": locator,
            "source_sha256": source_sha256,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"structure:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _audit_retired_source_section_wrappers(
    prefix: str,
    manifest: dict[str, Any],
    node_ids: set[str],
    leaf_ids: set[str],
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    errors: list[str],
) -> None:
    wrappers = manifest.get("retired_source_section_wrappers")
    if not isinstance(wrappers, list):
        errors.append(f"{prefix}: retired_source_section_wrappers must be an array")
        return
    seen: set[str] = set()
    for index, wrapper in enumerate(wrappers):
        label = f"{prefix}: retired_source_section_wrappers[{index}]"
        if not isinstance(wrapper, dict):
            errors.append(f"{label} must be an object")
            continue
        _audit_exact_fields(
            label,
            wrapper,
            {
                "wrapper_id",
                "map_id",
                "group_label",
                "first_leaf_id",
                "source_locator",
                "relocated_delivery_span",
                "relocated_delivery_span_sha256",
            },
            errors,
        )
        wrapper_id = _text(wrapper.get("wrapper_id"))
        map_id = _text(wrapper.get("map_id"))
        group_label = _text(wrapper.get("group_label"))
        first_leaf_id = _text(wrapper.get("first_leaf_id"))
        if not wrapper_id or wrapper_id in seen:
            errors.append(f"{label}.wrapper_id must be unique and non-empty")
        seen.add(wrapper_id)
        if wrapper_id in node_ids:
            errors.append(f"{label}: retired wrapper remains a manifest node")
        if map_id not in pages:
            errors.append(f"{label}: map page does not exist: {map_id}")
        if first_leaf_id not in leaf_ids:
            errors.append(f"{label}: first_leaf_id must be a terminal source leaf")
        if not group_label:
            errors.append(f"{label}.group_label is required")
        if not _stable_locator(_text(wrapper.get("source_locator"))):
            errors.append(f"{label}.source_locator must be a stable non-machine locator")
        span = wrapper.get("relocated_delivery_span")
        if not isinstance(span, str) or not span.strip():
            errors.append(f"{label}.relocated_delivery_span must be non-empty")
            continue
        digest = hashlib.sha256(span.encode("utf-8")).hexdigest()
        if _text(wrapper.get("relocated_delivery_span_sha256")) != digest:
            errors.append(f"{label}.relocated_delivery_span_sha256 does not match")
        first_leaf = pages.get(first_leaf_id)
        occurrences = _reader_body(first_leaf[2]).count(span) if first_leaf is not None else 0
        if occurrences != 1:
            errors.append(
                f"{label}: relocated wrapper span must occur exactly once in first leaf; "
                f"actual={occurrences}"
            )


def _audit_source_element_contract(
    prefix: str,
    manifest: dict[str, Any],
    node_ids: set[str],
    leaf_ids: set[str],
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    vault: Path,
    errors: list[str],
) -> tuple[dict[tuple[str, str], int], bool]:
    """Validate semantic source inventory and exact reader delivery for every kind."""

    initial_error_count = len(errors)
    inventory_evidence = manifest.get("source_element_inventory_evidence")
    if not isinstance(inventory_evidence, dict):
        errors.append(
            f"{prefix}: source_element_inventory_evidence must pin the complete "
            "example/code extraction"
        )
    else:
        expected_fields = {
            "locator",
            "sha256",
            "verified_on",
            "extraction_method",
            "semantic_unit_policy_sha256",
        }
        if set(inventory_evidence) != expected_fields:
            errors.append(
                f"{prefix}: source_element_inventory_evidence fields must be "
                "locator, sha256, verified_on, extraction_method, and "
                "semantic_unit_policy_sha256"
            )
        locator = _text(inventory_evidence.get("locator"))
        if not locator or locator.startswith(("/", "~")) or ".." in Path(locator).parts:
            errors.append(
                f"{prefix}: source_element_inventory_evidence.locator must be a stable "
                "non-machine locator"
            )
        digest = _text(inventory_evidence.get("sha256"))
        if _LOWER_SHA256.fullmatch(digest) is None:
            errors.append(
                f"{prefix}: source_element_inventory_evidence.sha256 must be a lowercase SHA-256"
            )
        if not _text(inventory_evidence.get("verified_on")):
            errors.append(f"{prefix}: source_element_inventory_evidence.verified_on is required")
        extraction_method = _text(inventory_evidence.get("extraction_method"))
        if extraction_method not in INVENTORY_EXTRACTION_METHODS:
            errors.append(
                f"{prefix}: source_element_inventory_evidence.extraction_method must "
                "describe semantic-unit review, not OCR line counting"
            )
        policy_digest = _text(inventory_evidence.get("semantic_unit_policy_sha256"))
        if _LOWER_SHA256.fullmatch(policy_digest) is None:
            errors.append(
                f"{prefix}: source_element_inventory_evidence.semantic_unit_policy_sha256 "
                "must be a lowercase SHA-256"
            )
    raw_elements = manifest.get("source_elements")
    raw_assignments = manifest.get("source_element_assignments")
    manifest_schema = manifest.get("schema_version")
    workflow_phase = _text(manifest.get("workflow_phase"))
    reader_languages = {
        _text(node.get("canonical_id")): _text(node.get("reader_language"))
        for node in manifest.get("nodes", [])
        if isinstance(node, dict)
    }
    if not isinstance(raw_elements, list):
        errors.append(
            f"{prefix}: source_elements must inventory claim, example, caution, figure, and code"
        )
        raw_elements = []
    if not isinstance(raw_assignments, list):
        errors.append(
            f"{prefix}: source_element_assignments must assign every source element exactly once"
        )
        raw_assignments = []

    elements: dict[str, dict[str, Any]] = {}
    source_identities: set[tuple[str, str, str, str]] = set()
    for index, element in enumerate(raw_elements):
        label = f"{prefix}: source_elements[{index}]"
        if not isinstance(element, dict):
            errors.append(f"{label} must be an object")
            continue
        element_id = _text(element.get("element_id"))
        kind = _text(element.get("kind"))
        semantic_unit = _text(element.get("semantic_unit"))
        source_locator = _text(element.get("source_locator"))
        source_sha256 = _text(element.get("source_sha256"))
        runnable_support = _text(element.get("runnable_support"))
        if not element_id:
            errors.append(f"{label}.element_id is required")
            continue
        if element_id in elements:
            errors.append(f"{label}: duplicate source element {element_id}")
            continue
        if kind not in SOURCE_ELEMENT_KINDS:
            errors.append(f"{label}.kind must be claim, example, caution, figure, or code")
        expected_element_fields = {
            "element_id",
            "kind",
            "semantic_unit",
            "source_locator",
            "source_sha256",
        }
        if kind in {"example", "code"}:
            expected_element_fields.add("runnable_support")
        if set(element) != expected_element_fields:
            errors.append(
                f"{label} fields are invalid for kind={kind or '<missing>'}; "
                "regenerate the semantic source inventory"
            )
        if semantic_unit not in SEMANTIC_UNITS:
            errors.append(f"{label}.semantic_unit is not a supported semantic unit")
        if not _semantic_unit_matches_kind(kind, semantic_unit):
            errors.append(f"{label}: semantic_unit does not match kind={kind}")
        if not _stable_locator(source_locator):
            errors.append(f"{label}.source_locator must be a stable non-machine locator")
        if _LOWER_SHA256.fullmatch(source_sha256) is None:
            errors.append(f"{label}.source_sha256 must be a lowercase SHA-256")
        expected_element_id = _source_element_id(
            kind,
            semantic_unit,
            source_locator,
            source_sha256,
        )
        if element_id and element_id != expected_element_id:
            errors.append(
                f"{label}.element_id is not the stable semantic identity; "
                f"expected={expected_element_id}"
            )
        source_identity = (kind, semantic_unit, source_locator, source_sha256)
        if source_identity in source_identities:
            errors.append(f"{label}: duplicate semantic source unit")
        source_identities.add(source_identity)
        if kind in {"example", "code"} and runnable_support not in RUNNABLE_SUPPORT:
            errors.append(
                f"{label}.runnable_support must be supported, static-exception, or not-applicable"
            )
        if kind == "code" and runnable_support == "not-applicable":
            errors.append(f"{label}: source code cannot declare runnable_support=not-applicable")
        elements[element_id] = element

    assignments: dict[str, dict[str, Any]] = {}
    assignment_counts: dict[str, int] = {}
    element_counts: dict[tuple[str, str], int] = {}
    reader_deliveries: set[tuple[str, str, str]] = set()
    for index, assignment in enumerate(raw_assignments):
        label = f"{prefix}: source_element_assignments[{index}]"
        if not isinstance(assignment, dict):
            errors.append(f"{label} must be an object")
            continue
        element_id = _text(assignment.get("element_id"))
        owner_id = _text(assignment.get("owner_id"))
        delivery = _text(assignment.get("delivery"))
        if not element_id:
            errors.append(f"{label}.element_id is required")
            continue
        assignment_counts[element_id] = assignment_counts.get(element_id, 0) + 1
        if assignment_counts[element_id] > 1:
            errors.append(f"{label}: source element assigned more than once: {element_id}")
            continue
        assignments[element_id] = assignment
        element = elements.get(element_id)
        if element is None:
            errors.append(f"{label}: assignment references unknown source element {element_id}")
            continue
        if owner_id not in node_ids:
            errors.append(f"{label}: owner is not a manifest node: {owner_id or '<missing>'}")
        elif owner_id not in leaf_ids:
            errors.append(f"{label}: source element owner must be a leaf: {owner_id}")
        else:
            kind = _text(element.get("kind"))
            if kind in SOURCE_ELEMENT_KINDS:
                key = (owner_id, COVERAGE_KIND_BY_ELEMENT[kind])
                element_counts[key] = element_counts.get(key, 0) + 1
        kind = _text(element.get("kind"))
        support = _text(element.get("runnable_support"))
        actual_page = pages.get(owner_id)
        page_path = actual_page[0] if actual_page is not None else None
        reader_body = _reader_body(actual_page[2]) if actual_page is not None else ""
        if kind in {"example", "code"} and support == "supported":
            signature = _audit_supported_assignment(
                label,
                assignment,
                delivery,
                reader_body,
                errors,
                vault=vault,
                local_only=manifest_schema == SCHEMA_VERSION
                and isinstance(manifest.get("source_archive"), dict)
                and manifest["source_archive"].get("privacy") == "local-only",
            )
            _audit_unique_reader_delivery(
                label, owner_id, "runnable", signature, reader_deliveries, errors
            )
        elif kind in {"example", "code"} and support == "static-exception":
            signature = _audit_static_exception(
                label,
                element,
                assignment,
                delivery,
                reader_body,
                errors,
                manifest_schema=manifest_schema,
                workflow_phase=workflow_phase,
                reader_language=reader_languages.get(owner_id, ""),
            )
            _audit_unique_reader_delivery(
                label, owner_id, "static-exception", signature, reader_deliveries, errors
            )
        elif kind == "example" and support == "not-applicable":
            signature = _audit_reader_span_assignment(
                label,
                assignment,
                delivery,
                reader_body,
                errors,
                require_reason=True,
            )
            _audit_unique_reader_delivery(
                label, owner_id, kind, signature, reader_deliveries, errors
            )
        elif kind in {"claim", "caution"}:
            signature = _audit_reader_span_assignment(
                label,
                assignment,
                delivery,
                reader_body,
                errors,
            )
            _audit_unique_reader_delivery(
                label, owner_id, kind, signature, reader_deliveries, errors
            )
        elif kind == "figure":
            signature = _audit_figure_assignment(
                label,
                assignment,
                delivery,
                reader_body,
                page_path,
                vault,
                errors,
            )
            _audit_unique_reader_delivery(
                label, owner_id, kind, signature, reader_deliveries, errors
            )

    for element_id in sorted(elements.keys() - assignments.keys()):
        errors.append(f"{prefix}: source element has no leaf assignment: {element_id}")
    for element_id in sorted(assignments.keys() - elements.keys()):
        errors.append(f"{prefix}: assignment has no source inventory element: {element_id}")
    return element_counts, len(errors) == initial_error_count


def _semantic_unit_matches_kind(kind: str, semantic_unit: str) -> bool:
    allowed = {
        "claim": {"paragraph", "definition", "theorem", "table", "procedure", "equation", "list"},
        "example": {"worked-example", "exercise", "scenario"},
        "caution": {"caution"},
        "figure": {"figure"},
        "code": {"code-block"},
    }
    return semantic_unit in allowed.get(kind, set())


def _stable_locator(locator: str) -> bool:
    return bool(locator) and not locator.startswith(("/", "~")) and ".." not in Path(locator).parts


def _source_element_id(
    kind: str,
    semantic_unit: str,
    source_locator: str,
    source_sha256: str,
) -> str:
    identity = json.dumps(
        {
            "kind": kind,
            "semantic_unit": semantic_unit,
            "source_locator": source_locator,
            "source_sha256": source_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _audit_supported_assignment(
    label: str,
    assignment: dict[str, Any],
    delivery: str,
    reader_body: str,
    errors: list[str],
    *,
    vault: Path,
    local_only: bool,
) -> str:
    _audit_exact_fields(
        label,
        assignment,
        {
            "element_id",
            "owner_id",
            "delivery",
            "run_language",
            "run_block_index",
            "verification_evidence",
            "verification_sha256",
        },
        errors,
    )
    if delivery != "run-block":
        errors.append(f"{label}: runnable-supported source element requires delivery=run-block")
        return ""
    language = _text(assignment.get("run_language"))
    block_index = assignment.get("run_block_index")
    if re.fullmatch(r"run-[A-Za-z0-9_-]+", language) is None:
        errors.append(f"{label}.run_language must be a run-* fence language")
    if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 1:
        errors.append(f"{label}.run_block_index must be a positive integer")
    elif language and _fence_count(reader_body, language) < block_index:
        errors.append(
            f"{label}: referenced reader run block does not exist: {language}#{block_index}"
        )
    elif language:
        run_body = _fence_body(reader_body, language, block_index)
        if run_body is not None and not _code_block_has_executable_content(run_body):
            errors.append(f"{label}: referenced reader run block is a comment-only placeholder")
    _audit_pinned_evidence(
        label,
        assignment,
        "verification_evidence",
        "verification_sha256",
        errors,
    )
    if local_only:
        _audit_local_only_execution_evidence(label, assignment, vault, errors)
    return f"{language}#{block_index}" if language and isinstance(block_index, int) else ""


def _audit_local_only_execution_evidence(
    label: str,
    assignment: dict[str, Any],
    vault: Path,
    errors: list[str],
) -> None:
    """Reject a recorded remote runner for local-only private source code."""

    locator = _text(assignment.get("verification_evidence"))
    if not locator or "://" in locator:
        return
    root = vault.parent.resolve()
    path = (root / locator).resolve()
    if root not in path.parents or not path.is_file() or path.suffix.lower() != ".json":
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    providers: set[str] = set()
    externally_transmitted = False

    def visit(value: object) -> None:
        nonlocal externally_transmitted
        if isinstance(value, dict):
            provider = value.get("provider")
            if isinstance(provider, str) and provider.strip():
                providers.add(provider.strip().lower())
            if value.get("external_transmission") is True:
                externally_transmitted = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    disallowed = providers - {"local", "browser-local"}
    if disallowed:
        errors.append(
            f"{label}: local-only private source execution evidence uses prohibited "
            f"provider(s): {', '.join(sorted(disallowed))}"
        )
    if externally_transmitted:
        errors.append(
            f"{label}: local-only private source execution evidence records external transmission"
        )


def _audit_static_exception(
    label: str,
    element: dict[str, Any],
    assignment: dict[str, Any],
    delivery: str,
    reader_body: str,
    errors: list[str],
    *,
    manifest_schema: object,
    workflow_phase: str,
    reader_language: str,
) -> str:
    source_static_fields = {
        "element_id",
        "owner_id",
        "delivery",
        "static_language",
        "static_block_index",
        "static_body_sha256",
        "exception_reason_code",
        "runnable_required",
        "source_locator",
        "source_sha256",
        "original_test_evidence",
        "original_test_sha256",
    }
    if set(assignment) == source_static_fields:
        return _audit_static_source_assignment(
            label,
            element,
            assignment,
            delivery,
            reader_body,
            errors,
            manifest_schema=manifest_schema,
        )

    _audit_exact_fields(
        label,
        assignment,
        {
            "element_id",
            "owner_id",
            "delivery",
            "static_language",
            "static_block_index",
            "exception_reason",
            "original_test_evidence",
            "original_test_sha256",
            "harness_run_language",
            "harness_block_index",
            "harness_fidelity_span",
            "harness_fidelity_span_sha256",
            "harness_verification_evidence",
            "harness_verification_sha256",
        },
        errors,
    )
    if delivery != "static-exception":
        errors.append(f"{label}: unsupported runnable requires delivery=static-exception")
        return ""
    language = _text(assignment.get("static_language"))
    block_index = assignment.get("static_block_index")
    if not language or language.startswith("run-"):
        errors.append(f"{label}.static_language must name a non-run code fence")
    if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 1:
        errors.append(f"{label}.static_block_index must be a positive integer")
    elif language and _fence_count(reader_body, language) < block_index:
        errors.append(
            f"{label}: referenced reader static block does not exist: {language}#{block_index}"
        )
    if not _text(assignment.get("exception_reason")):
        errors.append(f"{label}.exception_reason is required")
    _audit_pinned_evidence(
        label,
        assignment,
        "original_test_evidence",
        "original_test_sha256",
        errors,
    )
    harness_language = _text(assignment.get("harness_run_language"))
    harness_block_index = assignment.get("harness_block_index")
    if re.fullmatch(r"run-[A-Za-z0-9_-]+", harness_language) is None:
        errors.append(f"{label}.harness_run_language must be a run-* fence language")
    if (
        not isinstance(harness_block_index, int)
        or isinstance(harness_block_index, bool)
        or harness_block_index < 1
    ):
        errors.append(f"{label}.harness_block_index must be a positive integer")
    elif harness_language and _fence_count(reader_body, harness_language) < harness_block_index:
        errors.append(
            f"{label}: referenced same-leaf runnable harness does not exist: "
            f"{harness_language}#{harness_block_index}"
        )
    static_body = (
        _fence_body(reader_body, language, block_index)
        if language and isinstance(block_index, int) and not isinstance(block_index, bool)
        else None
    )
    harness_body = (
        _fence_body(reader_body, harness_language, harness_block_index)
        if harness_language
        and isinstance(harness_block_index, int)
        and not isinstance(harness_block_index, bool)
        else None
    )
    if static_body is not None and not _code_block_has_executable_content(static_body):
        errors.append(f"{label}: referenced source code block is a comment-only placeholder")
    _audit_static_harness_fidelity(
        label,
        assignment,
        reader_body,
        static_body,
        harness_body,
        errors,
        manifest_schema=manifest_schema,
        workflow_phase=workflow_phase,
        reader_language=reader_language,
    )
    _audit_pinned_evidence(
        label,
        assignment,
        "harness_verification_evidence",
        "harness_verification_sha256",
        errors,
    )
    if not language or not isinstance(block_index, int):
        return ""
    if not harness_language or not isinstance(harness_block_index, int):
        return ""
    return f"{language}#{block_index}|{harness_language}#{harness_block_index}"


def _audit_static_source_assignment(
    label: str,
    element: dict[str, Any],
    assignment: dict[str, Any],
    delivery: str,
    reader_body: str,
    errors: list[str],
    *,
    manifest_schema: object,
) -> str:
    """Accept an exact source code fence when running it would change its meaning."""

    if manifest_schema != SCHEMA_VERSION:
        errors.append(f"{label}: source-pinned static assignment requires schema_version=3")
    if delivery != "static-exception":
        errors.append(f"{label}: unsupported runnable requires delivery=static-exception")
        return ""

    language = _text(assignment.get("static_language"))
    block_index = assignment.get("static_block_index")
    if not language or language.startswith("run-"):
        errors.append(f"{label}.static_language must name a non-run code fence")
    if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 1:
        errors.append(f"{label}.static_block_index must be a positive integer")
        static_body = None
    else:
        static_body = _fence_body(reader_body, language, block_index) if language else None
        if static_body is None:
            errors.append(
                f"{label}: referenced reader static block does not exist: "
                f"{language}#{block_index}"
            )
    if static_body is not None:
        if not _code_block_has_executable_content(static_body):
            errors.append(f"{label}: referenced source code block is a comment-only placeholder")
        actual_static_sha256 = hashlib.sha256(static_body.encode("utf-8")).hexdigest()
        if _text(assignment.get("static_body_sha256")) != actual_static_sha256:
            errors.append(f"{label}.static_body_sha256 does not match the exact source fence")

    reason_code = _text(assignment.get("exception_reason_code"))
    if reason_code not in STATIC_EXCEPTION_REASON_CODES:
        errors.append(
            f"{label}.exception_reason_code must be one of: "
            f"{', '.join(sorted(STATIC_EXCEPTION_REASON_CODES))}"
        )
    if assignment.get("runnable_required") is not False:
        errors.append(f"{label}.runnable_required must be false")
    if _text(assignment.get("source_locator")) != _text(element.get("source_locator")):
        errors.append(f"{label}.source_locator must match the source element")
    if _text(assignment.get("source_sha256")) != _text(element.get("source_sha256")):
        errors.append(f"{label}.source_sha256 must match the source element")
    _audit_pinned_evidence(
        label,
        assignment,
        "original_test_evidence",
        "original_test_sha256",
        errors,
    )
    if not language or not isinstance(block_index, int):
        return ""
    return f"{language}#{block_index}"


def _audit_static_harness_fidelity(
    label: str,
    assignment: dict[str, Any],
    reader_body: str,
    static_body: str | None,
    harness_body: str | None,
    errors: list[str],
    *,
    manifest_schema: object,
    workflow_phase: str,
    reader_language: str,
) -> None:
    """Reject an unrelated toy harness masquerading as the source-code exercise."""

    span = assignment.get("harness_fidelity_span")
    requires_korean = (
        manifest_schema != SCHEMA_VERSION
        or book_workflow_phase_index(workflow_phase)
        >= book_workflow_phase_index("translated")
        or (workflow_phase == "source-landed" and reader_language == "ko")
    )
    if not isinstance(span, str) or len(re.sub(r"\s+", "", span)) < 40:
        errors.append(
            f"{label}.harness_fidelity_span must explain the preserved source input, "
            "operation, and observable result in substantive prose"
        )
    elif requires_korean and re.search(r"[가-힣]", span) is None:
        errors.append(
            f"{label}.harness_fidelity_span must use substantive Korean prose at "
            "translated-or-later phases"
        )
    else:
        digest = hashlib.sha256(span.encode("utf-8")).hexdigest()
        if _text(assignment.get("harness_fidelity_span_sha256")) != digest:
            errors.append(f"{label}.harness_fidelity_span_sha256 does not match the exact span")
        if reader_body.count(span) != 1:
            errors.append(
                f"{label}: harness fidelity explanation must appear exactly once in the owner page"
            )
    if static_body is None or harness_body is None:
        return
    source_tokens = _significant_code_tokens(static_body)
    harness_tokens = _significant_code_tokens(harness_body)
    required_overlap = 2 if len(source_tokens) >= 4 else 1
    overlap = source_tokens & harness_tokens
    if len(overlap) < required_overlap:
        errors.append(
            f"{label}: same-leaf runnable harness is not source-faithful; "
            f"required significant token overlap={required_overlap} actual={len(overlap)}"
        )


def _significant_code_tokens(body: str) -> set[str]:
    ignored = {
        "and",
        "as",
        "assert",
        "break",
        "class",
        "continue",
        "def",
        "do",
        "else",
        "false",
        "float",
        "for",
        "from",
        "fun",
        "if",
        "import",
        "in",
        "int",
        "is",
        "let",
        "main",
        "new",
        "none",
        "null",
        "or",
        "print",
        "println",
        "return",
        "string",
        "true",
        "val",
        "var",
        "void",
        "while",
        "with",
    }
    identifiers = {
        token.lower()
        for token in re.findall(r"(?u)\b[A-Za-z_][A-Za-z0-9_]{2,}\b", body)
        if token.lower() not in ignored
    }
    literals = set(re.findall(r"(?u)(?:\b\d+(?:\.\d+)?\b|['\"][^'\"\n]{2,}['\"])", body))
    return identifiers | literals


def _code_block_has_executable_content(body: str) -> bool:
    in_block_comment = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
                line = line.split("*/", 1)[1].strip()
                if not line:
                    continue
            else:
                continue
        if line.startswith("/*"):
            if "*/" not in line[2:]:
                in_block_comment = True
                continue
            line = line.split("*/", 1)[1].strip()
            if not line:
                continue
        if line.startswith(("#", "//", "--")):
            continue
        return True
    return False


def _audit_reader_span_assignment(
    label: str,
    assignment: dict[str, Any],
    delivery: str,
    reader_body: str,
    errors: list[str],
    *,
    require_reason: bool = False,
) -> str:
    fields = {
        "element_id",
        "owner_id",
        "delivery",
        "delivery_span",
        "delivery_span_sha256",
    }
    if require_reason:
        fields.add("not_applicable_reason")
    _audit_exact_fields(label, assignment, fields, errors)
    if delivery != "reader-span":
        errors.append(f"{label}: semantic source element requires delivery=reader-span")
        return ""
    span = assignment.get("delivery_span")
    if not isinstance(span, str) or not span.strip():
        errors.append(f"{label}.delivery_span must be a non-empty exact reader span")
        return ""
    digest = _text(assignment.get("delivery_span_sha256"))
    actual_digest = hashlib.sha256(span.encode("utf-8")).hexdigest()
    if digest != actual_digest:
        errors.append(f"{label}.delivery_span_sha256 does not match the exact reader span")
    occurrences = reader_body.count(span)
    if occurrences != 1:
        errors.append(
            f"{label}: delivery_span must occur exactly once in the owner page; "
            f"actual={occurrences}"
        )
    if require_reason and not _text(assignment.get("not_applicable_reason")):
        errors.append(f"{label}.not_applicable_reason is required")
    return actual_digest


def _audit_figure_assignment(
    label: str,
    assignment: dict[str, Any],
    delivery: str,
    reader_body: str,
    page_path: Path | None,
    vault: Path,
    errors: list[str],
) -> str:
    if delivery == "reader-span":
        signature = _audit_reader_span_assignment(
            label,
            assignment,
            delivery,
            reader_body,
            errors,
        )
        span = assignment.get("delivery_span")
        if isinstance(span, str) and span.strip() and not _substantive_figure_reader_span(span):
            errors.append(
                f"{label}: figure reader-span must explain the figure relationship; "
                "a short label-only sentence is not delivery evidence"
            )
        return signature
    if delivery == "figure-mermaid":
        _audit_exact_fields(
            label,
            assignment,
            {
                "element_id",
                "owner_id",
                "delivery",
                "mermaid_block_index",
                "delivery_sha256",
            },
            errors,
        )
        block_index = assignment.get("mermaid_block_index")
        if not isinstance(block_index, int) or isinstance(block_index, bool) or block_index < 1:
            errors.append(f"{label}.mermaid_block_index must be a positive integer")
            return ""
        block = _fence_body(reader_body, "mermaid", block_index)
        if block is None:
            errors.append(f"{label}: referenced reader Mermaid block does not exist")
            return ""
        digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
        if _text(assignment.get("delivery_sha256")) != digest:
            errors.append(f"{label}.delivery_sha256 does not match the Mermaid block")
        return digest
    if delivery == "figure-image":
        _audit_exact_fields(
            label,
            assignment,
            {"element_id", "owner_id", "delivery", "image_target", "delivery_sha256"},
            errors,
        )
        target = _text(assignment.get("image_target"))
        if not _stable_locator(target):
            errors.append(f"{label}.image_target must be a stable relative path")
            return ""
        if f"]({target})" not in reader_body and f"![[{target}]]" not in reader_body:
            errors.append(f"{label}: image_target is not embedded in the owner page")
        if page_path is None:
            errors.append(f"{label}: figure image owner page does not exist")
            return ""
        candidates = [page_path.parent / target, vault / target]
        image_path = next(
            (path for path in candidates if path.is_file() and not path.is_symlink()),
            None,
        )
        if image_path is None:
            errors.append(f"{label}: figure image does not exist as a regular local file")
            return ""
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if _text(assignment.get("delivery_sha256")) != digest:
            errors.append(f"{label}.delivery_sha256 does not match the figure image")
        return digest
    errors.append(f"{label}: figure delivery must be reader-span, figure-mermaid, or figure-image")
    return ""


def _substantive_figure_reader_span(span: str) -> bool:
    compact = re.sub(r"\s+", "", span)
    if len(compact) < 40:
        return False
    label_only = re.fullmatch(
        r"(?:[-*]\s*)?그림\s*[^\s:：의]+\s*(?:의|:|：)\s*[^\n.!?。]{1,60}[.!?。]?",
        span.strip(),
    )
    return label_only is None


def _audit_unique_reader_delivery(
    label: str,
    owner_id: str,
    kind: str,
    signature: str,
    seen: set[tuple[str, str, str]],
    errors: list[str],
) -> None:
    if not signature:
        return
    identity = (owner_id, kind, signature)
    if identity in seen:
        errors.append(
            f"{label}: multiple {kind} source elements reuse the same reader delivery span"
        )
    seen.add(identity)


def _audit_exact_fields(
    label: str,
    payload: dict[str, Any],
    expected: set[str],
    errors: list[str],
) -> None:
    if set(payload) != expected:
        errors.append(f"{label} fields are invalid for delivery={payload.get('delivery')}")


def _audit_pinned_evidence(
    label: str,
    assignment: dict[str, Any],
    locator_field: str,
    sha_field: str,
    errors: list[str],
) -> None:
    locator = _text(assignment.get(locator_field))
    if not locator:
        errors.append(f"{label}.{locator_field} is required")
    elif locator.startswith(("/", "~")) or ".." in Path(locator).parts:
        errors.append(f"{label}.{locator_field} must be a stable non-machine locator")
    digest = _text(assignment.get(sha_field))
    if _LOWER_SHA256.fullmatch(digest) is None:
        errors.append(f"{label}.{sha_field} must be a lowercase SHA-256")


def _fence_count(body: str, language: str) -> int:
    return sum(match.group("language") == language for match in _FENCE_OPEN.finditer(body))


def _fence_body(body: str, language: str, block_index: int) -> str | None:
    matches = [
        match.group("body")
        for match in _FENCE_BLOCK.finditer(body)
        if match.group("language") == language
    ]
    return matches[block_index - 1] if len(matches) >= block_index else None


def _audit_book_map_ui(
    book_id: str,
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    errors: list[str],
    *,
    allowed_ids: set[str] | None = None,
) -> None:
    """Verify the rendered managed map, not only its source metadata contract."""

    for canonical_id, (path, metadata, body) in pages.items():
        if canonical_id != book_id and not canonical_id.startswith(book_id + "/"):
            continue
        if allowed_ids is not None and canonical_id not in allowed_ids:
            continue
        groups = metadata.get("navigation_groups")
        if not isinstance(groups, list) or not groups:
            continue
        reader_body = _reader_body(body).strip()
        if reader_body:
            errors.append(f"{path.name}: UI map contains authored prose: {canonical_id}")
        managed_block = _managed_book_map_block(canonical_id, path, body, errors)
        seen_children: set[str] = set()
        expected_group_labels: list[str] = []
        expected_children: list[str] = []
        for group_index, group in enumerate(groups):
            label = f"{canonical_id}: UI navigation_groups[{group_index}]"
            if not isinstance(group, dict):
                errors.append(f"{label} must be an object")
                continue
            group_label = _text(group.get("label"))
            children = group.get("children")
            if not group_label:
                errors.append(f"{label}.label is required")
            else:
                expected_group_labels.append(group_label)
            if group_label in {
                "하위 키워드",
                "목차",
                "학습 자료",
                "체크포인트",
                "다시 열었을 때",
                "최신 문서",
            }:
                errors.append(f"{label}: UI group label is operational: {group_label}")
            if not isinstance(children, list) or not children:
                errors.append(f"{label}.children must contain direct child IDs")
                continue
            for child in children:
                child_id = _text(child)
                if not child_id:
                    errors.append(f"{label}: child ID is invalid")
                    continue
                if child_id in seen_children:
                    errors.append(f"{label}: duplicate UI child link: {child_id}")
                seen_children.add(child_id)
                expected_children.append(child_id)
                actual = pages.get(child_id)
                if actual is None:
                    errors.append(f"{label}: UI child page does not exist: {child_id}")
                    continue
                _, child_metadata, _ = actual
                if _canonical_parent(child_metadata.get("parent")) != canonical_id:
                    errors.append(f"{label}: UI child is not direct: {child_id}")
                if _text(child_metadata.get("title")) == group_label:
                    errors.append(
                        f"{label}: duplicate-title wrapper child is forbidden: {child_id}"
                    )
                child_groups = child_metadata.get("navigation_groups")
                if canonical_id != book_id and isinstance(child_groups, list) and child_groups:
                    errors.append(
                        f"{label}: descendant-owning source section wrapper is forbidden; "
                        f"retire {child_id}, keep {group_label} as H2, and relocate prose"
                    )
        if managed_block is None:
            continue
        actual_group_labels = [
            match.group("label").strip() for match in _MANAGED_MAP_H2.finditer(managed_block)
        ]
        if actual_group_labels != expected_group_labels:
            errors.append(
                f"{canonical_id}: UI managed group headings are stale: "
                f"expected={expected_group_labels!r}, actual={actual_group_labels!r}"
            )
        actual_children = [
            _normalized_wikilink_target(match.group("target"))
            for match in _MANAGED_MAP_LINK.finditer(managed_block)
        ]
        if actual_children != expected_children:
            errors.append(
                f"{canonical_id}: UI managed direct links are stale: "
                f"expected={expected_children!r}, actual={actual_children!r}"
            )


def _managed_book_map_block(
    canonical_id: str,
    path: Path,
    body: str,
    errors: list[str],
) -> str | None:
    start_count = body.count(CHILDREN_START)
    end_count = body.count(CHILDREN_END)
    if start_count != 1 or end_count != 1:
        errors.append(f"{path.name}: UI managed map block is missing or duplicated: {canonical_id}")
        return None
    start = body.index(CHILDREN_START) + len(CHILDREN_START)
    end = body.index(CHILDREN_END, start)
    if re.search(r"(?m)^##\s+(?:하위 키워드|목차)\s*$", body):
        errors.append(f"{path.name}: UI map contains a generic wrapper heading: {canonical_id}")
    return body[start:end].strip()


def _normalized_wikilink_target(target: str) -> str:
    normalized = target.strip()
    if normalized.startswith("wiki/"):
        normalized = normalized[5:]
    return normalized.removesuffix(".md")


def _lane_audits(errors: list[str]) -> dict[str, BookCoverageLaneAudit]:
    grouped: dict[str, list[str]] = {
        "structure": [],
        "source": [],
        "runnable": [],
        "quality": [],
        "ui": [],
    }
    for error in errors:
        lowered = error.lower()
        if "ui " in lowered or ": ui" in lowered:
            lane = "ui"
        elif any(
            token in lowered
            for token in (
                "runnable",
                "run-*",
                "run block",
                "run_language",
                "run_block",
                "static-exception",
                "static_language",
                "static_block",
                "test_evidence",
                "test_sha256",
                "verification_evidence",
                "verification_sha256",
                "code-verified state",
            )
        ):
            lane = "runnable"
        elif any(
            token in lowered
            for token in (
                "source_element",
                "source element",
                ".coverage",
                "source_locator",
                "source-covered",
                "exact source element",
                "source inventory",
                "source structure",
                "source_structure",
                "semantic source",
                "semantic_unit",
                "delivery_span",
                "delivery span",
                "figure delivery",
                "figure image",
                "source_archive",
                "source archive",
                "source_asset_inventory",
                "source asset inventory",
                "source_prose_verified",
                "phase_evidence.source-landed",
                "mermaid block",
                "front/back matter",
                "metadata-only",
            )
        ):
            lane = "source"
        elif any(
            token in lowered
            for token in (
                "korean_prose_reviewed",
                "reader_language",
                "translated reader body",
                "workflow_phase",
                "translation_required",
                "phase_evidence.translated",
                "phase_evidence.concept-linked",
                "phase_evidence.understanding-enriched",
                "leaf body is empty",
                "workflow or completion metadata",
                "not_applicable_reason",
            )
        ):
            lane = "quality"
        else:
            lane = "structure"
        grouped[lane].append(error)
    scopes = {
        "structure": "edition, TOC, canonical nodes, parent topology",
        "source": (
            "semantic claim/example/caution/figure/code inventory, exact reader delivery, "
            "and exact-one leaf ownership"
        ),
        "runnable": "run-* delivery or pinned static-exception test evidence",
        "quality": (
            "source-language reader fidelity at source-landed and reviewed Korean prose "
            "at translated-or-later phases, without workflow metadata"
        ),
        "ui": "static generated Markdown map surface; Obsidian runtime is separate",
    }
    return {
        lane: BookCoverageLaneAudit(
            scope=scopes[lane],
            complete=not lane_errors,
            error_count=len(lane_errors),
            errors=tuple(lane_errors),
        )
        for lane, lane_errors in grouped.items()
    }
