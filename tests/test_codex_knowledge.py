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
    record_codex_knowledge_entries,
)
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import record_governance_preflight


def test_projects_one_safe_batch_to_growth_wiki_and_daily_ledger(tmp_path: Path) -> None:
    _settings(tmp_path)
    (tmp_path / "brain/wiki").mkdir(parents=True)
    (tmp_path / "brain/wiki/herdr.md").write_text("# Herdr\n", encoding="utf-8")
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-18.md").write_text("# 일일 기록\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "학습",
                "title": "대화 지식화는 한 번 분류하고 두 번 사용한다",
                "summary": (
                    "대화 요약은 성장 Wiki와 하루 정리가 각각 다시 해석하지 않고 "
                    "하나의 최소 항목을 함께 사용해야 한다."
                ),
                "next_question": "결정 후보와 인물 후보는 어떤 기준으로 검토 경로에만 둘까?",
                "related_documents": ["brain/wiki/herdr.md"],
                "calendar_contexts": [
                    {
                        "event_day": "2026-08-18",
                        "event_title": "학습 모임",
                        "related_documents": ["brain/wiki/herdr.md"],
                        "reason": "준비",
                        "include_generated_growth_page": True,
                    }
                ],
            },
            {
                "day": "2026-08-18",
                "kind": "질문",
                "title": "대화 후보의 승격 기준을 어떻게 좁힐까",
                "summary": "반복해서 재사용할 수 없는 개인 대화는 성장 Wiki에 넣지 않는다.",
            },
        ]
    )

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-001",
        entries=entries,
    )
    digest = record_daily_digest_from_codex_ledger(tmp_path, day=date(2026, 8, 18))

    growth = tmp_path / "brain/wiki/대화-지식화는-한-번-분류하고-두-번-사용한다.md"
    assert result.entry_count == 2
    assert result.growth_page_count == 1
    assert growth.is_file()
    assert "원문" not in growth.read_text(encoding="utf-8")
    assert "다음 질문" in growth.read_text(encoding="utf-8")
    assert (tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18").is_dir()
    rendered = (tmp_path / digest.relative_path).read_text(encoding="utf-8")
    assert "대화 지식화는 한 번 분류하고 두 번 사용한다" in rendered
    assert "대화 후보의 승격 기준을 어떻게 좁힐까" in rendered
    assert (
        "[[../../brain/wiki/대화-지식화는-한-번-분류하고-두-번-사용한다|"
        "대화 지식화는 한 번 분류하고 두 번 사용한다]]"
    ) in rendered
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-18").glob("*.json")
        if path.name != "_input-status.json"
    ]
    record = next(item for item in records if item["kind"] == "학습")
    assert record["calendar_contexts"][0]["include_generated_growth_page"] is True


def test_rejects_raw_like_or_conflicting_growth_entries_without_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (tmp_path / "brain/wiki").mkdir(parents=True)
    page = tmp_path / "brain/wiki/같은-제목.md"
    page.write_text("사용자가 고친 문서\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-18",
                "kind": "학습",
                "title": "같은 제목",
                "summary": "새 요약은 자동 덮어쓰기가 아니라 검토 대상이어야 한다.",
            }
        ]
    )

    with pytest.raises(WoonError, match="candidate producer failed"):
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


def test_records_an_empty_conversation_range_without_creating_knowledge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = record_codex_knowledge_entries(
        tmp_path,
        source_range="codex-scope-20260818-empty",
        entries=entries_from_records([]),
    )

    assert result.entry_count == 0
    assert result.growth_page_count == 0
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


def test_can_repair_only_a_previously_empty_daily_digest(tmp_path: Path) -> None:
    _settings(tmp_path)
    (tmp_path / "brain/wiki").mkdir(parents=True)
    (tmp_path / "brain/wiki/herdr.md").write_text("# Herdr\n", encoding="utf-8")
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 일일 기록\n", encoding="utf-8")
    entries = entries_from_records(
        [
            {
                "day": "2026-08-17",
                "kind": "결정",
                "title": "성장 Wiki 승격 경로를 만든다",
                "summary": "후보에만 머물지 않고 안전한 학습과 결정을 성장 Wiki에 반영하기로 했다.",
                "related_documents": ["brain/wiki/herdr.md"],
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
    assert "성장 Wiki 승격 경로를 만든다" in (
        tmp_path / "inbox/daily/2026-08-17.md"
    ).read_text(encoding="utf-8")


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
      [growth-wiki, daily-history-ledger, calendar-document-context,
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
      owned_paths: [brain/wiki, brain/review/codex, .local/woon-knowledge/codex-knowledge]
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
