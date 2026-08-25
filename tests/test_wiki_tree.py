from datetime import date
from pathlib import Path

from woon_core.knowledge.wiki_tree import prepare_wiki_tree_refresh
from woon_core.knowledge.wiki_tree_migration import (
    apply_entity_landing_migration,
    prepare_entity_landing_migration,
)


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
    parent_row = f"parent: '{parent}'\n" if parent else ""
    keyword_rows = "\n".join(f"- {keyword}" for keyword in keywords)
    path.write_text(
        "---\n"
        f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n{parent_row}keywords:\n{keyword_rows}\n"
        f"aliases: []\nview_mode: {view_mode}\nupdated: 2026-08-25\n"
        "summary: 이 페이지의 핵심 내용을 한눈에 설명한다.\n"
        f"knowledge_state: 확인 필요\n{extra}---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def test_entity_without_children_lists_keywords_and_latest_related_documents(
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
        extra="entity_kind: project\n",
    )
    _write_page(
        tmp_path,
        "wiki/tokenizer.md",
        title="토크나이저",
        canonical_id="tokenizer",
        node_kind="topic",
        parent="[[wiki/README|Wiki]]",
        keywords=("토크나이저",),
    )

    report = prepare_wiki_tree_refresh(tmp_path)
    rendered = report.pages[tmp_path / "wiki/projects/llm-course.md"].decode("utf-8")

    assert report.issues == ()
    assert "한눈에 보기" not in rendered
    assert "## 최신 관련 문서" in rendered
    assert "[[wiki/tokenizer|토크나이저]]" in rendered
    latest = rendered.split("<!-- woon-wiki-latest:start -->", maxsplit=1)[1]
    assert "2026-08-25" not in latest
    assert " — " not in latest


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
        extra="entity_kind: person\n",
    )
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
        extra="entity_kind: book\n",
        body="## 목차\n\n- [[#Ch 1|Ch 1]]\n\n## Ch 1\n\n- [[wiki/ai/attention|Attention]]",
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
        ("wiki/projects/calendar.md", "Context Calendar", "projects/calendar"),
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
            extra="entity_kind: project\n",
        )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/projects/README.md"].decode("utf-8")
    assert "- 창작\n  - [[wiki/projects/novel|(미정)소설 집필]]" in catalog
    assert "- 시스템·도구\n  - [[wiki/projects/calendar|Context Calendar]]" in catalog
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
        extra=(
            "navigation_groups:\n"
            "- label: 창작\n"
            "  children:\n"
            "  - projects/missing\n"
        ),
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
        extra="entity_kind: project\n",
    )

    report = prepare_wiki_tree_refresh(tmp_path)

    assert any("contain non-direct children: projects/missing" in issue for issue in report.issues)
    assert any("omit direct children: projects/novel" in issue for issue in report.issues)


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
        extra=(
            "navigation_groups:\n"
            "- label: AI\n"
            "  children:\n"
            "  - resources/ai\n"
        ),
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


def test_resource_groups_merge_related_sources_into_bundle_links(tmp_path: Path) -> None:
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
        extra=(
            "navigation_groups:\n"
            "- label: AI\n"
            "  children:\n"
            "  - resources/ai\n"
        ),
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
            "- [[assets/aice-guide.png|시험 안내]]\n"
            "- [[assets/aice-schedule.png|시험 일정]]",
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

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/resources/README.md"].decode("utf-8")
    assert "- AI\n  - [[wiki/resources/ai/aice|AICE Associate]]" in catalog
    assert "  - [[wiki/resources/ai/transformer|Transformer Explainer]]" in catalog
    assert "시험 안내" not in catalog
    assert "Transformer 원자료" not in catalog


def test_large_grouping_hub_stays_as_one_link(tmp_path: Path) -> None:
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

    assert report.issues == ()
    catalog = report.pages[tmp_path / "wiki/concepts/README.md"].decode("utf-8")
    assert "- [[wiki/concepts/large|큰 분류]]" in catalog
    assert "  - [[wiki/concepts/topic-0|주제 0]]" not in catalog


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


def test_entity_landing_migration_preserves_information_and_separates_history(
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
        "wiki/projects.md",
        title="프로젝트",
        canonical_id="projects",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        keywords=("프로젝트",),
    )
    _write_page(
        tmp_path,
        "wiki/sample.md",
        title="샘플 프로젝트",
        canonical_id="sample",
        node_kind="entity",
        parent="[[wiki/projects|프로젝트]]",
        keywords=("샘플 프로젝트",),
        view_mode="project",
        extra="entity_kind: project\nrecord_owner: choi-woonyoung\nfacets:\n- 프로젝트\n",
        body=("## 목표\n\n검증 가능한 결과를 만든다.\n\n## 시간 이력\n\n- 2026-08-25 · 시작"),
    )

    report = prepare_entity_landing_migration(tmp_path, migration_day=date(2026, 8, 25))
    apply_entity_landing_migration(tmp_path, report)

    landing = (tmp_path / "wiki/sample.md").read_text(encoding="utf-8")
    information = (tmp_path / "wiki/sample-정보.md").read_text(encoding="utf-8")
    history = (tmp_path / "wiki/sample-히스토리.md").read_text(encoding="utf-8")
    replay = prepare_entity_landing_migration(tmp_path, migration_day=date(2026, 8, 25))

    assert "검증 가능한 결과" not in landing
    assert "[[wiki/sample-정보|샘플 프로젝트 정보]]" in landing
    assert "[[wiki/sample-히스토리|샘플 프로젝트 히스토리]]" in landing
    assert "검증 가능한 결과" in information
    assert "2026-08-25 · 시작" not in information
    assert "2026-08-25 · 시작" in history
    assert replay.changed_count == 0


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
    assert any("must not own Wiki child entities" in issue for issue in report.issues)
    assert any("resource entity cards are retired" in issue for issue in report.issues)
