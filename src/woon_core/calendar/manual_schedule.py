"""User-authorized EventKit schedule writes for the Woon-owned calendar."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from woon_core.errors import WoonError
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


def _candidate(
    request: UserScheduleRequest, authorized_at: datetime, timezone: str
) -> ScheduleCandidate:
    event_id = request.event_id.strip()
    if not _EVENT_ID.fullmatch(event_id):
        raise WoonError("calendar event ID must use lowercase letters, numbers, and hyphens")
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
