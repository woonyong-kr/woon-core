from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import (
    FakeCalendarPort,
    FakeThingsPort,
    ScheduleBridge,
    ScheduleCandidate,
)


def _candidate(**changes: object) -> ScheduleCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-001",
        "source_id": "gmail-thread:opaque-001",
        "activity_id": "activity-001",
        "intent": "크래프톤 직무면접",
        "timezone": "Asia/Seoul",
        "start_at": datetime(2026, 8, 21, 16, 30, tzinfo=UTC),
        "end_at": datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
        "approved_at": datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        "lifecycle": "create",
        "idempotency_key": "schedule-001",
    }
    values.update(changes)
    return ScheduleCandidate(**values)


def test_date_only_creates_only_things_after_approval() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(things, calendar)

    receipt = bridge.apply(_candidate(start_at=None, end_at=None))

    assert receipt.things_id is not None
    assert receipt.calendar_event_id is None
    assert things.write_count == 1
    assert calendar.write_count == 0


def test_datetime_creates_one_calendar_event_and_replay_does_not_duplicate() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(things, calendar)

    first = bridge.apply(_candidate())
    replay = bridge.apply(_candidate())

    assert first == replay
    assert things.write_count == 1
    assert calendar.write_count == 1


def test_rejects_unapproved_candidate_without_any_write() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="explicit approval"):
        ScheduleBridge(things, calendar).apply(_candidate(approved_at=None))

    assert things.write_count == calendar.write_count == 0


def test_permission_denied_leaves_no_success_receipt_and_retry_is_safe() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort(permission_granted=False)
    bridge = ScheduleBridge(things, calendar)

    with pytest.raises(WoonError, match="calendar permission denied"):
        bridge.apply(_candidate())
    assert bridge.receipts == {}
    assert things.write_count == 0
    assert calendar.write_count == 0

    calendar.permission_granted = True
    bridge.apply(_candidate())
    assert things.write_count == calendar.write_count == 1


def test_update_and_cancel_reuse_stable_external_ids() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(things, calendar)
    created = bridge.apply(_candidate())

    updated = bridge.apply(_candidate(lifecycle="update", intent="면접 장소 변경"))
    cancelled = bridge.apply(_candidate(lifecycle="cancel"))

    assert updated.things_id == cancelled.things_id == created.things_id
    assert updated.calendar_event_id == cancelled.calendar_event_id == created.calendar_event_id
    assert things.cancelled == {created.things_id}
    assert calendar.cancelled == {created.calendar_event_id}


def test_existing_calendar_reference_never_creates_another_calendar_event() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()

    receipt = ScheduleBridge(things, calendar).apply(
        _candidate(existing_calendar_event_id="calendar-existing-001")
    )

    assert receipt.calendar_event_id == "calendar-existing-001"
    assert calendar.write_count == 0
    assert things.write_count == 1


def test_existing_calendar_reference_is_never_updated_or_cancelled() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(things, calendar)
    referenced = _candidate(existing_calendar_event_id="calendar-existing-001")

    bridge.apply(referenced)
    bridge.apply(
        _candidate(
            existing_calendar_event_id="calendar-existing-001",
            lifecycle="update",
            intent="면접 장소 변경",
        )
    )
    bridge.apply(_candidate(existing_calendar_event_id="calendar-existing-001", lifecycle="cancel"))

    assert calendar.write_count == 0
    assert calendar.cancelled == set()
    assert things.cancelled == {"things-schedule-001"}


def test_rejects_invalid_time_range_without_any_write() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="end_at must be after start_at"):
        ScheduleBridge(things, calendar).apply(
            _candidate(end_at=datetime(2026, 8, 21, 16, 0, tzinfo=UTC))
        )

    assert things.write_count == calendar.write_count == 0


def test_rejects_unconfigured_things_area_and_personal_tags() -> None:
    things = FakeThingsPort()
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="configured Things area"):
        ScheduleBridge(things, calendar).apply(_candidate(area_id="people-private"))
    with pytest.raises(WoonError, match="configured action tags"):
        ScheduleBridge(things, calendar).apply(_candidate(things_tags=("KRAFTON",)))

    assert things.write_count == calendar.write_count == 0


def test_persists_receipt_and_stable_ids_across_bridge_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "schedule-state.json"
    first_things = FakeThingsPort()
    first_calendar = FakeCalendarPort()
    first = ScheduleBridge(first_things, first_calendar, state_path=state_path).apply(_candidate())

    restarted_things = FakeThingsPort()
    restarted_calendar = FakeCalendarPort()
    restarted = ScheduleBridge(restarted_things, restarted_calendar, state_path=state_path).apply(
        _candidate()
    )

    assert restarted == first
    assert restarted_things.write_count == restarted_calendar.write_count == 0
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_crash_after_external_write_leaves_pending_marker_and_blocks_replay(tmp_path: Path) -> None:
    class CrashAfterWriteThings(FakeThingsPort):
        def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
            super().create_or_update(candidate, existing_id)
            raise RuntimeError("simulated process crash")

    state_path = tmp_path / "schedule-state.json"
    things = CrashAfterWriteThings()
    bridge = ScheduleBridge(things, FakeCalendarPort(), state_path=state_path)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        bridge.apply(_candidate())
    with pytest.raises(WoonError, match="pending schedule operation"):
        ScheduleBridge(FakeThingsPort(), FakeCalendarPort(), state_path=state_path).apply(
            _candidate()
        )

    assert things.write_count == 1
