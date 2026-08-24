from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_orchestration import write_policy

from woon_core.errors import WoonError
from woon_core.knowledge.codex_daily_digest import record_daily_digest_from_codex_ledger
from woon_core.knowledge.codex_knowledge import (
    entries_from_records,
    load_daily_entries,
    load_daily_input_status,
    record_codex_knowledge_entries,
)
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import record_governance_preflight


def test_projects_one_safe_batch_to_single_wiki_and_daily_projection(tmp_path: Path) -> None:
    _settings(tmp_path)
    _write_wiki(tmp_path, "wiki/personal/herdr.md", "Herdr")
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-18.md").write_text("# 일일 기록\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "학습",
                "title": "대화 지식화는 한 번 분류하고 두 번 사용한다",
                "summary": (
                    "대화 요약은 Wiki와 하루 정리가 각각 다시 해석하지 않고 "
                    "하나의 최소 항목을 함께 사용해야 한다."
                ),
                "next_question": "결정 후보와 인물 후보는 어떤 기준으로 검토 경로에만 둘까?",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 정본 주제가 없다.",
                "related_documents": ["wiki/personal/herdr.md"],
                "calendar_contexts": [
                    {
                        "event_day": "2026-08-18",
                        "event_title": "학습 모임",
                        "related_documents": ["wiki/personal/herdr.md"],
                        "reason": "준비",
                        "include_wiki_subject": True,
                    }
                ],
            },
            {
                "day": "2026-08-18",
                "kind": "질문",
                "title": "대화 후보의 승격 기준을 어떻게 좁힐까",
                "summary": "반복해서 재사용할 수 없는 개인 대화는 Wiki에 넣지 않는다.",
                "wiki_update": False,
            },
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-001",
        entries=entries,
    )
    digest = record_daily_digest_from_codex_ledger(tmp_path, day=date(2026, 8, 18))

    wiki = tmp_path / "wiki/personal/대화-지식화는-한-번-분류하고-두-번-사용한다.md"
    assert result.entry_count == 2
    assert result.wiki_page_count == 1
    assert wiki.is_file()
    assert "다음 질문" in wiki.read_text(encoding="utf-8")
    assert (tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18").is_dir()
    rendered = (tmp_path / digest.relative_path).read_text(encoding="utf-8")
    assert "대화 지식화는 한 번 분류하고 두 번 사용한다" in rendered
    assert "대화 후보의 승격 기준을 어떻게 좁힐까" in rendered
    assert (
        "[[../../wiki/personal/대화-지식화는-한-번-분류하고-두-번-사용한다|"
        "대화 지식화는 한 번 분류하고 두 번 사용한다]]"
    ) in rendered
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18").glob("*.json")
        if path.name != "_input-status.json"
    ]
    record = next(item for item in records if item["kind"] == "학습")
    assert record["calendar_contexts"][0]["include_wiki_subject"] is True


def test_projects_both_reusable_criterion_and_one_off_event_into_single_wiki(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "생활",
                "title": "구매는 실제 착용 비교 뒤에 결정한다",
                "summary": "반복 구매에서는 직접 착용한 기준을 우선해 다음 선택에도 재사용한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 생활 기준이 없다.",
            },
            {
                "day": "2026-08-18",
                "kind": "생활",
                "title": "오늘 매장에 들렀다",
                "summary": "오늘의 이동과 방문 사실만 하루 이력에 남긴다.",
                "wiki_update": False,
            },
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-life-criterion",
        entries=entries,
    )

    assert result.wiki_page_count == 1
    assert (tmp_path / "wiki/personal/구매는-실제-착용-비교-뒤에-결정한다.md").is_file()
    assert not (tmp_path / "wiki/personal/오늘-매장에-들렀다.md").exists()


def test_rejects_raw_like_or_conflicting_wiki_entries_without_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "wiki/personal").mkdir(parents=True)
    page = tmp_path / "wiki/personal/같은-제목.md"
    page.write_text("사용자가 고친 문서\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "학습",
                "title": "같은 제목",
                "summary": "새 요약은 자동 덮어쓰기가 아니라 검토 대상이어야 한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 정본 제목이 없다.",
            }
        ]
    )

    with pytest.raises(WoonError, match="cannot replace an existing subject"):
        record_codex_knowledge_entries(
            tmp_path,
            source_range="codex-scope-20260818-002",
            entries=entries,
        )
    with pytest.raises(WoonError, match="safe visible text"):
        entries_from_records(
            [
                {
                    "day": "2026-08-18",
                    "kind": "학습",
                    "title": "비밀값",
                    "summary": "token=sk-this-must-not-be-stored",
                }
            ]
        )
    assert page.read_text(encoding="utf-8") == "사용자가 고친 문서\n"
    assert not (settings.receipt_directory / "codex-conversation-ingest").exists()


def test_rejects_retired_parallel_wiki_routing_fields() -> None:
    with pytest.raises(WoonError, match="unsupported fields"):
        entries_from_records(
            [
                {
                    "day": "2026-08-24",
                    "kind": "학습",
                    "title": "옛 분기 필드",
                    "summary": "활성 입력은 단일 Wiki 계약만 사용한다.",
                    "growth_candidate": True,
                }
            ]
        )
    with pytest.raises(WoonError, match="unsupported fields"):
        entries_from_records(
            [
                {
                    "day": "2026-08-24",
                    "kind": "학습",
                    "title": "옛 일정 연결 필드",
                    "summary": "활성 입력은 단일 Wiki 계약만 사용한다.",
                    "calendar_contexts": [
                        {
                            "event_day": "2026-08-24",
                            "event_title": "학습",
                            "related_documents": ["wiki/README.md"],
                            "reason": "참고",
                            "include_generated_growth_page": False,
                        }
                    ],
                }
            ]
        )


def test_requires_an_explicit_wiki_identity_before_any_write(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    entries = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "학습",
                "title": "기존 정본을 먼저 찾는다",
                "summary": "정본 경로나 새 문서 생성 근거가 없으면 Wiki를 만들지 않는다.",
                "wiki_update": True,
            }
        ]
    )

    with pytest.raises(WoonError, match="requires exactly one"):
        record_codex_knowledge_entries(
            tmp_path,
            source_range="codex-scope-20260824-missing-identity",
            entries=entries,
        )

    assert not (tmp_path / "wiki").exists()
    assert not (settings.receipt_directory / "codex-conversation-ingest").exists()


def test_explicit_existing_subject_prevents_a_sentence_shaped_duplicate(tmp_path: Path) -> None:
    _settings(tmp_path)
    _write_wiki(tmp_path, "wiki/personal/aice.md", "AICE Associate 준비")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "학습",
                "title": "실기 시험을 데이터 처리 흐름으로 나눈다",
                "summary": "탐색, 전처리, 학습, 평가 순서로 시험 준비를 진행한다.",
                "wiki_update": True,
                "wiki_subject_path": "wiki/personal/aice.md",
            }
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260824-existing-aice",
        entries=entries,
    )

    assert result.wiki_page_count == 1
    assert not (tmp_path / "wiki/personal/실기-시험을-데이터-처리-흐름으로-나눈다.md").exists()
    assert "탐색, 전처리, 학습, 평가" in (tmp_path / "wiki/personal/aice.md").read_text(
        encoding="utf-8"
    )
    ledger = load_daily_entries(tmp_path, day=date(2026, 8, 24))
    assert ledger[0]["wiki_subject_path"] == "wiki/personal/aice.md"


def test_one_batch_organizes_reviews_and_excludes_without_cross_blocking(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    entries = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "학습",
                "title": "분류 결과는 항목별로 끝낸다",
                "summary": "명확한 지식은 같은 실행에서 Wiki와 시간 이력에 반영한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 분류 원칙이 없다.",
            },
            {
                "day": "2026-08-24",
                "kind": "인물",
                "title": "같은 사람인지 확인이 필요하다",
                "summary": "동명이인 가능성이 있어 인물 문서와 자동 연결하지 않는다.",
                "disposition": "review",
                "review_reason": "기존 인물과 동일인인지 근거가 부족하다.",
            },
            {
                "disposition": "excluded",
                "raw_body": "token=sk-this-body-must-never-cross-the-boundary",
                "opaque_id": "advertisement-message-id",
            },
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260824-item-outcomes",
        entries=entries,
    )

    assert result.entry_count == 2
    assert result.wiki_page_count == 1
    assert (tmp_path / "wiki/personal/분류-결과는-항목별로-끝낸다.md").is_file()
    assert not (tmp_path / "wiki/personal/같은-사람인지-확인이-필요하다.md").exists()
    reviews = list((tmp_path / "brain/review/codex").glob("*.md"))
    assert len(reviews) == 1
    assert "확인 필요: 같은 사람인지 확인이 필요하다" in reviews[0].read_text(encoding="utf-8")
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert "sk-this-body" not in persisted
    assert "advertisement-message-id" not in persisted


def test_updates_a_managed_wiki_page_without_erasing_manual_sections(tmp_path: Path) -> None:
    _settings(tmp_path)
    (tmp_path / "wiki/personal").mkdir(parents=True)
    page = tmp_path / "wiki/personal/반복-학습-원칙.md"
    page.write_text(
        """---
type: Wiki
title: "반복 학습 원칙"
record_owner: choi-woonyoung
publish: false
access: local-only
status: Active
facets: ["개념", "학습"]
knowledge_state: "생각 중"
summary: "처음 이해"
---

# 반복 학습 원칙

## 현재 이해

처음 이해

## 내가 남긴 메모

이 문단은 자동화가 바꾸지 않는다.
""",
        encoding="utf-8",
    )
    entries = entries_from_records(
        [
            {
                "day": "2026-08-19",
                "kind": "학습",
                "title": "반복 학습 원칙",
                "summary": "새 질문을 기존 이해에 연결해 현재 설명과 변화 이력을 함께 갱신한다.",
                "wiki_update": True,
                "wiki_subject_path": "wiki/personal/반복-학습-원칙.md",
            }
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260819-growth-update",
        entries=entries,
    )

    text = page.read_text(encoding="utf-8")
    assert result.wiki_page_count == 1
    assert 'summary: "새 질문을 기존 이해에 연결해 현재 설명과 변화 이력을 함께 갱신한다."' in text
    assert "이 문단은 자동화가 바꾸지 않는다." in text
    assert "2026-08-19 · 실행 — 새 질문을 기존 이해에 연결해" in text


def test_allows_wiki_links_to_project_and_content_facets(tmp_path: Path) -> None:
    _settings(tmp_path)
    _write_wiki(tmp_path, "wiki/personal/aice.md", "AICE 프로젝트")
    _write_wiki(tmp_path, "wiki/personal/aice-자료.md", "AICE 자료")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-19",
                "kind": "학습",
                "title": "평가 지표는 문제 유형과 함께 고른다",
                "summary": "분류와 회귀는 목표값과 평가 지표를 함께 구분해야 한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 평가 지표 주제가 없다.",
                "related_documents": [
                    "wiki/personal/aice.md",
                    "wiki/personal/aice-자료.md",
                ],
            }
        ]
    )

    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260819-project-content-links",
        entries=entries,
    )

    text = (tmp_path / "wiki/personal/평가-지표는-문제-유형과-함께-고른다.md").read_text(
        encoding="utf-8"
    )
    assert "[[wiki/personal/aice]]" in text
    assert "[[wiki/personal/aice-자료]]" in text


def test_materializes_explicit_content_and_project_entities_once(tmp_path: Path) -> None:
    _settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-19.md").write_text("# 2026-08-19\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-19",
                "kind": "프로젝트",
                "title": "AICE 자격 준비를 시작한다",
                "summary": "제공된 학습 자료를 한 번만 식별하고 자격 취득 프로젝트에 연결한다.",
                "wiki_update": False,
                "contents": [
                    {
                        "title": "Pandas 공식 가이드",
                        "content_kind": "article",
                        "creators": [],
                    }
                ],
                "projects": [
                    {
                        "title": "AICE Associate 준비",
                        "objective": "회귀와 분류 문제를 스스로 풀고 자격을 취득한다.",
                        "status": "Active",
                        "materials": ["회귀 샘플", "분류 샘플", "실습 코드"],
                    }
                ],
            }
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260819-content-project",
        entries=entries,
    )
    digest = record_daily_digest_from_codex_ledger(tmp_path, day=date(2026, 8, 19))

    assert result.entry_count == 1
    content = tmp_path / "wiki/personal/pandas-공식-가이드.md"
    project = tmp_path / "wiki/personal/aice-associate-준비.md"
    assert content.is_file()
    assert project.is_file()
    assert 'facets: ["콘텐츠", "학습"]' in content.read_text(encoding="utf-8")
    assert 'facets: ["프로젝트"]' in project.read_text(encoding="utf-8")
    record = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-19").glob("*.json")
        if path.name != "_input-status.json"
    )
    assert record["related_documents"] == [
        "wiki/personal/pandas-공식-가이드.md",
        "wiki/personal/aice-associate-준비.md",
    ]
    project_text = project.read_text(encoding="utf-8")
    assert "project_id: aice-associate-준비" in project_text
    assert 'objective: "회귀와 분류 문제를 스스로 풀고 자격을 취득한다."' in project_text
    assert 'materials: ["회귀 샘플", "분류 샘플", "실습 코드"]' in project_text
    assert (
        tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-19"
    ).stat().st_mode & 0o777 == 0o700
    daily = (tmp_path / digest.relative_path).read_text(encoding="utf-8")
    assert "## 커리어·창작·자료" in daily
    assert "[[../../wiki/personal/pandas-공식-가이드|Pandas 공식 가이드]]" in daily
    assert "[[../../wiki/personal/aice-associate-준비|AICE Associate 준비]]" in daily


def test_rejects_project_exclusive_material_bundle_as_a_second_visible_card(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    entries = entries_from_records(
        [
            {
                "day": "2026-08-19",
                "kind": "프로젝트",
                "title": "AICE 자격 준비를 시작한다",
                "summary": "프로젝트 전용 자료는 프로젝트 허브 아래에서 관리한다.",
                "wiki_update": False,
                "contents": [
                    {
                        "title": "AICE Associate 학습 자료 묶음",
                        "content_kind": "learning-material-bundle",
                    }
                ],
                "projects": [
                    {
                        "title": "AICE Associate 준비",
                        "objective": "AICE Associate 자격을 취득한다.",
                        "materials": ["회귀 샘플", "분류 샘플", "실습 코드"],
                    }
                ],
            }
        ]
    )

    with pytest.raises(
        WoonError,
        match="Project-exclusive learning materials must be listed under the project hub",
    ):
        record_codex_knowledge_entries(
            tmp_path,
            source_range="codex-scope-20260819-project-exclusive-materials",
            entries=entries,
        )

    assert not (tmp_path / "wiki/personal/aice-associate-학습-자료-묶음.md").exists()


def test_interview_exchange_updates_one_question_under_one_project(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    project = tmp_path / "wiki/personal/interview/ai-engineer.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        "---\ntype: Wiki\ntitle: AI Engineer 면접 준비\n"
        'facets: ["프로젝트", "커리어"]\nknowledge_state: "확인 필요"\n---\n',
        encoding="utf-8",
    )
    entries = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "커리어",
                "title": "Kyro에서 본인이 직접 한 일은 무엇입니까?",
                "summary": "개인 기여와 팀 결과를 구분해 설명한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki에 같은 질문 정본이 없다.",
                "interview_answer": {
                    "question": "Kyro에서 본인이 직접 한 일은 무엇입니까?",
                    "answer": "관리 서버의 장애 판정 흐름을 설계하고 구현했다.",
                    "project_path": "wiki/personal/interview/ai-engineer.md",
                    "evidence": ["관리 서버 코드와 계약 테스트"],
                    "limitations": ["실환경 정확도 평가는 수행하지 않았다."],
                    "change_reason": "개인 기여와 검증 한계를 분리했다.",
                    "promote_current": True,
                },
            }
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260824-interview-answer",
        entries=entries,
    )

    assert result.wiki_page_count == 1
    question = tmp_path / "wiki/personal/kyro에서-본인이-직접-한-일은-무엇입니까.md"
    text = question.read_text(encoding="utf-8")
    assert "## 현재 최선 답변" in text
    assert "관리 서버의 장애 판정 흐름을 설계하고 구현했다." in text
    assert (
        'parent_topics: ["[[wiki/personal/interview/ai-engineer|AI Engineer 면접 준비]]"]' in text
    )
    ledger = load_daily_entries(tmp_path, day=date(2026, 8, 24))
    assert ledger[0]["interview_answer"]["promote_current"] is True
    assert "wiki/personal/interview/ai-engineer.md" in ledger[0]["related_documents"]


def test_wiki_upsert_does_not_depend_on_a_parallel_growth_index(tmp_path: Path) -> None:
    _settings(tmp_path)
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "학습",
                "title": "루트 연결이 필요한 성장 노트",
                "summary": "새 성장 노트는 루트 인덱스를 거치지 않으면 그래프에 고립될 수 있다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 연결 원칙이 없다.",
            }
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-index-marker",
        entries=entries,
    )

    assert result.wiki_page_count == 1
    assert (tmp_path / "wiki/personal/루트-연결이-필요한-성장-노트.md").is_file()


def test_records_an_empty_conversation_range_without_creating_knowledge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-empty",
        entries=entries_from_records([]),
    )

    assert result.entry_count == 0
    assert result.wiki_page_count == 0
    assert (settings.receipt_directory / "codex-conversation-ingest").is_dir()
    status = json.loads(
        (
            tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18/_input-status.json"
        ).read_text(encoding="utf-8")
    )
    assert status == {"input_state": "processed"}


def test_allows_a_pending_day_to_become_processed_when_the_session_flushes(tmp_path: Path) -> None:
    _settings(tmp_path)
    pending = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-pending",
        day=date(2026, 8, 18),
        entries=entries_from_records([]),
        input_state="pending",
    )
    completed = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-completed",
        day=date(2026, 8, 18),
        entries=entries_from_records(
            [
                {
                    "day": "2026-08-18",
                    "kind": "활동",
                    "title": "저장된 대화를 처리했다",
                    "summary": "열려 있던 대화가 저장된 뒤 하루 기록으로 처리했다.",
                }
            ]
        ),
        input_state="processed",
    )

    assert pending.input_state == "pending"
    assert completed.input_state == "processed"
    status = json.loads(
        (
            tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18/_input-status.json"
        ).read_text(encoding="utf-8")
    )
    assert status == {"input_state": "processed"}


def test_partial_day_records_completed_items_then_accepts_incremental_batch(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    first = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "학습",
                "title": "완료된 대화부터 누적한다",
                "summary": "활성 대화가 있어도 완료된 질문과 답변은 먼저 정리한다.",
                "wiki_update": True,
                "new_wiki_reason": "기존 Wiki를 검색했지만 같은 누적 원칙이 없다.",
            }
        ]
    )
    second = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "활동",
                "title": "추가 대화를 이어서 정리했다",
                "summary": "앞선 항목을 다시 만들지 않고 새로 완료된 대화만 더했다.",
            }
        ]
    )

    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-kst-2026-08-24-through-100",
        day=date(2026, 8, 24),
        entries=first,
        input_state="partial",
    )
    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-kst-2026-08-24-through-200",
        day=date(2026, 8, 24),
        entries=second,
        input_state="partial",
    )

    assert {record["title"] for record in load_daily_entries(tmp_path, day=date(2026, 8, 24))} == {
        "완료된 대화부터 누적한다",
        "추가 대화를 이어서 정리했다",
    }
    assert load_daily_input_status(tmp_path, day=date(2026, 8, 24)) == "partial"


def test_rejects_a_backward_completed_turn_boundary_without_writes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    newer = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "활동",
                "title": "새 경계까지 정리했다",
                "summary": "완료된 turn 경계를 단조 증가하도록 기록했다.",
            }
        ]
    )
    older = entries_from_records(
        [
            {
                "day": "2026-08-24",
                "kind": "활동",
                "title": "오래된 경계를 다시 제출했다",
                "summary": "이미 처리한 범위보다 과거인 요청은 거부해야 한다.",
            }
        ]
    )
    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-kst-2026-08-24-through-200-v5",
        entries=newer,
        input_state="partial",
    )
    checkpoint_before = settings.checkpoint_path.read_bytes()
    receipts_before = tuple(sorted(settings.receipt_directory.rglob("*.json")))

    with pytest.raises(WoonError, match="must not move backward"):
        record_codex_knowledge_entries(
            tmp_path,
            source_range="codex-kst-2026-08-24-through-100-v5",
            entries=older,
            input_state="partial",
        )

    assert settings.checkpoint_path.read_bytes() == checkpoint_before
    assert tuple(sorted(settings.receipt_directory.rglob("*.json"))) == receipts_before
    assert {record["title"] for record in load_daily_entries(tmp_path, day=date(2026, 8, 24))} == {
        "새 경계까지 정리했다"
    }


def test_can_repair_only_a_previously_empty_daily_digest(tmp_path: Path) -> None:
    _settings(tmp_path)
    _write_wiki(tmp_path, "wiki/personal/herdr.md", "Herdr")
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 일일 기록\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-17",
                "kind": "결정",
                "title": "Wiki 승격 경로를 만든다",
                "summary": "후보에만 머물지 않고 안전한 학습과 결정을 Wiki에 반영하기로 했다.",
                "related_documents": ["wiki/personal/herdr.md"],
            }
        ]
    )
    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260817-001",
        entries=entries,
    )

    result = record_daily_digest_from_codex_ledger(
        tmp_path,
        day=date(2026, 8, 17),
    )

    assert result.entry_count == 1
    assert "Wiki 승격 경로를 만든다" in (tmp_path / "inbox/daily/2026-08-17.md").read_text(
        encoding="utf-8"
    )


def test_projects_daily_activity_and_explicit_person_facts_without_identity_link(
    tmp_path: Path,
) -> None:
    _settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-19.md").write_text("# 일일 기록\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-19",
                "kind": "활동",
                "title": "면접 준비를 도왔다",
                "summary": "사용자가 확인한 일상 활동으로, 면접 준비를 함께 도운 사실만 남긴다.",
                "people": [
                    {
                        "display_name": "민정",
                        "explicit_facts": ["면접 준비를 함께 도왔다."],
                        "next_action": "후속 일정이 생기면 확인한다.",
                    }
                ],
            },
            {
                "day": "2026-08-19",
                "kind": "일정",
                "title": "면접 일정 확인 필요",
                "summary": "시각과 목적이 모두 확인되면 별도 일정 반영 경로에서 처리한다.",
            },
        ]
    )

    record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260819-001",
        entries=entries,
    )
    digest = record_daily_digest_from_codex_ledger(tmp_path, day=date(2026, 8, 19))

    rendered = (tmp_path / digest.relative_path).read_text(encoding="utf-8")
    candidates = list((tmp_path / "brain/review/codex").glob("*.md"))
    assert "하루의 활동" in rendered
    assert "일정·할 일" in rendered
    assert "관련 인물: 민정" in rendered
    person_candidate = next(
        candidate
        for candidate in candidates
        if "같은 이름의 기존 인물과 자동으로 연결하지 않는다."
        in candidate.read_text(encoding="utf-8")
    )
    schedule_candidate = next(
        candidate
        for candidate in candidates
        if "일정 검토" in candidate.read_text(encoding="utf-8")
    )
    assert "people:" not in person_candidate.read_text(encoding="utf-8")
    assert "외부 일정, 인물 카드" in schedule_candidate.read_text(encoding="utf-8")


def _settings(vault: Path):
    write_policy(vault)
    policy = vault / "config/second-brain-orchestrator.yaml"
    original = policy.read_text(encoding="utf-8")
    governance = """mode: proposal-only
      status: enabled
      task_thread_id: fixture-governance-thread
      codex_automation_id: fixture-governance-automation
      rrule: FREQ=DAILY;BYHOUR=8
      notification_policy: failed_runs_only
      prompt_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"""
    codex_lane = """
  - id: codex-conversation-ingest
    owner: codex-history-task
    cadence: four-hourly
    inputs: [codex-response-items]
    output:
      [wiki-upsert, runtime-history-receipt, calendar-document-context,
       schedule-action-review-candidate, person-memory-review-candidate,
       career-evidence-review-candidate, creative-link-review-candidate,
       source-intake-review-candidate]
    checkpoint_key: codex-conversation-ingest
    required_signals: [message-range, privacy-classification]
    prohibited:
      [system-prompt-ingest, tool-output-ingest, reasoning-ingest,
       person-profile-inference, unresolved-identity-link]
    execution:
      mode: materialize
      status: enabled
      task_thread_id: fixture-codex-thread
      codex_automation_id: fixture-codex-automation
      rrule: FREQ=HOURLY;INTERVAL=4
      notification_policy: failed_runs_only
      prompt_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      owned_paths: [wiki, brain/review/codex, .local/woon-knowledge/codex-knowledge]
  - id: daily-record-materialization
    owner: daily-record-task
    cadence: daily
    inputs: [codex-knowledge-ledger]
    output: [daily-codex-digest]
    checkpoint_key: daily-record-materialization
    required_signals: [kst-day, privacy-classification]
    prohibited:
      [raw-transcript-ingest, system-prompt-ingest, tool-output-ingest, reasoning-ingest,
       person-profile-inference, unresolved-identity-link]
    execution:
      mode: materialize
      status: enabled
      task_thread_id: fixture-daily-thread
      codex_automation_id: fixture-daily-automation
      rrule: FREQ=DAILY;BYHOUR=0;BYMINUTE=5
      notification_policy: failed_runs_only
      prompt_sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
      owned_paths: [inbox/daily, inbox/calendar, brain/review/activity]
"""
    policy.write_text(
        original.replace(
            """mode: proposal-only
      status: planned
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null""",
            governance,
        ).replace("cursor_contract:\n", codex_lane + "cursor_contract:\n"),
        encoding="utf-8",
    )
    settings = load_orchestrator_settings(vault)
    record_governance_preflight(settings, input_sha256="a" * 64, output_sha256="b" * 64)
    return settings


def _write_wiki(vault: Path, relative: str, title: str) -> None:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: Wiki\n"
        f'title: "{title}"\n'
        "record_owner: choi-woonyoung\n"
        "publish: false\n"
        "access: local-only\n"
        "status: Active\n"
        'facets: ["개념"]\n'
        'knowledge_state: "생각 중"\n'
        "---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )
