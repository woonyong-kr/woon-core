import copy
import hashlib
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import (
    _audit_navigation_delivery,
    _audit_source_structure_contract,
    _source_structure_id,
)
from woon_core.knowledge.wiki_tree import private_reader_target


@pytest.mark.parametrize("frontmatter", [True, False])
def test_pinned_navigation_preserves_inventory_and_rejects_drift(tmp_path: Path, frontmatter: bool):
    root = tmp_path / "wiki" / "book.md"
    root.parent.mkdir()
    reader = tmp_path / "private" / "reader.md"
    reader.parent.mkdir()
    reader.write_text(
        "---\naccess: local-only\npublish: false\n---\n# 1 소개\n\n실제 본문\n\n"
        "## 1.1 절\n\n두 번째 본문\n"
    )
    if not frontmatter:
        reader.write_text(reader.read_text().split("---\n", 2)[2])
    pages = {
        "book": (
            root,
            {"entity_kind": "book", "access": "local-only", "publish": False},
            "# 예제\n\n## [소개](../private/reader.md)\n### [절](../private/reader.md#1.1%20절)\n",
        )
    }
    titles = ["1.1 절", "1 소개"]  # immutable inventory can differ from reviewed display order
    elements, assignments = [], []
    for i, title in enumerate(titles, 1):
        element = {
            "kind": "section",
            "title": title,
            "source_locator": "source://book/" + str(i),
            "source_sha256": "a" * 64,
        }
        element["structure_id"] = _source_structure_id(
            *[element[k] for k in ("kind", "title", "source_locator", "source_sha256")]
        )
        elements.append(element)
        assignment = {
            "structure_id": element["structure_id"],
            "disposition": "private-reader",
            "owner_id": "book",
            "source_order": i,
            "reader_path": "private/reader.md",
            "reader_sha256": hashlib.sha256(reader.read_bytes()).hexdigest(),
            "heading": "## 1.1 절" if i == 1 else "# 1 소개",
            "reader_anchor": "1.1 절" if i == 1 else "",
            "body_sha256": hashlib.sha256(
                ("두 번째 본문" if i == 1 else "실제 본문\n\n## 1.1 절\n\n두 번째 본문").encode()
            ).hexdigest(),
        }
        assignments.append(assignment)
    manifest = {
        "book_id": "book",
        "source_structure_elements": elements,
        "source_structure_assignments": assignments,
        "source_structure_inventory_evidence": {
            "locator": "source://book/inventory",
            "sha256": "a" * 64,
            "verified_on": "2026-09-11",
        },
    }
    before = copy.deepcopy(manifest)
    errors = []
    _audit_source_structure_contract(
        "book", manifest, set(), set(), [], pages, errors, vault=tmp_path
    )
    assert errors == []
    assert manifest == before
    assert (
        private_reader_target(tmp_path, root, pages["book"][1], pages["book"][2].split("\n", 1)[1])
        == reader
    )
    duplicate_pages = {
        "book": (*pages["book"][:2], pages["book"][2] + "- [별도 도입](../private/reader.md)\n")
    }
    with pytest.raises(WoonError, match="exactly once"):
        _audit_navigation_delivery(
            tmp_path, "book", assignments[1], elements[1], duplicate_pages, source_order=2
        )
    for field, value in [
        ("reader_sha256", "b" * 64),
        ("body_sha256", "b" * 64),
        ("reader_anchor", "missing"),
        ("source_order", 2),
        ("heading", "## 9 unrelated"),
        ("reader_path", "private/../wiki/book.md"),
    ]:
        bad = {**assignments[0], field: value}
        with pytest.raises(WoonError):
            _audit_navigation_delivery(tmp_path, "book", bad, elements[0], pages, source_order=1)
    stale_errors = []
    _audit_source_structure_contract(
        "book",
        manifest,
        set(),
        set(),
        [],
        pages,
        stale_errors,
        vault=tmp_path,
        scope_base={"nodes": []},
    )
    assert any("full pinned manifest" in error for error in stale_errors)
    reader.unlink()
    reader.symlink_to(root)
    with pytest.raises(WoonError, match="symlinks"):
        _audit_navigation_delivery(
            tmp_path, "book", assignments[0], elements[0], pages, source_order=1
        )


def test_ocr_title_requires_explicit_same_section_review(tmp_path: Path):
    reader = tmp_path / "private" / "reader.md"
    reader.parent.mkdir()
    reader.write_text(
        "---\naccess: local-only\npublish: false\n---\n# 3.5.1 항등함수\n\n실제 설명\n"
    )
    root = tmp_path / "wiki" / "book.md"
    element = {"title": "3.5.1 등함수"}
    assignment = {
        "structure_id": "test",
        "disposition": "private-reader",
        "owner_id": "book",
        "source_order": 1,
        "heading": "# 3.5.1 항등함수",
        "reader_path": "private/reader.md",
        "reader_anchor": "",
        "reader_sha256": hashlib.sha256(reader.read_bytes()).hexdigest(),
        "body_sha256": hashlib.sha256("실제 설명".encode()).hexdigest(),
    }
    pages = {
        "book": (
            root,
            {"entity_kind": "book", "access": "local-only", "publish": False},
            "- [항등함수](../private/reader.md)",
        )
    }
    with pytest.raises(WoonError, match="reviewed numbered mapping"):
        _audit_navigation_delivery(tmp_path, "book", assignment, element, pages, source_order=1)
    assignment["heading_review"] = {
        "source_title": element["title"],
        "reader_title": "3.5.1 항등함수",
        "basis": "원본의 3.5.1 장절과 실제 reader 대조",
    }
    _audit_navigation_delivery(tmp_path, "book", assignment, element, pages, source_order=1)
    assignment["heading_review"]["source_title"] = "3.5.2 등함수"
    with pytest.raises(WoonError):
        _audit_navigation_delivery(tmp_path, "book", assignment, element, pages, source_order=1)
