from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.woon_wiki import (
    INTERVIEW_ARCHIVE_START,
    INTERVIEW_CURRENT_START,
    INTERVIEW_HISTORY_START,
    WIKI_CURRENT_START,
    WIKI_TIMELINE_START,
    InterviewAnswerRevision,
    WikiDelta,
    apply_prepared_wiki_pages,
    compiled_wiki_contract,
    prepare_wiki_article_view_refresh,
    prepare_wiki_corpus_migration,
    prepare_wiki_pages,
    preserve_managed_context,
    resolve_wiki_path,
    transition_knowledge_state,
)
from woon_core.knowledge.yaml_cache import _parse_yaml_text


def _metadata(text: str) -> dict[str, object]:
    block = text.split("---", 2)[1]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict)
    return parsed


def _write_parent(tmp_path: Path, relative: str = "wiki/README.md", title: str = "Wiki") -> str:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    canonical_id = Path(relative).with_suffix("").relative_to("wiki").as_posix()
    path.write_text(
        f"---\ntype: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n---\n",
        encoding="utf-8",
    )
    return f"[[{Path(relative).with_suffix('').as_posix()}|{title}]]"


def test_compiled_wiki_contract_matches_single_wiki_model() -> None:
    text = """---
type: Wiki
title: 활성화 함수
tags: [domain:ai, topic:activation-function]
---

# 활성화 함수

> 한 줄 요약: 활성화 함수는 신경망에 비선형성을 더한다.
"""

    contract = compiled_wiki_contract(Path("ai/activation-functions.md"), text)

    assert contract["canonical_id"] == "ai/activation-functions"
    assert contract["facets"] == ["개념", "학습"]
    assert contract["knowledge_state"] == "근거 확인됨"
    assert contract["parent"] == "[[wiki/ai/README|ai]]"
    assert contract["summary"] == "활성화 함수는 신경망에 비선형성을 더한다."


def test_compiled_wiki_contract_keeps_explicit_human_summary() -> None:
    text = """---
type: Wiki
title: Link Calendar
summary: 날짜 정본 노트를 월에서 날짜를 거쳐 여는 읽기 전용 탐색 도구다.
---

# Link Calendar

## 현재 이해

- 정본 링크만 표시한다.
"""

    contract = compiled_wiki_contract(Path("personal/context-calendar.md"), text)

    assert contract["summary"] == "날짜 정본 노트를 월에서 날짜를 거쳐 여는 읽기 전용 탐색 도구다."


def test_compiled_wiki_root_has_no_parent() -> None:
    contract = compiled_wiki_contract(
        Path("README.md"),
        "---\ntype: Wiki\ntitle: Wiki\n---\n\n# Wiki\n\n지식 정본의 입구다.\n",
    )

    assert contract["canonical_id"] == "README"
    assert "parent" not in contract


def test_compiled_page_without_section_index_links_to_wiki_root() -> None:
    contract = compiled_wiki_contract(
        Path("pintos/pintos.md"),
        "---\ntype: Wiki\ntitle: PintOS\n---\n\n# PintOS\n\n교육용 운영체제다.\n",
    )

    assert contract["parent"] == "[[wiki/README|Wiki]]"


def test_compiled_contract_preserves_explicit_semantic_parent() -> None:
    contract = compiled_wiki_contract(
        Path("personal/projects/kubernetes-runtime.md"),
        """---
type: Wiki
title: Kubernetes 장애 복구 서비스 런타임
parent: '[[wiki/personal/projects/kubernetes-장애-복구-서비스|Kubernetes 장애 복구 서비스]]'
---

# Kubernetes 장애 복구 서비스 런타임

이벤트 처리 런타임을 설명한다.
""",
    )

    assert contract["parent"] == (
        "[[wiki/personal/projects/kubernetes-장애-복구-서비스|Kubernetes 장애 복구 서비스]]"
    )


def test_compiled_contract_does_not_treat_a_project_folder_as_entity_metadata() -> None:
    contract = compiled_wiki_contract(
        Path("personal/projects/kubernetes-runtime.md"),
        """---
type: Wiki
title: Kubernetes 런타임
facets:
- 개념
- 학습
---

# Kubernetes 런타임

프로젝트에서 배운 런타임 개념이다.
""",
    )

    assert contract["facets"] == ["개념", "학습"]


def test_compiler_context_does_not_restore_stale_derived_metadata() -> None:
    existing = """---
type: Wiki
title: 이전 제목
summary: 이전 요약
facets:
- 개념
parent: '[[wiki/README|Wiki]]'
people:
- '[[wiki/personal/people/최우녕|최우녕]]'
knowledge_state: 근거 확인됨
---

# 이전 제목

이전 본문이다.
"""
    rendered = """---
type: Wiki
title: Kubernetes 장애 복구 서비스
summary: 새 compiler 요약
facets:
- 프로젝트
- 학습
parent: '[[wiki/personal/projects/README|프로젝트]]'
knowledge_state: 근거 확인됨
---

# Kubernetes 장애 복구 서비스

새 본문이다.
"""

    merged = preserve_managed_context(existing, rendered)

    assert "summary: 새 compiler 요약" in merged
    assert "- 프로젝트" in merged
    assert "- 학습" in merged
    assert "[[wiki/personal/projects/README|프로젝트]]" in merged
    assert "[[wiki/README|Wiki]]" not in merged
    assert "[[wiki/personal/people/최우녕|최우녕]]" in merged


def test_compiled_contract_accepts_standard_yaml_block_facets() -> None:
    contract = compiled_wiki_contract(
        Path("personal/interview/README.md"),
        """---
type: Wiki
title: 면접 답변 운영 원칙
facets:
- 커리어
- 학습
---

# 면접 답변 운영 원칙

질문별 답변을 관리한다.
""",
    )

    assert contract["facets"] == ["커리어", "학습"]


def test_new_entity_keeps_current_knowledge_on_root_and_separates_history(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    pages = prepare_wiki_pages(
        tmp_path,
        (
            WikiDelta(
                title="AICE Associate 준비",
                summary="회귀와 분류 문제를 반복해서 풀며 시험을 준비한다.",
                facets=("프로젝트", "학습", "커리어"),
                knowledge_state="생각 중",
                day=date(2026, 8, 24),
                intent="자격 취득 준비를 한곳에서 추적하려는 것으로 보인다.",
                parent=parent,
                keywords=("AICE Associate", "자격 준비"),
                node_kind="entity",
                view_mode="project",
                entity_kind="project",
            ),
        ),
    )

    assert [path.relative_to(tmp_path).as_posix() for path in pages] == [
        "wiki/nodes/aice-associate-준비.md",
        "wiki/nodes/aice-associate-준비-히스토리.md",
    ]
    landing = pages[tmp_path / "wiki/nodes/aice-associate-준비.md"].decode("utf-8")
    history = pages[tmp_path / "wiki/nodes/aice-associate-준비-히스토리.md"].decode("utf-8")
    assert _metadata(landing)["canonical_id"] == "nodes/aice-associate-준비"
    assert WIKI_CURRENT_START in landing
    assert WIKI_TIMELINE_START not in landing
    assert 'facets: ["프로젝트", "학습", "커리어"]' in landing
    assert "project_id: aice-associate-준비" in landing
    assert 'objective: "회귀와 분류 문제를 반복해서 풀며 시험을 준비한다."' in landing
    assert WIKI_CURRENT_START not in history
    assert WIKI_TIMELINE_START in history


def test_existing_subject_is_updated_in_place_and_keeps_free_prose(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path, "wiki/ai/README.md", "AI")
    path = tmp_path / "wiki/ai/transformer.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """---
type: Wiki
title: Transformer
facets: ["개념", "학습"]
knowledge_state: "근거 확인됨"
---

# Transformer

## 기존 설명

이 문단은 compiler 또는 사람이 소유한다.
""",
        encoding="utf-8",
    )

    pages = prepare_wiki_pages(
        tmp_path,
        (
            WikiDelta(
                title="Transformer",
                summary="Attention의 계산 흐름을 다시 확인할 필요가 있다.",
                facets=("개념", "학습"),
                knowledge_state="확인 필요",
                day=date(2026, 8, 24),
                parent=parent,
                keywords=("Transformer",),
            ),
        ),
    )

    assert list(pages) == [path]
    text = pages[path].decode("utf-8")
    assert "이 문단은 compiler 또는 사람이 소유한다." in text
    assert 'knowledge_state: "확인 필요"' in text
    assert WIKI_TIMELINE_START not in text


def test_resolver_rejects_duplicate_subject_titles(tmp_path: Path) -> None:
    for relative in ("wiki/ai/a.md", "wiki/tools/b.md"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntype: Wiki\ntitle: 같은 주제\n---\n", encoding="utf-8")

    with pytest.raises(WoonError, match="multiple documents"):
        resolve_wiki_path(tmp_path, "같은 주제")


def test_corpus_migration_normalizes_every_visible_wiki_document(tmp_path: Path) -> None:
    first = tmp_path / "wiki/database/index.md"
    first.parent.mkdir(parents=True)
    first.write_text(
        """---
type: 키워드
title: DB 인덱스
status: Active
tags:
- domain:database
llm_wiki:
  page_id: database/index
---

# DB 인덱스

근거가 있는 기존 설명이다.
""",
        encoding="utf-8",
    )
    canonical = tmp_path / "wiki/canonical/hidden.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        "---\ntype: Wiki\ntitle: 기존 정본\nstatus: Canonical\n---\n\n# 기존 정본\n",
        encoding="utf-8",
    )

    report = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))

    assert report.document_count == 2
    assert report.changed_count == 2
    text = report.pages[first].decode("utf-8")
    metadata = _metadata(text)
    assert "type: Wiki" in text
    assert metadata["canonical_id"] == "database/index"
    assert metadata["facets"] == ["개념", "학습"]
    assert metadata["knowledge_state"] == "근거 확인됨"
    assert "근거가 있는 기존 설명이다." in text
    assert metadata["summary"] == "근거가 있는 기존 설명이다."
    assert "단일 Wiki 정본 계약" not in text
    canonical_text = report.pages[canonical].decode("utf-8")
    canonical_metadata = _metadata(canonical_text)
    assert canonical_metadata["canonical_id"] == "canonical/hidden"
    assert canonical_metadata["knowledge_state"] == "확인 필요"


def test_article_view_refresh_does_not_rewrite_frontmatter_or_prose(tmp_path: Path) -> None:
    page = tmp_path / "wiki/ai/queue.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 큐
summary: 먼저 들어온 항목을 먼저 처리하는 자료구조다.
facets:
- 개념
- 학습
knowledge_state: 근거 확인됨
parent_topics:
- '[[wiki/algorithm/README|알고리즘과 자료구조]]'
---

# 큐

보존할 본문이다.
""",
        encoding="utf-8",
    )

    report = prepare_wiki_article_view_refresh(tmp_path)
    refreshed = report.pages[page].decode("utf-8")

    assert report.changed_count == 0
    assert refreshed.count("facets:") == 1
    assert refreshed.count("parent_topics:") == 1
    assert "보존할 본문이다." in refreshed
    assert "> [!info] 한눈에 보기" not in refreshed


def test_new_topic_keeps_one_current_section_without_duplicate_first_history(
    tmp_path: Path,
) -> None:
    parent = _write_parent(tmp_path)
    delta = WikiDelta(
        title="대화 지식화",
        summary="대화의 재사용 가능한 결론만 Wiki에 승격한다.",
        facets=("개념",),
        knowledge_state="확인 필요",
        day=date(2026, 8, 28),
        parent=parent,
        keywords=("대화 지식화",),
        intent="채팅 로그가 아니라 현재 결론을 다시 찾기 위해 남긴다.",
    )

    text = next(iter(prepare_wiki_pages(tmp_path, (delta,)).values())).decode("utf-8")

    assert text.count("대화의 재사용 가능한 결론만 Wiki에 승격한다.") == 2
    assert "## 핵심 정리" in text
    assert WIKI_TIMELINE_START not in text
    assert "추정 의도:" not in text
    assert "채팅 로그가 아니라 현재 결론을 다시 찾기 위해 남긴다." in text


def test_interview_question_uses_current_answer_as_sole_current_state(tmp_path: Path) -> None:
    parent = _write_parent(tmp_path)
    delta = WikiDelta(
        title="문제 선택 기준은 무엇입니까?",
        summary="영향과 검증 가능성을 함께 본다.",
        facets=("커리어",),
        knowledge_state="확인 필요",
        day=date(2026, 8, 28),
        parent=parent,
        keywords=("문제 선택 기준",),
        interview_tracks=("AI Engineer",),
        question_topic="문제 선택과 검증",
        interview_answer=InterviewAnswerRevision(
            question="문제 선택 기준은 무엇입니까?",
            answer="사용자 영향이 크고 결과를 검증할 수 있는 문제를 먼저 고른다.",
        ),
    )

    text = next(iter(prepare_wiki_pages(tmp_path, (delta,)).values())).decode("utf-8")

    assert "## 현재 이해" not in text
    assert WIKI_TIMELINE_START not in text
    assert text.count("## 현재 최선 답변") == 1
    assert "사용자 영향이 크고 결과를 검증할 수 있는 문제를 먼저 고른다." in text


def test_article_refresh_removes_legacy_repeated_scaffolding(tmp_path: Path) -> None:
    page = tmp_path / "wiki/question.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 질문
summary: 같은 결론
---

# 질문

## 현재 이해

<!-- woon-wiki-current:start -->
같은 결론
<!-- woon-wiki-current:end -->

## 한 줄 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-28 · 질문 — 같은 결론
<!-- woon-wiki-timeline:end -->

## 남긴 의도

추정 의도: 다시 읽는다.

## 현재 최선 답변

<!-- woon-interview-current:start -->
### 질문

무엇입니까?

### 답변

같은 결론
<!-- woon-interview-current:end -->
""",
        encoding="utf-8",
    )

    refreshed = prepare_wiki_article_view_refresh(tmp_path).pages[page].decode("utf-8")

    assert WIKI_CURRENT_START not in refreshed
    assert WIKI_TIMELINE_START not in refreshed
    assert "추정 의도:" not in refreshed
    assert "## 남긴 의도" not in refreshed
    assert "## 판단 기준" in refreshed
    assert "다시 읽는다." in refreshed
    assert INTERVIEW_CURRENT_START in refreshed


def test_article_refresh_removes_parent_only_related_section(tmp_path: Path) -> None:
    parent = tmp_path / "wiki/project.md"
    parent.parent.mkdir(parents=True)
    parent.write_text("# 프로젝트\n", encoding="utf-8")
    page = tmp_path / "wiki/decision.md"
    page.write_text(
        """---
type: Wiki
title: 결정
parent: '[[wiki/project|프로젝트]]'
---

# 결정

## 연결

- [[wiki/project]]
""",
        encoding="utf-8",
    )

    refreshed = prepare_wiki_article_view_refresh(tmp_path).pages[page].decode("utf-8")

    assert "## 연결" not in refreshed
    assert "## 관련 문서" not in refreshed


def test_article_refresh_keeps_related_document_without_repeating_parent(tmp_path: Path) -> None:
    page = tmp_path / "wiki/decision.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 결정
parent: '[[wiki/project|프로젝트]]'
---

# 결정

## 관련 문서

- [[wiki/project]]
- [[wiki/evidence|근거]]
""",
        encoding="utf-8",
    )

    refreshed = prepare_wiki_article_view_refresh(tmp_path).pages[page].decode("utf-8")

    assert "[[wiki/project]]" not in refreshed
    assert "[[wiki/evidence|근거]]" in refreshed


def test_article_refresh_keeps_archived_answer_without_repeating_stable_question(
    tmp_path: Path,
) -> None:
    page = tmp_path / "wiki/question.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 왜 Draft PR을 사용했습니까?
---

# 왜 Draft PR을 사용했습니까?

## 과거 답변

<!-- woon-interview-archive:start -->
### 2026-08-24 · 이전 답변

### 질문 맥락

이전 면접 그래프에서 연습한 질문이다.

### 질문

왜 Draft PR을 사용했습니까?

### 답변

사람이 승인하기 전에는 제안 상태임을 드러내기 위해 사용했다.
<!-- woon-interview-archive:end -->
""",
        encoding="utf-8",
    )

    refreshed = prepare_wiki_article_view_refresh(tmp_path).pages[page].decode("utf-8")

    assert refreshed.count("왜 Draft PR을 사용했습니까?") == 2
    assert "이전 면접 그래프에서 연습한 질문이다." not in refreshed
    assert "사람이 승인하기 전에는 제안 상태임을 드러내기 위해 사용했다." in refreshed


def test_article_refresh_removes_only_empty_archived_interview_attempts(
    tmp_path: Path,
) -> None:
    page = tmp_path / "wiki/question.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 질문
---

# 질문

## 과거 답변

<!-- woon-interview-archive:start -->
### 2026-08-23 · 빈 시도

### 답변

아직 답변하지 않았다.

### 2026-08-24 · 확인된 답변

### 답변

승인 전에 제안 상태로 남겼다.
<!-- woon-interview-archive:end -->
""",
        encoding="utf-8",
    )

    refreshed = prepare_wiki_article_view_refresh(tmp_path).pages[page].decode("utf-8")

    assert "아직 답변하지 않았다." not in refreshed
    assert "빈 시도" not in refreshed
    assert "확인된 답변" in refreshed
    assert "승인 전에 제안 상태로 남겼다." in refreshed


def test_corpus_migration_replaces_yaml_block_lists_without_duplicate_keys(tmp_path: Path) -> None:
    page = tmp_path / "wiki/ai/queue.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
type: Wiki
title: 큐
facets:
- 개념
- 학습
parent_topics:
- '[[wiki/algorithm/README|알고리즘과 자료구조]]'
---

# 큐

먼저 들어온 항목을 먼저 처리하는 자료구조다.
""",
        encoding="utf-8",
    )

    first = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))
    page.write_bytes(first.pages[page])
    second = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))
    refreshed = second.pages[page].decode("utf-8")
    metadata = _metadata(refreshed)

    assert refreshed.count("facets:") == 1
    assert "parent_topics:" not in refreshed
    assert metadata["facets"] == ["개념", "학습"]
    assert metadata["parent"] == "[[wiki/algorithm/README|알고리즘과 자료구조]]"


def test_corpus_migration_preserves_and_recovers_entity_facets(tmp_path: Path) -> None:
    fixtures = {
        "wiki/personal/project.md": (
            "---\ntype: Wiki\ntitle: 시험 준비\nproject_id: exam\n"
            'facets: ["프로젝트", "학습"]\n---\n',
            ["프로젝트", "학습"],
        ),
        "wiki/personal/book.md": (
            "---\ntype: Wiki\ntitle: 학습 책\ncontent_kind: book\n"
            'facets: ["콘텐츠", "학습"]\n---\n',
            ["학습"],
        ),
        "wiki/personal/person.md": (
            "---\ntype: Wiki\ntitle: 확인된 사람\nentity_type: person\n"
            'person_id: known-person\nfacets: ["개념", "학습"]\n---\n',
            ["인물"],
        ),
    }
    for relative, (body, _) in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    first = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))
    apply_prepared_wiki_pages(tmp_path, first.pages)
    second = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))

    for relative, (_, expected) in fixtures.items():
        text = (tmp_path / relative).read_text(encoding="utf-8")
        assert _metadata(text)["facets"] == expected
    assert second.changed_count == 0


def test_corpus_migration_removes_operational_history_and_keeps_real_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wiki/ai/queue.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
type: Wiki
title: 큐
facets: ["개념", "학습"]
knowledge_state: "확인 필요"
---

# 큐

큐는 먼저 들어온 값을 먼저 꺼내는 자료구조다.

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 변경 — 기존 문서를 단일 Wiki 정본 계약으로 전환
- 2026-08-24 · 실행 — 큐 구현을 다시 연습했다.
<!-- woon-wiki-timeline:end -->
""",
        encoding="utf-8",
    )

    report = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))
    text = report.pages[path].decode("utf-8")

    assert "단일 Wiki 정본 계약" not in text
    assert "큐 구현을 다시 연습했다." in text
    assert _metadata(text)["summary"] == "큐는 먼저 들어온 값을 먼저 꺼내는 자료구조다."


def test_corpus_migration_prefers_human_one_line_summary_over_breadcrumb(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wiki/ai/attention.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
type: Wiki
title: Attention
facets: ["개념", "학습"]
knowledge_state: "근거 확인됨"
---

# Attention

<!-- breadcrumb:start -->
상위 링크: WIKI / AI / Attention
<!-- breadcrumb:end -->

> 한 줄 요약: Query와 Key를 비교한 가중치로 Value를 섞는다.

## 설명

Attention의 세부 계산을 설명한다.
""",
        encoding="utf-8",
    )

    report = prepare_wiki_corpus_migration(tmp_path, migration_day=date(2026, 8, 24))

    assert _metadata(report.pages[path].decode("utf-8"))["summary"] == (
        "Query와 Key를 비교한 가중치로 Value를 섞는다."
    )


def test_compiler_render_preserves_wiki_context() -> None:
    existing = """---
type: Wiki
title: 큐
canonical_id: ai/queue
facets: ["개념", "학습"]
knowledge_state: "확인 필요"
record_owner: choi-woonyoung
---

# 큐

## 현재 이해

<!-- woon-wiki-current:start -->
대화에서 남긴 현재 이해
<!-- woon-wiki-current:end -->

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""
    rendered = """---
type: 키워드
title: 큐
---

# 큐

근거로 다시 만든 설명
"""

    merged = preserve_managed_context(existing, rendered)

    assert "facets:\n- 개념\n- 학습" in merged
    assert "canonical_id: ai/queue" in merged
    assert "type: Wiki" in merged
    assert 'knowledge_state: "근거 확인됨"' in merged
    assert "근거로 다시 만든 설명" in merged
    assert "대화에서 남긴 현재 이해" in merged
    assert "큐를 다시 학습했다" in merged


def test_compiler_context_refactor_preserves_exact_output_bytes() -> None:
    existing = '''---
type: Wiki
title: 큐
canonical_id: ai/queue
facets: ["개념", "학습"]
knowledge_state: "확인 필요"
record_owner: choi-woonyoung
aliases:
- FIFO
parent: '[[wiki/algorithm/README|알고리즘과 자료구조]]'
---

# 큐

## 현재 이해

<!-- woon-wiki-current:start -->
대화에서 남긴 현재 이해
<!-- woon-wiki-current:end -->

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
'''
    rendered = '''---
type: 키워드
title: 큐
summary: 먼저 들어온 항목을 먼저 처리한다.
facets:
- 개념
- 학습
node_kind: detail
view_mode: article
updated: '2026-09-01'
---

# 큐

근거로 다시 만든 설명
'''
    expected = '''---
type: Wiki
title: 큐
summary: 먼저 들어온 항목을 먼저 처리한다.
facets:
- 개념
- 학습
node_kind: detail
view_mode: article
updated: '2026-09-01'
canonical_id: ai/queue
parent: '[[wiki/algorithm/README|알고리즘과 자료구조]]'
aliases:
- FIFO
record_owner: choi-woonyoung
knowledge_state: "근거 확인됨"
state_reason: accepted-evidence-receipt
---

# 큐

근거로 다시 만든 설명

## 핵심 정리

<!-- woon-wiki-current:start -->
대화에서 남긴 현재 이해
<!-- woon-wiki-current:end -->

## 한 줄 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
'''

    assert preserve_managed_context(existing, rendered) == expected


def test_compiler_context_parses_existing_and_rendered_frontmatter_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = '''---
type: Wiki
title: 큐
canonical_id: ai/queue
facets: [개념, 학습]
knowledge_state: 확인 필요
aliases: [FIFO]
---

# 큐

기존 설명
'''
    rendered = '''---
type: 키워드
title: 큐
summary: 먼저 들어온 항목을 먼저 처리한다.
---

# 큐

근거로 다시 만든 설명
'''
    _parse_yaml_text.cache_clear()
    original_load = yaml.load
    load_calls = 0

    def counted_load(stream: object, *args: object, **kwargs: object) -> object:
        nonlocal load_calls
        load_calls += 1
        return original_load(stream, *args, **kwargs)

    monkeypatch.setattr(yaml, "load", counted_load)

    preserve_managed_context(existing, rendered)

    assert load_calls == 2


def test_compiler_context_keeps_existing_fail_closed_errors() -> None:
    retired = '''---
type: Wiki
title: 큐
knowledge_state: 폐기됨
---

# 큐

폐기된 설명
'''
    rendered = '''---
type: Wiki
title: 큐
---

# 큐

새 설명
'''

    with pytest.raises(WoonError, match="retired Wiki document"):
        preserve_managed_context(retired, rendered)
    with pytest.raises(WoonError, match="requires YAML frontmatter"):
        preserve_managed_context("", "# 큐\n\n새 설명\n")
    with pytest.raises(WoonError, match="frontmatter is malformed"):
        preserve_managed_context("", "---\n- Wiki\n---\n\n# 큐\n\n새 설명\n")


def test_compiler_render_does_not_duplicate_managed_timeline() -> None:
    existing = """---
type: Wiki
title: 큐
knowledge_state: "확인 필요"
---

# 큐

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""
    rendered = existing.replace("확인 필요", "근거 확인됨")

    merged = preserve_managed_context(existing, rendered)

    assert merged.count("<!-- woon-wiki-timeline:start -->") == 1
    assert merged.count("## 한 줄 이력") == 1
    assert "큐를 다시 학습했다" in merged


def test_compiler_drops_generic_migration_history() -> None:
    existing = """---
type: Wiki
title: Wiki
knowledge_state: "근거 확인됨"
---

# Wiki

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 변경 — 기존 문서를 단일 Wiki 정본 계약으로 전환
<!-- woon-wiki-timeline:end -->
"""
    rendered = """---
type: Wiki
title: Wiki
knowledge_state: "근거 확인됨"
---

# Wiki

지식 정본의 입구다.
"""

    merged = preserve_managed_context(existing, rendered)

    assert "단일 Wiki 정본 계약으로 전환" not in merged
    assert WIKI_TIMELINE_START not in merged


def test_compiler_repairs_duplicate_timeline_in_historical_rendered_source() -> None:
    existing = """---
type: Wiki
title: 큐
knowledge_state: "확인 필요"
---

# 큐

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""
    duplicate = """

## 시간 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-24 · 실행 — 큐를 다시 학습했다.
<!-- woon-wiki-timeline:end -->
"""

    merged = preserve_managed_context(existing, existing + duplicate)

    assert merged.count("<!-- woon-wiki-timeline:start -->") == 1
    assert merged.count("## 한 줄 이력") == 1


def test_wiki_update_renders_one_compact_article_index() -> None:
    rendered = preserve_managed_context(
        "",
        """---
type: Wiki
title: 큐
summary: 먼저 들어온 항목을 먼저 처리하는 자료구조다.
facets: [개념, 학습]
knowledge_state: 근거 확인됨
parent: '[[wiki/algorithm/README|알고리즘과 자료구조]]'
node_kind: topic
view_mode: tree
---

# 큐

본문이다.
""",
    )

    assert "<!-- woon-wiki-overview:start -->" not in rendered
    assert "> [!info] 한눈에 보기" not in rendered
    assert rendered.endswith("# 큐\n\n본문이다.\n")


def test_compiler_normalizes_excess_blank_lines_below_h1() -> None:
    rendered = preserve_managed_context(
        "",
        """---
type: Wiki
title: 큐
summary: 먼저 들어온 항목을 먼저 처리하는 자료구조다.
facets: [개념, 학습]
knowledge_state: 근거 확인됨
---

# 큐



본문이다.
""",
    )

    assert rendered.endswith("# 큐\n\n본문이다.\n")


def test_prepared_batch_applies_all_pages(tmp_path: Path) -> None:
    first = tmp_path / "wiki/a.md"
    second = tmp_path / "wiki/b.md"

    written = apply_prepared_wiki_pages(
        tmp_path,
        {first: "첫 문서\n".encode(), second: "둘째 문서\n".encode()},
    )

    assert written == (first, second)
    assert first.read_text(encoding="utf-8") == "첫 문서\n"
    assert second.read_text(encoding="utf-8") == "둘째 문서\n"


def test_prepared_batch_rejects_paths_outside_wiki(tmp_path: Path) -> None:
    with pytest.raises(WoonError, match="only wiki"):
        apply_prepared_wiki_pages(tmp_path, {tmp_path / "brain/wiki/a.md": b"not allowed\n"})


def test_state_authorities_cannot_skip_verification_or_reopen_retired_page() -> None:
    assert (
        transition_knowledge_state(
            current_state="생각 중",
            requested_state="근거 확인됨",
            authority="evidence-compiler",
        )
        == "근거 확인됨"
    )
    with pytest.raises(WoonError, match="exceeds"):
        transition_knowledge_state(
            current_state="생각 중",
            requested_state="근거 확인됨",
            authority="conversation",
        )
    with pytest.raises(WoonError, match="explicit user"):
        transition_knowledge_state(
            current_state="폐기됨",
            requested_state="근거 확인됨",
            authority="evidence-compiler",
        )


def test_interview_answer_keeps_current_and_archives_the_previous_revision(
    tmp_path: Path,
) -> None:
    project = tmp_path / "wiki/personal/interview/ai-engineer/README.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        "\n".join(
            (
                "---",
                "type: Wiki",
                "title: AI Engineer 면접 준비",
                'facets: ["프로젝트", "커리어"]',
                'knowledge_state: "확인 필요"',
                "---",
                "# AI Engineer 면접 준비",
                "",
            )
        ),
        encoding="utf-8",
    )
    parent = "[[wiki/personal/interview/ai-engineer/README|AI Engineer 면접 준비]]"
    first = WikiDelta(
        title="Kyro에서 본인이 직접 한 일은 무엇입니까?",
        summary="Kyro의 개인 기여를 근거와 함께 설명한다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 18),
        parent=parent,
        keywords=("Kyro", "개인 기여"),
        interview_tracks=("AI Engineer",),
        question_topic="Kubernetes 장애 복구 서비스",
        interview_answer=InterviewAnswerRevision(
            question="Kyro에서 본인이 직접 한 일은 무엇입니까?",
            context="개인 기여와 팀 결과를 구분하는 질문이다.",
            answer="전체 구조를 설계했다.",
            limitations=("개인 기여의 코드 범위를 더 확인해야 한다.",),
            change_reason="첫 답변을 정리했다.",
            source_label="초기 답변",
        ),
    )
    path, content = next(iter(prepare_wiki_pages(tmp_path, (first,)).items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    second = WikiDelta(
        title=first.title,
        summary="개인 기여와 팀 결과를 분리해 답변한다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 24),
        parent=parent,
        keywords=first.keywords,
        interview_tracks=first.interview_tracks,
        question_topic=first.question_topic,
        interview_answer=InterviewAnswerRevision(
            question=first.title,
            context="개인 기여와 팀 결과를 구분하는 질문이다.",
            answer="관리 서버의 장애 판정 흐름을 설계하고 구현했다.",
            evidence=("관리 서버 코드와 계약 테스트",),
            limitations=("실환경 정확도를 증명한 결과는 아니다.",),
            change_reason="개인 기여와 검증 한계를 분리했다.",
            quality_assessment="근거 범위가 이전보다 명확해졌다.",
            source_label="근거 보강 전 답변",
        ),
    )
    merged = next(iter(prepare_wiki_pages(tmp_path, (second,)).values())).decode()

    assert merged.count(INTERVIEW_CURRENT_START) == 1
    assert merged.count(INTERVIEW_HISTORY_START) == 1
    assert merged.count(INTERVIEW_ARCHIVE_START) == 1
    assert "관리 서버의 장애 판정 흐름을 설계하고 구현했다." in merged
    assert "전체 구조를 설계했다." in merged
    assert "개인 기여와 검증 한계를 분리했다." in merged
    assert "실환경 정확도를 증명한 결과는 아니다." in merged
    assert _metadata(merged)["parent"] == parent


def test_interview_parent_must_resolve_to_an_existing_wiki(tmp_path: Path) -> None:
    delta = WikiDelta(
        title="면접 질문",
        summary="답변을 정리한다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 24),
        parent="[[wiki/personal/없는-프로젝트|없는 프로젝트]]",
        keywords=("면접 질문",),
        interview_answer=InterviewAnswerRevision(
            question="무엇을 했습니까?",
            answer="확인 중이다.",
        ),
    )

    with pytest.raises(WoonError, match="parent must point"):
        prepare_wiki_pages(tmp_path, (delta,))


def test_interview_answer_must_not_create_an_empty_question_page(tmp_path: Path) -> None:
    parent_path = tmp_path / "wiki/personal/interview/topics/example.md"
    parent_path.parent.mkdir(parents=True)
    parent_path.write_text("---\ntype: Wiki\ntitle: 예시 주제\n---\n", encoding="utf-8")
    delta = WikiDelta(
        title="무엇을 했습니까?",
        summary="답변이 생긴 뒤에만 정본 문서를 만든다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 28),
        parent="[[wiki/personal/interview/topics/example|예시 주제]]",
        keywords=("예시 질문",),
        interview_tracks=("공통 면접",),
        question_topic="예시 주제",
        interview_answer=InterviewAnswerRevision(
            question="무엇을 했습니까?",
            answer=None,
        ),
    )

    with pytest.raises(WoonError, match="must contain a reusable answer"):
        prepare_wiki_pages(tmp_path, (delta,))


def test_weaker_interview_attempt_is_archived_without_replacing_current(
    tmp_path: Path,
) -> None:
    project = tmp_path / "wiki/personal/interview/project.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        '---\ntype: Wiki\ntitle: 면접 프로젝트\nfacets: ["프로젝트"]\n---\n',
        encoding="utf-8",
    )
    parent = "[[wiki/personal/interview/project|면접 프로젝트]]"
    initial = WikiDelta(
        title="개인 기여는 무엇입니까?",
        summary="개인 기여를 설명한다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 23),
        parent=parent,
        keywords=("개인 기여",),
        interview_tracks=("공통 면접",),
        question_topic="개인 기여",
        interview_answer=InterviewAnswerRevision(
            question="개인 기여는 무엇입니까?",
            answer="계약 검증 흐름을 구현했다.",
            promote_current=True,
        ),
    )
    path, content = next(iter(prepare_wiki_pages(tmp_path, (initial,)).items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    practice = WikiDelta(
        title=initial.title,
        summary="연습 답변의 약점을 기록한다.",
        facets=("커리어", "학습"),
        knowledge_state="확인 필요",
        day=date(2026, 8, 24),
        parent=parent,
        keywords=initial.keywords,
        interview_tracks=initial.interview_tracks,
        question_topic=initial.question_topic,
        interview_answer=InterviewAnswerRevision(
            question=initial.title,
            answer="그냥 전체를 만들었다.",
            change_reason="개인 기여와 팀 결과를 섞어 설명했다.",
            source_label="연습 답변",
            promote_current=False,
        ),
    )
    merged = next(iter(prepare_wiki_pages(tmp_path, (practice,)).values())).decode()

    current = merged.split(INTERVIEW_CURRENT_START, 1)[1].split(
        "<!-- woon-interview-current:end -->", 1
    )[0]
    assert "계약 검증 흐름을 구현했다." in current
    assert "그냥 전체를 만들었다." not in current
    assert "그냥 전체를 만들었다." in merged
    assert "개인 기여와 팀 결과를 섞어 설명했다." in merged
