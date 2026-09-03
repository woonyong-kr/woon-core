from pathlib import Path

from woon_core.knowledge.wiki_tree import (
    CHILDREN_END,
    CHILDREN_START,
    SOURCE_INDEX_END,
    SOURCE_INDEX_START,
    apply_wiki_tree_refresh,
    is_wiki_source_archive,
    prepare_wiki_tree_refresh,
    preserve_generated_wiki_views,
)


def test_refresh_preserves_existing_children_before_source_index(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
        body=(
            "## 하위 키워드\n\n"
            f"{CHILDREN_START}\n"
            "- [[wiki/child|하위]]\n"
            f"{CHILDREN_END}\n\n"
            "## 원자료\n\n"
            f"{SOURCE_INDEX_START}\n"
            "- [원자료](private/_sources/source.md)\n"
            f"{SOURCE_INDEX_END}"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/child.md",
        title="하위",
        canonical_id="child",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("하위",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/README.md"].decode("utf-8")
    assert rendered == (tmp_path / "wiki/README.md").read_text(encoding="utf-8")
    assert rendered.index("## 하위 키워드") < rendered.index("## 원자료")


def test_source_archive_detection_uses_the_vault_relative_boundary(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    archived = wiki_root / "private/_sources/knowledge/book/source.md"
    normal = wiki_root / "concepts/source.md"
    outside = tmp_path / "outside/source.md"

    assert is_wiki_source_archive(archived, wiki_root)
    assert not is_wiki_source_archive(normal, wiki_root)
    assert not is_wiki_source_archive(outside, wiki_root)


def test_preserved_view_reuses_an_empty_managed_heading() -> None:
    rendered = "---\ntype: Wiki\n---\n\n# 프로젝트\n\n## 하위 키워드\n"
    existing = (
        "---\ntype: Wiki\n---\n\n# 프로젝트\n\n## 하위 키워드\n\n"
        f"{CHILDREN_START}\n- [[wiki/example|예시]]\n{CHILDREN_END}\n"
    )

    preserved = preserve_generated_wiki_views(existing, rendered)

    assert preserved.count("## 하위 키워드") == 1
    assert preserved.count(CHILDREN_START) == 1


def test_preserved_book_map_keeps_h2_topics_without_generic_children_heading() -> None:
    rendered = "---\ntype: Wiki\n---\n\n# 책\n\n책 소개다.\n"
    existing = (
        "---\ntype: Wiki\n---\n\n# 책\n\n"
        f"{CHILDREN_START}\n## 1부\n- [[wiki/book/chapter-01|1장]]\n"
        f"{CHILDREN_END}\n\n책 소개다.\n"
    )

    preserved = preserve_generated_wiki_views(existing, rendered)

    assert preserved.count(CHILDREN_START) == 1
    assert "## 1부\n- [[wiki/book/chapter-01|1장]]" in preserved
    assert "## 하위 키워드" not in preserved


def test_refresh_removing_direct_h1_navigation_keeps_canonical_spacing(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/article.md",
        title="문서",
        canonical_id="article",
        node_kind="detail",
        parent="[[wiki/README|Wiki]]",
        keywords=("문서",),
        view_mode="article",
        body=(
            f"{CHILDREN_START}\n"
            "- [[wiki/README|퇴역한 링크]]\n"
            f"{CHILDREN_END}\n\n"
            "## 남은 목차\n\n"
            "- 항목"
        ),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/article.md"].decode("utf-8")
    assert "# 문서\n\n## 남은 목차" in rendered
    assert "# 문서\n\n\n## 남은 목차" not in rendered


def test_refresh_removes_stale_empty_latest_heading(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/person.md",
        title="사람",
        canonical_id="person",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("사람",),
        body="## 현재 이해\n\n현재 확인한 사람 정보다.\n\n## 최신 관련 문서",
        extra="entity_kind: person\nlifecycle_status: active\n",
    )
    _write_history(tmp_path, "wiki/person.md", "사람")

    report = prepare_wiki_tree_refresh(tmp_path)
    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/person.md"].decode("utf-8")

    assert "## 최신 관련 문서" not in rendered


def test_refresh_replaces_conversation_scaffold_headings(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/topic.md",
        title="주제",
        canonical_id="topic",
        node_kind="detail",
        parent="[[wiki/README|Wiki]]",
        keywords=("주제",),
        body=(
            "## 현재 이해\n\n현재 결론이다.\n\n"
            "## 남긴 의도\n\n판단 기준이다.\n\n"
            "## 다음 질문\n\n검증할 내용이다.\n\n"
            "## 연결\n\n- [[wiki/README|Wiki]]"
        ),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/topic.md"].decode("utf-8")

    assert report.issues == ()
    assert "## 핵심 정리" in rendered
    assert "## 판단 기준" in rendered
    assert "## 다음 검증" in rendered
    assert "## 관련 문서" in rendered
    assert "## 현재 이해" not in rendered
    assert "## 남긴 의도" not in rendered
    assert "## 다음 질문" not in rendered
    assert "## 연결" not in rendered


def _write_page(
    vault: Path,
    relative: str,
    *,
    title: str,
    canonical_id: str,
    node_kind: str,
    parent: str | None,
    keywords: tuple[str, ...],
    view_mode: str = "tree",
    body: str = "",
    extra: str = "",
) -> None:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    sequence = len(tuple((vault / "wiki").rglob("*.md"))) + 1
    parent_row = f"parent: '{parent}'\n" if parent else ""
    keyword_rows = "\n".join(f"- {keyword}" for keyword in keywords)
    path.write_text(
        "---\n"
        f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n{parent_row}keywords:\n{keyword_rows}\n"
        f"aliases: []\nview_mode: {view_mode}\nsequence: {sequence}\n"
        "updated: 2026-08-25\n"
        "summary: 이 페이지의 핵심 내용을 한눈에 설명한다.\n"
        f"knowledge_state: 확인 필요\n{extra}---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def _write_history(vault: Path, parent_path: str, parent_title: str) -> None:
    path = Path(parent_path)
    _write_page(
        vault,
        f"{path.with_suffix('').as_posix()}-히스토리.md",
        title=f"{parent_title} 히스토리",
        canonical_id=f"{path.with_suffix('').relative_to('wiki').as_posix()}/history",
        node_kind="detail",
        parent=f"[[{path.with_suffix('').as_posix()}|{parent_title}]]",
        keywords=(f"{parent_title} 히스토리",),
        view_mode="topic-timeline",
        body="- 2026-08-25 · 최초 기록",
        extra="entity_section: history\n",
    )


def test_book_front_matter_titles_may_repeat_across_book_entities(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software.md",
        title="소프트웨어",
        canonical_id="books/software",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("소프트웨어",),
    )
    for index in (1, 2):
        book_path = f"wiki/personal/book-{index}.md"
        child_path = f"wiki/personal/book-{index}/afterword.md"
        _write_page(
            tmp_path,
            book_path,
            title=f"검증 책 {index}",
            canonical_id=f"personal/book-{index}",
            node_kind="entity",
            parent="[[wiki/books/software|소프트웨어]]",
            keywords=(f"검증 책 {index}",),
            view_mode="linear",
            body=f"- [[{child_path.removesuffix('.md')}|맺음말]]",
            extra="entity_kind: book\n",
        )
        _write_page(
            tmp_path,
            child_path,
            title="맺음말",
            canonical_id=f"personal/book-{index}/afterword",
            node_kind="detail",
            parent=f"[[{book_path.removesuffix('.md')}|검증 책 {index}]]",
            keywords=("맺음말",),
            view_mode="article",
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert not any("duplicate identity '맺음말'" in issue for issue in report.issues)


def test_entity_does_not_repeat_an_authored_link_in_latest_related_documents(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/projects/README.md",
        title="프로젝트",
        canonical_id="projects/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
    )
    _write_page(
        tmp_path,
        "wiki/projects/llm-course.md",
        title="LLM 강의",
        canonical_id="resources/llm-course",
        node_kind="entity",
        parent="[[wiki/projects/README|프로젝트]]",
        keywords=("LLM 강의", "LLM course"),
        view_mode="tree",
        body="## 키워드\n\n- [[tokenizer|토크나이저]]",
        extra="entity_kind: project\nlifecycle_status: active\n",
    )
    _write_history(tmp_path, "wiki/projects/llm-course.md", "LLM 강의")
    _write_page(
        tmp_path,
        "wiki/tokenizer.md",
        title="토크나이저",
        canonical_id="tokenizer",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("토크나이저",),
    )
    _write_page(
        tmp_path,
        "wiki/navigation-only.md",
        title="비공개 탐색 입구",
        canonical_id="navigation-only",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("비공개 탐색 입구",),
        body="[[wiki/projects/llm-course|LLM 강의]]로 이동한다.",
        extra="include_in_latest: false\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/projects/llm-course.md"].decode("utf-8")

    assert report.issues == ()
    assert "한눈에 보기" not in rendered
    assert "## 최신 관련 문서" not in rendered
    assert rendered.count("[[tokenizer|토크나이저]]") == 1
    assert "비공개 탐색 입구" not in rendered

    apply_wiki_tree_refresh(tmp_path, report)
    rerun = prepare_wiki_tree_refresh(tmp_path)
    assert rerun.issues == ()
    assert rerun.changed_count == 0


def test_person_entity_uses_incoming_people_links_for_latest_index(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/people.md",
        title="인물",
        canonical_id="people",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("인물",),
        body="<!-- 직접 하위 키워드 링크만 표시한다. -->",
    )
    _write_page(
        tmp_path,
        "wiki/person.md",
        title="최우녕",
        canonical_id="person",
        node_kind="entity",
        parent="[[wiki/people|인물]]",
        keywords=("최우녕", "Woonyoung"),
        view_mode="topic-timeline",
        body="현재 확인된 역할과 연결 문서를 이 페이지에서 관리한다.",
        extra="entity_kind: person\nlifecycle_status: active\n",
    )
    _write_history(tmp_path, "wiki/person.md", "최우녕")
    _write_page(
        tmp_path,
        "wiki/decision.md",
        title="구조 결정",
        canonical_id="decision",
        node_kind="decision",
        parent="[[wiki/README|Wiki]]",
        keywords=("구조 결정",),
        extra="people:\n- '[[wiki/person|최우녕]]'\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/person.md"].decode("utf-8")

    assert report.issues == ()
    assert "## 최신 관련 문서" in rendered
    assert "[[wiki/decision|구조 결정]]" in rendered
    latest = rendered.split("<!-- woon-wiki-latest:start -->", maxsplit=1)[1]
    assert "[[wiki/people|인물]]" not in latest


def test_entity_latest_index_does_not_repeat_direct_children(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/project.md",
        title="프로젝트",
        canonical_id="project",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
        body="현재 프로젝트의 문제와 범위를 정리한다.",
        extra=(
            "entity_kind: project\nlifecycle_status: active\n"
            "navigation_groups:\n"
            "- label: 문서\n"
            "  children:\n"
            "  - child\n"
            "  - project/history\n"
        ),
    )
    _write_history(tmp_path, "wiki/project.md", "프로젝트")
    _write_page(
        tmp_path,
        "wiki/child.md",
        title="설계",
        canonical_id="child",
        node_kind="topic",
        parent="[[wiki/project|프로젝트]]",
        keywords=("설계",),
        extra="related_to:\n- '[[wiki/project|프로젝트]]'\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/project.md"].decode("utf-8")

    assert rendered.count("[[wiki/child|설계]]") == 1
    assert "## 최신 관련 문서" not in rendered


def test_entity_does_not_repeat_direct_children_already_in_keyword_section(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/person.md",
        title="인물",
        canonical_id="person",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("인물",),
        view_mode="topic-timeline",
        body="## 키워드\n\n- [[wiki/person/project|함께한 프로젝트]]",
        extra="entity_kind: person\nlifecycle_status: active\n",
    )
    _write_page(
        tmp_path,
        "wiki/person/project.md",
        title="함께한 프로젝트",
        canonical_id="person/project",
        node_kind="topic",
        parent="[[wiki/person|인물]]",
        keywords=("함께한 프로젝트",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/person.md"].decode("utf-8")

    assert report.issues == ()
    assert rendered.count("[[wiki/person/project|함께한 프로젝트]]") == 1
    assert "## 하위 키워드" not in rendered


def test_root_and_hub_render_only_direct_keyword_links(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
        body="<!-- 직접 하위 키워드 링크만 표시한다. -->",
    )
    _write_page(
        tmp_path,
        "wiki/concepts.md",
        title="개념",
        canonical_id="concepts",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("개념",),
    )
    _write_page(
        tmp_path,
        "wiki/ai.md",
        title="AI — 인공지능 개념",
        canonical_id="ai",
        node_kind="topic",
        parent="[[wiki/concepts|개념]]",
        keywords=("AI — 인공지능 개념",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    root = report.pages[tmp_path / "wiki/README.md"].decode("utf-8")
    hub = report.pages[tmp_path / "wiki/concepts.md"].decode("utf-8")

    assert report.issues == ()
    assert "<!-- woon-wiki-overview:start -->" not in root
    assert "<!-- woon-wiki-overview:start -->" not in hub
    assert "# Wiki\n\n\n" not in root
    assert "## 최신 하위 문서" not in root
    root_children = root.split("<!-- woon-wiki-children:start -->", maxsplit=1)[1].split(
        "<!-- woon-wiki-children:end -->", maxsplit=1
    )[0]
    assert root_children.strip() == "- [[wiki/concepts|개념]]"
    assert "[[wiki/ai|AI]]" not in root_children
    assert " — " not in root_children
    assert "<details>" not in root_children
    hub_children = hub.split("<!-- woon-wiki-children:start -->", maxsplit=1)[1].split(
        "<!-- woon-wiki-children:end -->", maxsplit=1
    )[0]
    assert hub_children.strip() == "- [[wiki/ai|AI]]"
    assert "인공지능 개념" not in hub_children
    assert "## 최신 하위 문서" not in hub


def test_book_chapter_shows_only_direct_toc_depth_without_latest_descendants(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/ai.md",
        title="AI",
        canonical_id="books/ai",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("AI",),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm.md",
        title="LLM 책",
        canonical_id="books/llm",
        node_kind="entity",
        parent="[[wiki/books/ai|AI]]",
        keywords=("LLM 책",),
        extra=(
            "entity_kind: book\n"
            "navigation_groups:\n"
            "- label: 장\n"
            "  children:\n"
            "  - books/llm/chapter-01\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm/chapter-01.md",
        title="1장",
        canonical_id="books/llm/chapter-01",
        node_kind="topic",
        parent="[[wiki/books/llm|LLM 책]]",
        keywords=("1장",),
        extra=(
            "view_mode: linear\n"
            "navigation_groups:\n"
                "- label: 시작 흐름\n"
            "  children:\n"
            "  - books/llm/chapter-01/1-1\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm/chapter-01/1-1.md",
        title="1.1 시작",
        canonical_id="books/llm/chapter-01/1-1",
        node_kind="topic",
        parent="[[wiki/books/llm/chapter-01|1장]]",
        keywords=("1.1 시작",),
        extra=(
            "navigation_groups:\n"
            "- label: 1.1 시작\n"
            "  children:\n"
            "  - books/llm/chapter-01/1-1/1-1-1\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm/chapter-01/1-1/1-1-1.md",
        title="1.1.1 첫 절",
        canonical_id="books/llm/chapter-01/1-1/1-1-1",
        node_kind="detail",
        parent="[[wiki/books/llm/chapter-01/1-1|1.1 시작]]",
        keywords=("1.1.1 첫 절",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    assert report.issues == ()
    chapter = report.pages[tmp_path / "wiki/books/llm/chapter-01.md"].decode("utf-8")

    children = chapter.split(CHILDREN_START, maxsplit=1)[1].split(CHILDREN_END, maxsplit=1)[0]
    assert "[[wiki/books/llm/chapter-01/1-1|1.1 시작]]" in children
    assert "1-1-1" not in children
    assert "## 최신 하위 문서" not in chapter
    assert "<!-- woon-wiki-latest:start -->" not in chapter


def test_book_root_and_chapter_render_source_topics_as_h2_with_direct_links(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software.md",
        title="소프트웨어",
        canonical_id="books/software",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("소프트웨어",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software/refactoring.md",
        title="리팩터링 2판",
        canonical_id="books/refactoring-2e",
        node_kind="entity",
        parent="[[wiki/books/software|소프트웨어]]",
        keywords=("리팩터링 2판",),
        view_mode="linear",
        body="",
        extra=(
            "entity_kind: book\n"
            "navigation_groups:\n"
            "- label: 1부 원리\n"
            "  children:\n"
            "  - books/refactoring-2e/chapter-01\n"
            "  - books/refactoring-2e/chapter-02\n"
            "- label: 부록\n"
            "  children:\n"
            "  - books/refactoring-2e/appendix-a\n"
        ),
    )
    for number in (1, 2):
        extra = ""
        if number == 1:
            extra = (
                "navigation_groups:\n"
                "- label: 1.1 시작점\n"
                "  children:\n"
                "  - books/refactoring-2e/chapter-01/1-1\n"
                "  - books/refactoring-2e/chapter-01/1-1-start\n"
                "  - books/refactoring-2e/chapter-01/1-1-tests\n"
            )
        _write_page(
            tmp_path,
            f"wiki/books/software/refactoring/chapter-{number:02d}.md",
            title=f"{number}장",
            canonical_id=f"books/refactoring-2e/chapter-{number:02d}",
            node_kind="topic",
            parent="[[wiki/books/software/refactoring|리팩터링 2판]]",
            keywords=(f"{number}장",),
            view_mode="linear",
            extra=extra,
        )
    _write_page(
        tmp_path,
        "wiki/books/software/refactoring/chapter-01/1-1.md",
        title="1.1 시작점",
        canonical_id="books/refactoring-2e/chapter-01/1-1",
        node_kind="detail",
        parent="[[wiki/books/software/refactoring/chapter-01|1장]]",
        keywords=("1.1 시작점",),
        view_mode="article",
        body="이 절에서 다룰 출발점과 이후 세부 절을 연결하는 원문 도입 설명을 충분히 보존한다.",
    )
    for slug, title in (("1-1-start", "1.1.1 시작"), ("1-1-tests", "1.1.2 테스트")):
        _write_page(
            tmp_path,
            f"wiki/books/software/refactoring/chapter-01/{slug}.md",
            title=title,
            canonical_id=f"books/refactoring-2e/chapter-01/{slug}",
            node_kind="detail",
            parent="[[wiki/books/software/refactoring/chapter-01|1장]]",
            keywords=(title,),
            view_mode="article",
        )
    _write_page(
        tmp_path,
        "wiki/books/software/refactoring/appendix-a.md",
        title="부록 A",
        canonical_id="books/refactoring-2e/appendix-a",
        node_kind="detail",
        parent="[[wiki/books/software/refactoring|리팩터링 2판]]",
        keywords=("부록 A",),
        view_mode="article",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    book = report.pages[tmp_path / "wiki/books/software/refactoring.md"].decode("utf-8")
    chapter = report.pages[tmp_path / "wiki/books/software/refactoring/chapter-01.md"].decode(
        "utf-8"
    )
    assert "## 1부 원리\n- [[wiki/books/software/refactoring/chapter-01|1장]]" in book
    assert "- [[wiki/books/software/refactoring/chapter-02|2장]]" in book
    assert "## 부록\n- [[wiki/books/software/refactoring/appendix-a|부록 A]]" in book
    assert (
        "## 1.1 시작점\n- [[wiki/books/software/refactoring/chapter-01/1-1-start|1.1.1 시작]]"
    ) in chapter
    assert "[[wiki/books/software/refactoring/chapter-01/1-1|1.1 시작점]]" not in chapter
    assert ("- [[wiki/books/software/refactoring/chapter-01/1-1-tests|1.1.2 테스트]]") in chapter
    assert "## 하위 키워드" not in book
    assert "## 하위 키워드" not in chapter
    assert "1. [[" not in book and "1. [[" not in chapter
    assert "chapter-01/1-1-start/" not in chapter

    apply_wiki_tree_refresh(tmp_path, report)
    rerun = prepare_wiki_tree_refresh(tmp_path)
    assert rerun.issues == ()
    assert rerun.changed_count == 0


def test_nested_book_navigation_map_renders_h2_topics_with_direct_links(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software.md",
        title="소프트웨어",
        canonical_id="books/software",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("소프트웨어",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software/llm.md",
        title="밑바닥부터 만들면서 배우는 LLM",
        canonical_id="personal/build-llm-from-scratch",
        node_kind="entity",
        parent="[[wiki/books/software|소프트웨어]]",
        keywords=("밑바닥부터 만들면서 배우는 LLM",),
        body="",
        extra=(
            "entity_kind: book\n"
            "navigation_groups:\n"
            "- label: 1부 LLM 만들기\n"
            "  children:\n"
            "  - personal/build-llm-from-scratch/chapter-03\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/books/software/llm/chapter-03.md",
        title="3장 텍스트 데이터 처리",
        canonical_id="personal/build-llm-from-scratch/chapter-03",
        node_kind="topic",
        parent="[[wiki/books/software/llm|밑바닥부터 만들면서 배우는 LLM]]",
        keywords=("3장 텍스트 데이터 처리",),
        body="",
        extra=(
            "navigation_groups:\n"
            "- label: 3.3 토큰화\n"
            "  children:\n"
            "  - personal/build-llm-from-scratch/chapter-03/3-3\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/books/software/llm/chapter-03/3-3.md",
        title="3.3 토큰화",
        canonical_id="personal/build-llm-from-scratch/chapter-03/3-3",
        node_kind="topic",
        parent="[[wiki/books/software/llm/chapter-03|3장 텍스트 데이터 처리]]",
        keywords=("3.3 토큰화",),
        body="",
        extra=(
            "navigation_groups:\n"
            "- label: 3.3.1 단어 단위 토큰화\n"
            "  children:\n"
            "  - personal/build-llm-from-scratch/chapter-03/3-3/3-3-1\n"
            "- label: 3.3.2 부분 단어 토큰화\n"
            "  children:\n"
            "  - personal/build-llm-from-scratch/chapter-03/3-3/3-3-2\n"
        ),
    )
    for suffix, title in (
        ("3-3-1", "3.3.1 단어 단위 토큰화"),
        ("3-3-2", "3.3.2 부분 단어 토큰화"),
    ):
        _write_page(
            tmp_path,
            f"wiki/books/software/llm/chapter-03/3-3/{suffix}.md",
            title=title,
            canonical_id=f"personal/build-llm-from-scratch/chapter-03/3-3/{suffix}",
            node_kind="detail",
            parent="[[wiki/books/software/llm/chapter-03/3-3|3.3 토큰화]]",
            keywords=(title,),
            view_mode="article",
            body="토큰화 결과를 원문 예제로 확인한다.",
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    nested = report.pages[
        tmp_path / "wiki/books/software/llm/chapter-03/3-3.md"
    ].decode("utf-8")
    assert (
        "## 3.3.1 단어 단위 토큰화\n"
        "- [[wiki/books/software/llm/chapter-03/3-3/3-3-1|3.3.1 단어 단위 토큰화]]"
    ) in nested
    assert (
        "## 3.3.2 부분 단어 토큰화\n"
        "- [[wiki/books/software/llm/chapter-03/3-3/3-3-2|3.3.2 부분 단어 토큰화]]"
    ) in nested
    assert "- 3.3.1 단어 단위 토큰화" not in nested


def test_book_map_with_children_requires_source_owned_navigation_groups(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software.md",
        title="소프트웨어",
        canonical_id="books/software",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("소프트웨어",),
    )
    _write_page(
        tmp_path,
        "wiki/books/software/book.md",
        title="검증 책",
        canonical_id="books/verified",
        node_kind="entity",
        parent="[[wiki/books/software|소프트웨어]]",
        keywords=("검증 책",),
        view_mode="linear",
        body="## 장\n\n- [[wiki/books/software/book/chapter-01|1장]]",
        extra="entity_kind: book\n",
    )
    _write_page(
        tmp_path,
        "wiki/books/software/book/chapter-01.md",
        title="1장",
        canonical_id="books/verified/chapter-01",
        node_kind="topic",
        parent="[[wiki/books/software/book|검증 책]]",
        keywords=("1장",),
        view_mode="linear",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == (
        "wiki/books/software/book.md: book map requires navigation_groups for every "
        "source part, appendix, or section topic",
    )


def test_temporal_children_render_open_range_closed_range_and_single_day(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/career.md",
        title="커리어",
        canonical_id="career",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("커리어",),
        extra=(
            "navigation_groups:\n"
            "- label: 진행 중\n"
            "  children:\n"
            "  - career/active\n"
            "- label: 종료\n"
            "  children:\n"
            "  - career/completed\n"
            "  - career/one-day\n"
        ),
    )
    for relative, title, canonical_id, temporal in (
        (
            "wiki/active.md",
            "진행 중 준비",
            "career/active",
            "lifecycle_status: active\nstarted_on: 2026-08-01\n",
        ),
        (
            "wiki/completed.md",
            "종료된 지원",
            "career/completed",
            "lifecycle_status: completed\nstarted_on: 2026-07-01\nended_on: 2026-07-20\n",
        ),
        (
            "wiki/one-day.md",
            "하루 면접",
            "career/one-day",
            "lifecycle_status: completed\noccurred_on: 2026-08-25\n",
        ),
    ):
        _write_page(
            tmp_path,
            relative,
            title=title,
            canonical_id=canonical_id,
            node_kind="topic",
            parent="[[wiki/career|커리어]]",
            keywords=(title,),
            extra=temporal,
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/career.md"].decode("utf-8")
    assert "[[wiki/active|진행 중 준비]] · 2026-08-01 →" in rendered
    assert "[[wiki/completed|종료된 지원]] · 2026-07-01 → 2026-07-20" in rendered
    assert "[[wiki/one-day|하루 면접]] · 2026-08-25" in rendered


def test_temporal_contract_rejects_incomplete_or_inverted_lifecycle(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/missing-end.md",
        title="종료일 누락",
        canonical_id="missing-end",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("종료일 누락",),
        extra="lifecycle_status: completed\nstarted_on: 2026-08-25\n",
    )
    _write_page(
        tmp_path,
        "wiki/inverted.md",
        title="역전된 기간",
        canonical_id="inverted",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("역전된 기간",),
        extra=("lifecycle_status: completed\nstarted_on: 2026-08-25\nended_on: 2026-08-24\n"),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert "wiki/missing-end.md: closed lifecycle requires ended_on or occurred_on" in (
        report.issues
    )
    assert "wiki/inverted.md: ended_on cannot precede started_on" in report.issues


def test_temporal_entities_require_lifecycle_status(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/project.md",
        title="프로젝트",
        canonical_id="project",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
        view_mode="project",
        extra="entity_kind: project\n",
        body="현재 목표를 관리한다.",
    )
    _write_history(tmp_path, "wiki/project.md", "프로젝트")

    report = prepare_wiki_tree_refresh(tmp_path)

    assert "wiki/project.md: temporal entity requires lifecycle_status" in report.issues


def test_sibling_links_use_unique_suffixes_when_compact_labels_collide(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/project.md",
        title="개인 AI 원격 제어 앱 아이디어",
        canonical_id="project",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("개인 AI 원격 제어 앱 아이디어",),
        view_mode="project",
        body="원격 제어 제품의 문제와 검증 기준을 관리한다.",
        extra=(
            "entity_kind: project\n"
            "lifecycle_status: idea\n"
            "navigation_groups:\n"
            "- label: 설계\n"
            "  children:\n"
            "  - security\n"
            "  - runtime\n"
            "- label: 이력\n"
            "  children:\n"
            "  - project/history\n"
        ),
    )
    _write_history(tmp_path, "wiki/project.md", "개인 AI 원격 제어 앱 아이디어")
    _write_page(
        tmp_path,
        "wiki/security.md",
        title="리모트AI — 승인과 보안 모델",
        canonical_id="security",
        node_kind="topic",
        parent="[[wiki/project|개인 AI 원격 제어 앱 아이디어]]",
        keywords=("리모트AI — 승인과 보안 모델",),
    )
    _write_page(
        tmp_path,
        "wiki/runtime.md",
        title="리모트AI — Runtime Adapter 계약",
        canonical_id="runtime",
        node_kind="topic",
        parent="[[wiki/project|개인 AI 원격 제어 앱 아이디어]]",
        keywords=("리모트AI — Runtime Adapter 계약",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/project.md"].decode("utf-8")
    children = rendered.split("<!-- woon-wiki-children:start -->", maxsplit=1)[1].split(
        "<!-- woon-wiki-children:end -->", maxsplit=1
    )[0]

    assert report.issues == ()
    assert "[[wiki/security|승인과 보안 모델]]" in children
    assert "[[wiki/runtime|Runtime Adapter 계약]]" in children
    assert "|리모트AI]]" not in children


def test_navigation_page_rejects_visible_prose(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
        body="탐색 페이지에 장문 설명을 반복한다.",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.pages == {}
    assert report.issues == (
        "wiki/README.md: navigation page body must contain only generated keyword links",
    )


def test_books_are_separate_genre_catalog_with_link_only_book_contents(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/ai.md",
        title="AI·머신러닝",
        canonical_id="books/ai",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("AI·머신러닝",),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm.md",
        title="LLM 책",
        canonical_id="books/llm",
        node_kind="entity",
        parent="[[wiki/books/ai|AI·머신러닝]]",
        keywords=("LLM 책",),
        view_mode="linear",
        extra=(
            "entity_kind: book\n"
            "navigation_groups:\n"
            "- label: 장\n"
            "  children:\n"
            "  - books/llm/chapter-01\n"
        ),
            body="",
    )
    _write_page(
        tmp_path,
        "wiki/books/llm/chapter-01.md",
        title="Ch 1",
        canonical_id="books/llm/chapter-01",
        node_kind="topic",
        parent="[[wiki/books/llm|LLM 책]]",
        keywords=("Ch 1",),
        body="## 첫 절\n\n첫 장의 학습 본문이다.",
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/books/llm.md"].decode("utf-8")
    catalog = report.pages[tmp_path / "wiki/books/README.md"].decode("utf-8")
    assert "- AI·머신러닝" in catalog
    assert "- [[wiki/books/ai|AI·머신러닝]]" not in catalog
    assert "  - [[wiki/books/llm|LLM 책]]" in catalog
    assert "한눈에 보기" not in rendered
    assert "최신 관련 문서" not in rendered


def test_hub_explicit_groups_render_text_labels_with_direct_child_links(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/projects/README.md",
        title="프로젝트",
        canonical_id="projects/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
        extra=(
            "navigation_groups:\n"
            "- label: 창작\n"
            "  children:\n"
            "  - projects/novel\n"
            "- label: 시스템·도구\n"
            "  children:\n"
            "  - projects/calendar\n"
        ),
    )
    for relative, title, canonical_id in (
        ("wiki/projects/novel.md", "(미정)소설 집필", "projects/novel"),
        ("wiki/projects/calendar.md", "Link Calendar", "projects/calendar"),
    ):
        _write_page(
            tmp_path,
            relative,
            title=title,
            canonical_id=canonical_id,
            node_kind="entity",
            parent="[[wiki/projects/README|프로젝트]]",
            keywords=(title,),
            view_mode="project",
            body=f"{title}의 현재 목표와 검증 상태를 관리한다.",
            extra="entity_kind: project\nlifecycle_status: active\n",
        )
        _write_history(tmp_path, relative, title)

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/projects/README.md"].decode("utf-8")
    assert "- 창작\n  - [[wiki/projects/novel|(미정)소설 집필]]" in catalog
    assert "- 시스템·도구\n  - [[wiki/projects/calendar|Link Calendar]]" in catalog
    assert "\n- [[wiki/projects/novel|(미정)소설 집필]]" not in catalog


def test_explicit_groups_must_cover_each_direct_child_exactly_once(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/projects/README.md",
        title="프로젝트",
        canonical_id="projects/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
        extra=("navigation_groups:\n- label: 창작\n  children:\n  - projects/missing\n"),
    )
    _write_page(
        tmp_path,
        "wiki/projects/novel.md",
        title="(미정)소설 집필",
        canonical_id="projects/novel",
        node_kind="entity",
        parent="[[wiki/projects/README|프로젝트]]",
        keywords=("(미정)소설 집필",),
        view_mode="project",
        extra="entity_kind: project\nlifecycle_status: active\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any("contain non-direct children: projects/missing" in issue for issue in report.issues)
    assert any("omit direct children: projects/novel" in issue for issue in report.issues)


def test_flat_navigation_rejects_missing_or_duplicate_sibling_order(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/concepts.md",
        title="개념",
        canonical_id="concepts",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("개념",),
    )
    for relative, title, canonical_id in (
        ("wiki/first.md", "첫 개념", "first"),
        ("wiki/second.md", "둘째 개념", "second"),
    ):
        _write_page(
            tmp_path,
            relative,
            title=title,
            canonical_id=canonical_id,
            node_kind="topic",
            parent="[[wiki/concepts|개념]]",
            keywords=(title,),
        )
    first = tmp_path / "wiki/first.md"
    second = tmp_path / "wiki/second.md"
    first.write_text(
        first.read_text(encoding="utf-8").replace("sequence: 3\n", ""),
        encoding="utf-8",
    )
    second.write_text(
        second.read_text(encoding="utf-8").replace("sequence: 4", "sequence: 2"),
        encoding="utf-8",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any("ordered navigation requires sequence" in issue for issue in report.issues)


def test_dense_hub_requires_explicit_stage_groups(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/llm.md",
        title="LLM",
        canonical_id="llm",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("LLM",),
    )
    for index in range(11):
        _write_page(
            tmp_path,
            f"wiki/llm-{index}.md",
            title=f"LLM 개념 {index}",
            canonical_id=f"llm/{index}",
            node_kind="topic",
            parent="[[wiki/llm|LLM]]",
            keywords=(f"LLM 개념 {index}",),
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any(
        "11 direct children require explicit navigation groups" in issue for issue in report.issues
    )


def test_project_entity_can_render_explicit_reading_stages(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/project.md",
        title="프로젝트",
        canonical_id="project",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
        view_mode="project",
        extra=(
            "entity_kind: project\n"
            "lifecycle_status: active\n"
            "navigation_groups:\n"
            "- label: 이해 순서\n"
            "  children:\n"
            "  - project/problem\n"
            "  - project/architecture\n"
            "- label: 이력\n"
            "  children:\n"
            "  - project/history\n"
        ),
        body="문제와 아키텍처를 검증 가능한 순서로 관리한다.",
    )
    _write_history(tmp_path, "wiki/project.md", "프로젝트")
    for relative, title, canonical_id in (
        ("wiki/problem.md", "문제", "project/problem"),
        ("wiki/architecture.md", "아키텍처", "project/architecture"),
    ):
        _write_page(
            tmp_path,
            relative,
            title=title,
            canonical_id=canonical_id,
            node_kind="topic",
            parent="[[wiki/project|프로젝트]]",
            keywords=(title,),
        )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/project.md"].decode("utf-8")

    assert report.issues == ()
    assert "- 이해 순서\n  - [[wiki/problem|문제]]" in rendered
    assert "  - [[wiki/architecture|아키텍처]]" in rendered


def test_resource_groups_inline_raw_links_below_topic_text(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
        extra=("navigation_groups:\n- label: AI\n  children:\n  - resources/ai\n"),
    )
    _write_page(
        tmp_path,
        "wiki/resources/ai.md",
        title="AI 자료",
        canonical_id="resources/ai",
        node_kind="topic",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("AI 자료",),
        body="- [[assets/transformer.pdf|Transformer PDF]]",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/resources/README.md"].decode("utf-8")
    assert "- AI\n  - [[assets/transformer.pdf|Transformer PDF]]" in catalog
    assert "[[wiki/resources/ai|AI 자료]]" not in catalog


def test_resource_topic_allows_one_text_keyword_level_above_links(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/repositories.md",
        title="학습 저장소",
        canonical_id="resources/repositories",
        node_kind="topic",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("학습 저장소",),
        body=(
            "- AI·LLM\n"
            "  - [mini GPT](https://github.com/example/gpt)\n"
            "- 운영체제\n"
            "  - [[assets/pintos.pdf|PintOS 자료]]"
        ),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()


def test_large_resource_topic_stays_as_one_link_on_the_flat_root(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
        extra=("navigation_groups:\n- label: 저장소\n  children:\n  - resources/repositories\n"),
    )
    links = "\n".join(f"- [[assets/resource-{index}.pdf|자료 {index}]]" for index in range(21))
    _write_page(
        tmp_path,
        "wiki/resources/repositories.md",
        title="학습 저장소",
        canonical_id="resources/repositories",
        node_kind="topic",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("학습 저장소",),
        body=links,
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/resources/README.md"].decode("utf-8")
    assert "- 저장소\n  - [[wiki/resources/repositories|학습 저장소]]" in catalog
    assert "[[assets/resource-0.pdf|자료 0]]" not in catalog


def test_resource_topic_rejects_keyword_without_a_link(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/repositories.md",
        title="학습 저장소",
        canonical_id="resources/repositories",
        node_kind="topic",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("학습 저장소",),
        body="- AI·LLM",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == (
        "wiki/resources/repositories.md: every resource keyword must contain at least "
        "one indented hyperlink row",
    )


def test_resource_groups_reject_intermediate_bundle_topics(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
        extra=("navigation_groups:\n- label: AI\n  children:\n  - resources/ai\n"),
    )
    _write_page(
        tmp_path,
        "wiki/resources/ai.md",
        title="AI 자료",
        canonical_id="resources/ai",
        node_kind="hub",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("AI 자료",),
    )
    for relative, title, canonical_id, body in (
        (
            "wiki/resources/ai/aice.md",
            "AICE Associate",
            "resources/ai/aice",
            "- [[assets/aice-guide.png|시험 안내]]\n- [[assets/aice-schedule.png|시험 일정]]",
        ),
        (
            "wiki/resources/ai/transformer.md",
            "Transformer Explainer",
            "resources/ai/transformer",
            "- [[sources/transformer|Transformer 원자료]]",
        ),
    ):
        _write_page(
            tmp_path,
            relative,
            title=title,
            canonical_id=canonical_id,
            node_kind="topic",
            parent="[[wiki/resources/ai|AI 자료]]",
            keywords=(title,),
            body=body,
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert "wiki/resources/ai.md: direct children of resources must be category topics" in (
        report.issues
    )
    assert (
        "wiki/resources/ai.md: resource category topics must link raw sources directly "
        "and must not own intermediate Wiki children"
    ) in report.issues


def test_large_grouping_hub_must_define_reading_stages(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/concepts/README.md",
        title="개념",
        canonical_id="concepts/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("개념",),
    )
    _write_page(
        tmp_path,
        "wiki/concepts/large.md",
        title="큰 분류",
        canonical_id="concepts/large",
        node_kind="hub",
        parent="[[wiki/concepts/README|개념]]",
        keywords=("큰 분류",),
    )
    for index in range(21):
        _write_page(
            tmp_path,
            f"wiki/concepts/topic-{index}.md",
            title=f"주제 {index}",
            canonical_id=f"concepts/topic-{index}",
            node_kind="topic",
            parent="[[wiki/concepts/large|큰 분류]]",
            keywords=(f"주제 {index}",),
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any(
        "21 direct children require explicit navigation groups" in issue for issue in report.issues
    )


def test_concept_subtree_requires_groups_for_two_or_more_children(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/concepts/README.md",
        title="개념",
        canonical_id="concepts/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("개념",),
    )
    _write_page(
        tmp_path,
        "wiki/concepts/network.md",
        title="네트워크",
        canonical_id="concepts/network",
        node_kind="topic",
        parent="[[wiki/concepts/README|개념]]",
        keywords=("네트워크",),
    )
    for index in range(2):
        _write_page(
            tmp_path,
            f"wiki/concepts/network-{index}.md",
            title=f"네트워크 주제 {index}",
            canonical_id=f"concepts/network-{index}",
            node_kind="topic",
            parent="[[wiki/concepts/network|네트워크]]",
            keywords=(f"네트워크 주제 {index}",),
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any(
        "2 direct children require explicit navigation groups" in issue for issue in report.issues
    )


def test_book_page_rejects_repeated_explanation(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/ai.md",
        title="AI",
        canonical_id="books/ai",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("AI",),
    )
    _write_page(
        tmp_path,
        "wiki/books/llm.md",
        title="LLM 책",
        canonical_id="books/llm",
        node_kind="entity",
        parent="[[wiki/books/ai|AI]]",
        keywords=("LLM 책",),
        view_mode="linear",
        extra="entity_kind: book\n",
        body="책의 내용을 길게 설명한다.",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.pages == {}
    assert report.issues == (
        "wiki/books/llm.md: book page body must contain only headings and hyperlink rows",
    )


def test_book_page_accepts_text_group_with_indented_chapter_links(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/ai.md",
        title="AI",
        canonical_id="books/ai",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("AI",),
    )
    _write_page(
        tmp_path,
        "wiki/books/ai/llm.md",
        title="LLM 책",
        canonical_id="books/llm",
        node_kind="entity",
        parent="[[wiki/books/ai|AI]]",
        keywords=("LLM 책",),
        view_mode="linear",
        extra="entity_kind: book\n",
        body="## 1. 기초\n\n- [[wiki/books/ch-1|Ch 1]]",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()


def test_learning_book_rejects_legacy_study_horizons(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming.md",
        title="프로그래밍",
        canonical_id="books/programming",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("프로그래밍",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming/kotlin.md",
        title="Kotlin 책",
        canonical_id="books/kotlin",
        node_kind="entity",
        parent="[[wiki/books/programming|프로그래밍]]",
        keywords=("Kotlin 책",),
        view_mode="linear",
        extra="entity_kind: book\ncontent_kind: book\n",
        body=(
            "## 책 전체 학습 해상도\n\n"
            "같은 전체 목차를 세 깊이로 학습한다.\n\n"
            "### 2주 · 핵심\n\n- [[wiki/books/ch-1|Ch 1]]\n\n"
            "### 1달 · 전체\n\n- [[wiki/books/ch-1|Ch 1]]\n\n"
            "### 5달 · 심화\n\n- [[wiki/books/ch-1|Ch 1]]\n\n"
            "## 1. 기초\n\n- [[wiki/books/ch-1|Ch 1]]"
        ),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == (
        "wiki/books/programming/kotlin.md: book page must follow the verified table "
        "of contents without 2주·1달·5달 study horizons",
    )


def test_learning_book_accepts_verified_contents_without_study_horizons(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming.md",
        title="프로그래밍",
        canonical_id="books/programming",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("프로그래밍",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming/kotlin.md",
        title="Kotlin 책",
        canonical_id="books/kotlin",
        node_kind="entity",
        parent="[[wiki/books/programming|프로그래밍]]",
        keywords=("Kotlin 책",),
        view_mode="linear",
        extra="entity_kind: book\ncontent_kind: book\n",
        body="## 목차\n\n- [[wiki/books/ch-1|Ch 1]]",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()


def test_learning_book_projects_every_declared_child_as_visible(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming.md",
        title="프로그래밍",
        canonical_id="books/programming",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("프로그래밍",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming/kotlin.md",
        title="Kotlin 책",
        canonical_id="books/kotlin",
        node_kind="entity",
        parent="[[wiki/books/programming|프로그래밍]]",
        keywords=("Kotlin 책",),
        view_mode="linear",
        extra=(
            "entity_kind: book\n"
            "content_kind: book\n"
            "navigation_groups:\n"
            "- label: 목차\n"
            "  children:\n"
            "  - books/kotlin/chapter-01\n"
        ),
            body="",
    )
    _write_page(
        tmp_path,
        "wiki/books/programming/kotlin/chapter-01.md",
        title="1장",
        canonical_id="books/kotlin/chapter-01",
        node_kind="detail",
        parent="[[wiki/books/programming/kotlin|Kotlin 책]]",
        keywords=("1장",),
        view_mode="linear",
        body="장 본문",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    rendered = report.pages[tmp_path / "wiki/books/programming/kotlin.md"].decode("utf-8")
    assert "## 목차\n- [[wiki/books/programming/kotlin/chapter-01|1장]]" in rendered
    assert "## 하위 키워드" not in rendered


def test_learning_book_rejects_same_page_chapter_anchors(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/README.md",
        title="책",
        canonical_id="books/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("책",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming.md",
        title="프로그래밍",
        canonical_id="books/programming",
        node_kind="hub",
        parent="[[wiki/books/README|책]]",
        keywords=("프로그래밍",),
    )
    _write_page(
        tmp_path,
        "wiki/books/programming/kotlin.md",
        title="Kotlin 책",
        canonical_id="books/kotlin",
        node_kind="entity",
        parent="[[wiki/books/programming|프로그래밍]]",
        keywords=("Kotlin 책",),
        view_mode="linear",
        extra="entity_kind: book\ncontent_kind: book\n",
        body="## 목차\n\n- [[#1장|1장]]",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == (
        "wiki/books/programming/kotlin.md: book contents must link to chapter pages, not anchors",
    )


def test_learning_book_rejects_nested_heading_links_as_fake_children(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/books/kotlin.md",
        title="Kotlin in Action",
        canonical_id="books/kotlin",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        keywords=("Kotlin in Action",),
        extra="entity_kind: book\n",
        body=(
            "## 목차\n\n"
            "- 1부\n"
            "  - [[wiki/books/kotlin/chapter-01|1장]]\n"
            "    - [[wiki/books/kotlin/chapter-01/section-01|1.1절]]\n"
        ),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert (
        "wiki/books/kotlin.md: book page body must contain only headings and hyperlink rows"
    ) in report.issues


def test_numbered_lesson_must_stay_under_its_owning_chapter(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
        extra=(
            "navigation_groups:\n"
            "- label: Kotlin\n"
            "  children:\n"
            "  - personal/kotlin-in-action/chapter-02\n"
            "  - personal/kotlin-in-action/chapter-02/2-1-1-first-program\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/personal/kotlin-in-action/chapter-02.md",
        title="Ch 2",
        canonical_id="personal/kotlin-in-action/chapter-02",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("Kotlin Chapter 2",),
        view_mode="linear",
    )
    _write_page(
        tmp_path,
        "wiki/personal/kotlin-in-action/chapter-02/2-1-1-first-program.md",
        title="2.1.1 첫 프로그램",
        canonical_id="personal/kotlin-in-action/chapter-02/2-1-1-first-program",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("2.1.1 첫 프로그램",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert (
        "wiki/personal/kotlin-in-action/chapter-02/2-1-1-first-program.md: "
        "numbered lesson must stay below "
        "wiki/personal/kotlin-in-action/chapter-02.md"
    ) in report.issues


def test_numbered_lesson_accepts_verified_nested_section_depth(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-02.md",
        title="2장",
        canonical_id="personal/book/chapter-02",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("2장",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-02/2-3.md",
        title="2.3 구현하기",
        canonical_id="personal/book/chapter-02/2-3",
        node_kind="topic",
        parent="[[wiki/personal/book/chapter-02|2장]]",
        keywords=("2.3 구현하기",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-02/2-3/2-3-1.md",
        title="2.3.1 첫 구현",
        canonical_id="personal/book/chapter-02/2-3/2-3-1",
        node_kind="detail",
        parent="[[wiki/personal/book/chapter-02/2-3|2.3]]",
        keywords=("2.3.1 첫 구현",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert not any("numbered lesson" in issue for issue in report.issues)


def test_chapter_body_rejects_manual_duplicates_of_managed_lesson_links(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/kotlin-in-action/chapter-02.md",
        title="Ch 2",
        canonical_id="personal/kotlin-in-action/chapter-02",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("Kotlin Chapter 2",),
        view_mode="linear",
        body=(
            "상세 설명을 상위 페이지에 복제한다.\n\n"
            "- [[wiki/personal/kotlin-in-action/chapter-02/2-1-1-first-program|2.1.1]]"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/personal/kotlin-in-action/chapter-02/2-1-1-first-program.md",
        title="2.1.1 첫 프로그램",
        canonical_id="personal/kotlin-in-action/chapter-02/2-1-1-first-program",
        node_kind="topic",
        parent="[[wiki/personal/kotlin-in-action/chapter-02|Ch 2]]",
        keywords=("2.1.1 첫 프로그램",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == (
        "wiki/personal/kotlin-in-action/chapter-02.md: chapter body must not duplicate "
        "managed lesson links: personal/kotlin-in-action/chapter-02/2-1-1-first-program",
    )


def test_people_hub_rejects_topic_as_direct_person(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/people/README.md",
        title="인물",
        canonical_id="people/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("인물",),
    )
    _write_page(
        tmp_path,
        "wiki/people/analysis.md",
        title="이민정 AI 분석",
        canonical_id="people/analysis",
        node_kind="topic",
        parent="[[wiki/people/README|인물]]",
        keywords=("이민정 AI 분석",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.pages == {}
    assert report.issues == (
        "wiki/people/analysis.md: direct children of people must be person entities",
    )


def test_raw_source_archive_is_not_a_second_wiki_tree(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    raw = tmp_path / "wiki/private/_sources/novel/raw.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("원자료는 Wiki page schema가 아니다.\n", encoding="utf-8")

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.document_count == 1
    assert report.issues == ()
    assert raw not in report.pages


def test_resource_topic_rejects_prose_and_child_entity_cards(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/README.md",
        title="리소스",
        canonical_id="resources/README",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("리소스",),
    )
    _write_page(
        tmp_path,
        "wiki/resources/ai.md",
        title="AI",
        canonical_id="resources/ai",
        node_kind="topic",
        parent="[[wiki/resources/README|리소스]]",
        keywords=("AI",),
        body="이 설명은 리소스 색인에 두지 않는다.",
    )
    _write_page(
        tmp_path,
        "wiki/resources/old-card.md",
        title="과거 콘텐츠 카드",
        canonical_id="resources/old-card",
        node_kind="entity",
        parent="[[wiki/resources/ai|AI]]",
        keywords=("과거 콘텐츠 카드",),
        extra="entity_kind: resource\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any("must contain only hyperlink rows" in issue for issue in report.issues)
    assert any("must not own intermediate Wiki children" in issue for issue in report.issues)
    assert any("resource entity cards are retired" in issue for issue in report.issues)


def test_book_chapter_map_rejects_h2_topic_wrapper_child(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01.md",
        title="1장 기초",
        canonical_id="personal/book/chapter-01",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("1장 기초",),
        extra=(
            "navigation_groups:\n"
            "- label: 1.1 함수\n"
            "  children:\n"
            "  - personal/book/chapter-01/1-1-function\n"
            "  - personal/book/chapter-01/1-1-1-declaration\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01/1-1-function.md",
        title="1.1 함수",
        canonical_id="personal/book/chapter-01/1-1-function",
        node_kind="detail",
        parent="[[wiki/personal/book/chapter-01|1장 기초]]",
        keywords=("1.1 함수",),
        body="[[wiki/personal/book/chapter-01/1-1-1-declaration|함수 선언]]",
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01/1-1-1-declaration.md",
        title="1.1.1 함수 선언",
        canonical_id="personal/book/chapter-01/1-1-1-declaration",
        node_kind="detail",
        parent="[[wiki/personal/book/chapter-01|1장 기초]]",
        keywords=("1.1.1 함수 선언",),
        body="함수 선언을 설명한다.",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert (
        "wiki/personal/book/chapter-01.md: chapter H2 topic must not be repeated as a "
        "wrapper child: personal/book/chapter-01/1-1-function"
    ) in report.issues


def test_book_chapter_map_keeps_source_section_with_direct_content(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
        keywords=("Wiki",),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01.md",
        title="1장 기초",
        canonical_id="personal/book/chapter-01",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("1장 기초",),
        extra=(
            "navigation_groups:\n"
            "- label: 1.1 함수\n"
            "  children:\n"
            "  - personal/book/chapter-01/1-1-function\n"
            "  - personal/book/chapter-01/1-1-1-declaration\n"
        ),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01/1-1-function.md",
        title="1.1 함수",
        canonical_id="personal/book/chapter-01/1-1-function",
        node_kind="detail",
        parent="[[wiki/personal/book/chapter-01|1장 기초]]",
        keywords=("1.1 함수",),
        body=(
            "함수는 입력을 받아 값을 계산하며, 이 절의 도입에서는 선언과 호출의 "
            "공통 실행 흐름을 먼저 설명한다."
        ),
    )
    _write_page(
        tmp_path,
        "wiki/personal/book/chapter-01/1-1-1-declaration.md",
        title="1.1.1 함수 선언",
        canonical_id="personal/book/chapter-01/1-1-1-declaration",
        node_kind="detail",
        parent="[[wiki/personal/book/chapter-01|1장 기초]]",
        keywords=("1.1.1 함수 선언",),
        body="함수 선언을 설명한다.",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert not any("wrapper child" in issue for issue in report.issues)
