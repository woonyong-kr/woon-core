from copy import deepcopy
from pathlib import Path

from woon_core.knowledge.book_coverage import _audit_book_map_ui
from woon_core.knowledge.wiki_tree import (
    CHILDREN_END,
    CHILDREN_START,
    render_book_toc_group,
)


def test_single_leaf_toc_matches_renderer_without_losing_child_checks() -> None:
    book = "books/example"
    chapter = f"{book}/chapter-01"
    cases = (
        ("1.1 소개", "1.1 소개"),
        ("1.2 함수", "1.2 함수 정의"),
        ("요약", "1장 요약"),
        ("1.3 범위", "1.3.1 경계"),
    )
    groups = []
    rows = []
    pages = {}
    for number, (label, title) in enumerate(cases):
        child = f"{chapter}/section-{number}"
        groups.append({"label": label, "children": [child]})
        rows.extend(render_book_toc_group(label, ((title, f"[[wiki/{child}|{title}]]", False),)))
        pages[child] = (
            Path(f"{child}.md"),
            {"title": title, "parent": f"[[wiki/{chapter}]]"},
            "원문 본문",
        )
    body = "\n".join((CHILDREN_START, *rows, CHILDREN_END))
    pages[chapter] = (
        Path("chapter-01.md"),
        {"navigation_groups": groups, "parent": f"[[wiki/{book}]]"},
        body,
    )
    errors = []
    _audit_book_map_ui(book, pages, errors)
    assert errors == []

    first = f"- [[wiki/{chapter}/section-0|1.1 소개]]"
    for changed in (body.replace(first, ""), body.replace(first, first + "\n" + first)):
        damaged = deepcopy(pages)
        damaged[chapter] = (pages[chapter][0], pages[chapter][1], changed)
        errors = []
        _audit_book_map_ui(book, damaged, errors)
        assert any("managed direct links are stale" in error for error in errors)

    damaged = deepcopy(pages)
    damaged[f"{chapter}/section-0"][1]["parent"] = f"[[wiki/{book}]]"
    errors = []
    _audit_book_map_ui(book, damaged, errors)
    assert any("UI child is not direct" in error for error in errors)

    damaged = deepcopy(pages)
    damaged[f"{chapter}/section-0/sub"] = (
        Path("sub.md"),
        {"title": "1.1.1 하위", "parent": f"[[wiki/{chapter}/section-0]]"},
        "본문",
    )
    errors = []
    _audit_book_map_ui(book, damaged, errors)
    assert any("duplicate-title wrapper" in error for error in errors)
    assert any("managed group headings are stale" in error for error in errors)

    root_pages = {
        book: (
            Path("book.md"),
            {"navigation_groups": [{"label": "1부", "children": [chapter]}]},
            f"{CHILDREN_START}\n- [[wiki/{chapter}|1장]]\n{CHILDREN_END}",
        ),
        chapter: (Path("chapter.md"), {"title": "1장", "parent": f"[[wiki/{book}]]"}, "본문"),
    }
    errors = []
    _audit_book_map_ui(book, root_pages, errors)
    assert any("managed group headings are stale" in error for error in errors)
