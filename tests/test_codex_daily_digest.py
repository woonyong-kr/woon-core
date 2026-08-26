from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_orchestration import write_policy

from woon_core.errors import WoonError
from woon_core.knowledge.codex_daily_digest import (
    CodexDailyDigestEntry,
    CodexDailyExchange,
    entries_from_records,
    migrate_legacy_daily_digests,
    record_codex_daily_digest,
)
from woon_core.knowledge.codex_source_archive import (
    CodexSourceAttachment,
    CodexSourceBundle,
    CodexSourceMessage,
    record_codex_source_bundle,
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
    assert "## 오늘 한눈에" in path.read_text(encoding="utf-8")
    assert "## 오늘 기록" in path.read_text(encoding="utf-8")
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
    assert "> [!info]" not in rendered
    assert f"**{expected_title}** —" in rendered
    assert expected_title in rendered
    assert "## 오늘 기록" not in rendered


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
    assert "**정리 완료** —" in rendered


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
    assert "**현재까지 정리됨** —" in rendered
    assert "완료된 대화부터 누적한다" in rendered
    assert "**정리 완료** —" not in rendered
    assert "## 오늘 기록" in rendered


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
    assert rendered.count("### 자동화는 실제 산출물로 검증한다") == 1
    assert "`개념`" in rendered
    assert "`학습`" in rendered
    assert "실제 Wiki와 일일 기록" in rendered
    assert "재실행에서 문서가 변하지 않아야" in rendered
    assert "내부 식별자는 충돌하지 않게" in rendered


def test_daily_digest_renders_readable_question_answer_outcome_and_attachment(
    tmp_path: Path,
) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-24.md").write_text("# 2026-08-24\n", encoding="utf-8")

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(
            CodexDailyDigestEntry(
                kind="학습",
                title="AICE 학습 환경을 준비했다",
                summary="실습 자료와 코드를 한 프로젝트에서 실행할 수 있게 정리했다.",
                intent="시험 준비를 자료 보관에서 끝내지 않고 실습으로 이어가기 위한 작업이었다.",
                exchanges=(
                    CodexDailyExchange(
                        question="샘플 문항과 실습 코드를 한번에 학습할 수 있게 묶을 수 있나?",
                        answer=(
                            "원본은 변경하지 않고, 실행용 notebook과 검증 스크립트를 "
                            "별도로 두는 구조로 정리했다."
                        ),
                        understanding=(
                            "자료 보관이 아니라 재실행 가능한 학습 흐름이 필요하다고 판단했다."
                        ),
                        outcome="회귀·분류 자료와 실습 코드가 학습 순서에 연결됐다.",
                        attachments=("회귀 샘플 문항 PDF", "분류 샘플 해설 PDF"),
                    ),
                ),
            ),
        ),
    )

    rendered = (tmp_path / "inbox/daily/2026-08-24.md").read_text(encoding="utf-8")
    assert "**질문** —" in rendered
    assert "**답변** —" in rendered
    assert "**내 판단** — 자료 보관이 아니라 재실행 가능한 학습 흐름" in rendered
    assert "**결과** —" in rendered
    assert "**자료** — 회귀 샘플 문항 PDF, 분류 샘플 해설 PDF" in rendered


def test_daily_digest_renders_detailed_semantics_and_compact_source_index(
    tmp_path: Path,
) -> None:
    _digest_settings(tmp_path)
    daily = tmp_path / "inbox/daily/2026-08-24.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("# 2026-08-24\n", encoding="utf-8")
    record_codex_source_bundle(
        tmp_path,
        CodexSourceBundle(
            day=date(2026, 8, 24),
            source_locator="thread-fixture:2026-08-24",
            title="데님 핏을 실제 착용 기준으로 비교했다",
            messages=(
                CodexSourceMessage(
                    role="user",
                    text="기존에 편하게 입은 바지와 비교하면 어떤 핏이 맞아?",
                    created_at="2026-08-24T03:10:00Z",
                    attachments=(CodexSourceAttachment(label="착용 사진", media_type="image"),),
                ),
                CodexSourceMessage(
                    role="assistant",
                    text=(
                        "실제 착용감을 우선하면 와이드 계열이 더 가깝습니다. "
                        f"[근거]({tmp_path / 'private/note.md'})와 "
                        "ai-reference/는 사람 화면에 노출하지 않습니다."
                    ),
                    created_at="2026-08-24T03:11:00Z",
                ),
            ),
        ),
    )
    entries = entries_from_records(
        [
            {
                "kind": "생활",
                "title": "데님 핏을 실제 착용 기준으로 비교했다",
                "summary": "사이즈 표보다 실제 착용감을 우선했다.",
                "exchanges": [
                    {
                        "question": "어떤 핏이 맞나?",
                        "answer": "와이드 계열을 후보로 좁혔다.",
                        "facts": ["편하게 입은 기존 바지가 있다."],
                        "criteria": ["실제 착용감", "원하는 실루엣"],
                        "alternatives": ["와이드", "배럴"],
                        "evidence": ["착용 사진"],
                        "changes": ["구매 기준을 정리했다."],
                        "unresolved": ["기장을 최종 확인해야 한다."],
                    }
                ],
            }
        ]
    )

    record_codex_daily_digest(tmp_path, day=date(2026, 8, 24), entries=entries)

    rendered = daily.read_text(encoding="utf-8")
    assert "**확인한 사실** — 편하게 입은 기존 바지가 있다." in rendered
    assert "**판단 기준** — 실제 착용감 · 원하는 실루엣" in rendered
    assert "## 대화 찾아보기" in rendered
    assert "> [!note]" not in rendered
    assert "### 데님 핏을 실제 착용 기준으로 비교했다" in rendered
    assert "- 질문 1개 · 답변 1개" in rendered
    assert "**12:10**" in rendered
    assert "기존에 편하게 입은 바지와 비교하면" in rendered
    assert "실제 착용감을 우선하면 와이드 계열이 더 가깝습니다." not in rendered
    assert "thread-fixture" not in rendered
    assert str(tmp_path) not in rendered
    assert "ai-reference/" not in rendered


def test_daily_digest_links_explicit_canonical_wiki_from_local_source(
    tmp_path: Path,
) -> None:
    _digest_settings(tmp_path)
    daily = tmp_path / "inbox/daily/2026-08-24.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("# 2026-08-24\n", encoding="utf-8")
    canonical = tmp_path / "wiki/personal/link-calendar.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(
        '---\ntitle: "Link Calendar 사용 원칙"\n---\n\n# Link Calendar 사용 원칙\n',
        encoding="utf-8",
    )
    record_codex_source_bundle(
        tmp_path,
        CodexSourceBundle(
            day=date(2026, 8, 24),
            source_locator="thread-fixture:2026-08-24",
            title="Link Calendar 사용 원칙을 정리했다",
            messages=(
                CodexSourceMessage(
                    role="assistant",
                    text=f"정본은 {canonical}:1에 반영했다.",
                    created_at="2026-08-24T03:11:00Z",
                ),
            ),
        ),
    )

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(),
        input_state="source-only",
    )

    rendered = daily.read_text(encoding="utf-8")
    assert "## 관련 문서" not in rendered
    assert (
        "### [[../../wiki/personal/link-calendar|Link Calendar 사용 원칙을 정리했다]]"
        in rendered
    )


def test_daily_digest_places_canonical_links_with_their_subject(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    daily = tmp_path / "inbox/daily/2026-08-24.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("# 2026-08-24\n", encoding="utf-8")
    subject = tmp_path / "wiki/personal/link-calendar.md"
    parent = tmp_path / "wiki/personal/wiki.md"
    subject.parent.mkdir(parents=True)
    subject.write_text(
        '---\ntitle: "Link Calendar 사용 원칙"\n---\n\n# Link Calendar 사용 원칙\n',
        encoding="utf-8",
    )
    parent.write_text(
        '---\ntitle: "Wiki"\n---\n\n# Wiki\n',
        encoding="utf-8",
    )

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(
            CodexDailyDigestEntry(
                kind="결정",
                title="Link Calendar 사용 원칙",
                summary="날짜 탐색과 일정 연결의 소유권을 정리했다.",
                related_documents=(
                    "wiki/personal/link-calendar.md",
                    "wiki/personal/wiki.md",
                ),
            ),
        ),
    )

    rendered = daily.read_text(encoding="utf-8")
    assert "### [[../../wiki/personal/link-calendar|Link Calendar 사용 원칙]]" in rendered
    assert "**연결된 기준** — [[../../wiki/personal/wiki|Wiki]]" in rendered
    assert "## 관련 문서" not in rendered
    assert rendered.count("[[../../wiki/personal/link-calendar") == 1


def test_daily_digest_populates_native_base_metadata(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    daily = tmp_path / "inbox/daily/2026-08-24.md"
    daily.parent.mkdir(parents=True)
    daily.write_text(
        "---\n"
        "type: Daily\n"
        'title: "2026-08-24"\n'
        'summary: ""\n'
        'digest_status: ""\n'
        "---\n\n"
        "# 2026-08-24\n",
        encoding="utf-8",
    )

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(
            CodexDailyDigestEntry(
                kind="학습",
                title="AICE 학습 환경을 준비했다",
                summary="샘플 문항과 실습 코드를 한 흐름으로 정리했다.",
            ),
        ),
    )

    rendered = daily.read_text(encoding="utf-8")
    assert 'summary: "AICE 학습 환경을 준비했다"' in rendered
    assert 'digest_status: "정리 완료"' in rendered
    assert '  - "학습"' in rendered
    assert '  - "AICE"' in rendered


def test_daily_digest_removes_only_retired_empty_sections_and_keeps_manual_text(
    tmp_path: Path,
) -> None:
    _digest_settings(tmp_path)
    daily = tmp_path / "inbox/daily/2026-08-24.md"
    daily.parent.mkdir(parents=True)
    daily.write_text(
        "# 2026-08-24\n\n"
        "## 오늘의 초점\n\n-\n\n"
        "## 포착\n\n- 내가 직접 적은 메모\n\n"
        "## 사실 이력\n\n"
        "- 시간·행동·결정·외부 원본 위치만 짧게 기록\n\n"
        "## 질문\n\n-\n"
        "## 만든 문서\n\n-\n\n"
        "## Woon 처리 안내\n\n"
        "여기에 쓴 자유 메모는 자동으로 다른 폴더로 옮기지 않는다. "
        "지식화·원본 보존·검증이 필요한 내용은 이 채팅에 보내면 Woon이 "
        "판정하고 처리한다. 사용자는 검토 대기 후보에 실제 결정이 필요할 때만 "
        "확인한다.\n",
        encoding="utf-8",
    )

    record_codex_daily_digest(
        tmp_path,
        day=date(2026, 8, 24),
        entries=(),
        input_state="no-meaningful",
    )

    rendered = daily.read_text(encoding="utf-8")
    assert "## 오늘의 초점" not in rendered
    assert "## 사실 이력" not in rendered
    assert "## 질문" not in rendered
    assert "## 만든 문서" not in rendered
    assert "## Woon 처리 안내" not in rendered
    assert "## 포착" in rendered
    assert "내가 직접 적은 메모" in rendered
    assert "<!-- woon-tasks:start -->" in rendered
    assert rendered.endswith("## 자유 메모\n")


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
    checkpoint_key: daily-codex-projection
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
