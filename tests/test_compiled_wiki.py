from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import yaml
from pypdf import PdfWriter

import woon_core.knowledge.compiled_wiki as compiled_wiki_module
from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.book_coverage import audit_book_coverage
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    BookCoverageScopeRevision,
    CompilationAudit,
    CompiledWiki,
    CompiledWikiSettings,
    CuratedRevision,
    StagedBookAsset,
    VerifiedBookPage,
    VerifiedBookUpdateReport,
    _contains_mermaid_color_directive,
    _materialize_book_coverage_scopes,
    _navigation_only_body,
    _normalize_compiled_display_body,
    _relocate_retirement_image_targets,
    _retirement_body,
    _validate_claim_record,
    _validate_source,
)
from woon_core.knowledge.domain import DocumentMetadata, IndexedDocument
from woon_core.knowledge.learning_checkpoint import LearningCheckpoint
from woon_core.knowledge.service import KnowledgeService
from woon_core.knowledge.wiki_tree import split_markdown


class FailOnceIndex(SQLiteFtsSearchIndex):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.fail_next = False

    def rebuild(self, documents: list[IndexedDocument]) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected index failure")
        return super().rebuild(documents)


def test_compiler_never_rewrites_mermaid_shape_evidence() -> None:
    body = """```mermaid
flowchart LR
  broken["score<br/>(T"]
  valid["score<br/>(T, T)"]
```

본문의 shape은 (T, T)다.
"""

    repaired = _normalize_compiled_display_body(body)

    assert 'broken["score<br/>(T"]' in repaired
    assert 'valid["score<br/>(T, T)"]' in repaired
    assert "본문의 shape은 (T, T)다." in repaired


def test_compiler_rejects_mermaid_local_colors_but_allows_semantic_line_styles() -> None:
    colored = """```mermaid
flowchart TD
  failed["실패"]
  style failed fill:#f55,color:#fff
```
"""
    semantic = """```mermaid
flowchart TD
  start["시작"] -.->|"확인 필요"| review["검토"]
```
"""

    assert _contains_mermaid_color_directive(colored)
    assert not _contains_mermaid_color_directive(semantic)

    digest = hashlib.sha256(colored.encode("utf-8")).hexdigest()
    with pytest.raises(WoonError, match="source Mermaid"):
        _validate_source(
            {
                "source_id": "source://fixture/colored",
                "kind": "fixture",
                "locator": "fixture",
                "original_sha256": digest,
                "normalized_sha256": digest,
                "privacy": "local-only",
                "lifecycle": "compiled",
                "purpose": "색상 규칙 회귀 검사",
                "body": colored,
            }
        )
    with pytest.raises(WoonError, match="claim Mermaid"):
        _validate_claim_record(
            {
                "claim_id": "claim://fixture/colored",
                "kind": "fixture",
                "statement": "색 지정이 있는 Mermaid는 거부한다.",
                "status": "accepted",
                "source_ids": ["source://fixture/colored"],
                "markdown": colored,
            }
        )


def test_book_writer_rejects_generated_learning_workflow_before_source_landing(
    tmp_path: Path,
) -> None:
    compiler = CompiledWiki(compiled_settings(tmp_path))
    record = VerifiedBookPage(
        page_id="books/example/chapter-01/1-1",
        title="1.1 원문 절",
        body="원문 내용이다.\n\n## 이전과 다음\n\n- 다음: 다음 절\n",
        statement="원문 절이다.",
        current_use="원문을 읽는다.",
        source_locator="source://book/example#page=1",
        source_sha256="a" * 64,
        frontmatter={},
        expected_revision=None,
    )

    with pytest.raises(WoonError, match="generated learning workflow prose"):
        compiler.validate_book_workflow_pages((record,), "source-landed")


@pytest.mark.parametrize(
    "body",
    (
        "This runnable harness preserves the source example's observable outcome.\n",
        "Chapter 18 source code 23 preserves virtual time without real waiting.\n",
        '```run-kotlin\nfun main() { println("compiled") }\n```\n',
        "```run-kotlin\nclass VirtualTestScope { var currentTime = 0L }\n```\n",
    ),
)
def test_book_writer_rejects_generated_harness_and_synthetic_replacements(
    tmp_path: Path,
    body: str,
) -> None:
    compiler = CompiledWiki(compiled_settings(tmp_path))
    record = VerifiedBookPage(
        page_id="books/example/chapter-01/1-1",
        title="1.1 원문 절",
        body=body,
        statement="원문 절이다.",
        current_use="원문을 읽는다.",
        source_locator="source://book/example#page=1",
        source_sha256="a" * 64,
        frontmatter={},
        expected_revision=None,
    )

    with pytest.raises(WoonError, match="generated learning workflow prose"):
        compiler.validate_book_workflow_pages((record,), "source-landed")


def test_compiler_removes_retired_recent_document_list() -> None:
    body = """설명

<!-- recent-docs:start -->
## 최근 문서

- [[wiki/ai/example|중복 목록]]
<!-- recent-docs:end -->

본문
"""

    repaired = _normalize_compiled_display_body(body)

    assert "recent-docs" not in repaired
    assert "중복 목록" not in repaired
    assert "설명" in repaired and "본문" in repaired


def compiled_settings(vault: Path) -> CompiledWikiSettings:
    return CompiledWikiSettings(
        vault=vault,
        output_root=vault / "wiki",
        sources_path=vault / "catalog/llm-wiki/sources.yaml",
        claims_path=vault / "catalog/llm-wiki/claims.yaml",
        pages_path=vault / "catalog/llm-wiki/pages.yaml",
        curation_path=vault / "catalog/llm-wiki/curation.yaml",
        relations_path=vault / "catalog/llm-wiki/relations.yaml",
        receipts_path=vault / "catalog/llm-wiki/receipts.yaml",
        review_queue_path=vault / "catalog/llm-wiki/review-queue.yaml",
    )


def write_page(vault: Path, relative: str, title: str, body: str) -> None:
    path = vault / "wiki" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        f"title: {title}",
        "summary: 컴파일러 이관 검증 문서.",
        "access: local-only",
    ]
    if relative.startswith("canonical/"):
        canonical_id = Path(relative).with_suffix("").as_posix().removeprefix("canonical/")
        domain = canonical_id.split("/", 1)[0]
        frontmatter = [
            "type: Wiki",
            f"canonical_id: {canonical_id}",
            f"title: {title}",
            f"domain: {domain}",
            "summary: 컴파일러 이관 검증 문서.",
            "status: Canonical",
            "publish: false",
            "access: local-only",
            "difficulty: foundation",
            "prerequisites: []",
            "next_concepts: []",
            "related: []",
            "source_ids: []",
        ]
    path.write_text(
        "---\n" + "\n".join(frontmatter) + f"\n---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def verified_book_frontmatter(
    page_id: str, title: str, parent_id: str | None = None
) -> dict[str, object]:
    frontmatter: dict[str, object] = {
        "type": "Wiki",
        "canonical_id": page_id,
        "title": title,
        "domain": "books",
        "summary": "검증된 책 원문을 한국어 학습 흐름으로 재구성한 문서.",
        "status": "Canonical",
        "publish": False,
        "access": "local-only",
        "difficulty": "foundation",
        "prerequisites": [],
        "next_concepts": [],
        "related": [],
    }
    if parent_id is not None:
        frontmatter["parent"] = f"[[wiki/{parent_id}|상위 목차]]"
    return frontmatter


def atomic_book_service(
    vault: Path,
    *,
    wrapper_body: str = ("## 목차\n\n- [[books/atomic-book/chapter-01|1장 실제 내용]]\n"),
) -> tuple[CompiledWiki, KnowledgeService, FailOnceIndex, VerifiedBookPage, str, str]:
    """Create one root -> empty wrapper -> leaf fixture for atomic update tests."""

    root_id = "books/atomic-book"
    wrapper_id = f"{root_id}/part-01"
    leaf_id = f"{root_id}/chapter-01"
    write_page(vault, "README.md", "Wiki", "<!-- fixture root -->")
    write_page(vault, "books/README.md", "책", "<!-- fixture books hub -->")
    write_page(
        vault,
        "books/programming-language.md",
        "프로그래밍 언어",
        "<!-- fixture book genre -->",
    )
    write_page(vault, f"{root_id}.md", "원자적 책", "기존 책 설명이다.\n")
    write_page(vault, f"{wrapper_id}.md", "1부", wrapper_body)
    write_page(vault, f"{leaf_id}.md", "1장 실제 내용", "실제 장 내용이다.\n")
    compiler = CompiledWiki(compiled_settings(vault))
    compiler.migrate()

    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    payload = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    by_id = {page["page_id"]: page for page in payload["pages"]}
    by_id["books/README"]["frontmatter"].update(
        {
            "node_kind": "hub",
            "parent": "[[wiki/README|Wiki]]",
            "view_mode": "tree",
        }
    )
    by_id["books/programming-language"]["frontmatter"].update(
        {
            "node_kind": "hub",
            "parent": "[[wiki/books/README|책]]",
            "view_mode": "tree",
        }
    )
    by_id[root_id]["frontmatter"]["parent"] = "[[wiki/books/programming-language|프로그래밍 언어]]"
    by_id[root_id]["frontmatter"]["navigation_groups"] = [
        {"label": "1부", "children": [wrapper_id]}
    ]
    by_id[wrapper_id]["frontmatter"]["parent"] = f"[[wiki/{root_id}|원자적 책]]"
    by_id[wrapper_id]["frontmatter"]["navigation_groups"] = [{"label": "장", "children": [leaf_id]}]
    by_id[leaf_id]["frontmatter"]["parent"] = f"[[wiki/{wrapper_id}|1부]]"
    pages_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)

    index = FailOnceIndex(vault / ".local/search.sqlite3")
    service = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        index,
        GitKnowledgeHistory(vault),
        compiled_wiki=compiler,
    )
    service.reindex()
    root = service.get(root_id)
    wrapper = service.get(wrapper_id)
    root_frontmatter = dict(by_id[root_id]["frontmatter"])
    root_frontmatter["node_kind"] = "entity"
    root_frontmatter["entity_kind"] = "book"
    root_frontmatter["content_kind"] = "book"
    root_frontmatter["book_toc_only"] = True
    root_frontmatter["navigation_groups"] = [{"label": "1부", "children": [leaf_id]}]
    record = VerifiedBookPage(
        page_id=root_id,
        title="원자적 책",
        body="",
        statement="책 root는 실제 장을 직접 연결한다.",
        current_use="책의 선형 목차를 탐색할 때 사용한다.",
        source_locator="source://atomic-book#toc",
        source_sha256="d" * 64,
        frontmatter=root_frontmatter,
        expected_revision=root.revision,
    )
    wrapper_body_sha256 = hashlib.sha256(_retirement_body(wrapper_body).encode("utf-8")).hexdigest()
    return compiler, service, index, record, wrapper.revision, wrapper_body_sha256


def atomic_book_coverage_update(
    vault: Path,
    *,
    replacement_parent: str = "books/atomic-book",
) -> tuple[BookCoverageManifestUpdate, Path, bytes]:
    """Create one stale manifest and its source-covered post-retirement replacement."""

    source_locator = "source://atomic-book#chapter-01-paragraph-01"
    source_sha256 = "a" * 64
    element_identity = json.dumps(
        {
            "kind": "claim",
            "semantic_unit": "paragraph",
            "source_locator": source_locator,
            "source_sha256": source_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    element_id = f"claim:{hashlib.sha256(element_identity.encode('utf-8')).hexdigest()}"
    delivery_span = "실제 장 내용이다."
    structure_title = "1장 실제 내용"
    structure_identity = json.dumps(
        {
            "kind": "chapter",
            "source_locator": "source://atomic-book#chapter-01",
            "source_sha256": source_sha256,
            "title": structure_title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    structure_id = f"structure:{hashlib.sha256(structure_identity.encode('utf-8')).hexdigest()}"
    path = vault / "catalog/book-coverage/atomic-book.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = {
        "schema_version": 2,
        "book_id": "books/atomic-book",
        "edition": {"label": "검증판", "source_sha256": "d" * 64},
        "toc_evidence": [{"locator": "source://atomic-book#toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
        "source_structure_inventory_evidence": {
            "locator": "evidence/source-structure.json",
            "sha256": "9" * 64,
            "verified_on": "2026-09-01",
        },
        "source_structure_elements": [
            {
                "structure_id": structure_id,
                "kind": "chapter",
                "title": structure_title,
                "source_locator": "source://atomic-book#chapter-01",
                "source_sha256": source_sha256,
            }
        ],
        "source_structure_assignments": [
            {
                "structure_id": structure_id,
                "disposition": "canonical-node",
                "canonical_id": "books/atomic-book/chapter-01",
            }
        ],
        "retired_source_section_wrappers": [],
        "source_element_inventory_evidence": {
            "locator": "evidence/source-elements.json",
            "sha256": "c" * 64,
            "verified_on": "2026-09-01",
            "extraction_method": "manual-semantic-review",
            "semantic_unit_policy_sha256": "f" * 64,
        },
        "source_elements": [
            {
                "element_id": element_id,
                "kind": "claim",
                "semantic_unit": "paragraph",
                "source_locator": source_locator,
                "source_sha256": source_sha256,
            }
        ],
        "source_element_assignments": [
            {
                "element_id": element_id,
                "owner_id": "books/atomic-book/chapter-01",
                "delivery": "reader-span",
                "delivery_span": delivery_span,
                "delivery_span_sha256": hashlib.sha256(delivery_span.encode("utf-8")).hexdigest(),
            }
        ],
        "nodes": [
            {
                "canonical_id": "books/atomic-book/chapter-01",
                "parent_id": "books/atomic-book/part-01",
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "source://atomic-book#chapter-01",
                "state": "source-covered",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 0, "covered": 0},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 0, "covered": 0},
                },
                "runnable": {"expected": 0, "verified": 0},
                "korean_prose_reviewed": True,
            }
        ],
    }
    original = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(original)
    replacement = copy.deepcopy(current)
    replacement["nodes"][0]["parent_id"] = replacement_parent
    if replacement_parent == "books/atomic-book":
        replacement["retired_source_section_wrappers"] = [
            {
                "wrapper_id": "books/atomic-book/part-01",
                "map_id": "books/atomic-book",
                "group_label": "1부",
                "first_leaf_id": "books/atomic-book/chapter-01",
                "source_locator": "source://atomic-book#part-01",
                "relocated_delivery_span": delivery_span,
                "relocated_delivery_span_sha256": hashlib.sha256(
                    delivery_span.encode("utf-8")
                ).hexdigest(),
            }
        ]
    return (
        BookCoverageManifestUpdate(
            relative_path="catalog/book-coverage/atomic-book.json",
            expected_sha256=hashlib.sha256(original).hexdigest(),
            replacement=replacement,
        ),
        path,
        original,
    )


def atomic_book_leaf_record(
    vault: Path,
    service: KnowledgeService,
    body: str,
) -> VerifiedBookPage:
    leaf_id = "books/atomic-book/chapter-01"
    payload = yaml.safe_load((vault / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8"))
    frontmatter = next(
        page["frontmatter"] for page in payload["pages"] if page["page_id"] == leaf_id
    )
    frontmatter = dict(frontmatter)
    frontmatter["parent"] = "[[wiki/books/atomic-book|원자적 책]]"
    return VerifiedBookPage(
        page_id=leaf_id,
        title="1장 실제 내용",
        body=body,
        statement="퇴역 wrapper의 독자 내용을 실제 장 leaf에 보존한다.",
        current_use="원자적 책의 실제 장 내용을 읽을 때 사용한다.",
        source_locator="source://atomic-book#chapter-01",
        source_sha256="d" * 64,
        frontmatter=frontmatter,
        expected_revision=service.get(leaf_id).revision,
    )


def add_source_free_toc_leaf(compiler: CompiledWiki) -> str:
    """Add one compiled navigation shell with no source or claim records."""

    root_id = "books/atomic-book"
    page_id = f"{root_id}/section-shell"
    sources, claims, pages, curations, _ = compiler._load_inputs()
    frontmatter = verified_book_frontmatter(page_id, "1.1 빈 목차 절", root_id)
    frontmatter.update(
        {
            "content_state": "toc-only",
            "source_ids": [],
            "node_kind": "detail",
            "view_mode": "article",
        }
    )
    pages[page_id] = {
        "page_id": page_id,
        "output_path": f"{page_id}.md",
        "title": "1.1 빈 목차 절",
        "frontmatter": frontmatter,
        "source_ids": [],
        "claim_ids": [],
        "render": {"kind": "toc-only"},
    }
    curations[page_id] = {
        "page_id": page_id,
        "current_use": "원문 절의 목차 위치만 보존한다.",
        "basis": "manual-review",
        "status": "confirmed",
    }
    pages[root_id]["frontmatter"]["navigation_groups"][0]["children"].append(page_id)
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(force=True)
    return page_id


def convert_atomic_wrapper_to_rights_toc_only(compiler: CompiledWiki) -> str:
    """Replace the fixture wrapper's rendered source with rights-only provenance."""

    page_id = "books/atomic-book/part-01"
    source_id = "source://book-rights/books/atomic-book/part-01"
    claim_id = "claim://book-rights/books/atomic-book/part-01"
    source_body = "## 1부\n\n- 1장 실제 내용\n"
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page = pages[page_id]
    previous_source_ids = tuple(page["source_ids"])
    previous_claim_ids = tuple(page["claim_ids"])
    previous_source = copy.deepcopy(sources[previous_source_ids[0]])
    previous_claim = copy.deepcopy(claims[previous_claim_ids[0]])
    for previous_source_id in previous_source_ids:
        del sources[previous_source_id]
    for previous_claim_id in previous_claim_ids:
        del claims[previous_claim_id]

    previous_source.update(
        {
            "source_id": source_id,
            "kind": "book-rights-decision",
            "locator": "source://atomic-book#rights",
            "normalized_sha256": hashlib.sha256(source_body.encode("utf-8")).hexdigest(),
            "title": "원자적 책 1부 권리 제한 목차",
            "purpose": "권리 제한 중에는 원문 목차의 위치만 보존한다.",
            "body": source_body,
        }
    )
    previous_claim.update(
        {
            "claim_id": claim_id,
            "kind": "book-rights-decision",
            "status": "accepted",
            "statement": "1부는 권리 제한으로 목차 전용 상태다.",
            "source_ids": [source_id],
            "markdown": "",
        }
    )
    sources[source_id] = previous_source
    claims[claim_id] = previous_claim
    page["source_ids"] = [source_id]
    page["claim_ids"] = [claim_id]
    page["render"] = {"kind": "toc-only"}
    page["frontmatter"].update(
        {
            "content_state": "toc-only",
            "source_ids": [source_id],
            "state_reason": "blocked-rights",
        }
    )
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(force=True)
    return page_id


def convert_atomic_leaf_to_source_free_toc_only(compiler: CompiledWiki) -> str:
    """Replace the fixture leaf with an empty compiler-owned TOC shell."""

    page_id = "books/atomic-book/chapter-01"
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page = pages[page_id]
    for source_id in page["source_ids"]:
        del sources[source_id]
    for claim_id in page["claim_ids"]:
        del claims[claim_id]
    page["source_ids"] = []
    page["claim_ids"] = []
    page["render"] = {"kind": "toc-only"}
    page["frontmatter"].update(
        {
            "content_state": "toc-only",
            "source_ids": [],
        }
    )
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(force=True)
    return page_id


def atomic_book_new_coverage_update(
    vault: Path,
    *,
    replacement_parent: str = "books/atomic-book",
) -> tuple[BookCoverageManifestUpdate, Path]:
    """Prepare a first coverage manifest without pre-creating its target."""

    existing, path, _ = atomic_book_coverage_update(
        vault,
        replacement_parent=replacement_parent,
    )
    path.unlink()
    return replace(existing, expected_sha256=None), path


def atomic_book_scoped_coverage_update(
    vault: Path,
    *,
    replacement_parent: str = "books/atomic-book",
) -> tuple[BookCoverageManifestUpdate, Path, Path, bytes]:
    """Pin a full TOC while staging only the verified chapter fragment."""

    full_update, base_path, base_bytes = atomic_book_coverage_update(
        vault,
        replacement_parent=replacement_parent,
    )
    scope_root_id = "books/atomic-book/chapter-01"
    relative_path = "catalog/book-coverage-scopes/atomic-book/chapter-01.json"
    replacement = copy.deepcopy(full_update.replacement)
    replacement["coverage_scope"] = {
        "root_id": scope_root_id,
        "base_relative_path": "catalog/book-coverage/atomic-book.json",
        "base_sha256": hashlib.sha256(base_bytes).hexdigest(),
    }
    target = vault / relative_path
    return (
        BookCoverageManifestUpdate(
            mode="merge-scope",
            relative_path=relative_path,
            expected_sha256=None,
            replacement=replacement,
            base_relative_path="catalog/book-coverage/atomic-book.json",
            base_expected_sha256=hashlib.sha256(base_bytes).hexdigest(),
            scope_root_id=scope_root_id,
        ),
        target,
        base_path,
        base_bytes,
    )


def atomic_book_materialized_coverage_update(
    vault: Path,
) -> tuple[BookCoverageManifestUpdate, Path, Path, bytes, bytes]:
    """Create one byte-pinned scope and its exact full-manifest materialization."""

    scoped, scope_path, base_path, base_bytes = atomic_book_scoped_coverage_update(vault)
    empty_assets: list[dict[str, object]] = []
    empty_asset_evidence = {
        "expected_asset_count": 0,
        "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    base = json.loads(base_bytes)
    base["source_asset_inventory"] = empty_assets
    base["source_asset_inventory_evidence"] = empty_asset_evidence
    base_bytes = (json.dumps(base, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    base_path.write_bytes(base_bytes)
    scoped.replacement["source_asset_inventory"] = empty_assets
    scoped.replacement["source_asset_inventory_evidence"] = empty_asset_evidence
    scoped.replacement["coverage_scope"]["base_sha256"] = hashlib.sha256(base_bytes).hexdigest()
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_bytes = (
        json.dumps(scoped.replacement, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    scope_path.write_bytes(scope_bytes)
    replacement = _materialize_book_coverage_scopes(
        base,
        (scoped.replacement,),
        ("books/atomic-book/chapter-01",),
    )
    return (
        BookCoverageManifestUpdate(
            mode="materialize-scopes",
            relative_path="catalog/book-coverage/atomic-book.json",
            expected_sha256=hashlib.sha256(base_bytes).hexdigest(),
            replacement=replacement,
            materialized_scopes=(
                BookCoverageScopeRevision(
                    relative_path="catalog/book-coverage-scopes/atomic-book/chapter-01.json",
                    expected_sha256=hashlib.sha256(scope_bytes).hexdigest(),
                    root_id="books/atomic-book/chapter-01",
                ),
            ),
        ),
        base_path,
        scope_path,
        base_bytes,
        scope_bytes,
    )


def approve_archive(vault: Path, review_id: str, body: str) -> str:
    """Add a human approval fixture bound to the exact normalized body."""

    normalized = (
        "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").split("\n")).strip() + "\n"
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    path = vault / "catalog/llm-wiki/review-queue.yaml"
    existing = (
        yaml.safe_load(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"version": 1, "items": []}
    )
    existing["items"].append(
        {
            "candidate_id": review_id,
            "status": "approved",
            "kind": "manual-archive",
            "input_sha256": digest,
            "approved_by": "test-user",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return review_id


def replace_source_body(source: dict[str, object], body: str) -> None:
    source["body"] = body
    normalized = "\n".join(line.rstrip() for line in body.splitlines()).strip() + "\n"
    source["normalized_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def add_curated_source(vault: Path, *, purpose: str) -> None:
    sources_path = vault / "catalog/llm-wiki/sources.yaml"
    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    body = "기존 학습 문서를 현재 기준으로 다시 편집한 기록입니다.\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    source_id = "source://curated-wiki/os/virtual-memory-v1"
    sources["sources"].append(
        {
            "source_id": source_id,
            "kind": "curated-wiki",
            "locator": "curation/os/virtual-memory-v1",
            "original_sha256": digest,
            "normalized_sha256": digest,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": "가상 메모리",
            "purpose": purpose,
            "body": body,
        }
    )
    pages["pages"][0]["source_ids"].append(source_id)
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def rights_restore_scope_fixture(
    vault: Path,
) -> tuple[CompiledWiki, VerifiedBookPage, VerifiedBookPage, VerifiedBookPage]:
    book_id = "books/example"
    chapter_id = f"{book_id}/chapter-01"
    write_page(vault, f"{book_id}.md", "Example", "기존 책 지도이다.")
    write_page(
        vault,
        f"{chapter_id}.md",
        "1장",
        "기존 장 목차이다.",
    )
    compiler = CompiledWiki(compiled_settings(vault))
    compiler.migrate()

    sources_payload = yaml.safe_load(compiler._settings.sources_path.read_text(encoding="utf-8"))
    claims_payload = yaml.safe_load(compiler._settings.claims_path.read_text(encoding="utf-8"))
    pages_payload = yaml.safe_load(compiler._settings.pages_path.read_text(encoding="utf-8"))
    sources_by_id = {item["source_id"]: item for item in sources_payload["sources"]}
    pages_by_id = {item["page_id"]: item for item in pages_payload["pages"]}
    for page_id in (book_id, chapter_id):
        page = pages_by_id[page_id]
        render_source_id = page["render"]["source_id"]
        current_source = sources_by_id[render_source_id]
        suffix = hashlib.sha256(page_id.encode()).hexdigest()[:16]
        rights_source_id = f"source://book-rights/{book_id}/notice/{suffix}"
        rights_claim_id = f"claim://book-rights/{book_id}/notice/{suffix}"
        rights_source = copy.deepcopy(current_source)
        rights_source.update(
            {
                "source_id": rights_source_id,
                "kind": "book-rights-decision",
                "locator": f"rights://{page_id}",
            }
        )
        rights_claim = {
            "claim_id": rights_claim_id,
            "kind": "book-rights-decision",
            "status": "accepted",
            "statement": f"{page_id} 권리 보류",
            "source_ids": [rights_source_id],
            "markdown": str(rights_source["body"]),
        }
        sources_payload["sources"].append(rights_source)
        claims_payload["claims"].append(rights_claim)
        page["source_ids"].append(rights_source_id)
        page["claim_ids"].append(rights_claim_id)

    compiler._settings.sources_path.write_text(
        yaml.safe_dump(sources_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    compiler._settings.claims_path.write_text(
        yaml.safe_dump(claims_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    compiler._settings.pages_path.write_text(
        yaml.safe_dump(pages_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    def current_record(page_id: str) -> VerifiedBookPage:
        page = pages_by_id[page_id]
        source = sources_by_id[page["render"]["source_id"]]
        output = compiler._settings.output_root / page["output_path"]
        return VerifiedBookPage(
            page_id=page_id,
            title=page["title"],
            body=source["body"],
            statement="현재 권리 페이지를 그대로 유지한다.",
            current_use="현재 권리 페이지를 그대로 유지한다.",
            source_locator="source://book/example#current",
            source_sha256="a" * 64,
            frontmatter=copy.deepcopy(page["frontmatter"]),
            expected_revision=hashlib.sha256(output.read_bytes()).hexdigest(),
        )

    root = current_record(book_id)
    chapter = replace(
        current_record(chapter_id),
        body="",
        frontmatter={
            **copy.deepcopy(pages_by_id[chapter_id]["frontmatter"]),
            "book_toc_only": True,
            "navigation_groups": [{"label": "첫 주제", "children": [f"{chapter_id}/1-1"]}],
        },
    )
    leaf = VerifiedBookPage(
        page_id=f"{chapter_id}/1-1",
        title="1.1 첫 절",
        body="원문 절이다.\n",
        statement="첫 절 원문이다.",
        current_use="첫 절을 읽는다.",
        source_locator="source://book/example#page=1",
        source_sha256="a" * 64,
        frontmatter={
            "access": "local-only",
            "parent": f"[[wiki/{chapter_id}|1장]]",
        },
        expected_revision=None,
    )
    return compiler, root, chapter, leaf


def test_book_rights_restore_preflight_and_apply_reject_same_omission_before_mutation(
    tmp_path: Path,
) -> None:
    compiler, _, chapter, leaf = rights_restore_scope_fixture(tmp_path)
    inputs_before = compiler.snapshot_inputs()
    outputs_before = compiler.snapshot_outputs()

    with pytest.raises(WoonError) as preflight_error:
        compiler.validate_book_workflow_pages((chapter, leaf), "source-landed")
    with pytest.raises(WoonError) as apply_error:
        compiler.apply_verified_book_update(
            (chapter, leaf),
            {},
            {},
            rights_restore_book_id="books/example",
        )

    assert str(apply_error.value) == str(preflight_error.value)
    assert "must replace every surviving rights page: books/example" in str(preflight_error.value)
    assert compiler.snapshot_inputs() == inputs_before
    assert compiler.snapshot_outputs() == outputs_before


def test_book_rights_restore_requires_exact_carry_forward_and_allows_one_scope(
    tmp_path: Path,
) -> None:
    compiler, root, chapter, leaf = rights_restore_scope_fixture(tmp_path)

    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler.validate_book_workflow_pages(
            (replace(root, body="변경하면 안 된다.\n"), chapter, leaf),
            "source-landed",
        )

    compiler.validate_book_workflow_pages(
        (root, chapter, leaf),
        "source-landed",
    )


def test_book_rights_workflow_allows_only_explicit_retirement_survivor_change(
    tmp_path: Path,
) -> None:
    compiler, root, chapter, leaf = rights_restore_scope_fixture(tmp_path)
    changed_root = replace(root, body="기존 절을 합친 새 책 본문이다.\n")

    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler.validate_book_workflow_pages(
            (changed_root, chapter, leaf),
            "source-landed",
        )

    compiler.validate_book_workflow_pages(
        (changed_root, chapter, leaf),
        "source-landed",
        replacement_survivor_ids={changed_root.page_id},
    )


def test_book_rights_workflow_excludes_only_declared_retired_page_from_survivors(
    tmp_path: Path,
) -> None:
    compiler, root, chapter, leaf = rights_restore_scope_fixture(tmp_path)

    compiler.validate_book_workflow_pages(
        (root, leaf),
        "source-landed",
        retired_page_ids={chapter.page_id},
    )

    with pytest.raises(WoonError, match="must replace every surviving rights page"):
        compiler.validate_book_workflow_pages(
            (root, leaf),
            "source-landed",
        )


def test_book_rights_workflow_rejects_promoted_page_declared_as_retired(tmp_path: Path) -> None:
    compiler, root, chapter, leaf = rights_restore_scope_fixture(tmp_path)

    with pytest.raises(WoonError, match="cannot both promote and retire"):
        compiler.validate_book_workflow_pages(
            (root, chapter, leaf),
            "source-landed",
            retired_page_ids={chapter.page_id},
        )


def legacy_rights_toc_records(
    compiler: CompiledWiki,
) -> tuple[VerifiedBookPage, VerifiedBookPage]:
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page_ids = ("books/example", "books/example/chapter-01")
    for page_id in page_ids:
        page = pages[page_id]
        rights_source_id = next(
            source_id
            for source_id in page["source_ids"]
            if source_id.startswith("source://book-rights/")
        )
        rights_claim_id = next(
            claim_id
            for claim_id in page["claim_ids"]
            if claim_id.startswith("claim://book-rights/")
        )
        sources[rights_source_id]["original_sha256"] = "a" * 64
        sources[rights_source_id]["purpose"] = "권리 제한 목차 shell을 검증한다."
        page["source_ids"] = [rights_source_id]
        page["claim_ids"] = [rights_claim_id]
        page["frontmatter"]["source_ids"] = [rights_source_id]
        page["render"] = {"kind": "source-body", "source_id": rights_source_id}
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(force=True)

    def record(page_id: str) -> VerifiedBookPage:
        page = pages[page_id]
        source_id = page["render"]["source_id"]
        output = compiler._settings.output_root / page["output_path"]
        return VerifiedBookPage(
            page_id=page_id,
            title=page["title"],
            body=sources[source_id]["body"],
            statement="현재 권리 페이지를 그대로 유지한다.",
            current_use="현재 권리 페이지를 그대로 유지한다.",
            source_locator="source://book/example#current",
            source_sha256="a" * 64,
            frontmatter=copy.deepcopy(page["frontmatter"]),
            expected_revision=hashlib.sha256(output.read_bytes()).hexdigest(),
        )

    return record(page_ids[0]), record(page_ids[1])


def test_normal_full_replacement_allows_only_legacy_rights_toc_normalization(
    tmp_path: Path,
) -> None:
    compiler, _, _, _ = rights_restore_scope_fixture(tmp_path)
    root, chapter = legacy_rights_toc_records(compiler)
    normalized_root = replace(
        root,
        body="",
        frontmatter={
            **copy.deepcopy(root.frontmatter),
            "book_toc_only": True,
            "content_state": "toc-only",
            "ordered_reader_sections": [
                {"kind": "toc-heading", "label": "A.1 첫째 절"},
                {"kind": "navigation-group", "label": "A.2 둘째 절"},
            ],
            "navigation_groups": [{"label": "A.2 둘째 절", "children": [chapter.page_id]}],
        },
    )

    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler.validate_book_workflow_pages(
            (normalized_root, chapter),
            "source-landed",
        )

    compiler.validate_book_workflow_pages(
        (normalized_root, chapter),
        "source-landed",
        allow_legacy_toc_normalization=True,
    )

    invalid_heading = replace(
        normalized_root,
        frontmatter={
            **copy.deepcopy(normalized_root.frontmatter),
            "ordered_reader_sections": [{"kind": "source-body", "label": "A.1 첫째 절"}],
        },
    )
    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler.validate_book_workflow_pages(
            (invalid_heading, chapter),
            "toc-indexed",
            allow_legacy_toc_normalization=True,
        )


def test_toc_indexed_rights_restore_allows_only_legacy_appendix_toc_normalization(
    tmp_path: Path,
) -> None:
    compiler, _, _, _ = rights_restore_scope_fixture(tmp_path)
    root, _ = legacy_rights_toc_records(compiler)
    sources, _, pages, _, _ = compiler._load_inputs()
    appendix_id = "books/example/appendix-a"
    current_page = copy.deepcopy(pages[root.page_id])
    current_page["page_id"] = appendix_id
    current_page["title"] = "부록 A"
    candidate = replace(
        root,
        page_id=appendix_id,
        title="부록 A",
        body="",
        frontmatter={
            **copy.deepcopy(root.frontmatter),
            "book_toc_only": True,
            "content_state": "toc-only",
            "ordered_reader_sections": [{"kind": "toc-heading", "label": "A.1 첫째 절"}],
        },
    )
    appendix_pages = {appendix_id: current_page}

    carry_forward = compiler._validate_book_rights_restore_records(
        (candidate,),
        sources,
        appendix_pages,
        expected_book_id="books/example",
        allow_legacy_toc_normalization=True,
    )

    # A validated legacy rights shell must be rewritten as the explicit empty
    # toc-only page instead of preserving its old placeholder body.
    assert carry_forward == set()

    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler._validate_book_rights_restore_records(
            (replace(candidate, body="임의로 바꾼 본문이다.\n"),),
            sources,
            appendix_pages,
            expected_book_id="books/example",
            allow_legacy_toc_normalization=True,
        )


def test_rights_restore_writer_enables_legacy_toc_normalization_only_at_toc_indexed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, root, _, _ = rights_restore_scope_fixture(tmp_path)
    observed: list[bool] = []

    class ValidationReached(RuntimeError):
        pass

    def capture_validation(*args: object, **kwargs: object) -> set[str]:
        del args
        observed.append(bool(kwargs.get("allow_legacy_toc_normalization")))
        raise ValidationReached

    monkeypatch.setattr(
        compiler,
        "_validate_book_rights_restore_records",
        capture_validation,
    )
    for phase, expected in (("toc-indexed", True), ("source-landed", False)):
        coverage = BookCoverageManifestUpdate(
            mode="replace",
            relative_path=f"catalog/book-coverage/{phase}.json",
            expected_sha256=None,
            replacement={
                "schema_version": 3,
                "book_id": "books/example",
                "workflow_phase": phase,
                "translation_required": False,
            },
        )

        with pytest.raises(ValidationReached):
            compiler.apply_verified_book_update(
                (root,),
                {},
                {},
                coverage,
                rights_restore_book_id="books/example",
            )

        assert observed[-1] is expected


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("parent", "changed its body"),
        ("source", "changed its body"),
        ("revision", "changed after review"),
    ],
)
def test_legacy_rights_toc_normalization_rejects_identity_or_revision_changes(
    tmp_path: Path,
    change: str,
    message: str,
) -> None:
    compiler, _, _, _ = rights_restore_scope_fixture(tmp_path)
    root, chapter = legacy_rights_toc_records(compiler)
    frontmatter = {
        **copy.deepcopy(root.frontmatter),
        "book_toc_only": True,
        "content_state": "toc-only",
    }
    expected_revision = root.expected_revision
    source_sha256 = root.source_sha256
    if change == "parent":
        frontmatter["parent"] = "[[wiki/books/other|다른 책]]"
    elif change == "source":
        source_sha256 = "b" * 64
    else:
        expected_revision = "f" * 64
    candidate = replace(
        root,
        body="",
        frontmatter=frontmatter,
        source_sha256=source_sha256,
        expected_revision=expected_revision,
    )

    with pytest.raises(WoonError, match=message):
        compiler.validate_book_workflow_pages(
            (candidate, chapter),
            "source-landed",
            allow_legacy_toc_normalization=True,
        )


def test_legacy_rights_toc_normalization_rejects_new_descendants(
    tmp_path: Path,
) -> None:
    compiler, _, _, leaf = rights_restore_scope_fixture(tmp_path)
    root, chapter = legacy_rights_toc_records(compiler)
    normalized_root = replace(
        root,
        body="",
        frontmatter={
            **copy.deepcopy(root.frontmatter),
            "book_toc_only": True,
            "content_state": "toc-only",
        },
    )

    with pytest.raises(WoonError, match="carry-forward changed its body"):
        compiler.validate_book_workflow_pages(
            (normalized_root, chapter, leaf),
            "source-landed",
            allow_legacy_toc_normalization=True,
        )


def test_book_rights_scan_skips_only_explicit_source_free_toc_pages(
    tmp_path: Path,
) -> None:
    compiler, _, _, _ = rights_restore_scope_fixture(tmp_path)
    root, chapter = legacy_rights_toc_records(compiler)
    sources, _, pages, _, _ = compiler._load_inputs()
    toc_page_id = "books/example/appendix-a"
    pages[toc_page_id] = {
        "page_id": toc_page_id,
        "output_path": f"{toc_page_id}.md",
        "title": "부록 A",
        "frontmatter": {"content_state": "toc-only"},
        "source_ids": [],
        "claim_ids": [],
        "render": {"kind": "toc-only"},
    }

    compiler._validate_book_rights_restore_records(
        (root, chapter),
        sources,
        pages,
    )

    pages[toc_page_id]["render"] = {
        "kind": "source-body",
        "source_id": "source://missing",
    }
    with pytest.raises(WoonError, match="page source_ids must be a non-empty string list"):
        compiler._validate_book_rights_restore_records(
            (root, chapter),
            sources,
            pages,
        )


def test_book_dry_run_uses_resolved_temporary_vault_and_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "production"
    (vault / "catalog").mkdir(parents=True)
    (vault / "catalog/sentinel.txt").write_text("production", encoding="utf-8")
    (vault / "wiki").mkdir()
    compiler = CompiledWiki(compiled_settings(vault))
    seen: dict[str, Path] = {}

    def fake_install(self: CompiledWiki, assets: tuple[StagedBookAsset, ...]) -> None:
        del self, assets

    def fake_apply(
        self: CompiledWiki,
        *args: object,
        **kwargs: object,
    ) -> VerifiedBookUpdateReport:
        del args, kwargs
        seen["vault"] = self.vault
        assert self.vault == self.vault.resolve()
        (self.vault / "catalog/sentinel.txt").write_text("dry-run", encoding="utf-8")
        return VerifiedBookUpdateReport(0, 0, 0, 0, (), (), ())

    monkeypatch.setattr(CompiledWiki, "install_staged_book_assets", fake_install)
    monkeypatch.setattr(CompiledWiki, "apply_verified_book_update", fake_apply)
    coverage = BookCoverageManifestUpdate(
        mode="replace",
        relative_path="catalog/book-coverage/example.json",
        expected_sha256=None,
        replacement={"book_id": "books/example"},
    )

    compiler.dry_run_verified_book_update((), {}, {}, coverage)

    assert (vault / "catalog/sentinel.txt").read_text(encoding="utf-8") == "production"
    assert not seen["vault"].exists()


def test_migration_creates_reproducible_source_schema_and_incremental_build(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))

    migrated = compiler.migrate()
    audit = compiler.audit()

    assert migrated.migrated == 2
    assert migrated.compiled == 2
    assert audit.complete
    output = (tmp_path / "wiki/os/virtual-memory.md").read_text(encoding="utf-8")
    assert "llm_wiki:" in output
    assert compiler.compile().compiled == 0

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    source = next(item for item in sources["sources"] if item["locator"] == "os/virtual-memory.md")
    replace_source_body(source, "페이지 폴트와 보조 페이지 테이블을 함께 처리한다.\n")
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = compiler.compile()

    assert report.compiled == 1
    assert report.page_ids == ("os/virtual-memory",)
    assert "보조 페이지 테이블" in (tmp_path / "wiki/os/virtual-memory.md").read_text(
        encoding="utf-8"
    )


def test_compile_preserves_single_wiki_context_and_audits_it(tmp_path: Path) -> None:
    write_page(tmp_path, "os/queue.md", "큐", "근거로 확인한 설명이다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    path = tmp_path / "wiki/os/queue.md"
    context = """

## 현재 이해

<!-- woon-wiki-current:start -->
대화에서 다시 생각한 내용이다.
<!-- woon-wiki-current:end -->

## 한 줄 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""
    path.write_text(path.read_text(encoding="utf-8").rstrip() + context, encoding="utf-8")

    report = compiler.compile(force=True)
    output = path.read_text(encoding="utf-8")

    assert report.compiled == 1
    assert "근거로 확인한 설명이다." in output
    assert "대화에서 다시 생각한 내용이다." in output
    assert "큐를 다시 학습했다." in output
    assert compiler.audit().complete
    assert compiler.audit().complete


def test_compile_prunes_retired_page_receipt_without_rewriting_current_page(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/queue.md", "큐", "근거로 확인한 설명이다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    receipts_path = tmp_path / "catalog/llm-wiki/receipts.yaml"
    payload = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    retired = dict(payload["receipts"][0])
    retired["page_id"] = "retired/second-wiki"
    payload["receipts"].append(retired)
    receipts_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    page = tmp_path / "wiki/os/queue.md"
    before = page.read_bytes()

    report = compiler.compile()
    current = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))

    assert report.compiled == 0
    assert page.read_bytes() == before
    assert [item["page_id"] for item in current["receipts"]] == ["os/queue"]


def test_managed_context_refreshes_exact_output_receipt_without_losing_context(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/queue.md", "큐", "근거로 확인한 설명이다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    path = tmp_path / "wiki/os/queue.md"
    context = """

## 현재 이해

<!-- woon-wiki-current:start -->
대화에서 다시 생각한 내용이다.
<!-- woon-wiki-current:end -->

## 한 줄 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""
    path.write_text(path.read_text(encoding="utf-8").rstrip() + context, encoding="utf-8")

    assert not compiler.audit().complete
    assert any("output bytes differ" in error for error in compiler.audit().errors)
    assert compiler.compile().compiled == 1
    assert compiler.audit().complete
    assert "대화에서 다시 생각한 내용이다." in path.read_text(encoding="utf-8")

    path.write_text(
        path.read_text(encoding="utf-8").replace("근거로 확인한 설명", "임의로 바꾼 설명"),
        encoding="utf-8",
    )
    audit = compiler.audit()
    assert not audit.complete
    assert any("output bytes differ" in error for error in audit.errors)


def test_audit_fails_when_receipt_output_hash_does_not_match_current_bytes(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/queue.md", "큐", "근거로 확인한 설명이다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    receipts_path = tmp_path / "catalog/llm-wiki/receipts.yaml"
    payload = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    payload["receipts"][0]["output_sha256"] = "0" * 64
    receipts_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    audit = compiler.audit()

    assert not audit.complete
    assert any("output bytes differ" in error for error in audit.errors)


def test_current_use_curation_is_separate_from_legacy_source_purpose(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )
    assert "purpose" not in sources["sources"][0]

    curation_path = tmp_path / "catalog/llm-wiki/curation.yaml"
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    assert curation == {
        "page_id": "os/virtual-memory",
        "current_use": (
            "가상 메모리 내용을 다시 학습하거나 설명할 때, "
            "관련 개념과 자료를 찾는 기준으로 사용한다."
        ),
        "basis": "legacy-page-metadata",
        "status": "provisional",
    }

    curation["current_use"] = "가상 메모리의 주소 변환 흐름을 설명할 때 기준으로 사용한다."
    curation["basis"] = "manual-review"
    curation["status"] = "confirmed"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.compile().compiled == 1
    output = (tmp_path / "wiki/os/virtual-memory.md").read_text(encoding="utf-8")
    assert "purpose: 가상 메모리의 주소 변환 흐름을 설명할 때 기준으로 사용한다." in output
    assert "purpose_basis: manual-review" in output
    assert "purpose_status:" not in output
    curation["status"] = "provisional"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    assert compiler.compile().compiled == 0
    assert compiler.audit().complete


def test_curated_revision_preserves_legacy_source_and_archives_prior_curated_body(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "os/virtual-memory.md",
        "가상 메모리",
        "## 시작\n\n페이지 폴트를 처리한다.\n",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    first = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="os/virtual-memory",
                body="## 시작\n\n페이지 폴트가 나면 필요한 페이지를 찾는다.\n",
                statement="페이지 폴트가 발생했을 때 필요한 페이지를 찾는 흐름을 설명한다.",
                current_use="페이지 폴트가 필요한 페이지를 찾는 흐름을 다시 설명할 때 사용한다.",
            ),
        )
    )

    assert first.curated == 1
    assert first.compiled == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    legacy = next(item for item in sources if item["kind"] == "legacy-wiki")
    curated = next(item for item in sources if item["kind"] == "curated-wiki")
    assert legacy["lifecycle"] == "compiled"
    assert curated["body"].startswith("## 시작")
    page = yaml.safe_load((tmp_path / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8"))[
        "pages"
    ][0]
    assert legacy["source_id"] in page["source_ids"]
    assert page["render"] == {"kind": "source-body", "source_id": curated["source_id"]}
    curation = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )["curations"][0]
    assert curation["basis"] == "curated-revision"
    assert curation["status"] == "confirmed"
    assert curation["current_use"] == (
        "페이지 폴트가 필요한 페이지를 찾는 흐름을 다시 설명할 때 사용한다."
    )
    assert compiler.audit().complete

    second = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="os/virtual-memory",
                body="## 시작\n\n페이지 폴트가 나면 주소 변환에 필요한 페이지를 찾는다.\n",
                statement="페이지 폴트가 주소 변환에 필요한 페이지를 찾게 하는 흐름을 설명한다.",
            ),
        )
    )

    assert second.compiled == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    archived = next(item for item in sources if item["source_id"] == curated["source_id"])
    assert archived["lifecycle"] == "archived"
    assert archived["superseded_by"].startswith("source://curated-wiki/")
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    assert any(
        item["status"] == "superseded" and item["kind"] == "curated-document" for item in claims
    )
    assert compiler.audit().complete


def test_verified_book_promotion_creates_child_and_archives_prior_revision(
    tmp_path: Path,
) -> None:
    root_id = "books/kotlin-in-action"
    child_id = f"{root_id}/chapter-01"
    write_page(
        tmp_path,
        f"{root_id}.md",
        "코틀린 인 액션 2판",
        "책의 실제 목차를 따른다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    first = VerifiedBookPage(
        page_id=child_id,
        title="1장 코틀린이란 무엇이며 왜 필요한가?",
        body="## 문제 장면\n\nKotlin이 Java와 함께 동작하는 이유를 추적한다.\n",
        statement="Kotlin과 Java의 상호운용 경계를 설명한다.",
        current_use="Kotlin의 설계 목적과 JVM 경계를 학습할 때 사용한다.",
        source_locator="source://kotlin-in-action-2e#pdf-page=25",
        source_sha256="a" * 64,
        frontmatter=verified_book_frontmatter(
            child_id, "1장 코틀린이란 무엇이며 왜 필요한가?", root_id
        ),
    )

    created = compiler.promote_verified_book_pages((first,))

    assert created.curated == 1
    assert created.compiled == 1
    output = tmp_path / "wiki/books/kotlin-in-action/chapter-01.md"
    assert "Kotlin이 Java와 함께 동작하는 이유" in output.read_text(encoding="utf-8")
    assert compiler.audit().complete

    repeated = compiler.promote_verified_book_pages((first,))
    assert repeated.curated == 0
    assert repeated.compiled == 0

    with pytest.raises(
        WoonError,
        match="textual callouts must use ① through ⑩",
    ):
        compiler.promote_verified_book_pages(
            (
                replace(
                    first,
                    body="## 문제 장면\n\n```kotlin\nval answer = 42 // ❶\n```\n",
                ),
            )
        )

    second = replace(
        first,
        body="## 문제 장면\n\nKotlin과 Java가 같은 JVM type을 공유하는 경계를 추적한다.\n",
        statement="Kotlin과 Java가 JVM type을 공유하는 경계를 설명한다.",
    )
    updated = compiler.promote_verified_book_pages((second,))

    assert updated.curated == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    book_sources = [item for item in sources if item["kind"] == "verified-book"]
    assert {item["lifecycle"] for item in book_sources} == {"archived", "compiled"}
    archived_source = next(item for item in book_sources if item["lifecycle"] == "archived")
    assert archived_source["superseded_by"].startswith(f"source://verified-book/{child_id}/")
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    book_claims = [item for item in claims if item["kind"] == "verified-book-summary"]
    assert {item["status"] for item in book_claims} == {"accepted", "superseded"}
    assert compiler.audit().complete


def test_verified_book_promotion_uses_page_bound_identity_for_shared_locator(
    tmp_path: Path,
) -> None:
    root_id = "books/shared-source-book"
    write_page(tmp_path, f"{root_id}.md", "공유 원문 책", "책의 실제 목차를 따른다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    shared_locator = "https://example.com/books/shared-chapter/"
    records = tuple(
        VerifiedBookPage(
            page_id=f"{root_id}/section-{number}",
            title=f"{number}절",
            body=f"{number}절의 서로 다른 원문 요소를 설명한다.\n",
            statement=f"{number}절의 원문 요소를 설명한다.",
            current_use=f"{number}절을 학습할 때 사용한다.",
            source_locator=shared_locator,
            source_sha256="e" * 64,
            frontmatter={
                **verified_book_frontmatter(f"{root_id}/section-{number}", f"{number}절", root_id),
                # A book builder previously copied the common URL here.
                "source_ids": [shared_locator],
            },
        )
        for number in (1, 2)
    )

    report = compiler.promote_verified_book_pages(records)

    assert report.compiled == 2
    metadata_source_ids: list[str] = []
    for record in records:
        text = (tmp_path / "wiki" / f"{record.page_id}.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(text.split("---", 2)[1])
        source_ids = frontmatter["source_ids"]
        assert len(source_ids) == 1
        assert source_ids[0].startswith(f"source://verified-book/{record.page_id}/")
        assert shared_locator not in source_ids
        metadata_source_ids.extend(source_ids)
    assert len(set(metadata_source_ids)) == 2

    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    verified = [item for item in sources if item["kind"] == "verified-book"]
    assert len(verified) == 2
    assert {item["locator"] for item in verified} == {shared_locator}
    assert {item["source_id"] for item in verified} == set(metadata_source_ids)
    assert compiler.audit().complete


def test_verified_book_promotion_preserves_prior_catalog_provenance(
    tmp_path: Path,
) -> None:
    root_id = "books/provenance-book"
    child_id = f"{root_id}/chapter-01"
    write_page(tmp_path, f"{root_id}.md", "근거 보존 책", "책의 실제 목차를 따른다.")
    write_page(
        tmp_path,
        f"{child_id}.md",
        "1장 기존 내용",
        "이관 전 원문 근거를 가진 내용이다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    migrated_pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))["pages"]
    migrated_child = next(item for item in migrated_pages if item["page_id"] == child_id)
    legacy_source_id = migrated_child["render"]["source_id"]
    locator = "https://example.com/books/provenance/chapter-01/"
    record = VerifiedBookPage(
        page_id=child_id,
        title="1장 검증된 내용",
        body="검증된 원문을 바탕으로 기존 설명을 갱신한다.\n",
        statement="검증된 1장 내용을 설명한다.",
        current_use="검증된 1장을 학습할 때 사용한다.",
        source_locator=locator,
        source_sha256="f" * 64,
        frontmatter={
            **verified_book_frontmatter(child_id, "1장 검증된 내용", root_id),
            "source_ids": [locator],
        },
    )

    compiler.promote_verified_book_pages((record,))

    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))["pages"]
    child = next(item for item in pages if item["page_id"] == child_id)
    verified_source_id = child["render"]["source_id"]
    assert legacy_source_id in child["source_ids"]
    assert verified_source_id in child["source_ids"]
    assert locator not in child["source_ids"]
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    legacy = next(item for item in sources if item["source_id"] == legacy_source_id)
    verified = next(item for item in sources if item["source_id"] == verified_source_id)
    assert legacy["lifecycle"] == "compiled"
    assert verified["locator"] == locator

    text = (tmp_path / "wiki" / f"{child_id}.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["source_ids"] == [verified_source_id]
    assert compiler.audit().complete


def test_verified_book_service_requires_current_revision_and_rolls_back_index_failure(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    index.fail_next = True

    with pytest.raises(RuntimeError, match="injected index failure"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
            coverage_update,
        )

    assert service.get(wrapper_id) is not None
    assert compiler.audit().complete

    service.apply_verified_book_update(
        (record,),
        {wrapper_id: "books/atomic-book"},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
        coverage_update,
    )
    current = service.get("books/atomic-book")
    stale = replace(record, expected_revision="stale-revision")
    with pytest.raises(WoonError, match="changed after it was read"):
        service.promote_verified_book_pages((stale,), coverage_update)
    assert service.get("books/atomic-book").revision == current.revision


def test_verified_book_asset_preflight_rejects_tampered_staging_bytes(
    tmp_path: Path,
) -> None:
    _, service, _, record, _, _ = atomic_book_service(tmp_path)
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    intended = b"original source image"
    digest = hashlib.sha256(intended).hexdigest()
    source_locator = "source://atomic-book#images/figure.png"
    relative = "wiki/private/_sources/knowledge/local-only/atomic-book/images/figure.png"
    coverage_update.replacement["source_asset_inventory"] = [
        {
            "asset_id": f"asset:{digest}",
            "source_locator": source_locator,
            "source_sha256": digest,
            "archive_relative_path": relative,
            "archive_sha256": digest,
            "extraction_kind": "embedded-original",
            "crop_provenance": None,
        }
    ]
    staging = tmp_path / "staged-source-assets/figure.png"
    staging.parent.mkdir()
    staging.write_bytes(b"tampered source image")
    asset = StagedBookAsset(
        staging_path=staging,
        archive_relative_path=relative,
        sha256=digest,
        size=len(intended),
        provenance="embedded-original-byte-identical",
        source_entry_locator=source_locator,
    )

    with pytest.raises(WoonError, match="source size or hash does not match"):
        service.preflight_verified_book_update((record,), {}, {}, {}, coverage_update, (asset,))

    assert not (tmp_path / relative).exists()


def scan_crop_asset_fixture(
    tmp_path: Path,
) -> tuple[CompiledWiki, BookCoverageManifestUpdate, StagedBookAsset]:
    """Prepare one PDF-bound crop without mutating the private archive target."""

    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    pdf_relative = "wiki/private/_sources/knowledge/local-only/atomic-book/Atomic Book.pdf"
    pdf_path = tmp_path / pdf_relative
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with pdf_path.open("wb") as stream:
        writer.write(stream)
    pdf_digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    coverage_update.replacement["edition"]["source_sha256"] = pdf_digest
    coverage_update.replacement["source_archive"] = {
        "relative_path": pdf_relative,
        "actual_title": "Atomic Book",
        "sha256": pdf_digest,
        "privacy": "local-only",
    }

    crop_box = [10, 10, 200, 200]
    rendered_page = compiled_wiki_module._render_pdf_page_png(pdf_path, 1, 300)
    crop_bytes = compiled_wiki_module._crop_png(rendered_page, tuple(crop_box))
    crop_digest = hashlib.sha256(crop_bytes).hexdigest()
    page_locator = "source://atomic-book/Atomic%20Book.pdf#PDF-page-1"
    source_locator = f"{page_locator}:crop-10,10,200,200"
    archive_relative = (
        "wiki/private/_sources/knowledge/local-only/atomic-book/images/chapter-01/figure-1-1.png"
    )
    coverage_update.replacement["source_asset_inventory"] = [
        {
            "asset_id": f"asset:{crop_digest}",
            "source_locator": source_locator,
            "source_sha256": crop_digest,
            "archive_relative_path": archive_relative,
            "archive_sha256": crop_digest,
            "extraction_kind": "scan-crop",
            "crop_provenance": {
                "page_locator": page_locator,
                "crop_box": crop_box,
                "render_dpi": 300,
                "source_page_sha256": hashlib.sha256(rendered_page).hexdigest(),
            },
        }
    ]
    staging = tmp_path / "staged-source-assets/figure-1-1.png"
    staging.parent.mkdir()
    staging.write_bytes(crop_bytes)
    asset = StagedBookAsset(
        staging_path=staging,
        archive_relative_path=archive_relative,
        sha256=crop_digest,
        size=len(crop_bytes),
        provenance="scan-crop-with-pinned-page-and-box",
        source_entry_locator=source_locator,
    )
    return compiler, coverage_update, asset


def test_verified_book_asset_preflight_accepts_pdf_bound_scan_crop(tmp_path: Path) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)

    counts = compiler.validate_staged_book_assets((asset,), coverage_update)

    assert counts == (1, 0)
    assert not (tmp_path / asset.archive_relative_path).exists()


def test_verified_book_asset_preflight_rejects_scan_crop_source_pdf_hash(
    tmp_path: Path,
) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)
    coverage_update.replacement["source_archive"]["sha256"] = "f" * 64

    with pytest.raises(WoonError, match="source PDF hash does not match"):
        compiler.validate_staged_book_assets((asset,), coverage_update)


def test_verified_book_asset_preflight_rejects_scan_crop_box_locator_mismatch(
    tmp_path: Path,
) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)
    inventory = coverage_update.replacement["source_asset_inventory"]
    inventory[0]["crop_provenance"]["crop_box"] = [160, 160, 1800, 1000]

    with pytest.raises(WoonError, match="locator and crop box do not match"):
        compiler.validate_staged_book_assets((asset,), coverage_update)


def test_verified_book_asset_preflight_rejects_scan_crop_page_out_of_range(
    tmp_path: Path,
) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)
    inventory = coverage_update.replacement["source_asset_inventory"]
    page_locator = "source://atomic-book/Atomic%20Book.pdf#PDF-page-2"
    source_locator = f"{page_locator}:crop-10,10,200,200"
    inventory[0]["source_locator"] = source_locator
    inventory[0]["crop_provenance"]["page_locator"] = page_locator
    asset = replace(asset, source_entry_locator=source_locator)

    with pytest.raises(WoonError, match="page is outside the source PDF"):
        compiler.validate_staged_book_assets((asset,), coverage_update)


def test_verified_book_asset_preflight_rejects_scan_crop_rendered_page_hash(
    tmp_path: Path,
) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)
    inventory = coverage_update.replacement["source_asset_inventory"]
    inventory[0]["crop_provenance"]["source_page_sha256"] = "f" * 64

    with pytest.raises(WoonError, match="rendered page hash does not match"):
        compiler.validate_staged_book_assets((asset,), coverage_update)


def test_verified_book_asset_preflight_rejects_unreproducible_scan_crop_output(
    tmp_path: Path,
) -> None:
    compiler, coverage_update, asset = scan_crop_asset_fixture(tmp_path)
    altered = b"not the crop selected by the pinned page and box"
    altered_sha256 = hashlib.sha256(altered).hexdigest()
    asset.staging_path.write_bytes(altered)
    asset = replace(asset, sha256=altered_sha256, size=len(altered))
    inventory = coverage_update.replacement["source_asset_inventory"]
    inventory[0]["source_sha256"] = altered_sha256
    inventory[0]["archive_sha256"] = altered_sha256

    with pytest.raises(WoonError, match="output cannot be reproduced"):
        compiler.validate_staged_book_assets((asset,), coverage_update)


def test_verified_book_asset_preflight_rejects_destination_traversal(
    tmp_path: Path,
) -> None:
    _, service, _, record, _, _ = atomic_book_service(tmp_path)
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    content = b"source image"
    digest = hashlib.sha256(content).hexdigest()
    source_locator = "source://atomic-book#images/figure.png"
    relative = "wiki/private/_sources/knowledge/local-only/../../outside.png"
    coverage_update.replacement["source_asset_inventory"] = [
        {
            "asset_id": f"asset:{digest}",
            "source_locator": source_locator,
            "source_sha256": digest,
            "archive_relative_path": relative,
            "archive_sha256": digest,
            "extraction_kind": "embedded-original",
            "crop_provenance": None,
        }
    ]
    staging = tmp_path / "staged-source-assets/figure.png"
    staging.parent.mkdir()
    staging.write_bytes(content)
    asset = StagedBookAsset(
        staging_path=staging,
        archive_relative_path=relative,
        sha256=digest,
        size=len(content),
        provenance="embedded-original-byte-identical",
        source_entry_locator=source_locator,
    )

    with pytest.raises(WoonError, match="private source image archive"):
        service.preflight_verified_book_update((record,), {}, {}, {}, coverage_update, (asset,))


def test_verified_book_asset_landing_rolls_back_new_and_preserves_existing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    inventory: list[dict[str, object]] = []
    assets: list[StagedBookAsset] = []
    destinations: list[Path] = []
    for name in ("new.png", "existing.png"):
        content = f"source-{name}".encode()
        digest = hashlib.sha256(content).hexdigest()
        source_locator = f"source://atomic-book#images/{name}"
        relative = f"wiki/private/_sources/knowledge/local-only/atomic-book/images/{name}"
        staging = tmp_path / f"staged-source-assets/{name}"
        staging.parent.mkdir(exist_ok=True)
        staging.write_bytes(content)
        inventory.append(
            {
                "asset_id": f"asset:{digest}",
                "source_locator": source_locator,
                "source_sha256": digest,
                "archive_relative_path": relative,
                "archive_sha256": digest,
                "extraction_kind": "embedded-original",
                "crop_provenance": None,
            }
        )
        assets.append(
            StagedBookAsset(
                staging_path=staging,
                archive_relative_path=relative,
                sha256=digest,
                size=len(content),
                provenance="embedded-original-byte-identical",
                source_entry_locator=source_locator,
            )
        )
        destinations.append(tmp_path / relative)
    coverage_update.replacement["source_asset_inventory"] = inventory
    destinations[1].parent.mkdir(parents=True)
    existing_bytes = assets[1].staging_path.read_bytes()
    destinations[1].write_bytes(existing_bytes)

    def fail_after_asset_install(*_args: object, **_kwargs: object) -> None:
        assert destinations[0].is_file()
        assert destinations[1].read_bytes() == existing_bytes
        raise RuntimeError("injected book transaction failure")

    monkeypatch.setattr(compiler, "apply_verified_book_update", fail_after_asset_install)

    with pytest.raises(RuntimeError, match="injected book transaction failure"):
        service.apply_verified_book_update((record,), {}, {}, {}, coverage_update, tuple(assets))

    assert not destinations[0].exists()
    assert destinations[1].read_bytes() == existing_bytes


def test_verified_book_promotion_accepts_existing_uncompiled_wiki_parent(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "seed.md", "시드", "compiler catalog를 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    write_page(
        tmp_path,
        "books/programming-language.md",
        "프로그래밍 언어",
        "책을 언어별로 찾는다.",
    )
    page_id = "personal/kotlin-in-action"
    record = VerifiedBookPage(
        page_id=page_id,
        title="코틀린 인 액션 2판",
        body="실제 책 목차를 따라 학습한다.\n",
        statement="실제 책 목차를 설명한다.",
        current_use="Kotlin 책의 장을 찾을 때 사용한다.",
        source_locator="source://kotlin-in-action-2e#toc",
        source_sha256="c" * 64,
        frontmatter=verified_book_frontmatter(
            page_id, "코틀린 인 액션 2판", "books/programming-language"
        ),
    )

    report = compiler.promote_verified_book_pages((record,))

    assert report.compiled == 1
    assert compiler.audit().complete


def test_atomic_verified_book_update_promotes_retires_compiles_and_reindexes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    root_id = "books/atomic-book"
    leaf_id = "books/atomic-book/chapter-01"
    compile_calls = 0
    rebuild_calls = 0
    original_compile = compiler.compile
    original_rebuild = index.rebuild

    def counted_compile(*args: object, **kwargs: object):
        nonlocal compile_calls
        compile_calls += 1
        return original_compile(*args, **kwargs)

    def counted_rebuild(documents: list[IndexedDocument]) -> int:
        nonlocal rebuild_calls
        rebuild_calls += 1
        return original_rebuild(documents)

    monkeypatch.setattr(compiler, "compile", counted_compile)
    monkeypatch.setattr(index, "rebuild", counted_rebuild)

    report = service.apply_verified_book_update(
        (record,),
        {wrapper_id: root_id},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
    )

    assert report.curated == 1
    assert report.retired == 1
    assert report.retired_page_ids == (wrapper_id,)
    assert compile_calls == 1
    assert rebuild_calls == 1
    with pytest.raises(WoonError, match="canonical document not found"):
        service.get(wrapper_id)
    assert not (tmp_path / f"wiki/{wrapper_id}.md").exists()
    pages = yaml.safe_load((tmp_path / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8"))[
        "pages"
    ]
    by_id = {page["page_id"]: page for page in pages}
    assert by_id[leaf_id]["frontmatter"]["parent"] == f"[[wiki/{root_id}|1부]]"
    assert by_id[root_id]["frontmatter"]["navigation_groups"] == [
        {"label": "1부", "children": [leaf_id]}
    ]
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    retired_source = next(item for item in sources if item["locator"] == f"{wrapper_id}.md")
    assert by_id[root_id]["render"] == {"kind": "toc-only"}
    assert by_id[root_id]["frontmatter"]["content_state"] == "toc-only"
    assert all(item["locator"] != record.source_locator for item in sources)
    assert retired_source["locator"] == f"{wrapper_id}.md"
    assert retired_source["lifecycle"] == "archived"
    assert retired_source["superseded_by"] in by_id[root_id]["source_ids"]
    assert compiler.audit().complete


def test_verified_book_source_supersession_allows_only_explicit_source_free_toc_pages() -> None:
    prior_source_id = "source://verified-book/books/example/chapter-01/prior"
    successor_source_id = "source://verified-book/books/example/chapter-01/successor"
    sources = {
        prior_source_id: {"lifecycle": "compiled"},
        successor_source_id: {"lifecycle": "compiled"},
    }
    valid_pages = {
        "books/example/chapter-01": {
            "source_ids": [prior_source_id],
            "claim_ids": ["claim://prior"],
            "render": {"kind": "source-body", "source_id": prior_source_id},
        },
        "books/unrelated-map": {
            "source_ids": [],
            "claim_ids": [],
            "render": {"kind": "toc-only"},
        },
    }

    CompiledWiki._supersede_unshared_curated_source(
        prior_source_id,
        successor_source_id,
        "books/example/chapter-01",
        valid_pages,
        sources,
    )

    assert sources[prior_source_id] == {
        "lifecycle": "archived",
        "superseded_by": successor_source_id,
    }

    invalid_pages = copy.deepcopy(valid_pages)
    invalid_pages["books/unrelated-map"]["render"] = {"kind": "source-body"}
    with pytest.raises(WoonError, match="page source_ids must be a non-empty string list"):
        CompiledWiki._supersede_unshared_curated_source(
            prior_source_id,
            successor_source_id,
            "books/example/chapter-01",
            invalid_pages,
            sources,
        )


def test_atomic_verified_book_update_accepts_empty_authored_book_map_body(
    tmp_path: Path,
) -> None:
    _, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(tmp_path)

    report = service.apply_verified_book_update(
        (replace(record, body=""),),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": wrapper_revision},
        {"books/atomic-book/part-01": wrapper_body_sha256},
    )

    assert report.retired_page_ids == ("books/atomic-book/part-01",)
    rendered = (tmp_path / "wiki/books/atomic-book.md").read_text(encoding="utf-8")
    metadata, body = split_markdown(rendered)
    assert metadata["navigation_groups"] == [
        {"label": "1부", "children": ["books/atomic-book/chapter-01"]}
    ]
    assert body.strip() == (
        "# 원자적 책\n\n"
        "<!-- woon-wiki-children:start -->\n"
        "## 1부\n"
        "- [[wiki/books/atomic-book/chapter-01|1장 실제 내용]]\n"
        "<!-- woon-wiki-children:end -->"
    )


def test_verified_book_promotion_rejects_empty_leaf_body(tmp_path: Path) -> None:
    write_page(tmp_path, "seed.md", "시드", "컴파일러 catalog를 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        FailOnceIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    page_id = "books/atomic-book/chapter-01"
    record = VerifiedBookPage(
        page_id=page_id,
        title="1장",
        body="",
        statement="빈 leaf를 허용하지 않는다.",
        current_use="검증용이다.",
        source_locator="source://atomic-book#chapter-01",
        source_sha256="d" * 64,
        frontmatter=verified_book_frontmatter(page_id, "1장", "books/atomic-book"),
    )

    with pytest.raises(WoonError, match="body must be a non-empty string"):
        service.promote_verified_book_pages((record,))


def test_verified_book_promotion_accepts_explicit_toc_only_without_empty_source(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "seed.md", "시드", "컴파일러 catalog를 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    before_sources, before_claims, _, _, _ = compiler._load_inputs()
    record = VerifiedBookPage(
        page_id="books/example",
        title="예제 책",
        body="",
        statement="원문 목차에 존재하는 책이다.",
        current_use="책의 목차를 탐색한다.",
        source_locator="source://book/example#toc",
        source_sha256="d" * 64,
        frontmatter={
            **verified_book_frontmatter("books/example", "예제 책"),
            "book_toc_only": True,
        },
    )

    compiler.promote_verified_book_pages((record,))

    sources, claims, pages, _, _ = compiler._load_inputs()
    page = pages[record.page_id]
    assert sources == before_sources
    assert claims == before_claims
    assert page["render"] == {"kind": "toc-only"}
    assert page["source_ids"] == []
    assert page["claim_ids"] == []
    assert page["frontmatter"]["content_state"] == "toc-only"
    assert "book_toc_only" not in page["frontmatter"]


@pytest.mark.parametrize(
    ("body", "children", "message"),
    [
        ("임의 설명이다.\n", [], "must not contain authored prose"),
        ("", ["books/example"], "must not link to itself"),
    ],
)
def test_verified_book_promotion_rejects_invalid_explicit_toc_only(
    tmp_path: Path,
    body: str,
    children: list[str],
    message: str,
) -> None:
    write_page(tmp_path, "seed.md", "시드", "컴파일러 catalog를 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    frontmatter = {
        **verified_book_frontmatter("books/example", "예제 책"),
        "book_toc_only": True,
    }
    if children:
        frontmatter["navigation_groups"] = [{"label": "목차", "children": children}]
    record = VerifiedBookPage(
        page_id="books/example",
        title="예제 책",
        body=body,
        statement="TOC-only 무결성을 검증한다.",
        current_use="책의 목차를 탐색한다.",
        source_locator="source://book/example#toc",
        source_sha256="d" * 64,
        frontmatter=frontmatter,
    )

    with pytest.raises(WoonError, match=message):
        compiler.promote_verified_book_pages((record,))


def test_atomic_verified_book_update_replaces_coverage_manifest_in_same_transaction(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path, _ = atomic_book_coverage_update(tmp_path)
    wrapper_id = "books/atomic-book/part-01"

    report = service.apply_verified_book_update(
        (record,),
        {wrapper_id: "books/atomic-book"},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
        coverage_update,
    )

    assert report.retired_page_ids == (wrapper_id,)
    assert json.loads(coverage_path.read_text(encoding="utf-8")) == coverage_update.replacement
    assert coverage_path.read_bytes() == (
        json.dumps(
            coverage_update.replacement,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def test_scoped_book_preflight_is_read_only_and_preserves_full_manifest(
    tmp_path: Path,
) -> None:
    _, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(tmp_path)
    update, target, base_path, base_bytes = atomic_book_scoped_coverage_update(tmp_path)
    generation_before = index.generation()

    report = service.preflight_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": wrapper_revision},
        {"books/atomic-book/part-01": wrapper_body_sha256},
        update,
    )

    assert report.ready is True
    assert report.coverage_mode == "merge-scope"
    assert report.base_manifest_preserved is True
    assert not target.exists()
    assert base_path.read_bytes() == base_bytes
    assert index.generation() == generation_before


def test_verified_book_preflight_runs_cloned_writer_and_cleans_temporary_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, index, record, _, _ = atomic_book_service(tmp_path)
    update, coverage_path, _ = atomic_book_coverage_update(tmp_path)
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    dry_vaults: list[Path] = []

    def fake_apply(
        self: CompiledWiki,
        pages: tuple[VerifiedBookPage, ...],
        replacements: dict[str, str],
        retirement_body_sha256: dict[str, str],
        coverage_manifest: BookCoverageManifestUpdate,
        *,
        rights_restore_book_id: str | None = None,
        retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        retirement_content_relocations: dict[str, tuple[str, ...]] | None = None,
    ) -> VerifiedBookUpdateReport:
        del pages, replacements, retirement_body_sha256, coverage_manifest
        assert rights_restore_book_id is None
        assert retirement_image_replacements is None
        assert retirement_content_relocations is None
        assert self.vault != compiler.vault
        dry_vaults.append(self.vault)
        (self.vault / "dry-run-writer-ran").write_text("yes", encoding="utf-8")
        return VerifiedBookUpdateReport(0, 0, 0, 0, (), (), ())

    monkeypatch.setattr(CompiledWiki, "apply_verified_book_update", fake_apply)

    report = service.preflight_verified_book_update(
        (record,),
        {},
        {},
        {},
        update,
    )

    assert report.ready is True
    assert len(dry_vaults) == 1
    assert not dry_vaults[0].exists()
    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before


def test_scoped_book_preflight_rejects_unpreserved_wrapper_reader_body(
    tmp_path: Path,
) -> None:
    reader_body = "1부가 이 책의 가상화 흐름을 소개한다.\n"
    compiler, service, index, _, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path,
        wrapper_body=reader_body,
    )
    update, target, base_path, base_bytes = atomic_book_scoped_coverage_update(tmp_path)
    leaf_id = "books/atomic-book/chapter-01"
    leaf_record = atomic_book_leaf_record(tmp_path, service, "실제 장 내용이다.\n")
    input_before = compiler.snapshot_inputs(extra_paths=(target,))
    generation_before = index.generation()

    with pytest.raises(WoonError, match="reader content is not preserved"):
        service.preflight_verified_book_update(
            (leaf_record,),
            {"books/atomic-book/part-01": leaf_id},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            update,
        )

    assert compiler.snapshot_inputs(extra_paths=(target,)) == input_before
    assert not target.exists()
    assert base_path.read_bytes() == base_bytes
    assert index.generation() == generation_before


def test_scoped_book_preflight_rejects_candidate_the_writer_cannot_refresh(
    tmp_path: Path,
) -> None:
    reader_body = "1부가 이 책의 가상화 흐름을 소개한다.\n"
    compiler, service, index, _, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path,
        wrapper_body=reader_body,
    )
    update, target, base_path, base_bytes = atomic_book_scoped_coverage_update(tmp_path)
    leaf_id = "books/atomic-book/chapter-01"
    leaf_record = atomic_book_leaf_record(
        tmp_path,
        service,
        f"실제 장 내용이다.\n\n{reader_body}",
    )
    input_before = compiler.snapshot_inputs(extra_paths=(target,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="navigation_groups omit direct children"):
        service.preflight_verified_book_update(
            (leaf_record,),
            {"books/atomic-book/part-01": leaf_id},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            update,
        )

    assert compiler.snapshot_inputs(extra_paths=(target,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert not target.exists()
    assert base_path.read_bytes() == base_bytes
    assert index.generation() == generation_before


def test_atomic_scoped_book_update_writes_only_verified_fragment(
    tmp_path: Path,
) -> None:
    _, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(tmp_path)
    update, target, base_path, base_bytes = atomic_book_scoped_coverage_update(tmp_path)

    report = service.apply_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": wrapper_revision},
        {"books/atomic-book/part-01": wrapper_body_sha256},
        update,
    )

    assert report.retired_page_ids == ("books/atomic-book/part-01",)
    assert json.loads(target.read_text(encoding="utf-8")) == update.replacement
    assert base_path.read_bytes() == base_bytes


def test_scoped_book_update_rejects_base_manifest_drift_before_mutation(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    update, target, base_path, _ = atomic_book_scoped_coverage_update(tmp_path)
    base_path.write_text('{"changed": true}\n', encoding="utf-8")
    input_before = compiler.snapshot_inputs(extra_paths=(base_path, target))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="base manifest changed after scope review"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            update,
        )

    assert compiler.snapshot_inputs(extra_paths=(base_path, target)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert not target.exists()


def test_atomic_scoped_book_update_rolls_back_failed_scope_audit(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    update, target, base_path, base_bytes = atomic_book_scoped_coverage_update(
        tmp_path,
        replacement_parent="books/wrong-parent",
    )
    input_before = compiler.snapshot_inputs(extra_paths=(target,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="stale scoped book coverage"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            update,
        )

    assert compiler.snapshot_inputs(extra_paths=(target,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert not target.exists()
    assert base_path.read_bytes() == base_bytes


def test_verified_book_promotion_replaces_coverage_manifest_without_retirement(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    service.apply_verified_book_update(
        (record,),
        {wrapper_id: "books/atomic-book"},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
    )
    coverage_update, coverage_path, _ = atomic_book_coverage_update(
        tmp_path,
        replacement_parent="books/atomic-book",
    )
    coverage_update.replacement["edition"]["label"] = "검증판 개정"
    record = replace(record, expected_revision=service.get("books/atomic-book").revision)

    report = service.promote_verified_book_pages((record,), coverage_update)

    assert report.curated == 0
    assert json.loads(coverage_path.read_text(encoding="utf-8")) == coverage_update.replacement


def test_verified_book_promotion_creates_first_coverage_manifest_atomically(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path = atomic_book_new_coverage_update(tmp_path)

    report = service.apply_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": wrapper_revision},
        {"books/atomic-book/part-01": wrapper_body_sha256},
        coverage_update,
    )

    assert report.retired_page_ids == ("books/atomic-book/part-01",)
    assert json.loads(coverage_path.read_text(encoding="utf-8")) == coverage_update.replacement


def test_book_coverage_manifest_rejects_existing_file_without_revision(
    tmp_path: Path,
) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    coverage_update, coverage_path, _ = atomic_book_coverage_update(tmp_path)

    with pytest.raises(WoonError, match="existing book coverage manifest requires"):
        compiler.validate_book_coverage_manifest_update(
            replace(coverage_update, expected_sha256=None)
        )

    assert coverage_path.is_file()


def test_book_coverage_manifest_rejects_missing_file_with_revision(
    tmp_path: Path,
) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    coverage_update, coverage_path, _ = atomic_book_coverage_update(tmp_path)
    coverage_path.unlink()

    with pytest.raises(WoonError, match="new book coverage manifest requires"):
        compiler.validate_book_coverage_manifest_update(coverage_update)

    assert not coverage_path.exists()


def test_book_coverage_manifest_revalidates_missing_to_existing_race(
    tmp_path: Path,
) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    coverage_update, coverage_path = atomic_book_new_coverage_update(tmp_path)
    assert compiler.validate_book_coverage_manifest_update(coverage_update) == coverage_path

    coverage_path.write_text('{"concurrent": true}\n', encoding="utf-8")

    with pytest.raises(WoonError, match="existing book coverage manifest requires"):
        compiler.validate_book_coverage_manifest_update(coverage_update)


def test_book_coverage_manifest_rejects_symlink_and_non_regular_target(
    tmp_path: Path,
) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    update, path = atomic_book_new_coverage_update(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    path.symlink_to(outside)

    with pytest.raises(WoonError, match="must not use symlinks"):
        compiler.validate_book_coverage_manifest_update(update)

    path.unlink()
    path.mkdir()
    with pytest.raises(WoonError, match="must be a regular file"):
        compiler.validate_book_coverage_manifest_update(update)


@pytest.mark.parametrize(
    "relative_path",
    (
        "../catalog/book-coverage/atomic-book.json",
        "catalog/book-coverage/nested/atomic-book.json",
        "catalog/atomic-book.json",
        "/catalog/book-coverage/atomic-book.json",
        "catalog\\book-coverage\\atomic-book.json",
    ),
)
def test_book_coverage_manifest_rejects_noncanonical_path(
    tmp_path: Path,
    relative_path: str,
) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    update, _ = atomic_book_new_coverage_update(tmp_path)

    with pytest.raises(WoonError, match="one JSON file under catalog/book-coverage"):
        compiler.validate_book_coverage_manifest_update(
            replace(update, relative_path=relative_path)
        )


def test_atomic_verified_book_update_allows_unchanged_unrelated_coverage_error(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)
    unrelated_id = "books/unrelated-in-progress"
    unrelated_frontmatter = verified_book_frontmatter(unrelated_id, "집필 중인 다른 책")
    unrelated_frontmatter["entity_kind"] = "book"
    compiler.promote_verified_book_pages(
        (
            VerifiedBookPage(
                page_id=unrelated_id,
                title="집필 중인 다른 책",
                body="이 책은 아직 집필 중이다.\n",
                statement="집필 중인 책의 현재 범위를 설명한다.",
                current_use="다른 책의 진행 상태를 확인할 때 사용한다.",
                source_locator="source://unrelated-book#toc",
                source_sha256="e" * 64,
                frontmatter=unrelated_frontmatter,
            ),
        )
    )
    before = audit_book_coverage(tmp_path)
    unrelated_error = "books/unrelated-in-progress: book coverage manifest is missing"
    assert unrelated_error in before.errors

    report = service.apply_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": wrapper_revision},
        {"books/atomic-book/part-01": wrapper_body_sha256},
        coverage_update,
    )

    assert report.retired_page_ids == ("books/atomic-book/part-01",)
    assert audit_book_coverage(tmp_path).errors == (unrelated_error,)


def test_atomic_verified_book_update_rejects_stale_coverage_manifest_before_mutation(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path, original = atomic_book_coverage_update(tmp_path)
    stale_update = replace(coverage_update, expected_sha256="0" * 64)
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="coverage manifest changed after review"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            stale_update,
        )

    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert coverage_path.read_bytes() == original


def test_atomic_verified_book_update_rolls_back_manifest_on_coverage_audit_failure(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path, original = atomic_book_coverage_update(
        tmp_path,
        replacement_parent="books/wrong-parent",
    )
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="left stale book coverage"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            coverage_update,
        )

    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert coverage_path.read_bytes() == original
    assert service.get("books/atomic-book/part-01") is not None


def test_atomic_verified_book_update_rolls_back_sparse_count_only_manifest(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path, original = atomic_book_coverage_update(tmp_path)
    coverage_update.replacement["source_elements"] = []
    coverage_update.replacement["source_element_assignments"] = []
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="left stale book coverage"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            coverage_update,
        )

    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert coverage_path.read_bytes() == original
    assert service.get("books/atomic-book/part-01") is not None


def test_atomic_verified_book_update_removes_new_manifest_on_coverage_audit_failure(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path = atomic_book_new_coverage_update(
        tmp_path,
        replacement_parent="books/wrong-parent",
    )
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="left stale book coverage"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
            coverage_update,
        )

    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert not coverage_path.exists()
    assert service.get("books/atomic-book/part-01") is not None


def test_atomic_verified_book_update_restores_inputs_outputs_and_index_on_failure(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    root_id = "books/atomic-book"
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    index.fail_next = True

    with pytest.raises(RuntimeError, match="injected index failure"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: root_id},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None
    assert compiler.audit().complete


def test_atomic_materialized_scope_update_restores_consumed_scope_on_index_failure(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    update, base_path, scope_path, base_bytes, scope_bytes = (
        atomic_book_materialized_coverage_update(tmp_path)
    )
    wrapper_id = "books/atomic-book/part-01"
    input_before = compiler.snapshot_inputs(extra_paths=(base_path, scope_path))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    index.fail_next = True

    with pytest.raises(RuntimeError, match="injected index failure"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
            update,
        )

    assert compiler.snapshot_inputs(extra_paths=(base_path, scope_path)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert base_path.read_bytes() == base_bytes
    assert scope_path.read_bytes() == scope_bytes
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None


def test_merge_scope_rejects_materialized_scope_api_field(tmp_path: Path) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    scoped, _, _, _ = atomic_book_scoped_coverage_update(tmp_path)
    poisoned = replace(
        scoped,
        materialized_scopes=(
            BookCoverageScopeRevision(
                relative_path="catalog/book-coverage-scopes/atomic-book/chapter-01.json",
                expected_sha256="a" * 64,
                root_id="books/atomic-book/chapter-01",
            ),
        ),
    )

    with pytest.raises(WoonError, match="must not declare materialized scopes"):
        compiler._validated_coverage_manifest_update(poisoned)


def test_materialize_scopes_rejects_duplicate_scope_paths(tmp_path: Path) -> None:
    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    update, _, _, _, _ = atomic_book_materialized_coverage_update(tmp_path)
    first = update.materialized_scopes[0]
    duplicate_path = replace(first, root_id="books/atomic-book/chapter-02")
    poisoned = replace(update, materialized_scopes=(first, duplicate_path))

    with pytest.raises(WoonError, match="scope paths must be unique"):
        compiler._validated_coverage_manifest_update(poisoned)


def test_materialize_scopes_inherits_omitted_archive_and_preserves_absent_semantics(
    tmp_path: Path,
) -> None:
    scoped, _, _, base_bytes = atomic_book_scoped_coverage_update(tmp_path)
    base = json.loads(base_bytes)
    scope = copy.deepcopy(scoped.replacement)
    root_id = "books/atomic-book/chapter-01"
    base["source_archive"] = {
        "relative_path": "wiki/private/_sources/books/atomic-book.pdf",
        "sha256": "a" * 64,
    }

    # Model a toc-indexed fragment created before source/archive materialization:
    # it owns structure and nodes but intentionally omits semantic and asset fields.
    scope.pop("source_archive", None)
    scope["workflow_phase"] = "toc-indexed"
    scope.pop("source_elements")
    scope.pop("source_element_assignments")
    scope.pop("source_asset_inventory", None)
    for assignment in base["source_element_assignments"]:
        assignment["owner_id"] = "books/atomic-book/chapter-02"

    merged = _materialize_book_coverage_scopes(base, (scope,), (root_id,))

    assert merged["source_archive"] == base["source_archive"]
    assert merged.get("workflow_phase") == base.get("workflow_phase")
    assert merged["source_elements"] == base["source_elements"]
    assert merged["source_element_assignments"] == base["source_element_assignments"]


def test_materialize_scopes_rejects_omitted_semantics_for_populated_root(
    tmp_path: Path,
) -> None:
    scoped, _, _, base_bytes = atomic_book_scoped_coverage_update(tmp_path)
    base = json.loads(base_bytes)
    scope = copy.deepcopy(scoped.replacement)
    scope.pop("source_elements")
    scope.pop("source_element_assignments")

    with pytest.raises(WoonError, match="omitted semantic inventory for a populated root"):
        _materialize_book_coverage_scopes(
            base,
            (scope,),
            ("books/atomic-book/chapter-01",),
        )


def test_source_landed_progression_allows_declared_retirement_relocation() -> None:
    retired_id = "books/example/chapter-01"
    leaf_id = f"{retired_id}/1-1"
    current = {
        "schema_version": 3,
        "workflow_phase": "source-landed",
        "translation_required": False,
        "edition": {"label": "1판", "source_sha256": "a" * 64},
        "source_archive": {"relative_path": "book.pdf", "sha256": "a" * 64},
        "phase_evidence": {"source-landed": {"sha256": "b" * 64}},
        "retired_source_section_wrappers": [],
        "nodes": [{"canonical_id": retired_id}],
        "source_structure_elements": [{"structure_id": "structure:1"}],
        "source_structure_assignments": [
            {
                "structure_id": "structure:1",
                "disposition": "canonical-node",
                "canonical_id": retired_id,
            }
        ],
        "source_elements": [{"element_id": "claim:1"}],
        "source_element_assignments": [{"element_id": "claim:1", "owner_id": retired_id}],
        "source_asset_inventory": [],
        "source_structure_inventory_evidence": {"sha256": "c" * 64},
        "source_element_inventory_evidence": {"sha256": "d" * 64},
        "source_asset_inventory_evidence": {
            "expected_asset_count": 0,
            "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
        },
        "toc_evidence": [],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
    }
    replacement = copy.deepcopy(current)
    replacement["nodes"] = [{"canonical_id": leaf_id}]
    replacement["source_structure_assignments"][0] = {
        "structure_id": "structure:1",
        "disposition": "navigation-group-heading",
        "owner_id": "books/example",
    }
    replacement["source_element_assignments"][0]["owner_id"] = leaf_id
    replacement["source_structure_inventory_evidence"] = {"sha256": "e" * 64}
    replacement["source_element_inventory_evidence"] = {"sha256": "f" * 64}

    with pytest.raises(WoonError, match="cannot delete existing canonical_id"):
        compiled_wiki_module._validate_book_workflow_progression(
            current,
            replacement,
            "book coverage manifest",
            allow_source_landed_expansion=True,
        )

    compiled_wiki_module._validate_book_workflow_progression(
        current,
        replacement,
        "book coverage manifest",
        allow_source_landed_expansion=True,
        allowed_retired_ids={retired_id},
    )


def test_coverage_nodes_allow_only_declared_parent_move_to_new_navigation_group() -> None:
    old_parent = "books/example/chapter-03"
    new_parent = f"{old_parent}/3-3"
    leaf_id = f"{old_parent}/3-3-1"
    leaf = {
        "canonical_id": leaf_id,
        "parent_id": old_parent,
        "kind": "subsection",
        "leaf": True,
        "has_direct_content": True,
        "source_locator": "source://example#3.3.1",
    }
    group = {
        "canonical_id": new_parent,
        "parent_id": "books/example",
        "kind": "section",
        "leaf": False,
        "has_direct_content": False,
        "source_locator": "source://example#3.3",
    }
    moved = copy.deepcopy(leaf)
    moved["parent_id"] = new_parent

    compiled_wiki_module._validate_ordered_supersequence(
        [leaf],
        [group, moved],
        field="nodes",
        identity_field="canonical_id",
        label="book coverage manifest",
        allowed_node_parent_changes={leaf_id: (old_parent, new_parent)},
    )

    with pytest.raises(WoonError, match="cannot change existing canonical_id"):
        compiled_wiki_module._validate_ordered_supersequence(
            [leaf],
            [group, moved],
            field="nodes",
            identity_field="canonical_id",
            label="book coverage manifest",
        )

    changed_metadata = copy.deepcopy(moved)
    changed_metadata["kind"] = "chapter"
    with pytest.raises(WoonError, match="cannot change existing canonical_id"):
        compiled_wiki_module._validate_ordered_supersequence(
            [leaf],
            [group, changed_metadata],
            field="nodes",
            identity_field="canonical_id",
            label="book coverage manifest",
            allowed_node_parent_changes={leaf_id: (old_parent, new_parent)},
        )


def test_atomic_verified_book_update_restores_compiler_state_on_final_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    root_id = "books/atomic-book"
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    original_audit = compiler.audit
    failed_once = False

    def fail_final_audit_once() -> CompilationAudit:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            return CompilationAudit(0, 0, ("injected final audit failure",))
        return original_audit()

    monkeypatch.setattr(compiler, "audit", fail_final_audit_once)

    with pytest.raises(WoonError, match="injected final audit failure"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: root_id},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert original_audit().complete


def test_atomic_verified_book_update_rolls_back_tree_side_effect_on_refresh_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    root_path = tmp_path / "wiki/books/atomic-book.md"
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    def fail_after_tree_side_effect(vault: Path, report: object) -> None:
        del vault, report
        root_path.write_text("injected partial tree write\n", encoding="utf-8")
        raise RuntimeError("injected tree refresh failure")

    monkeypatch.setattr(
        compiled_wiki_module,
        "apply_wiki_tree_refresh",
        fail_after_tree_side_effect,
    )

    with pytest.raises(RuntimeError, match="injected tree refresh failure"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None


def test_atomic_verified_book_update_rolls_back_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    root_path = tmp_path / "wiki/books/atomic-book.md"
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    def interrupt_after_tree_side_effect(vault: Path, report: object) -> None:
        del vault, report
        root_path.write_text("injected interrupted tree write\n", encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(
        compiled_wiki_module,
        "apply_wiki_tree_refresh",
        interrupt_after_tree_side_effect,
    )

    with pytest.raises(KeyboardInterrupt):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None


def test_atomic_verified_book_update_rolls_back_index_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    wrapper_id = "books/atomic-book/part-01"
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    original_rebuild = index.rebuild
    interrupted = False

    def interrupt_once(documents: list[IndexedDocument]) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original_rebuild(documents)

    monkeypatch.setattr(index, "rebuild", interrupt_once)

    with pytest.raises(KeyboardInterrupt):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None


def test_atomic_verified_book_update_rolls_back_tree_on_rendered_ui_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, index, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    coverage_update, coverage_path, _ = atomic_book_coverage_update(tmp_path)
    wrapper_id = "books/atomic-book/part-01"
    root_path = tmp_path / "wiki/books/atomic-book.md"
    input_before = compiler.snapshot_inputs(extra_paths=(coverage_path,))
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()
    original_apply = compiled_wiki_module.apply_wiki_tree_refresh

    def apply_stale_rendered_link(vault: Path, report: object) -> None:
        original_apply(vault, report)
        current = root_path.read_text(encoding="utf-8")
        root_path.write_text(
            current.replace(
                "[[wiki/books/atomic-book/chapter-01|",
                "[[wiki/books/atomic-book/stale-chapter|",
                1,
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        compiled_wiki_module,
        "apply_wiki_tree_refresh",
        apply_stale_rendered_link,
    )

    with pytest.raises(WoonError, match="left stale book coverage"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
            coverage_update,
        )

    assert compiler.snapshot_inputs(extra_paths=(coverage_path,)) == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before
    assert service.get(wrapper_id) is not None


def test_atomic_verified_book_update_rejects_stale_retirement_before_mutation(
    tmp_path: Path,
) -> None:
    compiler, service, index, record, _, wrapper_body_sha256 = atomic_book_service(tmp_path)
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()
    generation_before = index.generation()

    with pytest.raises(WoonError, match="retired book page changed after it was read"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": "stale"},
            {"books/atomic-book/part-01": wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before
    assert index.generation() == generation_before


def test_atomic_verified_book_update_refuses_to_retire_reader_content(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path, wrapper_body="이 Part에는 독자가 읽어야 할 소개가 있다.\n"
    )
    input_before = compiler.snapshot_inputs()
    output_before = compiler.snapshot_outputs()

    with pytest.raises(WoonError, match="reader content is not preserved"):
        service.apply_verified_book_update(
            (record,),
            {"books/atomic-book/part-01": "books/atomic-book"},
            {"books/atomic-book/part-01": wrapper_revision},
            {"books/atomic-book/part-01": wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before
    assert compiler.snapshot_outputs() == output_before


def test_retirement_content_relocation_accepts_exact_one_to_many_source_split(
    tmp_path: Path,
) -> None:
    compiler, _, _, root_record, _, _ = atomic_book_service(tmp_path)
    update, path, _ = atomic_book_coverage_update(tmp_path)
    current = json.loads(path.read_text(encoding="utf-8"))
    original_element = current["source_elements"][0]
    second_element = dict(original_element)
    second_element["element_id"] = "claim:" + "b" * 64
    second_element["source_locator"] = "source://atomic-book#chapter-01-paragraph-02"
    current["source_elements"].append(second_element)
    second_assignment = dict(current["source_element_assignments"][0])
    second_assignment["element_id"] = second_element["element_id"]
    second_assignment["delivery_span"] = "둘째 절로 이동할 원문이다."
    second_assignment["delivery_span_sha256"] = hashlib.sha256(
        second_assignment["delivery_span"].encode("utf-8")
    ).hexdigest()
    current["source_element_assignments"].append(second_assignment)
    current_bytes = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(current_bytes)

    first_id = "books/atomic-book/chapter-01/1-1"
    second_id = "books/atomic-book/chapter-01/1-2"
    replacement = copy.deepcopy(current)
    replacement["source_element_assignments"][0]["owner_id"] = first_id
    replacement["source_element_assignments"][1]["owner_id"] = second_id
    split_update = replace(
        update,
        expected_sha256=hashlib.sha256(current_bytes).hexdigest(),
        replacement=replacement,
    )
    records = (
        replace(root_record, page_id=first_id, expected_revision=None),
        replace(root_record, page_id=second_id, expected_revision=None),
    )

    relocated = compiler._validate_retirement_content_relocations(
        records,
        {"books/atomic-book/chapter-01": first_id},
        split_update,
        {"books/atomic-book/chapter-01": (first_id, second_id)},
        current_reader_bodies={"books/atomic-book/chapter-01": "실제 장 내용이다.\n"},
    )

    assert relocated == {"books/atomic-book/chapter-01"}

    replacement["source_element_assignments"][1]["delivery_span"] = "변조된 전달"
    with pytest.raises(WoonError, match="changed source delivery evidence"):
        compiler._validate_retirement_content_relocations(
            records,
            {"books/atomic-book/chapter-01": first_id},
            split_update,
            {"books/atomic-book/chapter-01": (first_id, second_id)},
            current_reader_bodies={"books/atomic-book/chapter-01": "실제 장 내용이다.\n"},
        )


@pytest.mark.parametrize(
    ("delivery", "language_field", "index_field", "language"),
    (
        ("run-block", "run_language", "run_block_index", "run-python"),
        ("static-exception", "static_language", "static_block_index", "python"),
    ),
)
def test_retirement_content_relocation_reindexes_exact_code_fence_only(
    tmp_path: Path,
    delivery: str,
    language_field: str,
    index_field: str,
    language: str,
) -> None:
    compiler, _, _, root_record, _, _ = atomic_book_service(tmp_path)
    update, path, _ = atomic_book_coverage_update(tmp_path)
    current = json.loads(path.read_text(encoding="utf-8"))
    element_id = "code:" + "b" * 64
    current["source_elements"][0]["element_id"] = element_id
    current["source_elements"][0]["kind"] = "code"
    current["source_elements"][0]["semantic_unit"] = "code-block"
    current["source_element_assignments"][0] = {
        "element_id": element_id,
        "owner_id": "books/atomic-book/chapter-01",
        "delivery": delivery,
        language_field: language,
        index_field: 2,
        "verification_evidence": "evidence/code-run.json",
        "verification_sha256": "c" * 64,
    }
    current_bytes = (json.dumps(current, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(current_bytes)

    successor_id = "books/atomic-book/chapter-01/1-1"
    replacement = copy.deepcopy(current)
    replacement["source_element_assignments"][0]["owner_id"] = successor_id
    replacement["source_element_assignments"][0][index_field] = 1
    split_update = replace(
        update,
        expected_sha256=hashlib.sha256(current_bytes).hexdigest(),
        replacement=replacement,
    )
    moved_fence = f"```{language}\nprint('moved')\n```\n"
    old_body = f"```{language}\nprint('first')\n```\n\n{moved_fence}"
    records = (
        replace(
            root_record,
            page_id=successor_id,
            body=moved_fence,
            expected_revision=None,
        ),
    )

    assert compiler._validate_retirement_content_relocations(
        records,
        {"books/atomic-book/chapter-01": successor_id},
        split_update,
        {"books/atomic-book/chapter-01": (successor_id,)},
        current_reader_bodies={"books/atomic-book/chapter-01": old_body},
    ) == {"books/atomic-book/chapter-01"}

    replacement["source_element_assignments"][0][index_field] = 2
    with pytest.raises(WoonError, match="changed source code delivery"):
        compiler._validate_retirement_content_relocations(
            records,
            {"books/atomic-book/chapter-01": successor_id},
            split_update,
            {"books/atomic-book/chapter-01": (successor_id,)},
            current_reader_bodies={"books/atomic-book/chapter-01": old_body},
        )


def test_atomic_verified_book_update_preserves_reviewed_reader_body_in_replacement(
    tmp_path: Path,
) -> None:
    reader_body = "1부가 이 책의 가상화 흐름을 소개한다.\n"
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path,
        wrapper_body=reader_body,
    )
    wrapper_id = "books/atomic-book/part-01"
    leaf_id = "books/atomic-book/chapter-01"
    leaf_record = atomic_book_leaf_record(
        tmp_path,
        service,
        f"실제 장 내용이다.\n\n{reader_body}",
    )

    report = service.apply_verified_book_update(
        (replace(record, body=""), leaf_record),
        {wrapper_id: leaf_id},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
    )

    assert report.retired_page_ids == (wrapper_id,)
    assert reader_body.strip() in service.get(leaf_id).body
    assert reader_body.strip() not in service.get("books/atomic-book").body
    assert compiler.audit().complete


def test_atomic_verified_book_update_hashes_body_after_generated_views_are_removed(
    tmp_path: Path,
) -> None:
    reader_body = "1부가 이 책의 가상화 흐름을 소개한다.\n"
    wrapper_body = (
        reader_body
        + "\n## 하위 키워드\n\n<!-- woon-wiki-children:start -->\n"
        + "- [[wiki/books/atomic-book/chapter-01|1장 실제 내용]]\n"
        + "<!-- woon-wiki-children:end -->\n"
    )
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path,
        wrapper_body=wrapper_body,
    )
    wrapper_id = "books/atomic-book/part-01"
    leaf_id = "books/atomic-book/chapter-01"
    leaf_record = atomic_book_leaf_record(
        tmp_path,
        service,
        f"실제 장 내용이다.\n\n{reader_body}",
    )

    report = service.apply_verified_book_update(
        (replace(record, body=""), leaf_record),
        {wrapper_id: leaf_id},
        {wrapper_id: wrapper_revision},
        {wrapper_id: wrapper_body_sha256},
    )

    assert report.retired_page_ids == (wrapper_id,)
    assert reader_body.strip() in service.get(leaf_id).body
    assert compiler.audit().complete


def test_atomic_verified_book_update_accepts_source_free_toc_only_leaf_shell(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path
    )
    shell_id = add_source_free_toc_leaf(compiler)
    service.reindex()
    root_id = "books/atomic-book"
    wrapper_id = f"{root_id}/part-01"
    shell = service.get(shell_id)
    root = service.get(root_id)
    assert shell is not None
    assert root is not None
    assert shell.body == ""
    shell_body_sha256 = hashlib.sha256(shell.body.encode("utf-8")).hexdigest()

    report = service.apply_verified_book_update(
        (replace(record, expected_revision=root.revision),),
        {wrapper_id: root_id, shell_id: root_id},
        {wrapper_id: wrapper_revision, shell_id: shell.revision},
        {wrapper_id: wrapper_body_sha256, shell_id: shell_body_sha256},
    )

    assert report.retired_page_ids == (wrapper_id, shell_id)
    with pytest.raises(WoonError, match="canonical document not found"):
        service.get(wrapper_id)
    with pytest.raises(WoonError, match="canonical document not found"):
        service.get(shell_id)
    assert compiler.audit().complete


def test_source_free_toc_only_leaf_retirement_rejects_authored_prose(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    shell_id = add_source_free_toc_leaf(compiler)
    shell_path = tmp_path / "wiki" / f"{shell_id}.md"
    shell_path.write_text(
        shell_path.read_text(encoding="utf-8") + "\n이 문장은 보존해야 하는 독자 본문이다.\n",
        encoding="utf-8",
    )
    shell = service.get(shell_id)
    root = service.get("books/atomic-book")
    assert shell is not None
    assert root is not None
    body_sha256 = hashlib.sha256(_retirement_body(shell.body).encode("utf-8")).hexdigest()

    with pytest.raises(WoonError, match="contains authored reader content"):
        service.preflight_verified_book_update(
            (replace(record, expected_revision=root.revision),),
            {shell_id: "books/atomic-book"},
            {shell_id: shell.revision},
            {shell_id: body_sha256},
            None,
        )


def test_preflight_allows_empty_rights_toc_wrapper_coverage_node_removal(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    wrapper_id = convert_atomic_wrapper_to_rights_toc_only(compiler)
    service.reindex()
    root = service.get("books/atomic-book")
    wrapper = service.get(wrapper_id)
    coverage_update, coverage_path, _ = atomic_book_coverage_update(tmp_path)
    current_coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    current_coverage["nodes"].insert(
        0,
        {
            "canonical_id": wrapper_id,
            "has_direct_content": False,
            "kind": "section",
            "leaf": False,
            "parent_id": "books/atomic-book",
            "source_locator": "source://atomic-book#part-01",
            "state": "toc-only",
        },
    )
    current_coverage["toc_node_count"] = 2
    current_bytes = (json.dumps(current_coverage, ensure_ascii=False, indent=2) + "\n").encode()
    coverage_path.write_bytes(current_bytes)
    coverage_update = replace(
        coverage_update,
        expected_sha256=hashlib.sha256(current_bytes).hexdigest(),
    )

    report = service.preflight_verified_book_update(
        (replace(record, expected_revision=root.revision),),
        {wrapper_id: "books/atomic-book"},
        {wrapper_id: wrapper.revision},
        {wrapper_id: hashlib.sha256(b"").hexdigest()},
        coverage_update,
    )

    assert report.ready is True
    assert report.retirement_count == 1


@pytest.mark.parametrize("authored_child", (False, True))
def test_navigation_reparent_accepts_only_empty_source_free_toc_child(
    tmp_path: Path,
    authored_child: bool,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    wrapper_id = convert_atomic_wrapper_to_rights_toc_only(compiler)
    leaf_id = convert_atomic_leaf_to_source_free_toc_only(compiler)
    service.reindex()
    if authored_child:
        leaf_path = tmp_path / "wiki" / f"{leaf_id}.md"
        leaf_path.write_text(
            leaf_path.read_text(encoding="utf-8") + "\n이 문장은 보존해야 하는 독자 본문이다.\n",
            encoding="utf-8",
        )
    group_id = f"{wrapper_id}/1-1"
    root = service.get("books/atomic-book")
    leaf = service.get(leaf_id)
    pages_payload = yaml.safe_load(compiler._settings.pages_path.read_text(encoding="utf-8"))
    leaf_frontmatter = copy.deepcopy(
        next(page["frontmatter"] for page in pages_payload["pages"] if page["page_id"] == leaf_id)
    )
    leaf_frontmatter["parent"] = f"[[wiki/{group_id}|1.1 실제 절 묶음]]"
    group_frontmatter = verified_book_frontmatter(
        group_id,
        "1.1 실제 절 묶음",
        "books/atomic-book",
    )
    group_frontmatter.update(
        {
            "content_state": "toc-only",
            "source_ids": [],
            "navigation_groups": [{"label": "1.1 실제 절 묶음", "children": [leaf_id]}],
        }
    )
    group_record = VerifiedBookPage(
        page_id=group_id,
        title="1.1 실제 절 묶음",
        body="",
        statement="실제 원문 깊이의 하위 절을 묶는다.",
        current_use="1.1 하위 절을 탐색할 때 사용한다.",
        source_locator="source://atomic-book#section-1-1",
        source_sha256="d" * 64,
        frontmatter=group_frontmatter,
    )
    leaf_record = VerifiedBookPage(
        page_id=leaf_id,
        title="1장 실제 내용",
        body="",
        statement="빈 원문 목차 leaf의 위치를 보존한다.",
        current_use="실제 원문 leaf를 탐색할 때 사용한다.",
        source_locator="source://atomic-book#chapter-01",
        source_sha256="d" * 64,
        frontmatter=leaf_frontmatter,
        expected_revision=leaf.revision,
    )
    root_record = replace(
        record,
        expected_revision=root.revision,
        frontmatter={
            **copy.deepcopy(record.frontmatter),
            "navigation_groups": [{"label": "1부", "children": [group_id]}],
        },
    )
    current_manifest = {
        "nodes": [
            {"canonical_id": wrapper_id, "parent_id": "books/atomic-book"},
            {"canonical_id": leaf_id, "parent_id": wrapper_id},
        ],
        "source_element_assignments": [],
    }
    replacement_manifest = {
        "nodes": [
            {"canonical_id": group_id, "parent_id": "books/atomic-book"},
            {"canonical_id": leaf_id, "parent_id": group_id},
        ],
        "source_element_assignments": [],
    }
    records = (root_record, group_record, leaf_record)
    if authored_child:
        with pytest.raises(
            WoonError,
            match="source-free toc-only retirement contains authored reader content",
        ):
            compiler._validated_book_navigation_reparents(
                records,
                current_manifest,
                replacement_manifest,
                {wrapper_id},
                {wrapper_id: "books/atomic-book"},
            )
        return

    assert compiler._validated_book_navigation_reparents(
        records,
        current_manifest,
        replacement_manifest,
        {wrapper_id},
        {wrapper_id: "books/atomic-book"},
    ) == {
        leaf_id: (wrapper_id, group_id),
    }


def test_rights_toc_wrapper_retirement_rejects_authored_reader_body(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    wrapper_id = convert_atomic_wrapper_to_rights_toc_only(compiler)
    wrapper_path = tmp_path / "wiki" / f"{wrapper_id}.md"
    wrapper_path.write_text(
        wrapper_path.read_text(encoding="utf-8") + "\n이 문장은 보존해야 하는 독자 본문이다.\n",
        encoding="utf-8",
    )
    root = service.get("books/atomic-book")
    wrapper = service.get(wrapper_id)
    coverage_update, _, _ = atomic_book_coverage_update(tmp_path)

    with pytest.raises(
        WoonError,
        match="toc-only rights wrapper retirement contains authored reader content",
    ):
        service.preflight_verified_book_update(
            (replace(record, expected_revision=root.revision),),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper.revision},
            {wrapper_id: hashlib.sha256(_retirement_body(wrapper.body).encode()).hexdigest()},
            coverage_update,
        )


def test_retirement_body_excludes_previous_next_navigation() -> None:
    body = (
        "절이 설명하는 고유한 독자 내용을 보존한다.\n\n"
        "## 이전과 다음\n\n"
        "- 이전: [[wiki/books/book/previous|이전]]\n"
        "- 다음: [[wiki/books/book/next|다음]]\n"
    )

    assert _retirement_body(body) == "절이 설명하는 고유한 독자 내용을 보존한다.\n"


def test_retirement_image_relocation_changes_only_one_exact_markdown_target() -> None:
    old = "wiki/private/_sources/knowledge/local-only/book/images/figure.png"
    new = "wiki/private/_sources/knowledge/local-only/book/images/figure-v2.png"
    body = f"그림 앞의 설명이다.\n\n![그림 1]({old})\n\n그림 뒤의 설명이다.\n"

    relocated = _relocate_retirement_image_targets(body, {old: new})

    assert relocated == body.replace(old, new)
    assert relocated.replace(new, old) == body


def test_retirement_image_relocation_rejects_non_image_or_duplicate_targets() -> None:
    old = "wiki/private/_sources/knowledge/local-only/book/images/figure.png"
    new = "wiki/private/_sources/knowledge/local-only/book/images/figure-v2.png"

    with pytest.raises(WoonError, match="must occur exactly once"):
        _relocate_retirement_image_targets(f"본문의 경로는 {old}다.\n", {old: new})
    with pytest.raises(WoonError, match="must occur exactly once"):
        _relocate_retirement_image_targets(
            f"![첫 그림]({old})\n![둘째 그림]({old})\n",
            {old: new},
        )


def test_navigation_only_body_accepts_exact_legacy_linear_book_heading_only() -> None:
    navigation = (
        "## 책 전체 선형 이동\n\n"
        "- [[wiki/books/book/chapter-01|1장]]\n"
        "- [[wiki/books/book/chapter-02|2장]]\n"
    )

    assert _navigation_only_body(navigation)
    assert not _navigation_only_body(navigation + "\n이 책은 메모리 계층을 설명한다.\n")
    assert not _navigation_only_body("## 책 전체 선형 이동 안내\n")


def test_atomic_verified_book_update_accepts_parent_owned_navigation_wrapper(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    wrapper_id = "books/atomic-book/part-01"
    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    payload = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    wrapper_page = next(page for page in payload["pages"] if page["page_id"] == wrapper_id)
    wrapper_page["frontmatter"].pop("navigation_groups")
    pages_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)
    service.reindex()
    wrapper = service.get(wrapper_id)
    wrapper_body_sha256 = hashlib.sha256(_retirement_body(wrapper.body).encode("utf-8")).hexdigest()

    report = service.apply_verified_book_update(
        (replace(record, expected_revision=service.get("books/atomic-book").revision),),
        {wrapper_id: "books/atomic-book"},
        {wrapper_id: wrapper.revision},
        {wrapper_id: wrapper_body_sha256},
    )

    assert report.retired_page_ids == (wrapper_id,)
    assert compiler.audit().complete


def test_atomic_verified_book_update_accepts_parent_map_navigation_shell(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, _, _ = atomic_book_service(tmp_path)
    root_id = "books/atomic-book"
    wrapper_id = f"{root_id}/part-01"
    leaf_id = f"{root_id}/chapter-01"
    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    payload = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    by_id = {page["page_id"]: page for page in payload["pages"]}
    by_id[wrapper_id]["frontmatter"].pop("navigation_groups")
    by_id[leaf_id]["frontmatter"]["parent"] = f"[[wiki/{root_id}|원자적 책]]"
    pages_path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)
    service.reindex()
    wrapper = service.get(wrapper_id)
    wrapper_body_sha256 = hashlib.sha256(_retirement_body(wrapper.body).encode("utf-8")).hexdigest()

    report = service.apply_verified_book_update(
        (replace(record, expected_revision=service.get(root_id).revision),),
        {wrapper_id: root_id},
        {wrapper_id: wrapper.revision},
        {wrapper_id: wrapper_body_sha256},
    )

    assert report.retired_page_ids == (wrapper_id,)
    assert compiler.audit().complete


def test_atomic_verified_book_update_rejects_retirement_body_hash_mismatch(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, _ = atomic_book_service(tmp_path)
    wrapper_id = "books/atomic-book/part-01"
    input_before = compiler.snapshot_inputs()

    with pytest.raises(WoonError, match="retirement body changed after review"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: "0" * 64},
        )

    assert compiler.snapshot_inputs() == input_before


@pytest.mark.parametrize(
    "body_sha256",
    (
        {},
        {
            "books/atomic-book/part-01": "0" * 64,
            "books/atomic-book/part-02": "1" * 64,
        },
    ),
)
def test_atomic_verified_book_update_requires_exact_retirement_body_keys(
    tmp_path: Path,
    body_sha256: dict[str, str],
) -> None:
    compiler, service, _, record, wrapper_revision, _ = atomic_book_service(tmp_path)
    wrapper_id = "books/atomic-book/part-01"
    input_before = compiler.snapshot_inputs()

    with pytest.raises(WoonError, match="retirement_body_sha256 must match replacements"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            body_sha256,
        )

    assert compiler.snapshot_inputs() == input_before


def test_atomic_verified_book_update_rejects_unpreserved_reader_body(
    tmp_path: Path,
) -> None:
    compiler, service, _, record, wrapper_revision, wrapper_body_sha256 = atomic_book_service(
        tmp_path,
        wrapper_body="1부가 이 책의 가상화 흐름을 소개한다.\n",
    )
    wrapper_id = "books/atomic-book/part-01"
    input_before = compiler.snapshot_inputs()

    with pytest.raises(WoonError, match="reader content is not preserved"):
        service.apply_verified_book_update(
            (record,),
            {wrapper_id: "books/atomic-book"},
            {wrapper_id: wrapper_revision},
            {wrapper_id: wrapper_body_sha256},
        )

    assert compiler.snapshot_inputs() == input_before


def test_learning_checkpoint_uses_compiler_curation_and_receipt(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "ai/retrieval-practice.md",
        "인출 연습",
        "## 설명\n\n자료를 닫고 기억에서 답한다.\n",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    current = service.get("ai/retrieval-practice")

    report = service.record_learning_checkpoint(
        LearningCheckpoint(
            canonical_id="ai/retrieval-practice",
            unit="자료 없이 핵심을 재현한다",
            status="confirmed",
            evidence=("정의를 보지 않고 설명하고 변형 문제를 풀었다.",),
            unstable=(),
            next_question="다른 예제에서도 같은 원리를 설명할 수 있는가?",
            recorded_on=date(2026, 8, 29),
        ),
        current.revision,
    )

    assert report.changed is True
    assert report.compiler_owned is True
    assert "- 상태: 확인됨" in service.get("ai/retrieval-practice").body
    assert compiler.audit().complete

    replayed = service.record_learning_checkpoint(
        LearningCheckpoint(
            canonical_id="ai/retrieval-practice",
            unit="자료 없이 핵심을 재현한다",
            status="confirmed",
            evidence=("정의를 보지 않고 설명하고 변형 문제를 풀었다.",),
            unstable=(),
            next_question="다른 예제에서도 같은 원리를 설명할 수 있는가?",
            recorded_on=date(2026, 8, 29),
        ),
        report.revision,
    )
    assert replayed.changed is False


def test_curated_revision_supersedes_prior_claim_with_external_evidence(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "personal/career/job-search.md", "채용 탐색", "기존 후보를 검토한다.\n")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="personal/career/job-search",
                body="## 현재 후보\n\n공식 공고를 확인한다.\n",
                statement="공식 공고를 현재 이력과 대조한다.",
                current_use="현재 지원 후보를 고를 때 사용한다.",
            ),
        )
    )

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = pages["pages"][0]
    prior_source_id = page["render"]["source_id"]
    prior_claim = next(
        item
        for item in claims["claims"]
        if item["kind"] == "curated-document" and prior_source_id in item["source_ids"]
    )
    external_source_id = "source://verified-source/job-snapshot"
    external_body = "공식 공고 원문이다.\n"
    external_sha256 = hashlib.sha256(external_body.encode("utf-8")).hexdigest()
    sources["sources"].append(
        {
            "source_id": external_source_id,
            "kind": "verified-source-snapshot",
            "locator": "private/job-snapshot.md",
            "original_sha256": external_sha256,
            "normalized_sha256": external_sha256,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": "공식 공고 snapshot",
            "purpose": "공고 확인 시점의 근거를 보존한다.",
            "body": external_body,
        }
    )
    prior_claim["source_ids"].append(external_source_id)
    page["source_ids"].append(external_source_id)
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)

    report = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="personal/career/job-search",
                body="## 현재 후보\n\n살아 있는 공식 공고만 유지한다.\n",
                statement="현재 모집 중인 공식 공고만 유지한다.",
            ),
        )
    )

    assert report.compiled == 1
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))["claims"]
    archived = next(item for item in claims if item["claim_id"] == prior_claim["claim_id"])
    assert archived["status"] == "superseded"
    page = yaml.safe_load(pages_path.read_text(encoding="utf-8"))["pages"][0]
    assert external_source_id in page["source_ids"]
    assert compiler.audit().complete


def test_public_curation_keeps_local_history_out_of_current_page_provenance(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "README.md", "Wiki", "단일 지식 정본 입구다.\n")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = pages["pages"][0]
    page["frontmatter"]["publish"] = True
    page["frontmatter"]["access"] = "public"
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="README",
                body="단일 지식 정본의 공개 가능한 주제 입구다.\n",
                statement="Wiki는 하나의 지식 정본이다.",
                current_use="공개 가능한 주제 탐색 입구로 사용한다.",
            ),
        )
    )

    assert report.compiled == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    local_history = [item for item in sources if item["privacy"] == "local-only"]
    public_current = [item for item in sources if item["privacy"] == "public"]
    assert local_history
    assert len(public_current) == 1
    page = yaml.safe_load(pages_path.read_text(encoding="utf-8"))["pages"][0]
    assert page["source_ids"] == [public_current[0]["source_id"]]
    assert compiler.audit().complete


def test_retire_pages_redirects_relations_and_preserves_inactive_provenance(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "ai/old-topic.md", "중복 개념", "같은 개념을 반복한다.\n")
    write_page(tmp_path, "ai/canonical-topic.md", "정본 개념", "하나의 설명으로 병합한다.\n")
    write_page(tmp_path, "ai/reader.md", "읽는 문서", "정본 개념을 함께 읽는다.\n")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    reader = next(item for item in pages["pages"] if item["page_id"] == "ai/reader")
    reader["frontmatter"]["related_to"] = [
        "old-topic",
        "[[wiki/ai/old-topic|중복 개념]]",
    ]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(page_ids=("ai/reader",))

    report = compiler.retire_pages({"ai/old-topic": "ai/canonical-topic"})

    assert report.retired == 1
    assert report.page_ids == ("ai/old-topic",)
    assert not (tmp_path / "wiki/ai/old-topic.md").exists()
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))["pages"]
    assert {item["page_id"] for item in pages} == {"ai/canonical-topic", "ai/reader"}
    reader = next(item for item in pages if item["page_id"] == "ai/reader")
    assert reader["frontmatter"]["related_to"] == [
        "canonical-topic",
        "[[wiki/ai/canonical-topic|중복 개념]]",
    ]
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    retired_source = next(item for item in sources if item["locator"] == "ai/old-topic.md")
    assert retired_source["lifecycle"] == "archived"
    assert retired_source["superseded_by"].endswith("ai/canonical-topic.md")
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    retired_claim = next(
        item for item in claims if item["claim_id"] == "claim://legacy-wiki/ai/old-topic"
    )
    assert retired_claim["status"] == "superseded"
    assert retired_claim["superseded_by"] == "claim://legacy-wiki/ai/canonical-topic"
    assert compiler.compile().compiled == 0
    assert compiler.audit().complete


def test_initialize_curation_refuses_to_replace_existing_records(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    with pytest.raises(WoonError, match="already exists"):
        compiler.initialize_curation()


def test_refresh_provisional_curation_skips_manually_confirmed_records(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    curation_path = tmp_path / "catalog/llm-wiki/curation.yaml"
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    curation["current_use"] = "예전 자동 문구"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.refresh_provisional_curation() == 1
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    assert curation["current_use"].startswith("가상 메모리 내용")

    curation["current_use"] = "사람이 검토해 정한 현재 활용 목적"
    curation["basis"] = "manual-review"
    curation["status"] = "confirmed"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.refresh_provisional_curation() == 0
    current = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    assert current["curations"][0]["current_use"] == "사람이 검토해 정한 현재 활용 목적"


def test_refresh_provisional_curation_promotes_explicit_curated_page(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    add_curated_source(
        tmp_path,
        purpose="가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
    )

    assert compiler.refresh_provisional_curation() == 1
    curations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )
    assert curations["curations"][0] == {
        "page_id": "os/virtual-memory",
        "current_use": "가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
        "basis": "manual-review",
        "status": "confirmed",
    }


def test_initialize_curation_uses_explicit_source_provenance(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    add_curated_source(
        tmp_path,
        purpose="가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
    )
    (tmp_path / "catalog/llm-wiki/curation.yaml").unlink()

    assert compiler.initialize_curation() == 1
    curations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )
    assert curations["curations"][0]["basis"] == "manual-review"
    assert curations["curations"][0]["status"] == "confirmed"


def test_compiled_service_fails_closed_until_source_change_is_built(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    canonical_root = tmp_path / "wiki/canonical"
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    service.reindex()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    replace_source_body(
        sources["sources"][0], "헥사고날 아키텍처로 외부 기술의 의존 방향을 분리한다.\n"
    )
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="compiled Wiki is stale"):
        service.search("헥사고날", 1)

    assert service.compile().compiled == 1
    assert service.search("헥사고날", 1)[0].canonical_id == "backend/ports-and-adapters"


def test_compiler_rebuilds_derived_relations_from_page_specs(tmp_path: Path) -> None:
    write_page(tmp_path, "canonical/backend/first.md", "첫 문서", "첫 설명.")
    write_page(tmp_path, "canonical/backend/second.md", "둘째 문서", "둘째 설명.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = next(item for item in pages["pages"] if item["page_id"] == "canonical/backend/first")
    page["frontmatter"]["related"] = ["backend/second"]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    relations_path = tmp_path / "catalog/llm-wiki/relations.yaml"
    relations_path.write_text("version: 1\nrelations: []\n", encoding="utf-8")

    assert compiler.compile().compiled == 1
    relations = yaml.safe_load(relations_path.read_text(encoding="utf-8"))
    assert relations["relations"] == [
        {
            "from_page_id": "canonical/backend/first",
            "type": "related",
            "to_id": "backend/second",
        }
    ]
    assert compiler.audit().complete


def test_compiler_derives_relations_from_legacy_related_to_wikilinks(tmp_path: Path) -> None:
    write_page(tmp_path, "ai/attention.md", "Attention", "관련 개념을 연결한다.")
    write_page(tmp_path, "ai/query-key-value.md", "Query, Key, Value", "입력의 역할을 나눈다.")
    map_path = tmp_path / "maps/transformer-attention-map.md"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text("# Transformer attention map\n", encoding="utf-8")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = next(item for item in pages["pages"] if item["page_id"] == "ai/attention")
    page["frontmatter"]["related_to"] = [
        "[[wiki/ai/query-key-value|Query, Key, Value]]",
        "[[maps/transformer-attention-map#핵심 링크|Attention 지도]]",
    ]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    compiler.compile()

    relations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/relations.yaml").read_text(encoding="utf-8")
    )
    assert relations["relations"] == [
        {
            "from_page_id": "ai/attention",
            "type": "related",
            "to_id": "ai/query-key-value",
        },
        {
            "from_page_id": "ai/attention",
            "type": "related",
            "to_id": "maps/transformer-attention-map",
        },
    ]
    assert compiler.audit().complete


def test_compiler_audit_rejects_orphan_records_and_unresolved_relation_targets(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    orphan_source = dict(sources["sources"][0])
    orphan_source["source_id"] = "source://legacy-wiki/orphan.md"
    orphan_source["locator"] = "orphan.md"
    sources["sources"].append(orphan_source)
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    orphan_claim = dict(claims["claims"][0])
    orphan_claim["claim_id"] = "claim://legacy-wiki/orphan"
    claims["claims"].append(orphan_claim)
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    pages["pages"][0]["frontmatter"]["related"] = ["missing-target"]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)

    audit = compiler.audit()

    assert "source://legacy-wiki/orphan.md: source has no page spec" in audit.errors
    assert "claim://legacy-wiki/orphan: claim has no page spec" in audit.errors
    assert "relation target does not resolve: missing-target" in audit.errors


def test_archive_preserves_replaced_conversation_revision_as_superseded_history(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/transaction-boundary",
        title="트랜잭션 경계",
        domain="backend",
        summary="데이터 변경과 외부 호출의 순서를 구분한다.",
        purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
    )

    first_body = "## 첫 기록\n\n처음 확인한 경계다."
    second_body = "## 갱신한 기록\n\n새 근거를 반영한 경계다."
    compiler.archive(
        metadata,
        first_body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-first", first_body),
    )
    compiler.archive(
        metadata,
        second_body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-second", second_body),
    )

    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    prior_source = next(source for source in sources if "처음 확인한" in source["body"])
    current_source = next(source for source in sources if "새 근거를" in source["body"])
    prior_claim = next(claim for claim in claims if "처음 확인한" in claim["markdown"])
    current_claim = next(claim for claim in claims if "새 근거를" in claim["markdown"])

    assert prior_source["lifecycle"] == "archived"
    assert prior_source["superseded_by"] == current_source["source_id"]
    assert prior_claim["status"] == "superseded"
    assert prior_claim["superseded_by"] == current_claim["claim_id"]
    assert compiler.audit().complete


def test_archive_rejects_automated_or_sensitive_ingestion_origins(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/archive-boundary",
        title="아카이브 경계",
        domain="backend",
        summary="자동 수집은 근거 확인 전 공개 문서를 직접 쓰지 않는다.",
        purpose="수집과 검증의 경계를 확인한다.",
    )

    for origin in ("email", "chat", "novel", "system", "tool", "reasoning"):
        with pytest.raises(WoonError, match="only accepts"):
            compiler.archive(metadata, "## 원문\n\n차단되어야 한다.", (), archive_origin=origin)


def test_archive_requires_human_approval_bound_to_exact_body(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/archive-approval",
        title="아카이브 승인",
        domain="backend",
        summary="검토 승인은 정확한 본문에만 묶인다.",
        purpose="검토 receipt 경계를 확인한다.",
    )
    approved_body = "## 승인 본문\n\n검토한 본문이다."
    review_id = approve_archive(tmp_path, "review-bound", approved_body)

    with pytest.raises(WoonError, match="bound to the input hash"):
        compiler.archive(
            metadata,
            "## 바뀐 본문\n\n승인 뒤에 변경됐다.",
            (),
            approved_review_id=review_id,
        )
    with pytest.raises(WoonError, match="requires approved_review_id"):
        compiler.archive(metadata, approved_body, ())


def test_service_archive_restores_compiler_inputs_when_compilation_fails(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    service.reindex()
    body = "## 너무 긴 단일 주장\n\n" + ("분리해야 할 설명이다. " * 160)
    review_id = approve_archive(tmp_path, "review-compile-failure", body)
    before = compiler.snapshot_inputs()

    with pytest.raises(WoonError, match="exceeds 1800 characters"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/oversized-claim",
                title="너무 긴 단일 주장",
                domain="backend",
                summary="긴 본문은 하나의 주장으로 컴파일하지 않는다.",
                purpose="컴파일 실패가 catalog를 오염시키지 않는지 검증한다.",
            ),
            body,
            approved_review_id=review_id,
        )

    assert compiler.snapshot_inputs() == before
    assert not (tmp_path / "wiki/backend/oversized-claim.md").exists()


def test_compiler_navigation_issues_validate_the_reader_tree(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "README.md").write_text(
        """---
type: Wiki
title: Wiki
summary: 최상위 입구다.
canonical_id: README
node_kind: root
view_mode: tree
knowledge_state: 근거 확인됨
updated: 2026-08-31
keywords: [Wiki]
aliases: []
---

# Wiki
""",
        encoding="utf-8",
    )
    (wiki / "orphan.md").write_text(
        """---
type: Wiki
title: 고아 개념
summary: 잘못된 부모를 가진 문서다.
canonical_id: concepts/orphan
node_kind: topic
view_mode: article
knowledge_state: 근거 확인됨
updated: 2026-08-31
keywords: [고아 개념]
aliases: []
parent: '[[wiki/missing|없는 부모]]'
---

# 고아 개념

설명이다.
""",
        encoding="utf-8",
    )

    issues = CompiledWiki(compiled_settings(tmp_path)).navigation_issues()

    assert any("parent is missing" in issue for issue in issues)


def test_audit_rejects_archived_source_without_current_matching_review(tmp_path: Path) -> None:
    write_page(tmp_path, "canonical/backend/review-recheck.md", "승인 재검증", "기존 기록.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    body = "## 승인 기록\n\n승인 receipt를 다시 확인한다."
    compiler.archive(
        DocumentMetadata(
            canonical_id="backend/review-recheck",
            title="승인 재검증",
            domain="backend",
            summary="승인 receipt가 현재도 일치해야 한다.",
            purpose="archive 승인 경계를 검증한다.",
        ),
        body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-recheck", body),
    )
    review_path = tmp_path / "catalog/llm-wiki/review-queue.yaml"
    review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review["items"][0]["input_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review, allow_unicode=True), encoding="utf-8")

    with pytest.raises(WoonError, match="no matching review evidence"):
        compiler.compile()
    audit = compiler.audit()

    assert not audit.complete
    assert any("no matching review evidence" in error for error in audit.errors)


def test_git_restore_rejects_hash_that_does_not_match_body(tmp_path: Path) -> None:
    compiler = CompiledWiki(compiled_settings(tmp_path))
    with pytest.raises(WoonError, match="body hash does not match"):
        compiler.restore_from_git(
            DocumentMetadata(
                canonical_id="backend/git-restore",
                title="Git 복구",
                domain="backend",
                summary="Git 복구의 본문 hash를 검증한다.",
                purpose="복구 경계를 검증한다.",
            ),
            "## 복구\n\nGit 본문.",
            "abc123",
            "0" * 64,
        )


def test_compiled_wiki_restores_confirmed_git_revision(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.email", "test@example.com"], check=True)
    write_page(tmp_path, "os/seed.md", "초기 정본", "컴파일러 입력을 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    metadata = DocumentMetadata(
        canonical_id="backend/git-backed-restore",
        title="Git 정본 복구",
        domain="backend",
        summary="확정한 Git revision만 복구한다.",
        purpose="정상 복구가 승인 queue와 충돌하지 않는지 검증한다.",
    )
    first_body = "## 첫 기록\n\n첫 번째 Git 정본이다."
    first = service.archive(
        metadata,
        first_body,
        approved_review_id=approve_archive(tmp_path, "review-git-first", first_body),
    )
    subprocess.run(["git", "-C", tmp_path, "add", "wiki"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "docs: first"], check=True)
    revision = subprocess.run(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second_body = "## 두 번째 기록\n\n현재 정본이다."
    second = service.archive(
        metadata,
        second_body,
        first.document.revision,
        approved_review_id=approve_archive(tmp_path, "review-git-second", second_body),
    )

    restored = service.restore(
        metadata.canonical_id,
        revision,
        second.document.revision,
        confirmed=True,
    )

    assert "첫 번째 Git 정본" in restored.document.body
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    current = next(source for source in sources if source["lifecycle"] == "compiled")
    assert current["archive_origin"] == "git-restore"
    assert current["source_session_ids"] == [f"git:{revision}"]


def test_reconcile_marks_unreferenced_conversation_revision_without_deleting_it(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/transaction-boundary",
        title="트랜잭션 경계",
        domain="backend",
        summary="데이터 변경과 외부 호출의 순서를 구분한다.",
        purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
    )
    body = "## 현재 기록\n\n현재 정본이다."
    compiler.archive(
        metadata,
        body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-current", body),
    )

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    current_source = next(
        source for source in sources["sources"] if source["kind"] == "conversation"
    )
    current_claim = next(
        claim for claim in claims["claims"] if claim["kind"] == "conversation-summary"
    )
    prior_body = "## 이전 기록\n\n나중에 복구할 수 있는 원본이다.\n"
    prior_digest = hashlib.sha256(prior_body.encode("utf-8")).hexdigest()
    prior_source_id = "source://conversation/backend/transaction-boundary/" + "a" * 24
    prior_claim_id = "claim://conversation/backend/transaction-boundary/" + "a" * 24
    prior_review_id = approve_archive(tmp_path, "review-prior", prior_body)
    sources["sources"].append(
        {
            **current_source,
            "source_id": prior_source_id,
            "original_sha256": prior_digest,
            "normalized_sha256": prior_digest,
            "body": prior_body,
            "approved_review_id": prior_review_id,
        }
    )
    claims["claims"].append(
        {
            **current_claim,
            "claim_id": prior_claim_id,
            "source_ids": [prior_source_id],
            "markdown": prior_body,
        }
    )
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = compiler.reconcile_superseded_revisions()

    assert report.archived_sources == 1
    assert report.superseded_claims == 1
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))["sources"]
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))["claims"]
    prior_source = next(source for source in sources if source["source_id"] == prior_source_id)
    prior_claim = next(claim for claim in claims if claim["claim_id"] == prior_claim_id)
    assert prior_source["body"] == prior_body
    assert prior_source["superseded_by"] == current_source["source_id"]
    assert prior_claim["markdown"] == prior_body
    assert prior_claim["superseded_by"] == current_claim["claim_id"]
    assert compiler.audit().complete


def test_composed_page_rejects_a_whole_document_as_one_claim(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    pages["pages"][0]["render"] = {"kind": "claims"}
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    claims["claims"][0]["markdown"] = "가" * 1_801
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="split it by revisitable claim"):
        compiler.compile(force=True)


def test_compiler_rejects_source_body_that_does_not_match_normalized_digest(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["sources"][0]["body"] = "검증 없이 바뀐 source 본문.\n"
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="normalized_sha256 does not match"):
        compiler.compile(force=True)


def test_compiled_archive_restores_inputs_and_output_when_reindex_fails(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    canonical_root = tmp_path / "wiki"
    index = FailOnceIndex(tmp_path / ".local/search.sqlite3")
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        index,
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    service.reindex()
    index.fail_next = True

    body = "## 경계\n\n데이터 변경과 외부 호출의 순서를 분리한다."
    with pytest.raises(RuntimeError, match="injected index failure"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/transaction-boundary",
                title="트랜잭션 경계",
                domain="backend",
                summary="요청 처리의 원자성을 정의한다.",
                purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
                source_ids=("session://2026-08-14/001",),
            ),
            body,
            approved_review_id=approve_archive(tmp_path, "review-index-failure", body),
        )

    assert not (canonical_root / "backend/transaction-boundary.md").exists()
    assert compiler.audit().complete


def test_compiled_archive_preserves_session_ownership_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    canonical_root = tmp_path / "wiki"
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )

    body = "## 경계\n\n데이터 변경과 외부 호출의 순서를 분리한다."
    service.archive(
        DocumentMetadata(
            canonical_id="backend/transaction-boundary",
            title="트랜잭션 경계",
            domain="backend",
            summary="요청 처리의 원자성을 정의한다.",
            purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
            source_ids=("session://2026-08-14/001",),
        ),
        body,
        approved_review_id=approve_archive(tmp_path, "review-session-owner", body),
    )

    archived = (canonical_root / "backend/transaction-boundary.md").read_text(encoding="utf-8")
    assert "source_ids:\n- session://2026-08-14/001" in archived
    assert "purpose: 트랜잭션 경계와 복구 순서를 설계할 때 재사용한다." in archived
    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    conversation = next(source for source in sources["sources"] if source["kind"] == "conversation")
    assert conversation["purpose"] == "트랜잭션 경계와 복구 순서를 설계할 때 재사용한다."

    del conversation["purpose"]
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(WoonError, match="requires non-empty purpose"):
        compiler.compile()

    with pytest.raises(WoonError, match="already owned"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/transaction-order",
                title="트랜잭션 순서",
                domain="backend",
                summary="요청 처리 단계의 순서를 정의한다.",
                purpose="트랜잭션 실행 순서를 설계할 때 재사용한다.",
                source_ids=("session://2026-08-14/001",),
            ),
            "## 순서\n\n쓰기와 외부 호출의 실행 순서를 관리한다.",
        )
