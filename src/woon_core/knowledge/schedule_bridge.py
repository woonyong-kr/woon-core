"""Policy-authorized, idempotent Apple Calendar bridge contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from woon_core.calendar.categories import CALENDAR_CATEGORY_IDS
from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock


@dataclass(frozen=True, slots=True)
class ScheduleCandidate:
    """A policy-authorized date-time appointment for the Woon calendar."""

    candidate_id: str
    source_id: str
    activity_id: str
    intent: str
    timezone: str
    start_at: datetime
    end_at: datetime
    authorized_at: datetime | None
    lifecycle: Literal["create", "update", "cancel"]
    idempotency_key: str
    category_id: str = "career"
    bridge_revision: int = 1
    location: str | None = None
    notes: str | None = None
    display_category: bool = True


@dataclass(frozen=True, slots=True)
class ScheduleReceipt:
    """Stable Apple Calendar identifier returned after one completed operation."""

    candidate_id: str
    lifecycle: Literal["create", "update", "cancel"]
    idempotency_key: str
    calendar_event_id: str


class CalendarPort(Protocol):
    """Narrow EventKit-like port; permission is checked before every mutation."""

    def ensure_permission(self) -> None: ...

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str: ...

    def verify_applied(self, candidate: ScheduleCandidate, event_id: str) -> None: ...

    def cancel(self, event_id: str) -> None: ...

    def verify_cancelled(self, event_id: str) -> None: ...


class ScheduleBridge:
    """Apply one authorized appointment without duplicate Apple Calendar writes."""

    def __init__(self, calendar: CalendarPort, *, state_path: Path | None = None) -> None:
        self._calendar = calendar
        self._state_path = state_path.expanduser().resolve() if state_path else None
        self._receipts, self._stable_ids, self._pending = _load_state(self._state_path)

    @property
    def receipts(self) -> dict[str, ScheduleReceipt]:
        return dict(self._receipts)

    def apply(self, candidate: ScheduleCandidate) -> ScheduleReceipt:
        """Apply one candidate after validation and write a replay-safe receipt."""

        if self._state_path is None:
            return self._apply(candidate, state_lock_held=False)
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            self._receipts, self._stable_ids, self._pending = _load_state(self._state_path)
            return self._apply(candidate, state_lock_held=True)

    def _apply(self, candidate: ScheduleCandidate, *, state_lock_held: bool) -> ScheduleReceipt:
        _validate(candidate)
        operation_key = _operation_key(candidate)
        existing_receipt = self._receipts.get(operation_key)
        if existing_receipt is not None:
            return existing_receipt
        if operation_key in self._pending:
            raise WoonError("pending schedule operation requires manual reconciliation")

        self._calendar.ensure_permission()
        self._pending.add(operation_key)
        self._persist_state(lock_held=state_lock_held)
        try:
            receipt = self._apply_external(candidate)
        except BaseException:
            # An EventKit write could have succeeded before this process stopped.
            # Keep the pending marker so retries cannot create a second event.
            raise
        self._pending.remove(operation_key)
        self._receipts[operation_key] = receipt
        self._persist_state(lock_held=state_lock_held)
        return receipt

    def _apply_external(self, candidate: ScheduleCandidate) -> ScheduleReceipt:
        stable_key = candidate.idempotency_key
        known_calendar_id = self._stable_ids.get(stable_key)
        if candidate.lifecycle == "cancel":
            if known_calendar_id is None:
                raise WoonError("cannot cancel a schedule without a stable calendar ID")
            self._calendar.cancel(known_calendar_id)
            self._calendar.verify_cancelled(known_calendar_id)
            return ScheduleReceipt(
                candidate_id=candidate.candidate_id,
                lifecycle=candidate.lifecycle,
                idempotency_key=candidate.idempotency_key,
                calendar_event_id=known_calendar_id,
            )

        calendar_id = self._calendar.create_or_update(candidate, known_calendar_id)
        self._calendar.verify_applied(candidate, calendar_id)
        self._stable_ids[stable_key] = calendar_id
        return ScheduleReceipt(
            candidate_id=candidate.candidate_id,
            lifecycle=candidate.lifecycle,
            idempotency_key=candidate.idempotency_key,
            calendar_event_id=calendar_id,
        )

    def _persist_state(self, *, lock_held: bool = False) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 2,
            "pending": sorted(self._pending),
            "receipts": {
                key: {
                    "candidate_id": value.candidate_id,
                    "lifecycle": value.lifecycle,
                    "idempotency_key": value.idempotency_key,
                    "calendar_event_id": value.calendar_event_id,
                }
                for key, value in sorted(self._receipts.items())
            },
            "stable_ids": dict(sorted(self._stable_ids.items())),
        }
        if lock_held:
            atomic_write(self._state_path, encode_json(payload), mode=0o600)
            return
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            atomic_write(self._state_path, encode_json(payload), mode=0o600)


class FakeCalendarPort:
    """Deterministic fixture port; never communicates with Apple Calendar."""

    def __init__(self, *, permission_granted: bool = True) -> None:
        self.permission_granted = permission_granted
        self.write_count = 0
        self.cancelled: set[str] = set()

    def ensure_permission(self) -> None:
        if not self.permission_granted:
            raise WoonError("calendar permission denied")

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
        self.write_count += 1
        return existing_id or f"calendar-{candidate.idempotency_key}"

    def verify_applied(self, candidate: ScheduleCandidate, event_id: str) -> None:
        return None

    def cancel(self, event_id: str) -> None:
        self.cancelled.add(event_id)

    def verify_cancelled(self, event_id: str) -> None:
        return None


def _load_state(
    state_path: Path | None,
) -> tuple[dict[str, ScheduleReceipt], dict[str, str], set[str]]:
    if state_path is None or not state_path.exists():
        return {}, {}, set()
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("schedule bridge state is unreadable") from error
    if not isinstance(value, dict) or value.get("version") != 2:
        raise WoonError("schedule bridge state has an unsupported version")
    raw_pending = value.get("pending")
    raw_receipts = value.get("receipts")
    raw_stable_ids = value.get("stable_ids")
    if (
        not isinstance(raw_pending, list)
        or not all(isinstance(item, str) and item for item in raw_pending)
        or len(set(raw_pending)) != len(raw_pending)
        or not isinstance(raw_receipts, dict)
        or not isinstance(raw_stable_ids, dict)
    ):
        raise WoonError("schedule bridge state is malformed")
    receipts: dict[str, ScheduleReceipt] = {}
    for key, raw in raw_receipts.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise WoonError("schedule bridge receipt state is malformed")
        lifecycle = raw.get("lifecycle")
        fields = ("candidate_id", "idempotency_key", "calendar_event_id")
        if lifecycle not in {"create", "update", "cancel"} or not all(
            isinstance(raw.get(field), str) and raw[field] for field in fields
        ):
            raise WoonError("schedule bridge receipt state is malformed")
        receipts[key] = ScheduleReceipt(
            candidate_id=raw["candidate_id"],
            lifecycle=lifecycle,
            idempotency_key=raw["idempotency_key"],
            calendar_event_id=raw["calendar_event_id"],
        )
    stable_ids: dict[str, str] = {}
    for key, value in raw_stable_ids.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise WoonError("schedule bridge stable ID state is malformed")
        stable_ids[key] = value
    return receipts, stable_ids, set(raw_pending)


def _validate(candidate: ScheduleCandidate) -> None:
    if not candidate.authorized_at:
        raise WoonError("schedule apply requires policy authorization")
    if not candidate.candidate_id or not candidate.source_id or not candidate.activity_id:
        raise WoonError("schedule candidate requires stable source and activity IDs")
    if (
        not candidate.intent.strip()
        or not candidate.timezone.strip()
        or not candidate.idempotency_key
    ):
        raise WoonError("schedule candidate requires intent, timezone, and idempotency key")
    if candidate.start_at is None or candidate.end_at is None:
        raise WoonError("schedule bridge requires a date-time candidate")
    if candidate.start_at.tzinfo is None or candidate.end_at.tzinfo is None:
        raise WoonError("schedule times must be timezone-aware")
    if candidate.end_at <= candidate.start_at:
        raise WoonError("end_at must be after start_at")
    if candidate.lifecycle not in {"create", "update", "cancel"}:
        raise WoonError("unsupported schedule lifecycle")
    if candidate.bridge_revision < 1:
        raise WoonError("schedule candidate bridge_revision must be positive")
    if candidate.category_id not in CALENDAR_CATEGORY_IDS:
        raise WoonError("schedule candidate must use a configured calendar category")
    if not isinstance(candidate.display_category, bool):
        raise WoonError("schedule candidate display_category must be a boolean")


def _operation_key(candidate: ScheduleCandidate) -> str:
    payload = "\0".join(
        (
            candidate.idempotency_key,
            candidate.lifecycle,
            candidate.intent,
            candidate.start_at.isoformat(),
            candidate.end_at.isoformat(),
            candidate.category_id,
            str(candidate.bridge_revision),
            candidate.location or "",
            candidate.notes or "",
            str(candidate.display_category),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
