"""Public scoped-coverage audit regressions for virtual chapter headings."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from woon_core.knowledge.book_coverage import audit_book_coverage_scope

BOOK_ID = "books/navigation-example"
CHAPTERS = (
    ("chapter-01", "1장 첫 번째", "1-1", "1.1 첫 절"),
    ("chapter-02", "2장 두 번째", "2-1", "2.1 다음 절"),
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _structure_id(kind: str, title: str, locator: str, source_sha256: str) -> str:
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
    return f"structure:{_sha(identity)}"


def _element_id(kind: str, semantic_unit: str, locator: str, source_sha256: str) -> str:
    identity = json.dumps(
        {
            "kind": kind,
            "semantic_unit": semantic_unit,
            "source_locator": locator,
            "source_sha256": source_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{kind}:{_sha(identity)}"


def _write_page(path: Path, canonical_id: str, title: str, parent: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"title: {title}\n"
        f"canonical_id: {canonical_id}\n"
        f"parent: '[[wiki/{parent}|book]]'\n"
        "node_kind: detail\n"
        "---\n\n"
        f"# {title}\n\n"
        "자연스러운 한국어 학습 본문이다.\n",
        encoding="utf-8",
    )


def _write_navigation_scope_fixture(vault: Path) -> tuple[str, Path]:
    book_path = vault / "wiki/books/navigation-example.md"
    book_path.parent.mkdir(parents=True, exist_ok=True)
    groups = "\n".join(
        "\n".join(
            (
                f"- label: {chapter_title}",
                "  children:",
                f"  - {BOOK_ID}/{chapter_id}/{section_id}",
            )
        )
        for chapter_id, chapter_title, section_id, _ in CHAPTERS
    )
    headings = "\n\n".join(f"## {chapter_title}" for _, chapter_title, _, _ in CHAPTERS)
    book_path.write_text(
        "---\n"
        "title: Navigation example\n"
        f"canonical_id: {BOOK_ID}\n"
        "node_kind: entity\n"
        "content_kind: book\n"
        "entity_kind: book\n"
        "navigation_groups:\n"
        f"{groups}\n"
        "---\n\n"
        "# Navigation example\n\n"
        f"{headings}\n",
        encoding="utf-8",
    )

    structures: list[dict[str, str]] = []
    assignments: list[dict[str, object]] = []
    nodes: list[dict[str, object]] = []
    elements: list[dict[str, str]] = []
    element_assignments: list[dict[str, str]] = []
    for order, (chapter_id, chapter_title, section_id, section_title) in enumerate(
        CHAPTERS, start=1
    ):
        chapter_locator = f"PDF {order}"
        section_locator = f"PDF {order}:section"
        chapter_structure_id = _structure_id(
            "chapter", chapter_title, chapter_locator, _sha(chapter_id)
        )
        section_structure_id = _structure_id(
            "section", section_title, section_locator, _sha(section_id)
        )
        owner_id = f"{BOOK_ID}/{chapter_id}/{section_id}"
        structures.extend(
            (
                {
                    "structure_id": chapter_structure_id,
                    "kind": "chapter",
                    "title": chapter_title,
                    "source_locator": chapter_locator,
                    "source_sha256": _sha(chapter_id),
                },
                {
                    "structure_id": section_structure_id,
                    "kind": "section",
                    "title": section_title,
                    "source_locator": section_locator,
                    "source_sha256": _sha(section_id),
                },
            )
        )
        assignments.extend(
            (
                {
                    "structure_id": chapter_structure_id,
                    "disposition": "navigation-group-heading",
                    "owner_id": BOOK_ID,
                    "label": chapter_title,
                    "source_order": order,
                },
                {
                    "structure_id": section_structure_id,
                    "disposition": "canonical-node",
                    "canonical_id": owner_id,
                },
            )
        )
        claim_locator = f"PDF {order}:paragraph"
        claim_source_sha256 = _sha(f"claim-{chapter_id}")
        claim_id = _element_id("claim", "paragraph", claim_locator, claim_source_sha256)
        elements.append(
            {
                "element_id": claim_id,
                "kind": "claim",
                "semantic_unit": "paragraph",
                "source_locator": claim_locator,
                "source_sha256": claim_source_sha256,
            }
        )
        span = "자연스러운 한국어 학습 본문이다."
        element_assignments.append(
            {
                "element_id": claim_id,
                "owner_id": owner_id,
                "delivery": "reader-span",
                "delivery_span": span,
                "delivery_span_sha256": _sha(span),
            }
        )
        nodes.append(
            {
                "canonical_id": owner_id,
                "parent_id": BOOK_ID,
                "kind": "section",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": section_locator,
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
        )
        _write_page(vault / f"wiki/{owner_id}.md", owner_id, section_title, BOOK_ID)

    base = {
        "schema_version": 2,
        "book_id": BOOK_ID,
        "edition": {"label": "test", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "test/toc", "verified_on": "2026-09-08"}],
        "toc_node_count": len(nodes),
        "toc_leaf_count": len(nodes),
        "source_structure_inventory_evidence": {
            "locator": "test/structure",
            "sha256": "b" * 64,
            "verified_on": "2026-09-08",
        },
        "source_structure_elements": structures,
        "source_structure_assignments": assignments,
        "retired_source_section_wrappers": [],
        "source_element_inventory_evidence": {
            "locator": "test/elements",
            "sha256": "c" * 64,
            "verified_on": "2026-09-08",
            "extraction_method": "manual-semantic-review",
            "semantic_unit_policy_sha256": "d" * 64,
        },
        "source_elements": elements,
        "source_element_assignments": element_assignments,
        "nodes": nodes,
    }
    base_path = vault / "catalog/book-coverage/navigation-example.json"
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(json.dumps(base), encoding="utf-8")
    return "catalog/book-coverage/navigation-example.json", base_path


def _write_scope(vault: Path, base_relative_path: str, base_path: Path, chapter_id: str) -> str:
    base = json.loads(base_path.read_text(encoding="utf-8"))
    root_id = f"{BOOK_ID}/{chapter_id}"
    base["coverage_scope"] = {
        "root_id": root_id,
        "base_relative_path": base_relative_path,
        "base_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
    }
    base["nodes"] = [node for node in base["nodes"] if node["canonical_id"].startswith(root_id)]
    base["toc_node_count"] = len(base["nodes"])
    base["toc_leaf_count"] = len(base["nodes"])
    section_ids = {node["canonical_id"] for node in base["nodes"]}
    chapter_positions = [
        i for i, row in enumerate(base["source_structure_elements"]) if row["kind"] == "chapter"
    ]
    chapter_index = int(chapter_id.removeprefix("chapter-")) - 1
    start = chapter_positions[chapter_index]
    end = (
        chapter_positions[chapter_index + 1]
        if chapter_index + 1 < len(chapter_positions)
        else len(base["source_structure_elements"])
    )
    structure_ids = {row["structure_id"] for row in base["source_structure_elements"][start:end]}
    base["source_structure_elements"] = [
        element
        for element in base["source_structure_elements"]
        if element["structure_id"] in structure_ids
    ]
    base["source_structure_assignments"] = [
        assignment
        for assignment in base["source_structure_assignments"]
        if assignment["structure_id"] in structure_ids
    ]
    base["source_element_assignments"] = [
        assignment
        for assignment in base["source_element_assignments"]
        if assignment["owner_id"] in section_ids
    ]
    element_ids = {assignment["element_id"] for assignment in base["source_element_assignments"]}
    base["source_elements"] = [
        element for element in base["source_elements"] if element["element_id"] in element_ids
    ]
    path = f"catalog/book-coverage-scopes/navigation-example/{chapter_id}.json"
    scope_path = vault / path
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(json.dumps(base), encoding="utf-8")
    return path


@pytest.mark.parametrize("chapter_id", ["chapter-01", "chapter-02"])
def test_public_scope_audit_accepts_complete_virtual_chapter_group(
    tmp_path: Path, chapter_id: str
) -> None:
    base_relative_path, base_path = _write_navigation_scope_fixture(tmp_path)
    relative_scope = _write_scope(tmp_path, base_relative_path, base_path, chapter_id)

    audit = audit_book_coverage_scope(tmp_path, relative_scope)

    assert audit.complete, audit.errors


def test_public_scope_audit_rejects_incomplete_virtual_chapter_group(tmp_path: Path) -> None:
    base_relative_path, base_path = _write_navigation_scope_fixture(tmp_path)
    relative_scope = _write_scope(tmp_path, base_relative_path, base_path, "chapter-02")
    scope_path = tmp_path / relative_scope
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["nodes"] = []
    scope_path.write_text(json.dumps(scope), encoding="utf-8")

    audit = audit_book_coverage_scope(tmp_path, relative_scope)

    assert not audit.complete
    assert any("nodes must contain" in error for error in audit.errors)


@pytest.mark.parametrize("chapter_id", ["chapter-01", "chapter-02"])
def test_private_chapter_title_preserves_scope_and_other_chapter_order(tmp_path: Path, chapter_id):
    relative, base_path = _write_navigation_scope_fixture(tmp_path)
    base = json.loads(base_path.read_bytes())
    original_nodes = copy.deepcopy(base["nodes"])
    reader = tmp_path / "private/chapter-01.md"
    reader.parent.mkdir()
    reader.write_text("# 1장 첫 번째\n\n책의 원래 도입 설명이다.\n")
    base["source_structure_assignments"][0] = {
        "structure_id": base["source_structure_elements"][0]["structure_id"],
        "disposition": "private-reader",
        "owner_id": BOOK_ID,
        "source_order": 1,
        "heading": "# 1장 첫 번째",
        "reader_path": "private/chapter-01.md",
        "reader_anchor": "",
        "reader_sha256": hashlib.sha256(reader.read_bytes()).hexdigest(),
        "body_sha256": _sha("책의 원래 도입 설명이다."),
    }
    # Chapter 2 keeps its original group/metadata position even though chapter 1 links a reader.
    assert base["source_structure_assignments"][2]["source_order"] == 2
    book = tmp_path / "wiki/books/navigation-example.md"
    book.write_text(
        book.read_text()
        .replace("entity_kind: book\n", "entity_kind: book\naccess: local-only\npublish: false\n")
        .replace("## 1장 첫 번째", "## [1장 첫 번째](../../private/chapter-01.md)")
    )
    base_path.write_text(json.dumps(base))
    scope_relative = _write_scope(tmp_path, relative, base_path, chapter_id)
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    audit = audit_book_coverage_scope(tmp_path, scope_relative)
    assert audit.complete, audit.errors
    assert base["nodes"] == original_nodes
    assert {p: p.read_bytes() for p in snapshot} == snapshot
    if chapter_id == "chapter-01":
        scope_path = tmp_path / scope_relative
        scope = json.loads(scope_path.read_bytes())
        scope["source_structure_assignments"][0]["reader_sha256"] = "0" * 64
        scope_path.write_text(json.dumps(scope))
        audit = audit_book_coverage_scope(tmp_path, scope_relative)
        assert not audit.complete
        assert any("must equal its full pinned manifest" in issue for issue in audit.errors)
        scope_path.write_bytes(snapshot[scope_path])
        reader.write_text(reader.read_text() + "\n변경된 설명\n")
        audit = audit_book_coverage_scope(tmp_path, scope_relative)
        assert not audit.complete
        assert any("reader SHA-256 differs" in issue for issue in audit.errors)
