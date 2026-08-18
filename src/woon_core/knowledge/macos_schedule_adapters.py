"""macOS EventKit adapter for the approval-gated Woon calendar bridge."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Final

from woon_core.calendar.categories import CALENDAR_CATEGORY_TITLES
from woon_core.calendar.constants import OWNED_CALENDAR_NAME
from woon_core.calendar.projection import CalendarProjectionEvent
from woon_core.errors import WoonError
from woon_core.knowledge.schedule_bridge import ScheduleCandidate


def _native_script_path(filename: str) -> Path:
    """Prefer the wheel-bundled bridge while retaining the source-tree development path."""

    package_resource = Path(__file__).resolve().parents[1] / "calendar" / "native" / filename
    if package_resource.is_file():
        return package_resource
    return Path(__file__).resolve().parents[3] / "scripts" / filename


_SWIFT_SCRIPT: Final = _native_script_path("woon-calendar-bridge.swift")
_EXPORT_SWIFT_SCRIPT: Final = _native_script_path("woon-calendar-export.swift")
NativeRunner = Callable[[dict[str, str | None]], Mapping[str, str]]
CalendarReaderRunner = Callable[[dict[str, str]], tuple[CalendarProjectionEvent, ...]]


class MacOSCalendarPort:
    """EventKit port confined to the one Woon-owned calendar."""

    def __init__(self, runner: NativeRunner | None = None) -> None:
        self._runner = runner or _run_calendar_bridge

    def ensure_permission(self) -> None:
        response = self._runner({"action": "permission", "calendarName": OWNED_CALENDAR_NAME})
        if response.get("status") != "granted":
            raise WoonError("calendar bridge did not confirm EventKit permission")

    def create_or_update(self, candidate: ScheduleCandidate, existing_id: str | None) -> str:
        event_id = self._runner(
            {
                "action": "create-or-update",
                "calendarName": OWNED_CALENDAR_NAME,
                "title": _calendar_title(candidate),
                "startAt": _iso(candidate.start_at),
                "endAt": _iso(candidate.end_at),
                "existingID": existing_id,
                "location": candidate.location,
                "notes": _calendar_notes(candidate),
            }
        ).get("calendar_event_id")
        if not event_id:
            raise WoonError("calendar bridge did not return an event identifier")
        return event_id

    def verify_applied(self, candidate: ScheduleCandidate, event_id: str) -> None:
        """Re-read the saved EventKit record before a receipt becomes successful."""

        response = self._runner(
            {
                "action": "verify",
                "calendarName": OWNED_CALENDAR_NAME,
                "title": _calendar_title(candidate),
                "startAt": _iso(candidate.start_at),
                "endAt": _iso(candidate.end_at),
                "existingID": event_id,
                "location": candidate.location,
                "notes": _calendar_notes(candidate),
            }
        )
        if (
            response.get("status") != "verified"
            or response.get("calendar_event_id") != event_id
            or response.get("calendar_name") != OWNED_CALENDAR_NAME
        ):
            raise WoonError("calendar bridge verification receipt mismatch")

    def cancel(self, event_id: str) -> None:
        response = self._runner(
            {
                "action": "cancel",
                "calendarName": OWNED_CALENDAR_NAME,
                "existingID": event_id,
            }
        )
        if response.get("calendar_event_id") != event_id:
            raise WoonError("calendar bridge cancellation receipt mismatch")

    def verify_cancelled(self, event_id: str) -> None:
        """Re-read EventKit after removal so a cancellation receipt is not inferred."""

        response = self._runner(
            {
                "action": "verify-absent",
                "calendarName": OWNED_CALENDAR_NAME,
                "existingID": event_id,
                "title": None,
                "startAt": None,
                "endAt": None,
                "location": None,
                "notes": None,
            }
        )
        if response.get("status") != "absent" or response.get("calendar_event_id") != event_id:
            raise WoonError("calendar bridge cancellation verification mismatch")

    def migrate_legacy_owned_calendar(
        self, *, expected_event_id: str, legacy_name: str, target_name: str
    ) -> str:
        """Rename only the calendar proven by an existing Woon event receipt."""

        response = self._runner(
            {
                "action": "rename-owned-calendar",
                "calendarName": legacy_name,
                "targetCalendarName": target_name,
                "existingID": expected_event_id,
                "title": None,
                "startAt": None,
                "endAt": None,
            }
        )
        if response.get("calendar_event_id") != expected_event_id:
            raise WoonError("calendar rename receipt mismatch")
        if response.get("calendar_name") != target_name:
            raise WoonError("calendar rename did not return the target name")
        return target_name


class MacOSCalendarReader:
    """Read only event summaries for the local Obsidian Markdown projection."""

    def __init__(self, runner: CalendarReaderRunner | None = None) -> None:
        self._runner = runner or _run_calendar_export

    def list_events(
        self, *, start_at: datetime, end_at: datetime
    ) -> tuple[CalendarProjectionEvent, ...]:
        return self._runner({"startAt": _iso(start_at), "endAt": _iso(end_at)})


def _calendar_title(candidate: ScheduleCandidate) -> str:
    if not candidate.display_category:
        return candidate.intent.strip()
    try:
        category = CALENDAR_CATEGORY_TITLES[candidate.category_id]
    except KeyError as error:
        raise WoonError("calendar bridge requires a configured category") from error
    return f"{candidate.intent.strip()} · {category}"


def _calendar_notes(candidate: ScheduleCandidate) -> str:
    """Persist only a fixed category marker that the native exporter can read back."""

    marker = "Woon이 생성한 시간 일정입니다."
    category_marker = f"Woon category: {candidate.category_id}"
    if candidate.notes is None or not candidate.notes.strip():
        return marker + "\n" + category_marker
    return marker + "\n" + category_marker + "\n\n" + candidate.notes.strip()


def _run_calendar_bridge(payload: dict[str, str | None]) -> Mapping[str, str]:
    if not _SWIFT_SCRIPT.is_file():
        raise WoonError("Woon CalendarBridge script is missing")
    result = subprocess.run(
        ["/usr/bin/swift", str(_SWIFT_SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WoonError(f"calendar bridge failed: {detail or 'unknown native error'}")
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WoonError("calendar bridge returned invalid JSON") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise WoonError("calendar bridge returned an invalid payload")
    return decoded


def _run_calendar_export(payload: dict[str, str]) -> tuple[CalendarProjectionEvent, ...]:
    if not _EXPORT_SWIFT_SCRIPT.is_file():
        raise WoonError("Woon Calendar export script is missing")
    result = subprocess.run(
        ["/usr/bin/swift", str(_EXPORT_SWIFT_SCRIPT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise WoonError(f"calendar export failed: {detail or 'unknown native error'}")
    try:
        decoded = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WoonError("calendar export returned invalid JSON") from error
    raw_events = decoded.get("events") if isinstance(decoded, dict) else None
    if not isinstance(raw_events, list):
        raise WoonError("calendar export returned no event list")
    events: list[CalendarProjectionEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise WoonError("calendar export event is malformed")
        try:
            start_at = datetime.fromisoformat(_export_string(raw, "start_at"))
            end_at = datetime.fromisoformat(_export_string(raw, "end_at"))
        except ValueError as error:
            raise WoonError("calendar export event has invalid timestamps") from error
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise WoonError("calendar export event timestamps need a timezone")
        all_day = raw.get("all_day")
        if not isinstance(all_day, bool):
            raise WoonError("calendar export event all_day is malformed")
        events.append(
            CalendarProjectionEvent(
                source_event_id=_export_string(raw, "source_event_id"),
                calendar_name=_export_string(raw, "calendar_name"),
                title=_export_string(raw, "title"),
                start_at=start_at,
                end_at=end_at,
                all_day=all_day,
                category_id=_optional_export_category(raw, title=_export_string(raw, "title")),
            )
        )
    return tuple(events)


def _export_string(raw: Mapping[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise WoonError(f"calendar export event {field} is malformed")
    return value


def _optional_export_category(raw: Mapping[str, object], *, title: str) -> str | None:
    value = raw.get("category_id")
    if value is not None and value not in CALENDAR_CATEGORY_TITLES:
        raise WoonError("calendar export event category is malformed")
    if isinstance(value, str):
        return value
    # Older Woon events already carry an explicit, user-visible category suffix.
    # This is a format migration, not an inference about arbitrary calendar titles.
    for category_id, category_title in CALENDAR_CATEGORY_TITLES.items():
        if title.endswith(f" · {category_title}"):
            return category_id
    return None


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WoonError("calendar bridge requires timezone-aware event times")
    return value.isoformat()
