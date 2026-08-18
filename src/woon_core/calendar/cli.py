"""Command-line entry point helpers for read-only calendar projection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TextIO

from woon_core.calendar.factory import (
    build_calendar_projection_service,
    migrate_legacy_owned_calendar,
)
from woon_core.calendar.manual_schedule import UserScheduleRequest, apply_user_authorized_schedule
from woon_core.errors import WoonError
from woon_core.knowledge.factory import resolve_knowledge_vault


def run_calendar(arguments: list[str], output: TextIO) -> None:
    """Refresh the projection or make an explicitly authorized Woon calendar change."""

    if not arguments:
        raise WoonError("usage: woon calendar <refresh|migrate-legacy|upsert> [options]")
    command, *options = arguments
    if command not in {"refresh", "migrate-legacy", "upsert"}:
        raise WoonError(f"unknown calendar command {command!r}")
    if command == "upsert":
        _run_upsert(options, output)
        return
    vault: Path | None = None
    if options:
        if len(options) != 2 or options[0] != "--vault":
            raise WoonError(f"calendar {command} accepts only optional --vault <path>")
        vault = Path(options[1]).expanduser()
    if command == "migrate-legacy":
        migration = migrate_legacy_owned_calendar(vault)
        output.write(
            f"status: ok\nmigrated: {str(migration.migrated).lower()}\n"
            f"calendar_name: {migration.calendar_name}\n"
        )
        if migration.calendar_event_id:
            output.write(f"calendar_event_id: {migration.calendar_event_id}\n")
        return
    result = build_calendar_projection_service(vault).refresh()
    output.write(
        f"status: ok\nchanged: {str(result.changed).lower()}\n"
        f"events: {result.event_count}\ncalendar_markdown: {result.relative_path}\n"
        f"calendar_ics: {result.ics_relative_path}\n"
    )


def _run_upsert(options: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    allowed = {
        "--id",
        "--title",
        "--start",
        "--end",
        "--category",
        "--location",
        "--notes",
        "--display-category",
        "--vault",
    }
    index = 0
    while index < len(options):
        option = options[index]
        if option not in allowed or option in values or index + 1 >= len(options):
            raise WoonError(
                "usage: woon calendar upsert --id <stable-id> --title <text> "
                "--start <ISO8601> --end <ISO8601> --category <category> "
                "[--location <text>] [--notes <text>] [--display-category <true|false>] "
                "[--vault <path>]"
            )
        values[option] = options[index + 1]
        index += 2
    required = {"--id", "--title", "--start", "--end", "--category"}
    if required.difference(values):
        raise WoonError("calendar upsert is missing a required option")
    start_at = _datetime(values["--start"], "--start")
    end_at = _datetime(values["--end"], "--end")
    vault = Path(values["--vault"]).expanduser() if "--vault" in values else None
    request = UserScheduleRequest(
        event_id=values["--id"],
        title=values["--title"],
        start_at=start_at,
        end_at=end_at,
        category_id=values["--category"],
        location=values.get("--location"),
        notes=values.get("--notes"),
        display_category=_boolean(values.get("--display-category", "true")),
    )
    receipt = apply_user_authorized_schedule(vault or resolve_knowledge_vault(), request)
    output.write(
        "status: ok\n"
        f"lifecycle: {receipt.lifecycle}\n"
        f"idempotency_key: {receipt.idempotency_key}\n"
        f"calendar_event_id: {receipt.calendar_event_id}\n"
    )


def _datetime(value: str, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WoonError(f"{option} must use ISO8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WoonError(f"{option} must include a timezone")
    return parsed


def _boolean(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise WoonError("--display-category must be true or false")
