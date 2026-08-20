from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from woon_core.calendar.manual_schedule import (
    CalendarCategoryUpdateReceipt,
    UserScheduleRequest,
)
from woon_core.calendar.projection import CalendarProjectionResult
from woon_core.knowledge.schedule_bridge import ScheduleReceipt
from woon_core.tasks import mcp_server


def test_tasks_mcp_server_exposes_the_explicit_calendar_write_tool(tmp_path: Path) -> None:
    async def exercise() -> None:
        environment = dict(os.environ)
        environment["WOON_KNOWLEDGE_ROOT"] = str(tmp_path)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "woon_core.tasks.mcp_server"],
            env=environment,
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "woon_calendar_refresh_readonly",
                "woon_calendar_upsert",
                "woon_calendar_set_category",
            }.issubset(names)

    anyio.run(exercise)


def test_calendar_upsert_uses_the_user_authorized_bridge_then_refreshes_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def apply(vault: Path, request: object) -> ScheduleReceipt:
        captured["vault"] = vault
        captured["request"] = request
        return ScheduleReceipt(
            candidate_id="user-calendar:sample-event",
            lifecycle="create",
            idempotency_key="user-calendar:sample-event",
            calendar_event_id="event-001",
        )

    class ProjectionService:
        def refresh(self) -> CalendarProjectionResult:
            return CalendarProjectionResult(
                changed=True,
                event_count=1,
                relative_path="inbox/calendar/events",
                ics_relative_path="inbox/calendar/apple-calendar.ics",
                start_at=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
            )

    monkeypatch.setattr(mcp_server, "resolve_knowledge_vault", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "apply_user_authorized_schedule", apply)
    monkeypatch.setattr(
        mcp_server, "build_calendar_projection_service", lambda vault: ProjectionService()
    )

    result = mcp_server.upsert_calendar_event(
        event_id="sample-event",
        title="크래프톤 정글 입소식",
        start_at="2026-08-18T13:00:00+09:00",
        end_at=None,
        category_id="learning",
        location="정글 스테이지",
        notes="노트북을 챙긴다.",
        display_category=False,
        user_authorized=True,
    )

    request = cast(UserScheduleRequest, captured["request"])
    assert captured["vault"] == tmp_path
    assert request.event_id == "sample-event"
    assert request.end_at == datetime.fromisoformat("2026-08-18T14:00:00+09:00")
    assert result["status"] == "ok"
    assert result["duration_defaulted"] is True
    assert result["receipt"] == {
        "candidate_id": "user-calendar:sample-event",
        "lifecycle": "create",
        "idempotency_key": "user-calendar:sample-event",
        "calendar_event_id": "event-001",
    }
    assert result["projection"]["ics_relative_path"] == "inbox/calendar/apple-calendar.ics"


def test_calendar_upsert_rejects_missing_user_authorization_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "apply_user_authorized_schedule",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    with pytest.raises(ValueError, match="explicit user authorization"):
        mcp_server.upsert_calendar_event(
            event_id="sample-event",
            title="일정",
            start_at="2026-08-18T13:00:00+09:00",
            category_id="life",
            user_authorized=False,
        )


def test_calendar_set_category_preserves_event_content_then_refreshes_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class ProjectionService:
        def refresh(self) -> CalendarProjectionResult:
            return CalendarProjectionResult(
                changed=True,
                event_count=1,
                relative_path="inbox/calendar/events",
                ics_relative_path="inbox/calendar/apple-calendar.ics",
                start_at=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 11, 16, 0, 0, tzinfo=UTC),
            )

    def update(vault: Path, **kwargs: object) -> object:
        captured["vault"] = vault
        captured.update(kwargs)
        return CalendarCategoryUpdateReceipt(
            event_id="minjeong-interview-dropoff-2026-08-19",
            category_id="relationship",
            calendar_event_id="event-001",
            authorized_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
        )

    monkeypatch.setattr(mcp_server, "resolve_knowledge_vault", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "update_user_authorized_schedule_category", update)
    monkeypatch.setattr(
        mcp_server, "build_calendar_projection_service", lambda vault: ProjectionService()
    )

    result = mcp_server.set_calendar_category(
        event_id="minjeong-interview-dropoff-2026-08-19",
        category_id="relationship",
        user_authorized=True,
    )

    assert captured == {
        "vault": tmp_path,
        "event_id": "minjeong-interview-dropoff-2026-08-19",
        "category_id": "relationship",
    }
    assert result["status"] == "ok"
    assert result["receipt"]["category_id"] == "relationship"


def test_calendar_set_category_rejects_missing_user_authorization_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "update_user_authorized_schedule_category",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not write")),
    )

    with pytest.raises(ValueError, match="explicit user authorization"):
        mcp_server.set_calendar_category(
            event_id="minjeong-interview-dropoff-2026-08-19",
            category_id="relationship",
            user_authorized=False,
        )
