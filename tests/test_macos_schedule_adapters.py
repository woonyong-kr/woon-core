from __future__ import annotations

from datetime import UTC, datetime

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.macos_schedule_adapters import MacOSCalendarPort
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
        "category_id": "career",
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
            "calendarName": "Woon 일정",
            "title": "직무 면접 · 커리어",
            "startAt": "2026-08-21T16:30:00+00:00",
            "endAt": "2026-08-21T17:00:00+00:00",
            "existingID": None,
            "location": None,
            "notes": "Woon이 생성한 시간 일정입니다.\nWoon category: career",
        }
    ]


def test_calendar_port_can_hide_a_classification_from_the_visible_title() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        return {"calendar_event_id": "event-001"}

    MacOSCalendarPort(runner).create_or_update(_candidate(display_category=False), None)

    assert payloads[0]["title"] == "직무 면접"


def test_calendar_port_requires_native_permission_receipt() -> None:
    port = MacOSCalendarPort(lambda payload: {"status": "denied"})

    with pytest.raises(WoonError, match="did not confirm EventKit permission"):
        port.ensure_permission()


def test_calendar_port_keeps_explicit_location_and_notes_in_the_native_request() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        return {"calendar_event_id": "event-001"}

    MacOSCalendarPort(runner).create_or_update(
        _candidate(location="정글 스테이지", notes="노트북을 챙긴다."), None
    )

    assert payloads[0]["location"] == "정글 스테이지"
    assert payloads[0]["notes"] == (
        "Woon이 생성한 시간 일정입니다.\nWoon category: career\n\n노트북을 챙긴다."
    )


def test_calendar_port_requeries_the_saved_event_before_accepting_a_receipt() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        if payload["action"] == "verify":
            return {
                "status": "verified",
                "calendar_event_id": "event-001",
                "calendar_name": "Woon 일정",
            }
        return {"calendar_event_id": "event-001"}

    candidate = _candidate(location="정글 스테이지", notes="노트북을 챙긴다.")
    port = MacOSCalendarPort(runner)
    event_id = port.create_or_update(candidate, None)
    port.verify_applied(candidate, event_id)

    assert payloads[1] == {
        "action": "verify",
        "calendarName": "Woon 일정",
        "title": "직무 면접 · 커리어",
        "startAt": "2026-08-21T16:30:00+00:00",
        "endAt": "2026-08-21T17:00:00+00:00",
        "existingID": "event-001",
        "location": "정글 스테이지",
        "notes": "Woon이 생성한 시간 일정입니다.\nWoon category: career\n\n노트북을 챙긴다.",
    }


def test_calendar_port_rejects_a_mismatched_saved_event_verification() -> None:
    port = MacOSCalendarPort(lambda payload: {"status": "verified", "calendar_event_id": "other"})

    with pytest.raises(WoonError, match="verification receipt mismatch"):
        port.verify_applied(_candidate(), "event-001")


def test_calendar_port_requires_expected_cancellation_receipt() -> None:
    port = MacOSCalendarPort(lambda payload: {"calendar_event_id": "other"})

    with pytest.raises(WoonError, match="cancellation receipt mismatch"):
        port.cancel("event-001")


def test_calendar_port_requeries_after_cancellation() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        return {"status": "absent", "calendar_event_id": "event-001"}

    MacOSCalendarPort(runner).verify_cancelled("event-001")

    assert payloads == [
        {
            "action": "verify-absent",
            "calendarName": "Woon 일정",
            "existingID": "event-001",
            "title": None,
            "startAt": None,
            "endAt": None,
            "location": None,
            "notes": None,
        }
    ]


def test_calendar_port_updates_only_the_category_marker_then_requeries() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        if payload["action"] == "verify-category":
            return {
                "status": "verified",
                "calendar_event_id": "event-001",
                "calendar_name": "Woon 일정",
                "category_id": "relationship",
            }
        return {"calendar_event_id": "event-001"}

    port = MacOSCalendarPort(runner)
    port.update_category("event-001", "relationship")
    port.verify_category("event-001", "relationship")

    assert payloads == [
        {
            "action": "set-category",
            "calendarName": "Woon 일정",
            "existingID": "event-001",
            "categoryID": "relationship",
            "title": None,
            "startAt": None,
            "endAt": None,
            "location": None,
            "notes": None,
        },
        {
            "action": "verify-category",
            "calendarName": "Woon 일정",
            "existingID": "event-001",
            "categoryID": "relationship",
            "title": None,
            "startAt": None,
            "endAt": None,
            "location": None,
            "notes": None,
        },
    ]


def test_calendar_port_migrates_only_the_receipt_proven_legacy_calendar() -> None:
    payloads: list[dict[str, str | None]] = []

    def runner(payload: dict[str, str | None]) -> dict[str, str]:
        payloads.append(payload)
        return {"calendar_event_id": "event-001", "calendar_name": "Woon 일정"}

    port = MacOSCalendarPort(runner)

    assert (
        port.migrate_legacy_owned_calendar(
            expected_event_id="event-001",
            legacy_name="Woon Tasks",
            target_name="Woon 일정",
        )
        == "Woon 일정"
    )
    assert payloads == [
        {
            "action": "rename-owned-calendar",
            "calendarName": "Woon Tasks",
            "targetCalendarName": "Woon 일정",
            "existingID": "event-001",
            "title": None,
            "startAt": None,
            "endAt": None,
        }
    ]
