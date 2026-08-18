"""Composition root for the local Apple Calendar projection."""

from __future__ import annotations

from pathlib import Path

from woon_core.calendar.migration import LegacyCalendarMigrationResult
from woon_core.calendar.projection import CalendarProjectionService
from woon_core.knowledge.factory import resolve_knowledge_vault


def build_calendar_projection_service(vault: Path | None = None) -> CalendarProjectionService:
    """Connect the EventKit reader only when a local calendar refresh is requested."""

    from woon_core.knowledge.macos_schedule_adapters import MacOSCalendarReader

    return CalendarProjectionService(vault or resolve_knowledge_vault(), MacOSCalendarReader())


def migrate_legacy_owned_calendar(
    vault: Path | None = None,
) -> LegacyCalendarMigrationResult:
    """Retire the old local receipt schema and its receipt-proven calendar name."""

    from woon_core.calendar.migration import migrate_legacy_schedule_state
    from woon_core.knowledge.macos_schedule_adapters import MacOSCalendarPort

    return migrate_legacy_schedule_state(
        vault or resolve_knowledge_vault(),
        MacOSCalendarPort(),
    )
