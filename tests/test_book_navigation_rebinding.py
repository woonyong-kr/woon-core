import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import _source_structure_id
from woon_core.knowledge.book_navigation_rebinding import (
    BookNavigationCoverageRebinding,
    prepare_book_navigation_rebindings,
    validate_book_navigation_outputs,
)


def test_navigation_rebinding_preserves_evidence_and_rebases_verified_scope(tmp_path: Path) -> None:
    old, update, pages, roots, retiring = _fixture(tmp_path)
    writes = prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, retiring)
    assert len(writes) == 2
    replacement = json.loads(writes[tmp_path / update.relative_path])
    assert replacement["source_elements"] == old["source_elements"]
    assert replacement["phase_evidence"] == old["phase_evidence"]
    assert replacement["workflow_phase"] == "toc-indexed"
    scope_path = tmp_path / next(iter(update.scope_sha256))
    scope = json.loads(writes[scope_path])
    original_scope = json.loads(scope_path.read_bytes())
    original_scope["coverage_scope"]["base_sha256"] = hashlib.sha256(
        writes[tmp_path / update.relative_path]
    ).hexdigest()
    assert scope == original_scope
    assert json.loads((tmp_path / update.relative_path).read_bytes()) == old


@pytest.mark.parametrize(
    "fault",
    [
        "evidence",
        "progress",
        "prose",
        "semantic-owner",
        "scope-pin",
        "retirement",
        "source-assignment",
        "stale",
    ],
)
def test_navigation_rebinding_rejects_loss_or_unpinned_writes(tmp_path: Path, fault: str) -> None:
    _, update, pages, roots, retiring = _fixture(tmp_path)
    if fault == "evidence":
        update.replacement["source_elements"] = []
    elif fault == "progress":
        update.replacement["workflow_phase"] = "reviewed"
    elif fault == "prose":
        (tmp_path / "wiki/books/example/empty.md").write_text(
            "---\ntitle: 장\n---\n\n# 장\n\n보존할 설명\n"
        )
    elif fault == "scope-pin":
        update = replace(update, scope_sha256={})
    elif fault == "retirement":
        retiring = frozenset({"books/example/empty", "other/empty"})
    elif fault == "source-assignment":
        update.replacement["source_structure_assignments"] = []
    elif fault == "stale":
        update = replace(update, expected_sha256="0" * 64)
    else:
        path = tmp_path / update.relative_path
        old = json.loads(path.read_bytes())
        old["source_element_assignments"] = [{"owner_id": "books/example/empty"}]
        path.write_text(json.dumps(old))
        update = replace(update, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        update.replacement["source_element_assignments"] = old["source_element_assignments"]
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(WoonError):
        prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, retiring)
    assert {path: path.read_bytes() for path in before} == before


def _fixture(tmp_path: Path):
    book = "books/example"
    empty = book + "/empty"
    path = tmp_path / "catalog/book-coverage/example.json"
    path.parent.mkdir(parents=True)
    output = tmp_path / "wiki/books/example/empty.md"
    output.parent.mkdir(parents=True)
    output.write_text("---\ntitle: 장\n---\n\n# 장\n")
    current = {
        "schema_version": 3,
        "book_id": book,
        "workflow_phase": "toc-indexed",
        "edition": {"label": "1판", "source_sha256": "a" * 64},
        "phase_evidence": {"toc-indexed": {"verified": "source TOC"}},
        "nodes": [{"canonical_id": empty, "has_direct_content": False, "leaf": False}],
        "toc_node_count": 1,
        "toc_leaf_count": 0,
        "source_structure_elements": [{"structure_id": "structure:one", "title": "1장"}],
        "source_structure_assignments": [
            {
                "structure_id": "structure:one",
                "disposition": "canonical-node",
                "canonical_id": empty,
            }
        ],
        "source_elements": [{"element_id": "claim:original"}],
        "source_element_assignments": [],
    }
    path.write_text(json.dumps(current))
    before_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    after = copy.deepcopy(current)
    after.update(
        nodes=[],
        toc_node_count=0,
        source_structure_assignments=[
            {
                "structure_id": "structure:one",
                "disposition": "book-root-heading",
                "owner_id": book,
                "heading": "## 1장",
                "source_order": 1,
            }
        ],
    )
    scope_path = tmp_path / "catalog/book-coverage-scopes/example/chapter-02.json"
    scope_path.parent.mkdir(parents=True)
    scope_path.write_text(
        json.dumps(
            {
                "book_id": book,
                "coverage_scope": {
                    "base_relative_path": path.relative_to(tmp_path).as_posix(),
                    "base_sha256": before_sha,
                },
                "nodes": [],
                "source_structure_assignments": [],
                "source_element_assignments": [{"delivery_span": "이미 확인한 본문"}],
            }
        )
    )
    update = BookNavigationCoverageRebinding(
        path.relative_to(tmp_path).as_posix(),
        before_sha,
        after,
        {
            scope_path.relative_to(tmp_path).as_posix(): hashlib.sha256(
                scope_path.read_bytes()
            ).hexdigest()
        },
    )
    pages = {empty: {"output_path": "books/example/empty.md"}}
    roots = {
        book: {
            "frontmatter": {
                "entity_kind": "book",
                "access": "local-only",
                "publish": False,
                "reader_navigation": "sidebar-only",
            }
        }
    }
    return current, update, pages, roots, frozenset({empty})


def _display_fixture(tmp_path: Path):
    old, update, _, roots, _ = _fixture(tmp_path)
    book = old["book_id"]
    element = {
        "kind": "section",
        "title": "1 소개",
        "source_locator": "source://example/1",
        "source_sha256": "a" * 64,
    }
    element["structure_id"] = _source_structure_id(
        *[element[k] for k in ("kind", "title", "source_locator", "source_sha256")]
    )
    old.update(
        nodes=[],
        toc_node_count=0,
        source_structure_elements=[element],
        source_structure_assignments=[
            {
                "structure_id": element["structure_id"],
                "disposition": "book-root-heading",
                "owner_id": book,
                "heading": "## 1 소개",
                "source_order": 1,
            }
        ],
        source_structure_inventory_evidence={
            "locator": "source://example/inventory",
            "sha256": "a" * 64,
            "verified_on": "2026-09-11",
        },
    )
    manifest = tmp_path / update.relative_path
    manifest.write_text(json.dumps(old))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    scope_path = tmp_path / next(iter(update.scope_sha256))
    scope = json.loads(scope_path.read_bytes())
    scope["coverage_scope"]["base_sha256"] = digest
    scope_path.write_text(json.dumps(scope))
    reader = tmp_path / "private/reader.md"
    reader.parent.mkdir()
    reader.write_text("# 1 소개\n\n실제 원문 설명\n")
    after = copy.deepcopy(old)
    after["source_structure_assignments"][0].update(
        disposition="private-reader",
        heading="# 1 소개",
        reader_path="private/reader.md",
        reader_anchor="",
        reader_sha256=hashlib.sha256(reader.read_bytes()).hexdigest(),
        body_sha256=hashlib.sha256("실제 원문 설명".encode()).hexdigest(),
    )
    update = replace(
        update,
        expected_sha256=digest,
        replacement=after,
        scope_sha256={
            scope_path.relative_to(tmp_path).as_posix(): hashlib.sha256(
                scope_path.read_bytes()
            ).hexdigest(),
        },
    )
    root = tmp_path / "wiki/books/example.md"
    root.write_text(
        "---\nentity_kind: book\naccess: local-only\npublish: false\n---\n"
        "# 예제\n\n## [1 소개](../../private/reader.md)\n"
    )
    return old, update, {book: {"output_path": "books/example.md"}}, roots


def test_display_only_rebinding_keeps_owners_progress_and_validates_actual_output(tmp_path: Path):
    old, update, pages, roots = _display_fixture(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    writes = prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())
    after = json.loads(writes[tmp_path / update.relative_path])
    assert {k: v for k, v in old.items() if k != "source_structure_assignments"} == {
        k: v for k, v in after.items() if k != "source_structure_assignments"
    }
    scope_path = tmp_path / next(iter(update.scope_sha256))
    expected_scope = json.loads(before[scope_path])
    expected_scope["coverage_scope"]["base_sha256"] = hashlib.sha256(
        writes[tmp_path / update.relative_path]
    ).hexdigest()
    assert json.loads(writes[scope_path]) == expected_scope
    validate_book_navigation_outputs(tmp_path, (update,), pages)
    assert {p: p.read_bytes() for p in before} == before
    root = tmp_path / "wiki/books/example.md"
    root.write_text(root.read_text() + "- [별도 도입](../../private/reader.md)\n")
    with pytest.raises(WoonError, match="exactly once"):
        validate_book_navigation_outputs(tmp_path, (update,), pages)


@pytest.mark.parametrize(
    "fault",
    [
        "nodes",
        "count",
        "inventory",
        "meaning",
        "progress",
        "owner",
        "order",
        "identity",
        "canonical-owner",
        "extra-field",
        "scope-pin",
        "noop",
    ],
)
def test_display_only_rebinding_rejects_changes_outside_delivery(tmp_path: Path, fault: str):
    old, update, pages, roots = _display_fixture(tmp_path)
    after = update.replacement
    row = after["source_structure_assignments"][0]
    if fault == "nodes":
        after["nodes"] = [{"canonical_id": "other"}]
    elif fault == "count":
        after["toc_leaf_count"] = 1
    elif fault == "inventory":
        after["source_structure_elements"][0]["title"] = "다른 구조"
    elif fault == "meaning":
        after["source_element_assignments"] = [{"owner_id": old["book_id"]}]
    elif fault == "progress":
        after["workflow_phase"] = "reviewed"
    elif fault == "owner":
        row["owner_id"] = "other/book"
    elif fault == "order":
        row["source_order"] = 2
    elif fault == "identity":
        row["structure_id"] = "structure:other"
    elif fault == "canonical-owner":
        row["disposition"] = "canonical-node"
        row["canonical_id"] = "other/leaf"
    elif fault == "extra-field":
        row["content_complete"] = True
    elif fault == "scope-pin":
        update = replace(update, scope_sha256={})
    else:
        update = replace(update, replacement=copy.deepcopy(old))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(WoonError):
        prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())
    assert {p: p.read_bytes() for p in before} == before


def test_no_retirement_cannot_reassign_an_existing_canonical_owner(tmp_path: Path):
    _, update, pages, roots, _ = _fixture(tmp_path)
    old = json.loads((tmp_path / update.relative_path).read_bytes())
    update.replacement["nodes"] = old["nodes"]
    update.replacement["toc_node_count"] = old["toc_node_count"]
    with pytest.raises(WoonError, match="existing root delivery"):
        prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())


def test_display_only_cannot_refresh_a_scope_with_stale_structure_delivery(tmp_path: Path):
    old, update, pages, roots = _display_fixture(tmp_path)
    scope_path = tmp_path / next(iter(update.scope_sha256))
    scope = json.loads(scope_path.read_bytes())
    scope["source_structure_assignments"] = copy.deepcopy(old["source_structure_assignments"])
    scope_path.write_text(json.dumps(scope))
    update = replace(
        update,
        scope_sha256={
            scope_path.relative_to(tmp_path).as_posix(): hashlib.sha256(
                scope_path.read_bytes()
            ).hexdigest(),
        },
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(WoonError, match="affected or stale chapter scope"):
        prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("fault", [None, "source-position", "remaining-order", "source-label"])
def test_existing_chapter_group_can_link_reader_without_reordering_other_groups(
    tmp_path: Path, fault
):
    old, update, pages, roots = _display_fixture(tmp_path)
    chapter = old["source_structure_elements"][0]
    chapter["kind"] = "chapter"
    chapter["structure_id"] = _source_structure_id(
        *[chapter[k] for k in ("kind", "title", "source_locator", "source_sha256")]
    )
    old["source_structure_assignments"][0] = {
        "structure_id": chapter["structure_id"],
        "disposition": "navigation-group-heading",
        "owner_id": old["book_id"],
        "label": chapter["title"],
        "source_order": 1,
    }
    second = {**chapter, "title": "2 다음 장", "source_locator": "source://example/2"}
    second["structure_id"] = _source_structure_id(
        *[second[k] for k in ("kind", "title", "source_locator", "source_sha256")]
    )
    old["source_structure_elements"].append(second)
    old["source_structure_assignments"].append(
        {
            "structure_id": second["structure_id"],
            "disposition": "navigation-group-heading",
            "owner_id": old["book_id"],
            "label": second["title"],
            "source_order": 2,
        }
    )
    after = copy.deepcopy(old)
    after["source_structure_assignments"][0] = {
        **update.replacement["source_structure_assignments"][0],
        "structure_id": chapter["structure_id"],
    }
    if fault == "source-position":
        after["source_structure_assignments"][0]["source_order"] = 2
    elif fault == "remaining-order":
        after["source_structure_assignments"][1]["source_order"] = 1
    elif fault == "source-label":
        after["source_structure_assignments"][1]["label"] = "다른 장"
    manifest = tmp_path / update.relative_path
    manifest.write_text(json.dumps(old))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    scope_path = tmp_path / next(iter(update.scope_sha256))
    scope = json.loads(scope_path.read_bytes())
    scope["coverage_scope"]["base_sha256"] = digest
    scope_path.write_text(json.dumps(scope))
    update = replace(
        update,
        expected_sha256=digest,
        replacement=after,
        scope_sha256={
            scope_path.relative_to(tmp_path).as_posix(): hashlib.sha256(
                scope_path.read_bytes()
            ).hexdigest(),
        },
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    if fault is None:
        writes = prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())
        assert json.loads(writes[manifest]) == after
    else:
        with pytest.raises(WoonError):
            prepare_book_navigation_rebindings(tmp_path, (update,), pages, roots, frozenset())
    assert {p: p.read_bytes() for p in before} == before
