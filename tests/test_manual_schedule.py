from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core.calendar import manual_schedule
from woon_core.calendar.manual_schedule import UserScheduleRequest
from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import FakeCalendarPort


def _request(**changes: object) -> UserScheduleRequest:
    values: dict[str, object] = {
        "event_id": "krafton-jungle-entrance-2026-08-18",
        "title": "크래프톤 정글 12기 심화과정 입소식",
        "start_at": datetime(2026, 8, 18, 4, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 18, 5, 0, tzinfo=UTC),
        "category_id": "learning",
        "location": "정글 스테이지",
        "notes": "노트북 등 짐을 챙겨 온다.",
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
    assert first.idempotency_key == "user-calendar:krafton-jungle-entrance-2026-08-18"
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
            _request(event_id="KRAFTON JUNGLE"),
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
