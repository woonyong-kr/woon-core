from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core.calendar import manual_schedule
from woon_core.calendar.manual_schedule import (
    UserScheduleRequest,
    update_user_authorized_schedule_category,
)
from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import FakeCalendarPort


def _request(**changes: object) -> UserScheduleRequest:
    values: dict[str, object] = {
        "event_id": "training-orientation-2026-08-18",
        "title": "교육 과정 입소식",
        "start_at": datetime(2026, 8, 18, 4, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 18, 5, 0, tzinfo=UTC),
        "category_id": "learning",
        "location": "교육장",
        "notes": "준비물을 챙겨 온다.",
    }
    values.update(changes)
    return UserScheduleRequest(**values)


def test_user_authorized_schedule_is_replay_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)
    calendar = FakeCalendarPort()

    first = manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )
    replay = manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )

    assert first == replay
    assert first.idempotency_key == "user-calendar:training-orientation-2026-08-18"
    assert calendar.write_count == 1


def test_user_authorized_schedule_rejects_an_unstable_event_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="calendar event ID"):
        manual_schedule.apply_user_authorized_schedule(
            tmp_path,
            _request(event_id="TRAINING ORIENTATION"),
            calendar=calendar,
        )

    assert calendar.write_count == 0


def test_user_authorized_schedule_keeps_category_but_can_hide_it_from_the_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)
    calendar = FakeCalendarPort()

    first = manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(display_category=False),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )
    second = manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(display_category=True),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )

    assert first.calendar_event_id == second.calendar_event_id
    assert calendar.write_count == 2


def test_category_update_preserves_the_receipt_proven_event_and_is_replay_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)
    calendar = FakeCalendarPort()
    created = manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(event_id="interview-dropoff-2026-08-19"),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )

    first = update_user_authorized_schedule_category(
        tmp_path,
        event_id="interview-dropoff-2026-08-19",
        category_id="relationship",
        calendar=calendar,
        authorized_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
    )
    replay = update_user_authorized_schedule_category(
        tmp_path,
        event_id="interview-dropoff-2026-08-19",
        category_id="relationship",
        calendar=calendar,
    )

    assert first == replay
    assert first.calendar_event_id == created.calendar_event_id
    assert calendar.categories == {created.calendar_event_id: "relationship"}
    assert calendar.category_update_count == 1
    receipt_path = (
        tmp_path
        / ".local/woon-knowledge/schedule-apply/calendar-category-receipts"
        / "interview-dropoff-2026-08-19.json"
    )
    assert receipt_path.is_file()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "verified"


def test_category_update_reconciles_a_pending_external_write_without_repeating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)

    class InterruptedVerificationPort(FakeCalendarPort):
        def __init__(self) -> None:
            super().__init__()
            self.interrupt_verification = True

        def verify_category(self, event_id: str, category_id: str) -> None:
            if self.interrupt_verification:
                self.interrupt_verification = False
                raise WoonError("simulated interruption after EventKit write")
            super().verify_category(event_id, category_id)

    calendar = InterruptedVerificationPort()
    manual_schedule.apply_user_authorized_schedule(
        tmp_path,
        _request(event_id="interview-followup-2026-08-19"),
        calendar=calendar,
        authorized_at=datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
    )
    receipt_path = (
        tmp_path
        / ".local/woon-knowledge/schedule-apply/calendar-category-receipts"
        / "interview-followup-2026-08-19.json"
    )

    with pytest.raises(WoonError, match="simulated interruption"):
        update_user_authorized_schedule_category(
            tmp_path,
            event_id="interview-followup-2026-08-19",
            category_id="relationship",
            calendar=calendar,
            authorized_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
        )

    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "pending"
    recovered = update_user_authorized_schedule_category(
        tmp_path,
        event_id="interview-followup-2026-08-19",
        category_id="relationship",
        calendar=calendar,
    )

    assert recovered.category_id == "relationship"
    assert calendar.category_update_count == 1
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["status"] == "verified"


def test_category_update_rejects_an_event_without_a_woon_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(
        timezone="Asia/Seoul", receipt_directory=tmp_path / ".local/woon-knowledge/receipts"
    )
    monkeypatch.setattr(manual_schedule, "load_orchestrator_settings", lambda _vault: settings)
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="receipt-proven Woon event"):
        update_user_authorized_schedule_category(
            tmp_path,
            event_id="unknown-calendar-event",
            category_id="relationship",
            calendar=calendar,
        )

    assert calendar.category_update_count == 0
