"""Create private, read-only calendar projections for Obsidian."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from woon_core.calendar.categories import (
    CALENDAR_CATEGORY_IDS,
    UNCATEGORIZED_CALENDAR_CATEGORY_ID,
    calendar_category_title,
)
from woon_core.calendar.constants import (
    LINK_CALENDAR_DASHBOARD_CSS_CLASS,
    LINK_CALENDAR_PLUGIN_ID,
    LINK_CALENDAR_PROFILE_ID,
    OWNED_CALENDAR_NAME,
)
from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.codex_knowledge import (
    CalendarDocumentLink,
    calendar_document_links_for_event,
)
from woon_core.people.dashboard import PersonDashboardProjection
from woon_core.people.service import (
    CalendarIdentityAmbiguity,
    CalendarIdentityResolution,
    CalendarPersonReference,
    PersonService,
)

_KST = ZoneInfo("Asia/Seoul")
_MARKDOWN_PROJECTION = "apple-calendar"
_MARKDOWN_MARKER = f"woon_projection: {_MARKDOWN_PROJECTION}\n"
APPLE_CALENDAR_ICS_FILENAME = "apple-calendar.ics"
APPLE_CALENDAR_ICS_RELATIVE_PATH = f"inbox/calendar/{APPLE_CALENDAR_ICS_FILENAME}"
APPLE_CALENDAR_EVENTS_RELATIVE_PATH = "inbox/calendar/events"
APPLE_CALENDAR_NOTION_DATABASE_FILENAME = "_database.md"
APPLE_CALENDAR_NOTION_DATABASE_RELATIVE_PATH = (
    f"{APPLE_CALENDAR_EVENTS_RELATIVE_PATH}/{APPLE_CALENDAR_NOTION_DATABASE_FILENAME}"
)
APPLE_CALENDAR_DASHBOARD_FILENAME = "apple-calendar.md"
APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH = f"inbox/calendar/{APPLE_CALENDAR_DASHBOARD_FILENAME}"
CALENDAR_PERSON_IDENTITY_REVIEW_RELATIVE_PATH = "inbox/review/calendar-person-identity-review.md"
PRISMA_VIRTUAL_EVENTS_FILENAME = ".prisma-virtual-events.md"
PRISMA_EMPTY_VIRTUAL_EVENTS = "\n```prisma-virtual-events\n[]\n```\n"
_ICS_PRODID = "-//Woon//Apple Calendar Read-only Projection//KO"
_DASHBOARD_MARKER = "woon_projection: apple-calendar-dashboard\n"
_PERSON_IDENTITY_REVIEW_MARKER = "woon_projection: calendar-person-identity-review\n"
_PROJECTION_DIRECTORY_WRITABLE_MODE = 0o700
_PROJECTION_DIRECTORY_READONLY_MODE = 0o500
_PROJECTION_FILE_READONLY_MODE = 0o400


@dataclass(frozen=True, slots=True)
class CalendarProjectionEvent:
    """The minimum event shape permitted to enter the local Markdown projection."""

    source_event_id: str
    calendar_name: str
    title: str
    start_at: datetime
    end_at: datetime
    all_day: bool
    category_id: str | None = None


class CalendarReader(Protocol):
    """Read event summaries from the local Calendar account without mutation."""

    def list_events(
        self, *, start_at: datetime, end_at: datetime
    ) -> tuple[CalendarProjectionEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class CalendarProjectionResult:
    changed: bool
    event_count: int
    relative_path: str
    ics_relative_path: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarPersonIdentityReview:
    """One unresolved exact identifier in a generated local Calendar view."""

    date: str
    title: str
    ambiguity: CalendarIdentityAmbiguity


class CalendarProjectionService:
    """Own generated read-only views, never an Apple Calendar event."""

    def __init__(self, vault: Path, reader: CalendarReader) -> None:
        self._vault = vault.expanduser().resolve()
        self._reader = reader
        self._ics_path = self._vault / APPLE_CALENDAR_ICS_RELATIVE_PATH
        self._markdown_directory = self._vault / APPLE_CALENDAR_EVENTS_RELATIVE_PATH
        self._dashboard_path = self._vault / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH
        self._person_identity_review_path = (
            self._vault / CALENDAR_PERSON_IDENTITY_REVIEW_RELATIVE_PATH
        )
        self._people = PersonService(self._vault)
        self._person_dashboard = PersonDashboardProjection(self._vault)

    def refresh(
        self,
        *,
        now: datetime | None = None,
        days_before: int | None = None,
        days_after: int = 90,
    ) -> CalendarProjectionResult:
        """Refresh a bounded private view that always retains the current month."""

        invalid_days_before = days_before is not None and not 0 <= days_before <= 31
        if invalid_days_before or not 1 <= days_after <= 366:
            raise WoonError("calendar projection range is outside the allowed window")
        current = now or datetime.now(_KST)
        if current.tzinfo is None or current.utcoffset() is None:
            raise WoonError("calendar projection requires a timezone-aware current time")
        if days_before is None:
            start_at = current.astimezone(_KST).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            start_at = current - timedelta(days=days_before)
        end_at = current + timedelta(days=days_after)
        events = self._reader.list_events(start_at=start_at, end_at=end_at)
        _validate_events(events, start_at, end_at)
        markdown_changed, identity_reviews = self._refresh_markdown(events)
        dashboard_changed = self._refresh_dashboard()
        person_dashboard_changed = self._person_dashboard.refresh().changed
        ics_changed = self._refresh_ics(events)
        review_changed = self._refresh_person_identity_review(identity_reviews)
        return CalendarProjectionResult(
            changed=(
                markdown_changed
                or dashboard_changed
                or person_dashboard_changed
                or ics_changed
                or review_changed
            ),
            event_count=len(events),
            relative_path=self._markdown_directory.relative_to(self._vault).as_posix(),
            ics_relative_path=APPLE_CALENDAR_ICS_RELATIVE_PATH,
            start_at=start_at,
            end_at=end_at,
        )

    def _refresh_markdown(
        self, events: tuple[CalendarProjectionEvent, ...]
    ) -> tuple[bool, tuple[CalendarPersonIdentityReview, ...]]:
        """Mirror summaries into a Core-owned directory that Obsidian cannot modify."""

        self._markdown_directory.mkdir(parents=True, exist_ok=True)
        self._markdown_directory.chmod(_PROJECTION_DIRECTORY_WRITABLE_MODE)
        try:
            existing = {path.name: path for path in self._markdown_directory.glob("*.md")}
            legacy_database = existing.pop(APPLE_CALENDAR_NOTION_DATABASE_FILENAME, None)
            unmanaged = [
                path.relative_to(self._vault).as_posix()
                for path in existing.values()
                if _MARKDOWN_MARKER not in path.read_text(encoding="utf-8")
            ]
            if legacy_database is not None and not is_core_calendar_notion_database(
                legacy_database
            ):
                unmanaged.append(legacy_database.relative_to(self._vault).as_posix())
            if unmanaged:
                raise WoonError(
                    "calendar Markdown projection directory contains an unmanaged note: "
                    + ", ".join(sorted(unmanaged))
                )

            changed = False
            identity_reviews: list[CalendarPersonIdentityReview] = []
            current_names: set[str] = set()
            # The conversation ledger intentionally does not retain opaque
            # EventKit IDs.  Consequently a same-day, same-title pair cannot
            # be identified safely from an explicit title context alone.  Do
            # not project a document link onto either event in that case.
            # This is stricter than guessing from time or filename and keeps
            # one conversation outcome from becoming two event histories.
            linkable_event_keys = _unique_document_link_event_keys(events)
            for event, name in _markdown_filenames(events):
                current_names.add(name)
                destination = self._markdown_directory / name
                resolution = self._people.calendar_title_resolution(event.title)
                identity_reviews.extend(_identity_reviews(event, resolution))
                document_links = (
                    calendar_document_links_for_event(
                        self._vault,
                        day=event.start_at.astimezone(_KST).date(),
                        event_title=event.title,
                    )
                    if _document_link_event_key(event) in linkable_event_keys
                    else ()
                )
                content = _render_markdown(
                    event,
                    self._calendar_people(event, resolution),
                    document_links,
                )
                if _write_projection_file(destination, content):
                    changed = True

            stale = [path for name, path in existing.items() if name not in current_names]
            if legacy_database is not None:
                stale.append(legacy_database)
            if stale:
                retired = self._retired_directory()
                retired.mkdir(parents=True, exist_ok=True)
                for path in stale:
                    os.replace(path, retired / path.name)
                changed = True
            return changed, tuple(identity_reviews)
        finally:
            self._markdown_directory.chmod(_PROJECTION_DIRECTORY_READONLY_MODE)

    def _refresh_dashboard(self) -> bool:
        """Expose the Core-owned Link Calendar month view as the user entrypoint."""

        if self._dashboard_path.exists() and not is_core_calendar_dashboard(self._dashboard_path):
            raise WoonError("calendar dashboard path is not a Core-generated projection")
        return _write_projection_file(self._dashboard_path, _render_dashboard())

    def _refresh_person_identity_review(
        self, reviews: tuple[CalendarPersonIdentityReview, ...]
    ) -> bool:
        """Expose unresolved duplicate names without writing an automatic person link."""

        if (
            self._person_identity_review_path.exists()
            and _PERSON_IDENTITY_REVIEW_MARKER
            not in self._person_identity_review_path.read_text(encoding="utf-8")
        ):
            raise WoonError(
                "calendar person identity review path is not a Core-generated projection"
            )
        if not reviews:
            if self._person_identity_review_path.exists():
                self._person_identity_review_path.unlink()
                return True
            return False
        return _write_projection_file(
            self._person_identity_review_path, _render_person_identity_review(reviews)
        )

    def _refresh_ics(self, events: tuple[CalendarProjectionEvent, ...]) -> bool:
        """Write the same minimized event summaries as a renderer-only ICS feed."""

        self._ics_path.parent.mkdir(parents=True, exist_ok=True)
        content = _render_ics(events)
        previous = self._ics_path.read_bytes() if self._ics_path.exists() else b""
        if previous and f"PRODID:{_ICS_PRODID}".encode() not in previous:
            raise WoonError("calendar ICS file is not a Core-generated projection")
        if previous != content:
            atomic_write(self._ics_path, content, mode=_PROJECTION_FILE_READONLY_MODE)
            return True
        if self._ics_path.stat().st_mode & 0o777 != _PROJECTION_FILE_READONLY_MODE:
            self._ics_path.chmod(_PROJECTION_FILE_READONLY_MODE)
            return True
        return False

    def _retired_directory(self) -> Path:
        """Keep a local rollback copy when a source event leaves the bounded window."""

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self._vault / ".local/woon-knowledge/calendar-projection/retired" / timestamp

    def _calendar_people(
        self, event: CalendarProjectionEvent, resolution: CalendarIdentityResolution
    ) -> tuple[tuple[CalendarPersonReference, str, str, str], ...]:
        """Link only user-confirmed identifiers; unresolved duplicates remain unlinked."""

        owner = self._people.default_owner_reference()
        selected: list[tuple[CalendarPersonReference, str, str, str]] = []
        if event.calendar_name == OWNED_CALENDAR_NAME and owner is not None:
            selected.append(
                (
                    owner,
                    "organizer",
                    "user-authorized-calendar-request",
                    "Woon 일정에 사용자 요청으로 저장된 일정",
                )
            )
        for match in resolution.matches:
            reference = match.reference
            if any(item[0].person_id == reference.person_id for item in selected):
                continue
            selected.append(
                (
                    reference,
                    "mentioned",
                    "user-confirmed-identifier-in-calendar-title",
                    (
                        f"일정 제목의 {match.identifier.value} 표기는 "
                        f"{reference.title}으로 사용자 확인됨"
                    ),
                )
            )
        return tuple(selected)


def _identity_reviews(
    event: CalendarProjectionEvent, resolution: CalendarIdentityResolution
) -> tuple[CalendarPersonIdentityReview, ...]:
    """Turn only duplicate exact matches into a visible, local-only review queue."""

    date = event.start_at.astimezone(_KST).date().isoformat()
    return tuple(
        CalendarPersonIdentityReview(date=date, title=event.title, ambiguity=ambiguity)
        for ambiguity in resolution.ambiguities
    )


def _validate_events(
    events: tuple[CalendarProjectionEvent, ...], start_at: datetime, end_at: datetime
) -> None:
    seen: set[tuple[str, datetime]] = set()
    for event in events:
        if not event.source_event_id or not event.calendar_name.strip() or not event.title.strip():
            raise WoonError("calendar projection event is missing a required summary field")
        if event.start_at.tzinfo is None or event.end_at.tzinfo is None:
            raise WoonError("calendar projection event timestamps must have a timezone")
        if event.end_at <= event.start_at:
            raise WoonError("calendar projection event end must follow its start")
        if event.category_id is not None and event.category_id not in CALENDAR_CATEGORY_IDS:
            raise WoonError("calendar projection event category is not configured")
        if event.end_at <= start_at or event.start_at >= end_at:
            raise WoonError("calendar reader returned an event outside the requested window")
        key = (event.source_event_id, event.start_at)
        if key in seen:
            raise WoonError("calendar reader returned a duplicate event occurrence")
        seen.add(key)


def _markdown_filenames(
    events: tuple[CalendarProjectionEvent, ...],
) -> tuple[tuple[CalendarProjectionEvent, str], ...]:
    """Use a readable title because Link Calendar renders the Markdown basename.

    A date suffix is added only when two projected events would otherwise have the
    same visible title.  The source event ID stays out of both the note name and body.
    """

    ordered = tuple(sorted(events, key=lambda item: (item.start_at, item.source_event_id)))
    grouped: dict[str, list[CalendarProjectionEvent]] = {}
    for event in ordered:
        grouped.setdefault(_calendar_filename_stem(event.title), []).append(event)

    names: list[tuple[CalendarProjectionEvent, str]] = []
    for stem, matching_events in grouped.items():
        if len(matching_events) == 1:
            names.append((matching_events[0], f"{stem}.md"))
            continue
        dated: dict[str, list[CalendarProjectionEvent]] = {}
        for event in matching_events:
            date = event.start_at.astimezone(_KST).date().isoformat()
            dated.setdefault(date, []).append(event)
        for date, date_events in dated.items():
            if len(date_events) == 1:
                names.append((date_events[0], f"{stem} · {date}.md"))
                continue
            for index, event in enumerate(date_events, start=1):
                names.append((event, f"{stem} · {date} · {index}.md"))
    return tuple(names)


def _calendar_filename_stem(title: str) -> str:
    """Keep card labels readable while avoiding path separators and hidden files."""

    replacements = str.maketrans(
        {
            "/": "／",
            "\\": "＼",
            ":": "：",
            "*": "＊",
            "?": "？",
            '"': "＂",
            "<": "＜",
            ">": "＞",
            "|": "｜",
        }
    )
    stem = " ".join(title.translate(replacements).split()).strip(". ") or "일정"
    # Common filesystems limit one path component to 255 bytes, not Unicode
    # code points.  Leave room for the duplicate date/index suffix and `.md`
    # while retaining as much of the human-readable Korean title as possible.
    encoded = stem.encode("utf-8")
    if len(encoded) <= 180:
        return stem
    return encoded[:180].decode("utf-8", errors="ignore").rstrip(". ") or "일정"


def _document_link_event_key(event: CalendarProjectionEvent) -> tuple[str, str]:
    """Return the human-safe identity available to conversation contexts."""

    day = event.start_at.astimezone(_KST).date().isoformat()
    normalized_title = " ".join(event.title.split())
    return (day, normalized_title)


def _unique_document_link_event_keys(
    events: tuple[CalendarProjectionEvent, ...],
) -> frozenset[tuple[str, str]]:
    """Keep links off duplicate visible Calendar occurrences.

    The ledger deliberately stores no EventKit identifiers, so its strongest
    safe match is one exact KST-day/title pair.  Requiring that pair to occur
    once in the current projection prevents silent fan-out to duplicate events.
    """

    counts: dict[tuple[str, str], int] = {}
    for event in events:
        key = _document_link_event_key(event)
        counts[key] = counts.get(key, 0) + 1
    return frozenset(key for key, count in counts.items() if count == 1)


def _render_markdown(
    event: CalendarProjectionEvent,
    people: tuple[tuple[CalendarPersonReference, str, str, str], ...],
    document_links: tuple[CalendarDocumentLink, ...],
) -> str:
    """Render searchable event summaries without exposing an EventKit identifier."""

    lines = [
        "---",
        "type: calendar-event",
        f"title: {_yaml_string(event.title)}",
        "publish: false",
        "access: local-only",
        "status: Generated",
        f"source: {_MARKDOWN_PROJECTION}-readonly",
        f"calendar: {_yaml_string(event.calendar_name)}",
        "record_owner: choi-woonyoung",
        f"Date: {_yaml_string(event.start_at.astimezone(_KST).date().isoformat())}",
        f"Time: {_yaml_string(_event_time_label(event))}",
        f"Category: {_yaml_string(calendar_category_title(event.category_id))}",
        f"Category ID: {_yaml_string(event.category_id or UNCATEGORIZED_CALENDAR_CATEGORY_ID)}",
    ]
    if people:
        lines.append("people:")
        lines.extend(f"  - {_yaml_string(reference.link)}" for reference, *_ in people)
        lines.append("person_roles:")
        for reference, role, basis, evidence in people:
            lines.extend(
                (
                    f"  - person: {_yaml_string(reference.link)}",
                    f"    role: {_yaml_string(role)}",
                    f"    basis: {_yaml_string(basis)}",
                    f"    evidence: {_yaml_string(evidence)}",
                )
            )
    if event.all_day:
        lines.extend(("All Day: true",))
    else:
        lines.extend(
            (
                f"Start Date: {_yaml_string(event.start_at.astimezone(_KST).isoformat())}",
                f"End Date: {_yaml_string(event.end_at.astimezone(_KST).isoformat())}",
                "All Day: false",
            )
        )
    lines.extend(
        (
            _MARKDOWN_MARKER.rstrip(),
            "---",
            "",
            f"# {event.title}",
            "",
            "Apple Calendar에서 관리하는 보기 전용 일정입니다.",
            "",
            f"- 날짜: {_format_korean_date(event.start_at)}",
            _event_time_summary(event),
            "",
            "일정 변경이 필요하면 Apple Calendar에서 수정합니다.",
        )
    )
    if document_links:
        lines.extend(
            (
                "",
                "## 관련 문서",
                "",
                "대화 지식화에서 이 일정과 직접 함께 확인된 문서입니다.",
                "",
            )
        )
        for link in document_links:
            lines.append(
                f"- [[{link.relative_path[:-3]}|{link.title}]] · {', '.join(link.reasons)}"
            )
    return "\n".join(lines) + "\n"


def _render_dashboard() -> str:
    """Render the Link Calendar entrypoint for the managed read-only profile."""

    return (
        "\n".join(
            (
                "---",
                "type: calendar-dashboard",
                "title: Apple Calendar",
                "publish: false",
                "access: local-only",
                "status: Generated",
                "source: apple-calendar-readonly",
                "woon_projection: apple-calendar-dashboard",
                f"cssclasses: {LINK_CALENDAR_DASHBOARD_CSS_CLASS}",
                "---",
                "",
                f"```{LINK_CALENDAR_PLUGIN_ID}",
                f"profile: {LINK_CALENDAR_PROFILE_ID}",
                "```",
            )
        )
        + "\n"
    )


def _render_person_identity_review(reviews: tuple[CalendarPersonIdentityReview, ...]) -> str:
    """Render a deterministic review document without selecting a candidate itself."""

    lines = [
        "---",
        "type: calendar-person-identity-review",
        'title: "Calendar 인물 식별 검토"',
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Generated",
        "source: apple-calendar-readonly",
        _PERSON_IDENTITY_REVIEW_MARKER.rstrip(),
        "people: []",
        "person_roles: []",
        "---",
        "",
        "# Calendar 인물 식별 검토",
        "",
        "일정 제목의 표기가 둘 이상의 등록 인물을 가리켜 자동 연결하지 않은 목록입니다.",
        (
            "사용자가 누구인지 지정하면 그 카드의 `identifiers`에 제목 맥락을 함께 등록한 뒤 "
            "다음 새로 고침부터 연결합니다."
        ),
        "",
    ]
    if not reviews:
        lines.extend(("- 현재 검토할 동명이 일정이 없습니다.", ""))
        return "\n".join(lines)

    for review in sorted(
        reviews,
        key=lambda item: (item.date, item.title, item.ambiguity.identifier),
    ):
        lines.extend(
            (
                f"## {review.date} · {review.title}",
                "",
                f"- 제목 표기: `{review.ambiguity.identifier}`",
                "- 후보:",
            )
        )
        for candidate in review.ambiguity.candidates:
            lines.append(f"  - {candidate.link} (`{candidate.person_id}`)")
        lines.extend(
            (
                (
                    "- 다음 조치: 사용자에게 이 제목의 표기가 어느 사람인지 확인한 뒤, "
                    "필요하면 구분할 제목 단어를 함께 등록한다."
                ),
                "",
            )
        )
    return "\n".join(lines)


def _write_projection_file(path: Path, content: str) -> bool:
    """Atomically refresh one Core-owned local-only file and restore its read-only mode."""

    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if previous != content:
        atomic_write(path, content.encode("utf-8"), mode=_PROJECTION_FILE_READONLY_MODE)
        return True
    if path.stat().st_mode & 0o777 != _PROJECTION_FILE_READONLY_MODE:
        path.chmod(_PROJECTION_FILE_READONLY_MODE)
        return True
    return False


def is_core_calendar_notion_database(path: Path) -> bool:
    """Return whether a Notion Bases schema belongs to the Core projection."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        "notion-bases: true\n" in content
        and "woon_projection: apple-calendar-notion-bases\n" in content
    )


def is_core_calendar_dashboard(path: Path) -> bool:
    """Return whether the visible dashboard belongs to the Core calendar projection."""

    try:
        return _DASHBOARD_MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _format_korean_date(value: datetime) -> str:
    localized = value.astimezone(_KST)
    return f"{localized.year}년 {localized.month}월 {localized.day}일"


def _event_time_summary(event: CalendarProjectionEvent) -> str:
    if event.all_day:
        return "- 시간: 하루 종일"
    return f"- 시간: {_event_time_label(event)}"


def _event_time_label(event: CalendarProjectionEvent) -> str:
    """Return one compact human-facing time value for Obsidian projections."""

    if event.all_day:
        return "하루 종일"
    start = event.start_at.astimezone(_KST)
    end = event.end_at.astimezone(_KST)
    return f"{_format_korean_time(start)} - {_format_korean_time(end)}"


def _format_korean_time(value: datetime) -> str:
    period = "오전" if value.hour < 12 else "오후"
    hour = value.hour % 12 or 12
    return f"{period} {hour}:{value:%M}"


def _yaml_string(value: str) -> str:
    """JSON strings are valid YAML scalars and preserve Korean punctuation safely."""

    return json.dumps(value, ensure_ascii=False)


def _render_ics(events: tuple[CalendarProjectionEvent, ...]) -> bytes:
    """Render a deterministic, privacy-minimized RFC 5545 calendar feed."""

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"PRODID:{_ICS_PRODID}",
        "X-WR-CALNAME:Apple Calendar",
        "X-WR-TIMEZONE:Asia/Seoul",
    ]
    for event in sorted(events, key=lambda item: (item.start_at, item.source_event_id)):
        lines.extend(_render_ics_event(event))
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold_ics_line(line) for line in lines) + "\r\n").encode("utf-8")


def _render_ics_event(event: CalendarProjectionEvent) -> tuple[str, ...]:
    """Keep only identity-free title and schedule fields in each ICS event."""

    uid = hashlib.sha256(
        f"{event.source_event_id}\0{event.start_at.isoformat()}".encode()
    ).hexdigest()
    lines = ["BEGIN:VEVENT", f"UID:{uid}@woon.local"]
    if event.all_day:
        start_date = event.start_at.astimezone(_KST).date()
        end_date = event.end_at.astimezone(_KST).date()
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        lines.extend(
            (
                f"DTSTAMP:{_ics_timestamp(event.start_at)}",
                f"DTSTART;VALUE=DATE:{start_date:%Y%m%d}",
                f"DTEND;VALUE=DATE:{end_date:%Y%m%d}",
            )
        )
    else:
        lines.extend(
            (
                f"DTSTAMP:{_ics_timestamp(event.start_at)}",
                f"DTSTART:{_ics_timestamp(event.start_at)}",
                f"DTEND:{_ics_timestamp(event.end_at)}",
            )
        )
    lines.extend((f"SUMMARY:{_ics_escape(event.title)}", "END:VEVENT"))
    return tuple(lines)


def _ics_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_ics_line(value: str) -> str:
    """Fold long UTF-8 content lines without splitting a Korean code point."""

    chunks: list[str] = []
    current = ""
    limit = 75
    for character in value:
        if current and len((current + character).encode("utf-8")) > limit:
            chunks.append(current)
            current = character
            limit = 74  # Continuation lines begin with one ASCII space.
        else:
            current += character
    if current or not chunks:
        chunks.append(current)
    return "\r\n ".join(chunks)
