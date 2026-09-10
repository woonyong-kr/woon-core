"""A source chapter rendered as a book-root heading still has a bounded scope."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import navigation_group_scope_nodes
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    CompiledWiki,
    CompiledWikiSettings,
)


def navigation_scope(tmp_path: Path) -> tuple[CompiledWiki, BookCoverageManifestUpdate]:
    book = "books/example"
    children = [f"{book}/chapter-01/section-01", f"{book}/chapter-01/section-02"]
    base = {
        "schema_version": 2,
        "book_id": book,
        "nodes": [{"canonical_id": child} for child in children],
        "source_structure_elements": [
            {"structure_id": "chapter", "kind": "chapter", "title": "1장 시작"},
            {"structure_id": "one", "kind": "section", "title": "1.1 첫 절"},
            {"structure_id": "two", "kind": "section", "title": "1.2 다음 절"},
        ],
        "source_structure_assignments": [
            {
                "structure_id": "chapter",
                "disposition": "navigation-group-heading",
                "owner_id": book,
                "label": "1장 시작",
                "source_order": 1,
            },
            {"structure_id": "one", "disposition": "canonical-node", "canonical_id": children[0]},
            {"structure_id": "two", "disposition": "canonical-node", "canonical_id": children[1]},
        ],
    }
    path = tmp_path / "catalog/book-coverage/example.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(base), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    fragment = copy.deepcopy(base)
    fragment["coverage_scope"] = {
        "root_id": f"{book}/chapter-01",
        "base_relative_path": "catalog/book-coverage/example.json",
        "base_sha256": digest,
    }
    catalog = tmp_path / "catalog/llm-wiki"
    settings = CompiledWikiSettings(
        tmp_path,
        tmp_path / "wiki",
        catalog / "sources.yaml",
        catalog / "claims.yaml",
        catalog / "pages.yaml",
        catalog / "curation.yaml",
        catalog / "relations.yaml",
        catalog / "receipts.yaml",
        catalog / "review-queue.yaml",
    )
    update = BookCoverageManifestUpdate(
        "catalog/book-coverage-scopes/example/chapter-01.json",
        None,
        fragment,
        "merge-scope",
        "catalog/book-coverage/example.json",
        digest,
        f"{book}/chapter-01",
    )
    return CompiledWiki(settings), update


def test_heading_scope_accepts_all_pinned_children_without_creating_wrapper(tmp_path: Path) -> None:
    compiler, update = navigation_scope(tmp_path)
    base_path = tmp_path / str(update.base_relative_path)
    before = base_path.read_bytes()
    target = compiler.validate_book_coverage_manifest_update(update)
    assert target == tmp_path / update.relative_path
    assert not target.exists()
    assert base_path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["omitted-child", "extra-child", "duplicate-child"])
def test_heading_scope_rejects_nonexact_children(tmp_path: Path, mutation: str) -> None:
    compiler, update = navigation_scope(tmp_path)
    nodes = update.replacement["nodes"]
    if mutation == "omitted-child":
        nodes.pop()
    elif mutation == "extra-child":
        nodes.append({"canonical_id": "books/example/chapter-02/section-01"})
    else:
        nodes.append(copy.deepcopy(nodes[0]))
    with pytest.raises(WoonError):
        compiler.validate_book_coverage_manifest_update(update)
    assert not (tmp_path / update.relative_path).exists()


@pytest.mark.parametrize(
    "mutation", ["no-heading", "wrong-owner", "wrong-title", "wrong-order", "cross-chapter"]
)
def test_heading_scope_requires_real_pinned_source_heading(tmp_path: Path, mutation: str) -> None:
    compiler, update = navigation_scope(tmp_path)
    base_path = tmp_path / str(update.base_relative_path)
    base = json.loads(base_path.read_text())
    assignment = base["source_structure_assignments"][0]
    if mutation == "no-heading":
        assignment["disposition"] = "metadata-only"
    elif mutation == "wrong-owner":
        assignment["owner_id"] = "books/another"
    elif mutation == "wrong-title":
        assignment["label"] = "2장 다른 장"
    elif mutation == "wrong-order":
        assignment["source_order"] = 2
    else:
        base["source_structure_assignments"][2]["canonical_id"] = (
            "books/example/chapter-02/section-01"
        )
    base_path.write_text(json.dumps(base), encoding="utf-8")
    # Pin the malformed base itself: the rejection must validate its semantic boundary.
    digest = hashlib.sha256(base_path.read_bytes()).hexdigest()
    update = replace(update, base_expected_sha256=digest)
    update.replacement["coverage_scope"]["base_sha256"] = digest
    with pytest.raises(WoonError):
        compiler.validate_book_coverage_manifest_update(update)


@pytest.mark.parametrize("invalid_node", ["duplicate", None, {}, {"canonical_id": []}])
def test_heading_scope_rejects_malformed_pinned_base_nodes(
    tmp_path: Path, invalid_node: object
) -> None:
    compiler, update = navigation_scope(tmp_path)
    base_path = tmp_path / str(update.base_relative_path)
    base = json.loads(base_path.read_text())
    base["nodes"].append(
        copy.deepcopy(base["nodes"][0]) if invalid_node == "duplicate" else invalid_node
    )
    base_path.write_text(json.dumps(base), encoding="utf-8")
    digest = hashlib.sha256(base_path.read_bytes()).hexdigest()
    update = replace(update, base_expected_sha256=digest)
    update.replacement["coverage_scope"]["base_sha256"] = digest
    with pytest.raises(WoonError):
        compiler.validate_book_coverage_manifest_update(update)


@pytest.mark.parametrize("delivery", ["book-root-heading", "private-reader"])
def test_source_section_title_delivery_preserves_exact_canonical_scope(tmp_path: Path, delivery):
    _, update = navigation_scope(tmp_path)
    base = json.loads((tmp_path / str(update.base_relative_path)).read_bytes())
    base["source_structure_elements"].insert(
        1,
        {
            "structure_id": "section-intro",
            "kind": "section",
            "title": "1.0 도입",
        },
    )
    base["source_structure_assignments"].insert(
        1,
        {
            "structure_id": "section-intro",
            "disposition": delivery,
            "owner_id": "books/example",
            "source_order": 2,
        },
    )
    expected = frozenset(node["canonical_id"] for node in base["nodes"])
    assert navigation_group_scope_nodes(base, update.scope_root_id) == expected
    before = copy.deepcopy(base)
    for field, value in [("owner_id", "books/other"), ("source_order", 99)]:
        bad = copy.deepcopy(base)
        bad["source_structure_assignments"][1][field] = value
        assert not navigation_group_scope_nodes(bad, update.scope_root_id)
    bad = copy.deepcopy(base)
    bad["source_structure_assignments"].pop()
    assert not navigation_group_scope_nodes(bad, update.scope_root_id)
    assert base == before
