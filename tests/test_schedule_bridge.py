from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import FakeCalendarPort, ScheduleBridge, ScheduleCandidate


def _candidate(**changes: object) -> ScheduleCandidate:
    values: dict[str, object] = {
        "candidate_id": "candidate-001",
        "source_id": "gmail-thread:opaque-001",
        "activity_id": "activity-001",
        "intent": "크래프톤 직무면접",
        "timezone": "Asia/Seoul",
        "start_at": datetime(2026, 8, 21, 16, 30, tzinfo=UTC),
        "end_at": datetime(2026, 8, 21, 17, 0, tzinfo=UTC),
        "authorized_at": datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        "lifecycle": "create",
        "idempotency_key": "schedule-001",
    }
    values.update(changes)
    return ScheduleCandidate(**values)


def test_datetime_creates_one_calendar_event_and_replay_does_not_duplicate() -> None:
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(calendar)

    first = bridge.apply(_candidate())
    replay = bridge.apply(_candidate())

    assert first == replay
    assert calendar.write_count == 1


def test_category_change_updates_the_existing_calendar_event() -> None:
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(calendar)

    created = bridge.apply(_candidate())
    recategorized = bridge.apply(_candidate(category_id="learning"))

    assert recategorized.calendar_event_id == created.calendar_event_id
    assert calendar.write_count == 2


def test_rejects_unauthorized_candidate_without_any_write() -> None:
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="policy authorization"):
        ScheduleBridge(calendar).apply(_candidate(authorized_at=None))

    assert calendar.write_count == 0


def test_permission_denied_leaves_no_success_receipt_and_retry_is_safe() -> None:
    calendar = FakeCalendarPort(permission_granted=False)
    bridge = ScheduleBridge(calendar)

    with pytest.raises(WoonError, match="calendar permission denied"):
        bridge.apply(_candidate())
    assert bridge.receipts == {}
    assert calendar.write_count == 0

    calendar.permission_granted = True
    bridge.apply(_candidate())
    assert calendar.write_count == 1


def test_update_and_cancel_reuse_stable_calendar_id() -> None:
    calendar = FakeCalendarPort()
    bridge = ScheduleBridge(calendar)
    created = bridge.apply(_candidate())

    updated = bridge.apply(_candidate(lifecycle="update", intent="면접 장소 변경"))
    cancelled = bridge.apply(_candidate(lifecycle="cancel"))

    assert updated.calendar_event_id == cancelled.calendar_event_id == created.calendar_event_id
    assert calendar.cancelled == {created.calendar_event_id}


def test_rejects_invalid_time_range_without_any_write() -> None:
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="end_at must be after start_at"):
        ScheduleBridge(calendar).apply(_candidate(end_at=datetime(2026, 8, 21, 16, 0, tzinfo=UTC)))

    assert calendar.write_count == 0


def test_rejects_date_only_or_unconfigured_category_without_any_write() -> None:
    calendar = FakeCalendarPort()

    with pytest.raises(WoonError, match="requires a date-time candidate"):
        ScheduleBridge(calendar).apply(_candidate(start_at=None, end_at=None))
    with pytest.raises(WoonError, match="configured calendar category"):
        ScheduleBridge(calendar).apply(_candidate(category_id="people-private"))

    assert calendar.write_count == 0


def test_persists_receipt_and_stable_id_across_bridge_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "schedule-state.json"
    first_calendar = FakeCalendarPort()
    first = ScheduleBridge(first_calendar, state_path=state_path).apply(_candidate())

    restarted_calendar = FakeCalendarPort()
    restarted = ScheduleBridge(restarted_calendar, state_path=state_path).apply(_candidate())

    assert restarted == first
    assert restarted_calendar.write_count == 0
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_crash_after_external_write_leaves_pending_marker_and_blocks_replay(tmp_path: Path) -> None:
    class CrashAfterWriteCalendar(FakeCalendarPort):
        def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
            super().create_or_update(candidate, existing_id)
            raise RuntimeError("simulated process crash")

    state_path = tmp_path / "schedule-state.json"
    calendar = CrashAfterWriteCalendar()
    bridge = ScheduleBridge(calendar, state_path=state_path)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        bridge.apply(_candidate())
    with pytest.raises(WoonError, match="pending schedule operation"):
        ScheduleBridge(FakeCalendarPort(), state_path=state_path).apply(_candidate())

    assert calendar.write_count == 1


def test_failed_eventkit_requery_leaves_the_operation_pending_without_a_receipt(
    tmp_path: Path,
) -> None:
    class VerificationFailureCalendar(FakeCalendarPort):
        def verify_applied(self, candidate: ScheduleCandidate, event_id: str) -> None:
            raise WoonError("simulated EventKit requery failure")

    state_path = tmp_path / "schedule-state.json"
    calendar = VerificationFailureCalendar()

    with pytest.raises(WoonError, match="simulated EventKit requery failure"):
        ScheduleBridge(calendar, state_path=state_path).apply(_candidate())

    restarted = ScheduleBridge(FakeCalendarPort(), state_path=state_path)
    with pytest.raises(WoonError, match="pending schedule operation"):
        restarted.apply(_candidate())
    assert restarted.receipts == {}
