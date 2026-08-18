from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.tasks.service import TaskService


def _service(tmp_path: Path) -> TaskService:
    template = tmp_path / "templates/daily-note.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "---\ntype: Daily\ntitle: \"{{date}}\"\n---\n\n# {{date}}\n\n## 오늘의 초점\n",
        encoding="utf-8",
    )
    return TaskService(tmp_path)


def test_materializes_daily_routine_once_and_preserves_user_content(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.upsert_recurring_todo(
        task_id="health-morning-run",
        title="아침에 러닝하기",
        purpose="3개월 동안 건강 목표를 꾸준히 실행하기 위해 매일 아침에 러닝한다.",
        area="health",
        start_date=date(2026, 8, 17),
    )

    first = service.materialize_due(on_date=date(2026, 8, 17))
    daily_path = tmp_path / first.daily_relative_path
    existing = daily_path.read_text(encoding="utf-8")
    daily_path.write_text(existing + "\n사용자 메모\n", encoding="utf-8")
    replay = service.materialize_due(on_date=date(2026, 8, 17))

    content = daily_path.read_text(encoding="utf-8")
    assert created.created is True
    assert first.created_daily_note is True
    assert replay.created_daily_note is False
    assert replay.changed_daily_note is False
    assert "- [ ] 아침에 러닝하기 <!-- woon-task:health-morning-run:2026-08-17 -->" in content
    assert "사용자 메모" in content
    assert (tmp_path / created.routine.relative_path).is_file()
    assert (tmp_path / ".local/woon-knowledge/tasks-state.json").stat().st_mode & 0o777 == 0o600


def test_complete_changes_only_the_requested_daily_task(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for task_id, title in (
        ("health-morning-run", "아침에 러닝하기"),
        ("learning-classroom-arrival", "10:00까지 강의실에 출근하기"),
    ):
        service.upsert_recurring_todo(
            task_id=task_id,
            title=title,
            purpose="매일 지키는 약속을 빠뜨리지 않기 위해 기록한다.",
            area="health" if task_id.startswith("health") else "learning",
            start_date=date(2026, 8, 17),
        )

    result = service.complete(task_id="health-morning-run", on_date=date(2026, 8, 17))
    content = (tmp_path / result.daily_relative_path).read_text(encoding="utf-8")

    assert "- [x] 아침에 러닝하기" in content
    assert "- [ ] 10:00까지 강의실에 출근하기" in content


def test_task_requires_purpose_and_never_materializes_before_start_date(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(WoonError, match="purpose"):
        service.upsert_recurring_todo(
            task_id="health-morning-run",
            title="아침에 러닝하기",
            purpose="",
            area="health",
        )
    service.upsert_recurring_todo(
        task_id="health-morning-run",
        title="아침에 러닝하기",
        purpose="건강 목표를 위해 매일 아침 러닝한다.",
        area="health",
        start_date=date(2026, 8, 18),
    )

    result = service.materialize_due(on_date=date(2026, 8, 17))

    assert result.tasks == ()
