from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.second_brain_candidates import (
    CodexResponseItem,
    MailScheduleInput,
    candidate_from_allowlisted_mail,
    candidate_from_codex_messages,
    persist_review_candidates,
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
    path = tmp_path / "brain/review/mail" / f"{candidate.candidate_id}.md"
    stored = path.read_text(encoding="utf-8")
    assert "status: Review" in stored
    assert "things_candidate: true" in stored
    assert "calendar_candidate: true" in stored
    assert "Things 3와 Apple Calendar에 자동 반영할 수 있다." in stored
    assert "원문 메일 본문은 절대 저장하면 안 된다" not in stored


def test_date_only_mail_can_be_a_things_candidate_but_never_calendar_candidate() -> None:
    candidate = candidate_from_allowlisted_mail(_mail(scheduled_for=date(2026, 8, 20)))

    assert candidate is not None
    assert candidate.things_candidate is True
    assert candidate.calendar_candidate is False
    assert candidate.time_precision == "date-only"


def test_refuses_to_overwrite_a_changed_review_candidate(tmp_path: Path) -> None:
    candidate = candidate_from_allowlisted_mail(_mail())
    assert candidate is not None
    path = tmp_path / "brain/review/mail" / f"{candidate.candidate_id}.md"
    path.parent.mkdir(parents=True)
    path.write_text("user-edited\n", encoding="utf-8")

    with pytest.raises(WoonError, match="candidate conflicts with an existing review file"):
        persist_review_candidates(tmp_path, "brain/review/mail", (candidate,))

    assert path.read_text(encoding="utf-8") == "user-edited\n"


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
