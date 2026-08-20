from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from woon_core.calendar.projection import (
    APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH,
    APPLE_CALENDAR_ICS_RELATIVE_PATH,
    CALENDAR_PERSON_IDENTITY_REVIEW_RELATIVE_PATH,
    CalendarProjectionEvent,
    CalendarProjectionService,
)
from woon_core.errors import WoonError


class FakeCalendarReader:
    def __init__(self, events: tuple[CalendarProjectionEvent, ...]) -> None:
        self.events = events
        self.calls: list[tuple[datetime, datetime]] = []

    def list_events(
        self, *, start_at: datetime, end_at: datetime
    ) -> tuple[CalendarProjectionEvent, ...]:
        self.calls.append((start_at, end_at))
        return self.events


def _event(**changes: object) -> CalendarProjectionEvent:
    values: dict[str, object] = {
        "source_event_id": "opaque-event-001",
        "calendar_name": "개인",
        "title": "러닝 약속, 공원",
        "start_at": datetime(2026, 8, 17, 1, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 17, 2, 0, tzinfo=UTC),
        "all_day": False,
    }
    values.update(changes)
    return CalendarProjectionEvent(**values)


def _write_person_card(
    vault: Path,
    *,
    person_id: str,
    title: str,
    person_scope: str = "general",
    identifiers: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> None:
    path = vault / "users" / person_id / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    identifier_lines = ""
    for value, context_terms in identifiers:
        identifier_lines += "identifiers:\n" if not identifier_lines else ""
        identifier_lines += (
            f"  - value: {value}\n"
            "    basis: user-confirmed\n"
            "    evidence: 사용자 직접 확인\n"
            f"    context_terms: {list(context_terms)}\n"
        )
    path.write_text(
        "---\n"
        f"title: {title}\n"
        "entity_type: person\n"
        f"person_id: {person_id}\n"
        "person_kind: related-person\n"
        f"person_scope: {person_scope}\n"
        "relationship_to_owner: 테스트\n"
        f"{identifier_lines}"
        "---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )


def test_refresh_writes_only_approved_event_summary_fields(tmp_path: Path) -> None:
    reader = FakeCalendarReader((_event(),))
    service = CalendarProjectionService(tmp_path, reader)

    first = service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    second = service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    markdown_files = [
        path
        for path in (tmp_path / first.relative_path).glob("*.md")
        if path.name != "_database.md"
    ]
    markdown = markdown_files[0].read_text(encoding="utf-8")
    ics_path = tmp_path / first.ics_relative_path
    ics = ics_path.read_bytes()

    assert first.changed is True
    assert second.changed is False
    assert first.event_count == 1
    assert first.relative_path == "inbox/calendar/events"
    assert first.ics_relative_path == APPLE_CALENDAR_ICS_RELATIVE_PATH
    assert len(markdown_files) == 1
    assert markdown_files[0].name == "러닝 약속, 공원.md"
    assert 'title: "러닝 약속, 공원"' in markdown
    assert "publish: false" in markdown
    assert "access: local-only" in markdown
    assert "status: Generated" in markdown
    assert 'Start Date: "2026-08-17T10:00:00+09:00"' in markdown
    assert 'End Date: "2026-08-17T11:00:00+09:00"' in markdown
    assert "All Day: false" in markdown
    assert "woon_projection: apple-calendar" in markdown
    assert 'Date: "2026-08-17"' in markdown
    assert 'Category: "기타"' in markdown
    assert 'Category ID: "other"' in markdown
    assert "- 시간: 오전 10:00 - 오전 11:00" in markdown
    assert "opaque-event-001" not in markdown
    assert (markdown_files[0].stat().st_mode & 0o777) == 0o400
    assert (tmp_path / first.relative_path).stat().st_mode & 0o777 == 0o500
    assert b"BEGIN:VCALENDAR\r\n" in ics
    assert b"PRODID:-//Woon//Apple Calendar Read-only Projection//KO\r\n" in ics
    assert b"DTSTART:20260817T010000Z\r\n" in ics
    assert b"DTEND:20260817T020000Z\r\n" in ics
    expected_summary = "SUMMARY:러닝 약속\\, 공원\r\n".encode()
    assert expected_summary in ics
    assert b"opaque-event-001" not in ics
    assert b"\xea\xb0\x9c\xec\x9d\xb8" not in ics
    assert (ics_path.stat().st_mode & 0o777) == 0o400
    assert not (tmp_path / "inbox/calendar/events/_database.md").exists()
    dashboard = (tmp_path / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "cssclasses: woon-simple-calendar-dashboard" in dashboard
    assert "```woon-simple-calendar" in dashboard
    assert "source: inbox/calendar/events" in dashboard
    assert "category_id_field: Category ID" in dashboard
    assert (tmp_path / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH).stat().st_mode & 0o777 == 0o400


def test_refresh_does_not_rewrite_an_unchanged_projection_when_time_moves(tmp_path: Path) -> None:
    reader = FakeCalendarReader((_event(),))
    service = CalendarProjectionService(tmp_path, reader)

    first = service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    second = service.refresh(now=datetime(2026, 8, 17, 10, 0, tzinfo=UTC))

    assert first.changed is True
    assert second.changed is False


def test_refresh_uses_a_date_suffix_only_for_duplicate_calendar_titles(tmp_path: Path) -> None:
    reader = FakeCalendarReader(
        (
            _event(source_event_id="first", title="주간 회의"),
            _event(
                source_event_id="second",
                title="주간 회의",
                start_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
                end_at=datetime(2026, 8, 18, 2, 0, tzinfo=UTC),
            ),
            _event(source_event_id="third", title="메모/검토: 초안"),
        )
    )

    result = CalendarProjectionService(tmp_path, reader).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    names = {
        path.name
        for path in (tmp_path / result.relative_path).glob("*.md")
        if path.name != "_database.md"
    }
    assert names == {
        "주간 회의 · 2026-08-17.md",
        "주간 회의 · 2026-08-18.md",
        "메모／검토： 초안.md",
    }


def test_refresh_renders_all_day_events_for_markdown_and_ics(tmp_path: Path) -> None:
    reader = FakeCalendarReader(
        (
            _event(
                start_at=datetime(2026, 8, 16, 15, 0, tzinfo=UTC),
                end_at=datetime(2026, 8, 17, 15, 0, tzinfo=UTC),
                all_day=True,
            ),
        )
    )
    result = CalendarProjectionService(tmp_path, reader).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    markdown = next(
        path
        for path in (tmp_path / result.relative_path).glob("*.md")
        if path.name != "_database.md"
    ).read_text(encoding="utf-8")

    assert 'Date: "2026-08-17"' in markdown
    assert "All Day: true" in markdown
    assert "Start Date:" not in markdown
    assert "End Date:" not in markdown
    ics = (tmp_path / result.ics_relative_path).read_text(encoding="utf-8")
    assert "DTSTART;VALUE=DATE:20260817" in ics
    assert "DTEND;VALUE=DATE:20260818" in ics


def test_refresh_retires_only_stale_core_owned_markdown_notes(tmp_path: Path) -> None:
    reader = FakeCalendarReader((_event(),))
    service = CalendarProjectionService(tmp_path, reader)
    service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    reader.events = ()

    result = service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))

    assert result.changed is True
    assert not [
        path
        for path in (tmp_path / result.relative_path).glob("*.md")
        if path.name != "_database.md"
    ]
    retired = list((tmp_path / ".local/woon-knowledge/calendar-projection/retired").rglob("*.md"))
    assert len(retired) == 1


def test_refresh_retires_the_known_legacy_notion_bases_database(tmp_path: Path) -> None:
    events = tmp_path / "inbox/calendar/events"
    events.mkdir(parents=True)
    legacy = events / "_database.md"
    legacy.write_text(
        "---\nnotion-bases: true\nwoon_projection: apple-calendar-notion-bases\n---\n",
        encoding="utf-8",
    )

    CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    assert not legacy.exists()
    retired_root = tmp_path / ".local/woon-knowledge/calendar-projection/retired"
    assert list(retired_root.rglob("_database.md"))


def test_refresh_projects_an_explicit_calendar_category_without_changing_ics(
    tmp_path: Path,
) -> None:
    result = CalendarProjectionService(
        tmp_path, FakeCalendarReader((_event(category_id="learning"),))
    ).refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))

    markdown = next((tmp_path / result.relative_path).glob("*.md")).read_text(encoding="utf-8")
    assert 'Category: "학습"' in markdown
    assert 'Category ID: "learning"' in markdown
    assert b"CATEGORY" not in (tmp_path / result.ics_relative_path).read_bytes()


def test_refresh_renders_explicit_conversation_document_links_for_exact_event_context(
    tmp_path: Path,
) -> None:
    document = tmp_path / "brain/wiki/일정-준비-원칙.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 큐 설계\n", encoding="utf-8")
    ledger = tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-17/context.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "kind": "학습",
                "title": "일정 준비 원칙",
                "summary": "일정 준비와 함께 문서를 만들었다.",
                "related_documents": [],
                "calendar_contexts": [
                    {
                        "event_day": "2026-08-17",
                        "event_title": "러닝 약속, 공원",
                        "related_documents": [],
                        "reason": "준비",
                        "include_generated_growth_page": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    markdown = next((tmp_path / result.relative_path).glob("*.md")).read_text(encoding="utf-8")
    assert "## 관련 문서" in markdown
    assert "[[brain/wiki/일정-준비-원칙|큐 설계]] · 준비" in markdown


def test_refresh_does_not_link_same_day_context_with_a_different_event_title(
    tmp_path: Path,
) -> None:
    document = tmp_path / "brain/wiki/queue-design.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 큐 설계\n", encoding="utf-8")
    ledger = tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-17/context.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "kind": "결정",
                "title": "학습 순서를 정한다",
                "summary": "다른 일정의 준비 문서다.",
                "related_documents": ["brain/wiki/queue-design.md"],
                "calendar_contexts": [
                    {
                        "event_day": "2026-08-17",
                        "event_title": "다른 학습 약속",
                        "related_documents": ["brain/wiki/queue-design.md"],
                        "reason": "준비",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    markdown = next((tmp_path / result.relative_path).glob("*.md")).read_text(encoding="utf-8")
    assert "## 관련 문서" not in markdown


def test_refresh_does_not_fan_out_context_to_duplicate_same_day_event_titles(
    tmp_path: Path,
) -> None:
    document = tmp_path / "brain/wiki/queue-design.md"
    document.parent.mkdir(parents=True)
    document.write_text("# 큐 설계\n", encoding="utf-8")
    ledger = tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-17/context.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "kind": "결정",
                "title": "면접 준비 순서를 정한다",
                "summary": "면접 약속을 준비하며 문서를 만들었다.",
                "related_documents": ["brain/wiki/queue-design.md"],
                "calendar_contexts": [
                    {
                        "event_day": "2026-08-17",
                        "event_title": "면접 준비",
                        "related_documents": ["brain/wiki/queue-design.md"],
                        "reason": "준비",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = _event(source_event_id="opaque-event-001", title="면접 준비")
    second = _event(
        source_event_id="opaque-event-002",
        title="면접 준비",
        start_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 17, 4, 0, tzinfo=UTC),
    )
    result = CalendarProjectionService(tmp_path, FakeCalendarReader((first, second))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    documents = [
        path.read_text(encoding="utf-8") for path in (tmp_path / result.relative_path).glob("*.md")
    ]
    assert all("## 관련 문서" not in markdown for markdown in documents)


def test_refresh_links_known_people_and_owned_calendar_owner(tmp_path: Path) -> None:
    _write_person_card(tmp_path, person_id="choi-woonyoung", title="최우녕")
    _write_person_card(tmp_path, person_id="kim-heejun", title="김희준")
    _write_person_card(
        tmp_path,
        person_id="lee-minjeong",
        title="이민정",
        person_scope="novel-local-only",
        identifiers=(("이민정", ()), ("민정", ())),
    )
    event = _event(calendar_name="Woon 일정", title="김희준과 민정이 면접 준비")

    result = CalendarProjectionService(tmp_path, FakeCalendarReader((event,))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    markdown = next((tmp_path / result.relative_path).glob("*.md")).read_text(encoding="utf-8")
    assert "record_owner: choi-woonyoung" in markdown
    assert '"[[users/choi-woonyoung/README|최우녕]]"' in markdown
    assert '"[[users/kim-heejun/README|김희준]]"' in markdown
    assert '"[[users/lee-minjeong/README|이민정]]"' in markdown
    assert 'role: "organizer"' in markdown
    assert 'role: "mentioned"' in markdown
    assert 'basis: "user-confirmed-identifier-in-calendar-title"' in markdown
    assert "opaque-event-001" not in markdown


def test_refresh_writes_review_without_linking_an_ambiguous_calendar_identifier(
    tmp_path: Path,
) -> None:
    _write_person_card(
        tmp_path,
        person_id="park-minjeong",
        title="박민정",
        identifiers=(("민정", ()),),
    )
    _write_person_card(
        tmp_path,
        person_id="kim-minjeong",
        title="김민정",
        identifiers=(("민정", ()),),
    )
    event = _event(title="민정 면접 데려다주기")

    result = CalendarProjectionService(tmp_path, FakeCalendarReader((event,))).refresh(
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    )

    markdown = next((tmp_path / result.relative_path).glob("*.md")).read_text(encoding="utf-8")
    review = (tmp_path / CALENDAR_PERSON_IDENTITY_REVIEW_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "users/park-minjeong" not in markdown
    assert "users/kim-minjeong" not in markdown
    assert "calendar-person-identity-review" in review
    assert "민정" in review
    assert "users/park-minjeong/README|박민정" in review
    assert "users/kim-minjeong/README|김민정" in review


def test_refresh_removes_owned_identity_review_after_user_resolves_candidates(
    tmp_path: Path,
) -> None:
    _write_person_card(
        tmp_path,
        person_id="park-minjeong",
        title="박민정",
        identifiers=(("민정", ()),),
    )
    _write_person_card(
        tmp_path,
        person_id="kim-minjeong",
        title="김민정",
        identifiers=(("민정", ()),),
    )
    reader = FakeCalendarReader((_event(title="민정 면접"),))
    service = CalendarProjectionService(tmp_path, reader)

    service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    review = tmp_path / CALENDAR_PERSON_IDENTITY_REVIEW_RELATIVE_PATH
    assert review.exists()

    reader.events = (_event(title="다른 일정"),)
    service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))

    assert not review.exists()


def test_refresh_rejects_manual_note_in_core_owned_markdown_directory(tmp_path: Path) -> None:
    events = tmp_path / "inbox/calendar/events"
    events.mkdir(parents=True)
    (events / "manual.md").write_text("# 직접 만든 일정\n", encoding="utf-8")

    with pytest.raises(WoonError, match="contains an unmanaged note"):
        CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        )


def test_refresh_rejects_the_retired_prisma_virtual_events_store(tmp_path: Path) -> None:
    events = tmp_path / "inbox/calendar/events"
    events.mkdir(parents=True)
    store = events / ".prisma-virtual-events.md"
    store.write_text("\n```prisma-virtual-events\n[]\n```\n", encoding="utf-8")

    with pytest.raises(WoonError, match="contains an unmanaged note"):
        CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        )


def test_refresh_rejects_an_unmanaged_dashboard_path(tmp_path: Path) -> None:
    dashboard = tmp_path / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("# 직접 만든 달력\n", encoding="utf-8")

    with pytest.raises(WoonError, match="dashboard path"):
        CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        )


def test_refresh_rejects_an_unmanaged_ics_file(tmp_path: Path) -> None:
    ics = tmp_path / APPLE_CALENDAR_ICS_RELATIVE_PATH
    ics.parent.mkdir(parents=True)
    ics.write_text("BEGIN:VCALENDAR\r\nPRODID:-//Other//Calendar//KO\r\n", encoding="utf-8")

    with pytest.raises(WoonError, match="not a Core-generated projection"):
        CalendarProjectionService(tmp_path, FakeCalendarReader((_event(),))).refresh(
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        )


def test_refresh_folds_long_korean_ics_summary_without_splitting_utf8(tmp_path: Path) -> None:
    title = "긴 제목 " + "가나다라마바사아자차카타파하" * 8
    service = CalendarProjectionService(tmp_path, FakeCalendarReader((_event(title=title),)))

    result = service.refresh(now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC))
    lines = (tmp_path / result.ics_relative_path).read_bytes().split(b"\r\n")

    assert all(len(line) <= 75 for line in lines if line)
    assert any(line.startswith(b" ") for line in lines)


def test_refresh_rejects_reader_event_outside_the_requested_window(tmp_path: Path) -> None:
    reader = FakeCalendarReader(
        (
            _event(
                start_at=datetime(2027, 1, 1, 1, 0, tzinfo=UTC),
                end_at=datetime(2027, 1, 1, 2, 0, tzinfo=UTC),
            ),
        )
    )

    with pytest.raises(WoonError, match="outside the requested window"):
        CalendarProjectionService(tmp_path, reader).refresh(
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        )
