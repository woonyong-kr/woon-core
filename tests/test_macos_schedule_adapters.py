from __future__ import annotations

from datetime import UTC, datetime

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.macos_schedule_adapters import (
    MacOSCalendarPort,
    MacOSThingsURLSchemePort,
)
from woon_core.knowledge.schedule_bridge import ScheduleCandidate


def _candidate(**changes: object) -> ScheduleCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-001",
        "source_id": "gmail-thread:opaque-001",
        "activity_id": "activity-001",
        "intent": "직무 면접",
        "timezone": "Asia/Seoul",
        "start_at": datetime(2026, 8, 21, 16, 30, tzinfo=UTC),
        "end_at": datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
        "authorized_at": datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        "lifecycle": "create",
        "idempotency_key": "schedule-001",
        "area_id": "career",
    }
    values.update(changes)
    return ScheduleCandidate(**values)


def test_calendar_port_confines_event_to_woon_calendar_with_category_suffix() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        return {"calendar_event_id": "event-001"}

    port = MacOSCalendarPort(runner)
    assert port.create_or_update(_candidate(), None) == "event-001"

    assert payloads == [
        {
            "action": "create-or-update",
            "calendarName": "Woon Tasks",
            "title": "직무 면접 · 커리어",
            "startAt": "2026-08-21T16:30:00+00:00",
            "endAt": "2026-08-21T17:00:00+00:00",
            "existingID": None,
        }
    ]


def test_calendar_port_requires_native_permission_receipt() -> None:
    port = MacOSCalendarPort(lambda payload: {"status": "denied"})

    with pytest.raises(WoonError, match="did not confirm EventKit permission"):
        port.ensure_permission()


def test_calendar_port_rejects_date_only_candidate() -> None:
    port = MacOSCalendarPort(lambda payload: {"calendar_event_id": "event-001"})

    with pytest.raises(WoonError, match="date-time candidate"):
        port.create_or_update(_candidate(start_at=None, end_at=None), None)


def test_calendar_port_requires_expected_cancellation_receipt() -> None:
    port = MacOSCalendarPort(lambda payload: {"calendar_event_id": "other"})

    with pytest.raises(WoonError, match="cancellation receipt mismatch"):
        port.cancel("event-001")


def test_things_url_port_uses_area_tags_and_callback_identifier() -> None:
    payloads: list[dict[str, object]] = []

    def runner(payload: dict[str, object]) -> dict[str, str]:
        payloads.append(payload)
        return {"things_id": "things-001"}

    port = MacOSThingsURLSchemePort(runner)
    assert (
        port.create_or_update(_candidate(things_tags=("컴퓨터", "일정")), None) == "things-001"
    )

    assert payloads == [
        {
            "action": "add",
            "title": "직무 면접",
            "when": "2026-08-21T16:30:00+00:00",
            "tags": ["컴퓨터", "일정"],
            "list": "커리어·일",
            "notes": "Woon Second Brain이 생성한 일정입니다.",
            "existingID": None,
        }
    ]


def test_things_url_port_requires_callback_identifier_and_uses_token_only_for_update() -> None:
    port = MacOSThingsURLSchemePort(lambda payload: {"status": "keychain-ready"})
    port.ensure_authorization()

    with pytest.raises(WoonError, match="did not return a to-do identifier"):
        port.create_or_update(_candidate(), "things-existing")
