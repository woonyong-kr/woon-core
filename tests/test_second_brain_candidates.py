from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.second_brain_candidates import (
    CodexResponseItem,
    MailScheduleInput,
    PersonMemoryInput,
    candidate_from_allowlisted_mail,
    candidate_from_codex_messages,
    candidate_from_codex_person_memory,
    persist_review_candidates,
    prepare_review_candidates,
)


def _mail(**changes: object) -> MailScheduleInput:
    fields: dict[str, object] = {
        "source_locator": "gmail-thread:opaque-krafton-001",
        "classification": "allowlisted",
        "actionable": True,
        "summary": "크래프톤 면접 일정 확인이 필요하다.",
        "occurred_at": datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        "scheduled_for": datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
    }
    fields.update(changes)
    return MailScheduleInput(**fields)


def test_discards_advertising_and_ambiguous_mail_without_a_candidate() -> None:
    ad = _mail(
        classification="advertising",
        summary="쿠폰을 확인하세요.",
        source_locator="gmail-thread:opaque-promotion-001",
    )
    ambiguous = _mail(
        classification="ambiguous",
        summary="확인할 수 없는 외부 발신 요청",
        source_locator="gmail-thread:opaque-unknown-001",
    )

    assert candidate_from_allowlisted_mail(ad) is None
    assert candidate_from_allowlisted_mail(ambiguous) is None


def test_creates_only_review_candidate_for_allowlisted_datetime_mail(tmp_path: Path) -> None:
    candidate = candidate_from_allowlisted_mail(_mail())
    assert candidate is not None

    outcome = persist_review_candidates(tmp_path, "brain/review/mail", (candidate,))

    assert outcome.candidate_ids == (candidate.candidate_id,)
    path = tmp_path / "brain/review/mail" / "크래프톤-면접-일정-확인이-필요하다.md"
    stored = path.read_text(encoding="utf-8")
    assert 'title: "크래프톤 면접 일정 확인이 필요하다."' in stored
    assert candidate.candidate_id not in stored
    assert candidate.source_locator not in stored
    assert "status: Review" in stored
    assert "calendar_candidate:" not in stored
    assert "Apple Calendar에 바로 반영하지 않는다." in stored
    assert "원문 메일 본문은 절대 저장하면 안 된다" not in stored


def test_date_only_mail_stays_a_review_candidate_and_never_a_calendar_candidate() -> None:
    candidate = candidate_from_allowlisted_mail(_mail(scheduled_for=date(2026, 8, 20)))

    assert candidate is not None
    assert candidate.calendar_candidate is False
    assert candidate.time_precision == "date-only"


def test_refuses_to_overwrite_a_changed_review_candidate(tmp_path: Path) -> None:
    candidate = candidate_from_allowlisted_mail(_mail())
    assert candidate is not None
    path = tmp_path / "brain/review/mail" / "크래프톤-면접-일정-확인이-필요하다.md"
    path.parent.mkdir(parents=True)
    path.write_text("user-edited\n", encoding="utf-8")

    with pytest.raises(WoonError, match="candidate conflicts with an existing review file"):
        persist_review_candidates(tmp_path, "brain/review/mail", (candidate,))

    assert path.read_text(encoding="utf-8") == "user-edited\n"


def test_prepares_all_review_candidates_without_writing(tmp_path: Path) -> None:
    candidate = candidate_from_allowlisted_mail(_mail())
    assert candidate is not None

    prepared = prepare_review_candidates(tmp_path, "brain/review/mail", (candidate,))

    assert len(prepared) == 1
    path, data = prepared[0]
    assert path.name == "크래프톤-면접-일정-확인이-필요하다.md"
    assert b"status: Review" in data
    assert not path.exists()


def test_codex_candidate_uses_only_opted_in_user_and_assistant_messages() -> None:
    items = (
        CodexResponseItem("message", "user", "사용자 요청", "thread-001", 1),
        CodexResponseItem("message", "assistant", "응답", "thread-001", 2),
        CodexResponseItem("system", "system", "SYSTEM_SECRET", "thread-001", 3),
        CodexResponseItem("tool", "tool", "TOOL_SECRET", "thread-001", 4),
        CodexResponseItem("reasoning", "assistant", "REASONING_SECRET", "thread-001", 5),
    )

    candidate = candidate_from_codex_messages(
        items,
        opt_in=True,
        summary="Second Brain 자동화의 후보·승인 경계를 결정했다.",
    )

    assert candidate is not None
    outcome = persist_review_candidates(Path("/tmp") / "not-used", "brain/review/codex", ())
    assert outcome.candidate_ids == ()
    serialized = json.dumps(candidate.as_record(), ensure_ascii=False)
    assert "SYSTEM_SECRET" not in serialized
    assert "TOOL_SECRET" not in serialized
    assert "REASONING_SECRET" not in serialized
    assert candidate.source_locator == "codex-thread:thread-001#1-2"


def test_codex_candidate_hides_unknown_epoch_from_human_review_file(tmp_path: Path) -> None:
    candidate = candidate_from_codex_messages(
        (CodexResponseItem("message", "user", "요청", "thread-001", 1),),
        opt_in=True,
        summary="작업 경계를 검토했다.",
    )
    assert candidate is not None

    persist_review_candidates(tmp_path, "brain/review/codex", (candidate,))

    content = (tmp_path / "brain/review/codex/작업-경계를-검토했다.md").read_text(encoding="utf-8")
    assert "1970-" not in content


def test_codex_candidate_requires_opt_in_and_real_message_items() -> None:
    item = CodexResponseItem("message", "user", "사용자 요청", "thread-001", 1)

    assert candidate_from_codex_messages((item,), opt_in=False, summary="요약") is None
    assert (
        candidate_from_codex_messages(
            (CodexResponseItem("tool", "tool", "출력", "thread-001", 1),),
            opt_in=True,
            summary="요약",
        )
        is None
    )


def test_person_memory_candidate_keeps_only_explicit_facts_pending_review(tmp_path: Path) -> None:
    candidate = candidate_from_codex_person_memory(
        (
            CodexResponseItem("message", "user", "김희준과 금요일 면담", "thread-001", 1),
            CodexResponseItem("message", "assistant", "다음 행동 정리", "thread-001", 2),
        ),
        opt_in=True,
        person=PersonMemoryInput(
            display_name="김희준",
            explicit_facts=("금요일 면담 일정이 언급되었다.",),
            next_action="면담 시간을 확인한다.",
        ),
    )

    assert candidate is not None
    assert candidate.kind == "person-memory"
    persist_review_candidates(tmp_path, "brain/review/codex", (candidate,))

    content = next((tmp_path / "brain/review/codex").glob("*.md")).read_text(encoding="utf-8")
    assert 'review_kind: "인물 정리"' in content
    assert "김희준" in content
    assert "금요일 면담 일정이 언급되었다." in content
    assert "면담 시간을 확인한다." in content
    assert "인물 카드·관계·연락처·신상은 만들거나 추정하지 않는다." in content
    assert "people:" not in content
    assert "person_roles:" not in content
    assert "김희준과 금요일 면담" not in content
    assert candidate.candidate_id not in content
    assert candidate.source_locator not in content


def test_person_memory_candidate_rejects_bare_name_and_contact_like_content() -> None:
    items = (CodexResponseItem("message", "user", "요청", "thread-001", 1),)

    with pytest.raises(WoonError, match="one to three explicit facts"):
        candidate_from_codex_person_memory(
            items,
            opt_in=True,
            person=PersonMemoryInput(display_name="김희준", explicit_facts=()),
        )
    with pytest.raises(WoonError, match="contact-free"):
        candidate_from_codex_person_memory(
            items,
            opt_in=True,
            person=PersonMemoryInput(
                display_name="김희준",
                explicit_facts=("연락처는 someone@example.com이다.",),
            ),
        )
    assert (
        candidate_from_codex_person_memory(
            items,
            opt_in=False,
            person=PersonMemoryInput(
                display_name="김희준",
                explicit_facts=("면담 일정이 언급되었다.",),
            ),
        )
        is None
    )
