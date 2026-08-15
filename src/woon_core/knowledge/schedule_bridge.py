"""Policy-authorized, idempotent schedule bridge contract.

This module owns the approval, idempotency, and receipt contract. macOS
adapters live separately; this module never reaches a private Things database
or an arbitrary calendar.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

_AREA_IDS = frozenset({"career", "learning", "creative", "life", "relationship", "health", "admin"})
_THINGS_TAGS = frozenset(
    {"컴퓨터", "전화", "외부", "집", "집중", "빠른 처리", "대기", "일정", "위임"}
)


@dataclass(frozen=True, slots=True)
class ScheduleCandidate:
    """A policy-authorized candidate to create, update, or cancel a schedule."""

    candidate_id: str
    source_id: str
    activity_id: str
    intent: str
    timezone: str
    start_at: datetime | None
    end_at: datetime | None
    authorized_at: datetime | None
    lifecycle: Literal["create", "update", "cancel"]
    idempotency_key: str
    existing_calendar_event_id: str | None = None
    area_id: str = "career"
    things_tags: tuple[str, ...] = ()
    bridge_revision: int = 1


@dataclass(frozen=True, slots=True)
class ScheduleReceipt:
    """Stable external identifiers returned after one completed operation."""

    candidate_id: str
    lifecycle: Literal["create", "update", "cancel"]
    idempotency_key: str
    things_id: str | None
    calendar_event_id: str | None


class ThingsPort(Protocol):
    """Narrow mutation port; production adapters are intentionally absent."""

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str: ...

    def cancel(self, things_id: str) -> None: ...


class CalendarPort(Protocol):
    """Narrow EventKit-like port; permission is checked before every mutation."""

    def ensure_permission(self) -> None: ...

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str: ...

    def cancel(self, event_id: str) -> None: ...


class ScheduleBridge:
    """Apply one authorized candidate without duplicate external writes."""

    def __init__(
        self,
        things: ThingsPort,
        calendar: CalendarPort,
        *,
        state_path: Path | None = None,
    ) -> None:
        self._things = things
        self._calendar = calendar
        self._state_path = state_path.expanduser().resolve() if state_path else None
        self._receipts, self._stable_ids, self._pending = _load_state(self._state_path)

    @property
    def receipts(self) -> dict[str, ScheduleReceipt]:
        """Expose a copy for a durable receipt store to persist later."""

        return dict(self._receipts)

    def apply(self, candidate: ScheduleCandidate) -> ScheduleReceipt:
        """Apply one candidate, failing before mutations when validation fails."""

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

        needs_calendar = (
            candidate.start_at is not None and candidate.existing_calendar_event_id is None
        )
        if needs_calendar:
            # Permission precedes the Things write so permission failure has no
            # partial external side effect and can safely be retried.
            self._calendar.ensure_permission()
        _ensure_things_authorization(self._things)
        self._pending.add(operation_key)
        self._persist_state(lock_held=state_lock_held)

        try:
            receipt = self._apply_external(candidate)
        except BaseException:
            # A bridge can crash after a remote side effect. Leave the durable
            # pending marker so retrying never creates a duplicate item/event.
            raise
        self._pending.remove(operation_key)
        self._receipts[operation_key] = receipt
        self._persist_state(lock_held=state_lock_held)
        return receipt

    def _apply_external(self, candidate: ScheduleCandidate) -> ScheduleReceipt:
        stable_key = candidate.idempotency_key
        known_things_id, known_calendar_id, calendar_owned = self._stable_ids.get(
            stable_key, (None, None, False)
        )
        calendar_id = candidate.existing_calendar_event_id or known_calendar_id

        if candidate.lifecycle == "cancel":
            if known_things_id is None and (calendar_id is None or not calendar_owned):
                raise WoonError("cannot cancel a schedule without stable external IDs")
            if calendar_id is not None and calendar_owned:
                self._calendar.ensure_permission()
            if known_things_id is not None:
                self._things.cancel(known_things_id)
            if calendar_id is not None and calendar_owned:
                self._calendar.cancel(calendar_id)
            return ScheduleReceipt(
                candidate_id=candidate.candidate_id,
                lifecycle=candidate.lifecycle,
                idempotency_key=candidate.idempotency_key,
                things_id=known_things_id,
                calendar_event_id=calendar_id,
            )

        things_id = self._things.create_or_update(candidate, known_things_id)
        if candidate.start_at is None:
            calendar_id = None
        elif candidate.existing_calendar_event_id is None:
            calendar_id = self._calendar.create_or_update(candidate, known_calendar_id)
        self._stable_ids[stable_key] = (
            things_id,
            calendar_id,
            candidate.start_at is not None and candidate.existing_calendar_event_id is None,
        )
        return ScheduleReceipt(
            candidate_id=candidate.candidate_id,
            lifecycle=candidate.lifecycle,
            idempotency_key=candidate.idempotency_key,
            things_id=things_id,
            calendar_event_id=calendar_id,
        )

    def _persist_state(self, *, lock_held: bool = False) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 1,
            "pending": sorted(self._pending),
            "receipts": {
                key: {
                    "candidate_id": value.candidate_id,
                    "lifecycle": value.lifecycle,
                    "idempotency_key": value.idempotency_key,
                    "things_id": value.things_id,
                    "calendar_event_id": value.calendar_event_id,
                }
                for key, value in sorted(self._receipts.items())
            },
            "stable_ids": {
                key: {
                    "things_id": value[0],
                    "calendar_event_id": value[1],
                    "calendar_owned": value[2],
                }
                for key, value in sorted(self._stable_ids.items())
            },
        }
        if lock_held:
            atomic_write(self._state_path, encode_json(payload), mode=0o600)
            return
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            atomic_write(self._state_path, encode_json(payload), mode=0o600)


class FakeThingsPort:
    """Deterministic fixture port; never communicates with Things 3."""

    def __init__(self) -> None:
        self.write_count = 0
        self.cancelled: set[str] = set()

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
        self.write_count += 1
        return existing_id or f"things-{candidate.idempotency_key}"

    def cancel(self, things_id: str) -> None:
        self.cancelled.add(things_id)


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

    def cancel(self, event_id: str) -> None:
        self.cancelled.add(event_id)


def _ensure_things_authorization(things: ThingsPort) -> None:
    checker = getattr(things, "ensure_authorization", None)
    if checker is not None:
        checker()


def _load_state(
    state_path: Path | None,
) -> tuple[
    dict[str, ScheduleReceipt],
    dict[str, tuple[str | None, str | None, bool]],
    set[str],
]:
    if state_path is None or not state_path.exists():
        return {}, {}, set()
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("schedule bridge state is unreadable") from error
    if not isinstance(value, dict) or value.get("version") != 1:
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
        if lifecycle not in {"create", "update", "cancel"}:
            raise WoonError("schedule bridge receipt lifecycle is malformed")
        fields = ("candidate_id", "idempotency_key")
        if not all(isinstance(raw.get(field), str) and raw[field] for field in fields):
            raise WoonError("schedule bridge receipt state is malformed")
        things_id = raw.get("things_id")
        calendar_id = raw.get("calendar_event_id")
        if not all(value is None or isinstance(value, str) for value in (things_id, calendar_id)):
            raise WoonError("schedule bridge receipt IDs are malformed")
        receipts[key] = ScheduleReceipt(
            candidate_id=raw["candidate_id"],
            lifecycle=lifecycle,
            idempotency_key=raw["idempotency_key"],
            things_id=things_id,
            calendar_event_id=calendar_id,
        )
    stable_ids: dict[str, tuple[str | None, str | None, bool]] = {}
    for key, raw in raw_stable_ids.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            raise WoonError("schedule bridge stable ID state is malformed")
        things_id = raw.get("things_id")
        calendar_id = raw.get("calendar_event_id")
        calendar_owned = raw.get("calendar_owned")
        if not all(
            value is None or isinstance(value, str) for value in (things_id, calendar_id)
        ) or not isinstance(calendar_owned, bool):
            raise WoonError("schedule bridge stable ID state is malformed")
        stable_ids[key] = (things_id, calendar_id, calendar_owned)
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
    if (candidate.start_at is None) != (candidate.end_at is None):
        raise WoonError("start_at and end_at must be both present or absent")
    if candidate.start_at is not None:
        if (
            candidate.start_at.tzinfo is None
            or candidate.end_at is None
            or candidate.end_at.tzinfo is None
        ):
            raise WoonError("schedule times must be timezone-aware")
        if candidate.end_at <= candidate.start_at:
            raise WoonError("end_at must be after start_at")
    if candidate.lifecycle not in {"create", "update", "cancel"}:
        raise WoonError("unsupported schedule lifecycle")
    if candidate.bridge_revision < 1:
        raise WoonError("schedule candidate bridge_revision must be positive")
    if candidate.area_id not in _AREA_IDS:
        raise WoonError("schedule candidate must use a configured Things area")
    if len(set(candidate.things_tags)) != len(candidate.things_tags):
        raise WoonError("schedule candidate Things tags must be unique")
    if any(tag not in _THINGS_TAGS for tag in candidate.things_tags):
        raise WoonError("schedule candidate may use only configured action tags")


def _operation_key(candidate: ScheduleCandidate) -> str:
    payload = "\0".join(
        (
            candidate.idempotency_key,
            candidate.lifecycle,
            candidate.intent,
            candidate.start_at.isoformat() if candidate.start_at else "date-only",
            candidate.end_at.isoformat() if candidate.end_at else "date-only",
            candidate.existing_calendar_event_id or "",
            candidate.area_id,
            "\0".join(sorted(candidate.things_tags)),
            str(candidate.bridge_revision),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
