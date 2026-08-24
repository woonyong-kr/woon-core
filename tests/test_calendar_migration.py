from __future__ import annotations

import json
from pathlib import Path

import pytest

from woon_core.calendar.migration import migrate_legacy_schedule_state
from woon_core.errors import WoonError


class FakeLegacyCalendar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def migrate_legacy_owned_calendar(
        self, *, expected_event_id: str, legacy_name: str, target_name: str
    ) -> str:
        self.calls.append((expected_event_id, legacy_name, target_name))
        return target_name


def _write_v1_state(vault: Path, *, second_event_id: str | None = None) -> None:
    state_path = vault / ".local/woon-knowledge/schedule-bridge-state.json"
    state_path.parent.mkdir(parents=True)
    second = second_event_id or "event-001"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "receipts": {
                    "operation-001": {
                        "candidate_id": "candidate-001",
                        "lifecycle": "create",
                        "idempotency_key": "schedule-001",
                        "calendar_event_id": "event-001",
                        "legacy_metadata": "retired-external-id",
                    },
                    "operation-002": {
                        "candidate_id": "candidate-001",
                        "lifecycle": "update",
                        "idempotency_key": "schedule-001",
                        "calendar_event_id": second,
                        "legacy_metadata": "retired-external-id",
                    },
                },
                "stable_ids": {
                    "schedule-001": {
                        "calendar_event_id": "event-001",
                        "calendar_owned": True,
                        "legacy_metadata": "retired-external-id",
                    }
                },
                "pending": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_migration_renames_only_receipt_proven_calendar_and_removes_retired_fields(
    tmp_path: Path,
) -> None:
    _write_v1_state(tmp_path)
    calendar = FakeLegacyCalendar()

    result = migrate_legacy_schedule_state(tmp_path, calendar)
    payload = json.loads(
        (tmp_path / ".local/woon-knowledge/schedule-bridge-state.json").read_text(encoding="utf-8")
    )

    assert result.migrated is True
    assert result.calendar_name == "Woon 일정"
    assert result.calendar_event_id == "event-001"
    assert calendar.calls == [("event-001", "Woon Tasks", "Woon 일정")]
    assert payload == {
        "version": 2,
        "pending": [],
        "receipts": {
            "operation-001": {
                "candidate_id": "candidate-001",
                "lifecycle": "create",
                "idempotency_key": "schedule-001",
                "calendar_event_id": "event-001",
            },
            "operation-002": {
                "candidate_id": "candidate-001",
                "lifecycle": "update",
                "idempotency_key": "schedule-001",
                "calendar_event_id": "event-001",
            },
        },
        "stable_ids": {"schedule-001": "event-001"},
    }


def test_migration_refuses_state_that_does_not_prove_one_calendar(tmp_path: Path) -> None:
    _write_v1_state(tmp_path, second_event_id="event-002")

    with pytest.raises(WoonError, match="cannot prove one owned calendar"):
        migrate_legacy_schedule_state(tmp_path, FakeLegacyCalendar())
