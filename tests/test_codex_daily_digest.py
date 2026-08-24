from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_orchestration import write_policy

from woon_core.errors import WoonError
from woon_core.knowledge.codex_daily_digest import (
    CodexDailyDigestEntry,
    entries_from_records,
    migrate_legacy_daily_digests,
    record_codex_daily_digest,
)
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import record_governance_preflight


def test_records_transcript_free_daily_digest_once(tmp_path: Path) -> None:
    settings = _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")
    (tmp_path / "wiki/personal").mkdir(parents=True)
    (tmp_path / "wiki/personal/herdr.md").write_text("# Herdr\n", encoding="utf-8")
    entries = (
        CodexDailyDigestEntry(
            kind="결정",
            title="일일 대화 요약 자동화 추가",
            summary="원문 대신 결정과 다음 행동만 하루 노트에 남기도록 정했다.",
            related_documents=("wiki/personal/herdr.md",),
        ),
    )

    first = record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=entries)
    second = record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=entries)

    path = tmp_path / first.relative_path
    receipt = (
        settings.receipt_directory / "daily-record-materialization" / f"{first.receipt_id}.json"
    )
    assert first.entry_count == 1
    assert second.replayed is True
    assert path.is_file()
    assert "원문 대신 결정과 다음 행동만" in path.read_text(encoding="utf-8")
    assert first.relative_path == "inbox/daily/2026-08-17.md"
    assert "## 대화에서 남긴 것" in path.read_text(encoding="utf-8")
    assert "## 성장·학습" in path.read_text(encoding="utf-8")
    assert "[[../../wiki/personal/herdr|Herdr]]" in path.read_text(encoding="utf-8")
    assert receipt.is_file()
    assert json.loads(receipt.read_text(encoding="utf-8"))["candidate_ids"]


def test_rejects_sensitive_or_unrelated_digest_input_without_writing(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")

    with pytest.raises(WoonError, match="safe visible text"):
        record_codex_daily_digest(
            tmp_path,
            day=date(2026, 8, 17),
            entries=(
                CodexDailyDigestEntry(
                    kind="학습",
                    title="비밀값 점검",
                    summary="token=sk-this-must-never-be-persisted",
                ),
            ),
        )
    with pytest.raises(WoonError, match="unsupported fields"):
        entries_from_records(
            [{"kind": "결정", "title": "제목", "summary": "요약", "raw_transcript": "금지"}]
        )
    assert not (tmp_path / "inbox/daily-digests").exists()


def test_creates_a_missing_daily_note_from_the_canonical_template(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    template = tmp_path / "templates" / "daily-note.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# {{date}}\n\n<!-- woon-codex-digest:start -->\n<!-- woon-codex-digest:end -->\n",
        encoding="utf-8",
    )

    record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=())

    assert (
        (tmp_path / "inbox/daily/2026-08-17.md")
        .read_text(encoding="utf-8")
        .startswith("# 2026-08-17")
    )


@pytest.mark.parametrize(
    ("input_state", "expected_title"),
    [
        ("partial", "현재까지 정리됨"),
        ("pending", "다음 실행 대기"),
        ("unavailable", "세션 원본을 찾지 못해 대기"),
        ("no-meaningful", "남길 항목 없음"),
    ],
)
def test_empty_daily_digest_explains_its_honest_input_state(
    tmp_path: Path, input_state: str, expected_title: str
) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 17),
        entries=(),
        input_state=input_state,
    )

    rendered = (tmp_path / "inbox/daily/2026-08-17.md").read_text(encoding="utf-8")
    assert "## 대화 정리 상태" in rendered
    assert expected_title in rendered
    assert "## 대화에서 남긴 것" not in rendered


def test_nonempty_daily_digest_marks_completion_before_rendering_entries(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 17),
        entries=(
            CodexDailyDigestEntry(
                kind="결정",
                title="정리 상태를 구분한다",
                summary="빈 노트와 완료된 기록을 같은 문구로 보이지 않게 한다.",
            ),
        ),
    )

    rendered = (tmp_path / "inbox/daily/2026-08-17.md").read_text(encoding="utf-8")
    assert "**정리 완료**" in rendered


def test_partial_daily_digest_renders_items_without_claiming_day_complete(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-24.md").write_text("# 2026-08-24\n", encoding="utf-8")

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(
            CodexDailyDigestEntry(
                kind="학습",
                title="완료된 대화부터 누적한다",
                summary="진행 중인 날에도 완료된 질문과 답변은 먼저 정리한다.",
            ),
        ),
        input_state="partial",
    )

    rendered = (tmp_path / "inbox/daily/2026-08-24.md").read_text(encoding="utf-8")
    assert "**현재까지 정리됨**" in rendered
    assert "완료된 대화부터 누적한다" in rendered
    assert "**정리 완료**" not in rendered
    assert "## 대화에서 남긴 것" in rendered


def test_daily_digest_coalesces_incremental_updates_for_the_same_subject(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-24.md").write_text("# 2026-08-24\n", encoding="utf-8")

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(
            CodexDailyDigestEntry(
                kind="개념",
                title="자동화는 실제 산출물로 검증한다",
                summary="실제 Wiki와 일일 기록이 생성돼야 한다.",
                intent="산출물 존재를 확인한다.",
            ),
            CodexDailyDigestEntry(
                kind="학습",
                title="자동화는 실제 산출물로 검증한다",
                summary="같은 입력의 재실행에서 문서가 변하지 않아야 한다.",
                intent="재실행 안전성을 확인한다.",
            ),
            CodexDailyDigestEntry(
                kind="학습",
                title="자동화는 실제 산출물로 검증한다",
                summary="같은 입력의 재실행에서 문서가 변하지 않아야 한다.",
                intent="서로 다른 대화 근거도 내부 식별자는 충돌하지 않게 한다.",
            ),
        ),
        input_state="partial",
    )

    rendered = (tmp_path / "inbox/daily/2026-08-24.md").read_text(encoding="utf-8")
    assert rendered.count("자동화는 실제 산출물로 검증한다") == 1
    assert "개념·학습" in rendered
    assert "실제 Wiki와 일일 기록" in rendered
    assert "재실행에서 문서가 변하지 않아야" in rendered
    assert "내부 식별자는 충돌하지 않게" in rendered


def test_migrates_only_generated_legacy_digests_into_the_daily_record(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text(
        "# 2026-08-17\n\n## 포착\n\n- 사용자 메모\n", encoding="utf-8"
    )
    legacy = tmp_path / "inbox/daily-digests/2026-08-17.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        '---\ntype: DailyDigest\ntitle: "2026-08-17 Codex 하루 정리"\n'
        "publish: false\naccess: local-only\nstatus: Active\ndate: 2026-08-17\n"
        "record_owner: choi-woonyoung\n---\n\n# 2026-08-17 Codex 하루 정리\n\n"
        "## 대화에서 남긴 것\n\n- 이전 자동 요약\n",
        encoding="utf-8",
    )

    result = migrate_legacy_daily_digests(tmp_path)

    assert result.migrated_days == ("2026-08-17",)
    content = (tmp_path / "inbox/daily/2026-08-17.md").read_text(encoding="utf-8")
    assert "이전 자동 요약" in content
    assert "<!-- woon-codex-digest:start -->" in content
    assert "사용자 메모" in content
    assert not legacy.exists()


def _digest_settings(vault: Path):
    write_policy(vault)
    policy = vault / "config/second-brain-orchestrator.yaml"
    original = policy.read_text(encoding="utf-8")
    digest_lane = """
  - id: daily-record-materialization
    owner: daily-record-task
    cadence: daily
    inputs: [codex-opted-in-summaries]
    output: [daily-codex-digest]
    checkpoint_key: daily-record-materialization
    required_signals: [kst-day, privacy-classification]
    prohibited: [raw-transcript-ingest, system-prompt-ingest, tool-output-ingest, reasoning-ingest,
      person-profile-inference, unresolved-identity-link]
    execution:
      mode: materialize
      status: enabled
      task_thread_id: fixture-daily-thread
      codex_automation_id: fixture-daily-automation
      rrule: FREQ=DAILY;BYHOUR=23;BYMINUTE=55;BYSECOND=0
      notification_policy: failed_runs_only
      prompt_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      owned_paths: [inbox/daily, inbox/calendar, brain/review/activity]
"""
    runnable = original.replace(
        """mode: proposal-only
      status: planned
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null""",
        """mode: proposal-only
      status: enabled
      task_thread_id: fixture-governance-thread
      codex_automation_id: fixture-governance-automation
      rrule: FREQ=DAILY;BYHOUR=8
      notification_policy: failed_runs_only
      prompt_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb""",
    )
    policy.write_text(
        runnable.replace("cursor_contract:\n", digest_lane + "cursor_contract:\n"),
        encoding="utf-8",
    )
    settings = load_orchestrator_settings(vault)
    record_governance_preflight(settings, input_sha256="a" * 64, output_sha256="b" * 64)
    return settings
