"""Local stdio MCP server for Woon's Obsidian task workflow."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from woon_core.calendar.factory import build_calendar_projection_service
from woon_core.calendar.manual_schedule import (
    UserScheduleRequest,
    apply_user_authorized_schedule,
    update_user_authorized_schedule_category,
)
from woon_core.errors import WoonError
from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.tasks.factory import build_task_service
from woon_core.tasks.service import TaskService

FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "Woon Obsidian Tasks",
    instructions=(
        "Manage the user's private Markdown task sources and daily task blocks. "
        "Use a stated purpose before creating a routine, materialize before completion, "
        "and never operate a graphical task application or an external task database. "
        "Create or update Apple Calendar events only through woon_calendar_upsert after "
        "the user has explicitly supplied the appointment details and authorization. "
        "Correct an existing Woon event category only through woon_calendar_set_category "
        "after direct user authorization."
    ),
    json_response=True,
)


@lru_cache(maxsize=1)
def _service() -> TaskService:
    return build_task_service()


@mcp.tool(
    name="woon_tasks_find",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def find_tasks(query: str, day: str | None = None) -> dict[str, object]:
    """Find matching routines and today's completion state without changing Markdown."""

    target_day = _parse_day(day) if day else None
    tasks = _service().find(query, on_date=target_day)
    return {"query": query, "count": len(tasks), "tasks": [asdict(task) for task in tasks]}


@mcp.tool(
    name="woon_tasks_upsert_recurring_todo",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_recurring_todo(
    task_id: str,
    title: str,
    purpose: str,
    area: str,
    start_date: str | None = None,
    goal_id: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Create or update a daily Markdown routine. purpose is required, never inferred."""

    result = _service().upsert_recurring_todo(
        task_id=task_id,
        title=title,
        purpose=purpose,
        area=area,
        start_date=_parse_day(start_date) if start_date else None,
        goal_id=goal_id,
        expected_revision=expected_revision,
    )
    return {"created": result.created, "changed": result.changed, "routine": asdict(result.routine)}


@mcp.tool(
    name="woon_tasks_upsert_goal",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_goal(
    goal_id: str,
    title: str,
    purpose: str,
    completion_condition: str,
    end_date: str | None = None,
    current_value: float | None = None,
    target_value: float | None = None,
    target_operator: str | None = None,
    unit: str | None = None,
    measurement_confirmed: bool = False,
    status: str = "active",
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Create or update a user-editable daily-routine goal and its stop condition."""

    result = _service().upsert_goal(
        goal_id=goal_id,
        title=title,
        purpose=purpose,
        completion_condition=completion_condition,
        end_date=_parse_day(end_date) if end_date else None,
        current_value=current_value,
        target_value=target_value,
        target_operator=target_operator,
        unit=unit,
        measurement_confirmed=measurement_confirmed,
        status=status,
        expected_revision=expected_revision,
    )
    return {"created": result.created, "changed": result.changed, "goal": asdict(result.goal)}


@mcp.tool(
    name="woon_tasks_materialize_due",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def materialize_due(day: str | None = None) -> dict[str, object]:
    """Create the requested day's note once and refresh only its managed task block."""

    result = _service().materialize_due(on_date=_parse_day(day) if day else None)
    return asdict(result)


@mcp.tool(
    name="woon_tasks_complete",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def complete_task(task_id: str, day: str | None = None) -> dict[str, object]:
    """Mark exactly one already-materialized task complete in its daily Markdown note."""

    result = _service().complete(task_id=task_id, on_date=_parse_day(day) if day else None)
    return asdict(result)


@mcp.tool(
    name="woon_calendar_refresh_readonly",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def refresh_calendar_readonly() -> dict[str, object]:
    """Refresh the private local Markdown view without changing calendar events."""

    return asdict(build_calendar_projection_service().refresh())


@mcp.tool(
    name="woon_calendar_upsert",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_calendar_event(
    event_id: str,
    title: str,
    start_at: str,
    category_id: str,
    user_authorized: bool,
    end_at: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    display_category: bool = True,
) -> dict[str, object]:
    """Apply one explicit Apple Calendar request, verify EventKit, then refresh local views.

    ``user_authorized`` must be true only for a direct user instruction. ``event_id`` is a
    stable, lowercase identifier retained for later updates; it is not an Apple event ID.
    When ``end_at`` is omitted, the tool uses a one-hour appointment and reports that default.
    """

    if user_authorized is not True:
        raise ValueError("calendar writes require explicit user authorization")
    parsed_start = _parse_datetime(start_at, "start_at")
    if end_at is None:
        defaulted_end = True
        parsed_end = parsed_start + timedelta(hours=1)
    else:
        defaulted_end = False
        parsed_end = _parse_datetime(end_at, "end_at")
    vault = resolve_knowledge_vault()
    receipt = apply_user_authorized_schedule(
        vault,
        UserScheduleRequest(
            event_id=event_id,
            title=title,
            start_at=parsed_start,
            end_at=parsed_end,
            category_id=category_id,
            location=location,
            notes=notes,
            display_category=display_category,
        ),
    )
    try:
        projection = build_calendar_projection_service(vault).refresh()
    except WoonError as error:
        # The EventKit receipt is durable. Do not report the UI projection as complete when
        # the subsequent read-only export did not succeed.
        return {
            "status": "applied_projection_pending",
            "receipt": asdict(receipt),
            "duration_defaulted": defaulted_end,
            "projection_error": str(error),
        }
    return {
        "status": "ok",
        "receipt": asdict(receipt),
        "duration_defaulted": defaulted_end,
        "projection": asdict(projection),
    }


@mcp.tool(
    name="woon_calendar_set_category",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def set_calendar_category(
    event_id: str,
    category_id: str,
    user_authorized: bool,
) -> dict[str, object]:
    """Correct a receipt-proven Woon event category while preserving event content."""

    if user_authorized is not True:
        raise ValueError("calendar writes require explicit user authorization")
    vault = resolve_knowledge_vault()
    receipt = update_user_authorized_schedule_category(
        vault,
        event_id=event_id,
        category_id=category_id,
    )
    try:
        projection = build_calendar_projection_service(vault).refresh()
    except WoonError as error:
        return {
            "status": "applied_projection_pending",
            "receipt": asdict(receipt),
            "projection_error": str(error),
        }
    return {
        "status": "ok",
        "receipt": asdict(receipt),
        "projection": asdict(projection),
    }


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("day must use YYYY-MM-DD") from error


def _parse_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field} must use ISO8601 with a timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must use ISO8601 with a timezone")
    return parsed


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
