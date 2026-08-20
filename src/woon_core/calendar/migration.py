"""One-time migration of the retired Woon-owned calendar receipt format."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json

_LEGACY_CALENDAR_NAME = "Woon Tasks"
_TARGET_CALENDAR_NAME = "Woon 일정"
_STATE_RELATIVE_PATH = Path(".local/woon-knowledge/schedule-bridge-state.json")


class LegacyCalendarRenamer(Protocol):
    """Native adapter needed for the one controlled calendar-name migration."""

    def migrate_legacy_owned_calendar(
        self, *, expected_event_id: str, legacy_name: str, target_name: str
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class LegacyCalendarMigrationResult:
    """Local receipt for an idempotent legacy calendar migration."""

    migrated: bool
    calendar_name: str
    calendar_event_id: str | None


def migrate_legacy_schedule_state(
    vault: Path, calendar: LegacyCalendarRenamer
) -> LegacyCalendarMigrationResult:
    """Rename the receipt-proven calendar, then retire the v1 local state schema."""

    state_path = (vault.expanduser().resolve() / _STATE_RELATIVE_PATH).resolve()
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return LegacyCalendarMigrationResult(False, _TARGET_CALENDAR_NAME, None)
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("legacy calendar receipt state is unreadable") from error
    if not isinstance(raw, dict):
        raise WoonError("legacy calendar receipt state is malformed")
    version = raw.get("version")
    if version == 2:
        return LegacyCalendarMigrationResult(False, _TARGET_CALENDAR_NAME, None)
    if version != 1:
        raise WoonError("legacy calendar receipt state has an unsupported version")

    receipts, stable_ids, event_id = _migrate_v1_payload(raw)
    calendar_name = calendar.migrate_legacy_owned_calendar(
        expected_event_id=event_id,
        legacy_name=_LEGACY_CALENDAR_NAME,
        target_name=_TARGET_CALENDAR_NAME,
    )
    payload = {
        "version": 2,
        "pending": [],
        "receipts": receipts,
        "stable_ids": stable_ids,
    }
    atomic_write(state_path, encode_json(payload), mode=0o600)
    return LegacyCalendarMigrationResult(True, calendar_name, event_id)


def _migrate_v1_payload(
    raw: dict[str, object],
) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
    source_receipts = raw.get("receipts")
    source_stable_ids = raw.get("stable_ids")
    if not isinstance(source_receipts, dict) or not isinstance(source_stable_ids, dict):
        raise WoonError("legacy calendar receipt state is malformed")

    receipts: dict[str, dict[str, str]] = {}
    event_ids: set[str] = set()
    for operation_key, value in source_receipts.items():
        if not isinstance(operation_key, str) or not isinstance(value, dict):
            raise WoonError("legacy calendar receipt state is malformed")
        receipt = {
            field: _required_string(value, field)
            for field in ("candidate_id", "lifecycle", "idempotency_key", "calendar_event_id")
        }
        if receipt["lifecycle"] not in {"create", "update", "cancel"}:
            raise WoonError("legacy calendar receipt state has an invalid lifecycle")
        receipts[operation_key] = receipt
        event_ids.add(receipt["calendar_event_id"])

    stable_ids: dict[str, str] = {}
    for idempotency_key, value in source_stable_ids.items():
        if not isinstance(idempotency_key, str) or not isinstance(value, dict):
            raise WoonError("legacy calendar receipt state is malformed")
        if value.get("calendar_owned") is not True:
            raise WoonError("legacy calendar receipt does not prove Woon calendar ownership")
        event_id = _required_string(value, "calendar_event_id")
        stable_ids[idempotency_key] = event_id
        event_ids.add(event_id)

    if not receipts or not stable_ids or len(event_ids) != 1:
        raise WoonError("legacy calendar receipt state cannot prove one owned calendar")
    return receipts, stable_ids, event_ids.pop()


def _required_string(value: dict[object, object], field: str) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or not candidate:
        raise WoonError("legacy calendar receipt state is malformed")
    return candidate
