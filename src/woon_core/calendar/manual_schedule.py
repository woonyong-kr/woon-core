"""User-authorized EventKit schedule writes for the Woon-owned calendar."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from woon_core.calendar.categories import CALENDAR_CATEGORY_IDS
from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock
from woon_core.knowledge.macos_schedule_adapters import MacOSCalendarPort
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.schedule_bridge import (
    CalendarPort,
    ScheduleBridge,
    ScheduleCandidate,
    ScheduleReceipt,
)

_EVENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,100}$")


@dataclass(frozen=True, slots=True)
class UserScheduleRequest:
    """One explicit user request to create or update an appointment."""

    event_id: str
    title: str
    start_at: datetime
    end_at: datetime
    category_id: str
    location: str | None = None
    notes: str | None = None
    display_category: bool = True


@dataclass(frozen=True, slots=True)
class CalendarCategoryUpdateReceipt:
    """Proof that only a receipt-proven EventKit category marker changed."""

    event_id: str
    category_id: str
    calendar_event_id: str
    authorized_at: datetime


@dataclass(frozen=True, slots=True)
class _CalendarCategoryUpdateState:
    """Local pending or verified state for one EventKit category mutation."""

    status: Literal["pending", "verified"]
    event_id: str
    category_id: str
    calendar_event_id: str
    authorized_at: datetime


def apply_user_authorized_schedule(
    vault: Path,
    request: UserScheduleRequest,
    *,
    calendar: CalendarPort | None = None,
    authorized_at: datetime | None = None,
) -> ScheduleReceipt:
    """Write an explicitly requested appointment through the receipt-gated bridge."""

    settings = load_orchestrator_settings(vault)
    candidate = _candidate(request, authorized_at or datetime.now(UTC), settings.timezone)
    state_path = settings.receipt_directory.parent / "schedule-bridge-state.json"
    return ScheduleBridge(calendar or MacOSCalendarPort(), state_path=state_path).apply(candidate)


def update_user_authorized_schedule_category(
    vault: Path,
    *,
    event_id: str,
    category_id: str,
    calendar: CalendarPort | None = None,
    authorized_at: datetime | None = None,
) -> CalendarCategoryUpdateReceipt:
    """Correct one Woon-owned category without replacing title, place, or notes."""

    normalized_event_id = _validated_event_id(event_id)
    if category_id not in CALENDAR_CATEGORY_IDS:
        raise WoonError("calendar category update must use a configured calendar category")
    settings = load_orchestrator_settings(vault)
    calendar_port = calendar or MacOSCalendarPort()
    state_path = settings.receipt_directory.parent / "schedule-bridge-state.json"
    receipt_path = (
        settings.receipt_directory.parent
        / "schedule-apply/calendar-category-receipts"
        / f"{normalized_event_id}.json"
    )
    authorized = authorized_at or datetime.now(UTC)
    with exclusive_file_lock(receipt_path.with_suffix(".lock")):
        bridge = ScheduleBridge(calendar_port, state_path=state_path)
        calendar_event_id = bridge.calendar_event_id_for(f"user-calendar:{normalized_event_id}")
        prior = _load_category_state(receipt_path)
        calendar_port.ensure_permission()
        if prior is not None:
            if (
                prior.event_id != normalized_event_id
                or prior.category_id != category_id
                or prior.calendar_event_id != calendar_event_id
            ):
                raise WoonError("calendar category receipt conflicts with the requested update")
            calendar_port.verify_category(calendar_event_id, category_id)
            if prior.status == "pending":
                prior = _CalendarCategoryUpdateState(
                    status="verified",
                    event_id=prior.event_id,
                    category_id=prior.category_id,
                    calendar_event_id=prior.calendar_event_id,
                    authorized_at=prior.authorized_at,
                )
                atomic_write(receipt_path, encode_json(_category_state_payload(prior)), mode=0o600)
            return _verified_category_receipt(prior)

        pending = _CalendarCategoryUpdateState(
            status="pending",
            event_id=normalized_event_id,
            category_id=category_id,
            calendar_event_id=calendar_event_id,
            authorized_at=authorized,
        )
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(receipt_path, encode_json(_category_state_payload(pending)), mode=0o600)
        calendar_port.update_category(calendar_event_id, category_id)
        calendar_port.verify_category(calendar_event_id, category_id)
        verified = _CalendarCategoryUpdateState(
            status="verified",
            event_id=pending.event_id,
            category_id=pending.category_id,
            calendar_event_id=pending.calendar_event_id,
            authorized_at=pending.authorized_at,
        )
        atomic_write(receipt_path, encode_json(_category_state_payload(verified)), mode=0o600)
        return _verified_category_receipt(verified)


def _candidate(
    request: UserScheduleRequest, authorized_at: datetime, timezone: str
) -> ScheduleCandidate:
    event_id = _validated_event_id(request.event_id)
    title = request.title.strip()
    if not title:
        raise WoonError("calendar title must not be empty")
    if request.start_at.tzinfo is None or request.end_at.tzinfo is None:
        raise WoonError("calendar times must include a timezone")
    return ScheduleCandidate(
        candidate_id=f"user-calendar:{event_id}",
        source_id=f"user-request:{event_id}",
        activity_id=f"user-calendar:{event_id}",
        intent=title,
        timezone=timezone,
        start_at=request.start_at,
        end_at=request.end_at,
        authorized_at=authorized_at,
        lifecycle="create",
        idempotency_key=f"user-calendar:{event_id}",
        category_id=request.category_id,
        location=_optional_text(request.location),
        notes=_optional_text(request.notes),
        display_category=request.display_category,
    )


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validated_event_id(value: str) -> str:
    event_id = value.strip()
    if not _EVENT_ID.fullmatch(event_id):
        raise WoonError("calendar event ID must use lowercase letters, numbers, and hyphens")
    return event_id


def _load_category_state(path: Path) -> _CalendarCategoryUpdateState | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        authorized_at = datetime.fromisoformat(value["authorized_at"])
        state = _CalendarCategoryUpdateState(
            status=value["status"],
            event_id=value["event_id"],
            category_id=value["category_id"],
            calendar_event_id=value["calendar_event_id"],
            authorized_at=authorized_at,
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WoonError("calendar category receipt is unreadable") from error
    if (
        value.get("version") != 1
        or state.status not in {"pending", "verified"}
        or not _EVENT_ID.fullmatch(state.event_id)
        or state.category_id not in CALENDAR_CATEGORY_IDS
        or not state.calendar_event_id
        or state.authorized_at.tzinfo is None
    ):
        raise WoonError("calendar category receipt is malformed")
    return state


def _category_state_payload(state: _CalendarCategoryUpdateState) -> dict[str, object]:
    return {
        "version": 1,
        "status": state.status,
        "event_id": state.event_id,
        "category_id": state.category_id,
        "calendar_event_id": state.calendar_event_id,
        "authorized_at": state.authorized_at.isoformat(),
    }


def _verified_category_receipt(
    state: _CalendarCategoryUpdateState,
) -> CalendarCategoryUpdateReceipt:
    if state.status != "verified":
        raise WoonError("calendar category update is still pending verification")
    return CalendarCategoryUpdateReceipt(
        event_id=state.event_id,
        category_id=state.category_id,
        calendar_event_id=state.calendar_event_id,
        authorized_at=state.authorized_at,
    )
