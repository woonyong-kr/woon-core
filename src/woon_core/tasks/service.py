"""Canonical Markdown routines and daily task materialization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

_TASK_ID = re.compile(r"^[a-z][a-z0-9-]{2,79}$")
_MARKER = re.compile(
    r"^\s*- \[([ xX])\] .*<!-- woon-task:([a-z][a-z0-9-]{2,79}):(\d{4}-\d{2}-\d{2}) -->\s*$"
)
_START = "<!-- woon-tasks:start -->"
_END = "<!-- woon-tasks:end -->"
_KST = ZoneInfo("Asia/Seoul")
_AREAS = frozenset({"career", "learning", "creative", "life", "relationship", "health", "admin"})


@dataclass(frozen=True, slots=True)
class TaskRoutine:
    task_id: str
    title: str
    purpose: str
    area: str
    recurrence: str
    start_date: date
    status: str
    relative_path: str
    revision: str


@dataclass(frozen=True, slots=True)
class TaskWriteResult:
    created: bool
    changed: bool
    routine: TaskRoutine


@dataclass(frozen=True, slots=True)
class DailyTask:
    task_id: str
    title: str
    completed: bool


@dataclass(frozen=True, slots=True)
class DailyMaterializationResult:
    day: str
    created_daily_note: bool
    changed_daily_note: bool
    tasks: tuple[DailyTask, ...]
    daily_relative_path: str


class TaskService:
    """Own routine sources and only the marked task block inside daily notes."""

    def __init__(self, vault: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._routines_root = self._vault / "inbox/tasks/routines"
        self._daily_root = self._vault / "inbox/daily"
        self._template_path = self._vault / "templates/daily-note.md"
        self._state_path = self._vault / ".local/woon-knowledge/tasks-state.json"

    def upsert_recurring_todo(
        self,
        *,
        task_id: str,
        title: str,
        purpose: str,
        area: str,
        start_date: date | None = None,
        expected_revision: str | None = None,
    ) -> TaskWriteResult:
        """Create or update one daily routine with a stated retention purpose."""

        _validate_task_fields(task_id, title, purpose, area)
        actual_start = start_date or datetime.now(_KST).date()
        path = self._routines_root / f"{task_id}.md"
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            existing = _read_routine(path, self._vault) if path.exists() else None
            if expected_revision is not None and (
                existing is None or existing.revision != expected_revision
            ):
                raise WoonError("task routine revision changed; read it again before updating")
            content = _render_routine(task_id, title, purpose, area, actual_start)
            changed = not path.exists() or path.read_text(encoding="utf-8") != content
            if changed:
                atomic_write(path, content.encode("utf-8"))
            routine = _read_routine(path, self._vault)
            _record_operation(
                self._state_path,
                operation=f"upsert:{task_id}",
                payload={"routine_revision": routine.revision},
            )
            return TaskWriteResult(created=existing is None, changed=changed, routine=routine)

    def materialize_due(self, *, on_date: date | None = None) -> DailyMaterializationResult:
        """Create one KST daily note and synchronize its tool-owned task block."""

        target_day = on_date or datetime.now(_KST).date()
        routines = tuple(
            routine
            for routine in self.list_routines()
            if routine.status == "active"
            and routine.recurrence == "daily"
            and routine.start_date <= target_day
        )
        daily_path = self._daily_root / f"{target_day.isoformat()}.md"
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            created_daily_note = not daily_path.exists()
            if created_daily_note:
                template = _render_daily_template(self._template_path, target_day)
                atomic_write(daily_path, template.encode("utf-8"))
            existing = daily_path.read_text(encoding="utf-8")
            completed = _completed_task_ids(existing, target_day)
            tasks = tuple(
                DailyTask(
                    task_id=routine.task_id,
                    title=routine.title,
                    completed=routine.task_id in completed,
                )
                for routine in routines
            )
            updated = _replace_managed_tasks(existing, tasks, target_day)
            changed_daily_note = updated != existing
            if changed_daily_note:
                atomic_write(daily_path, updated.encode("utf-8"))
            _record_operation(
                self._state_path,
                operation=f"materialize:{target_day.isoformat()}",
                payload={
                    "daily_path": _relative(daily_path, self._vault),
                    "task_ids": [task.task_id for task in tasks],
                },
            )
        return DailyMaterializationResult(
            day=target_day.isoformat(),
            created_daily_note=created_daily_note,
            changed_daily_note=changed_daily_note,
            tasks=tasks,
            daily_relative_path=_relative(daily_path, self._vault),
        )

    def complete(self, *, task_id: str, on_date: date | None = None) -> DailyMaterializationResult:
        """Mark one materialized task complete without touching other daily content."""

        _validate_task_id(task_id)
        target_day = on_date or datetime.now(_KST).date()
        result = self.materialize_due(on_date=target_day)
        daily_path = self._daily_root / f"{target_day.isoformat()}.md"
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            existing = daily_path.read_text(encoding="utf-8")
            marker = f"<!-- woon-task:{task_id}:{target_day.isoformat()} -->"
            replaced = re.sub(
                rf"^- \[ \](.*{re.escape(marker)})$",
                r"- [x]\1",
                existing,
                flags=re.MULTILINE,
            )
            if replaced == existing:
                if marker not in existing:
                    raise WoonError("task is not materialized for this day")
                return result
            atomic_write(daily_path, replaced.encode("utf-8"))
            _record_operation(
                self._state_path,
                operation=f"complete:{task_id}:{target_day.isoformat()}",
                payload={"daily_path": _relative(daily_path, self._vault)},
            )
        completed_tasks = tuple(
            DailyTask(task.task_id, task.title, task.completed or task.task_id == task_id)
            for task in result.tasks
        )
        return DailyMaterializationResult(
            day=result.day,
            created_daily_note=result.created_daily_note,
            changed_daily_note=True,
            tasks=completed_tasks,
            daily_relative_path=result.daily_relative_path,
        )

    def list_routines(self) -> tuple[TaskRoutine, ...]:
        if not self._routines_root.exists():
            return ()
        return tuple(
            _read_routine(path, self._vault)
            for path in sorted(self._routines_root.glob("*.md"))
        )

    def find(self, query: str, *, on_date: date | None = None) -> tuple[DailyTask, ...]:
        """Return routine matches with completion from the requested day's note."""

        needle = query.strip().casefold()
        if not needle:
            raise WoonError("task search query must not be empty")
        target_day = on_date or datetime.now(_KST).date()
        daily_path = self._daily_root / f"{target_day.isoformat()}.md"
        completed = _completed_task_ids(
            daily_path.read_text(encoding="utf-8") if daily_path.exists() else "", target_day
        )
        return tuple(
            DailyTask(routine.task_id, routine.title, routine.task_id in completed)
            for routine in self.list_routines()
            if needle in routine.title.casefold() or needle in routine.purpose.casefold()
        )


def _read_routine(path: Path, vault: Path) -> TaskRoutine:
    text = path.read_text(encoding="utf-8")
    metadata = _frontmatter(text, path)
    task_id = _string(metadata.get("task_id"), "task_id", path)
    title = _string(metadata.get("title"), "title", path)
    purpose = _string(metadata.get("purpose"), "purpose", path)
    area = _string(metadata.get("area"), "area", path)
    recurrence = _string(metadata.get("recurrence"), "recurrence", path)
    status = _string(metadata.get("status"), "status", path)
    raw_start = _string(metadata.get("start_date"), "start_date", path)
    try:
        start_date = date.fromisoformat(raw_start)
    except ValueError as error:
        raise WoonError(f"task routine start_date is invalid: {path}") from error
    _validate_task_fields(task_id, title, purpose, area)
    if recurrence != "daily" or status not in {"active", "paused"}:
        raise WoonError(f"task routine has unsupported recurrence or status: {path}")
    return TaskRoutine(
        task_id=task_id,
        title=title,
        purpose=purpose,
        area=area,
        recurrence=recurrence,
        start_date=start_date,
        status=status,
        relative_path=_relative(path, vault),
        revision=_revision(text),
    )


def _frontmatter(text: str, path: Path) -> dict[str, object]:
    if not text.startswith("---\n"):
        raise WoonError(f"task routine needs YAML frontmatter: {path}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise WoonError(f"task routine frontmatter is incomplete: {path}")
    try:
        raw = yaml.safe_load(text[4:end])
    except yaml.YAMLError as error:
        raise WoonError(f"task routine frontmatter is invalid: {path}") from error
    if not isinstance(raw, dict):
        raise WoonError(f"task routine frontmatter must be a mapping: {path}")
    return raw


def _string(value: object, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"task routine {field} is required: {path}")
    return value.strip()


def _validate_task_fields(task_id: str, title: str, purpose: str, area: str) -> None:
    _validate_task_id(task_id)
    if not title.strip() or "\n" in title or len(title) > 160:
        raise WoonError("task title must be one non-empty line up to 160 characters")
    if not purpose.strip() or "\n" in purpose or len(purpose) > 280:
        raise WoonError("task purpose must be one non-empty line up to 280 characters")
    if area not in _AREAS:
        raise WoonError("task area must use the configured responsibility taxonomy")


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID.fullmatch(task_id):
        raise WoonError("task_id must be lowercase kebab-case and at least three characters")


def _render_routine(task_id: str, title: str, purpose: str, area: str, start_date: date) -> str:
    return "\n".join(
        (
            "---",
            "type: Task Routine",
            f"title: {json.dumps(title, ensure_ascii=False)}",
            "publish: false",
            "access: local-only",
            "status: active",
            f"task_id: {task_id}",
            f"purpose: {json.dumps(purpose, ensure_ascii=False)}",
            f"area: {area}",
            "recurrence: daily",
            f"start_date: {json.dumps(start_date.isoformat())}",
            "---",
            "",
            f"# {title}",
            "",
            purpose,
            "",
        )
    )


def _render_daily_template(template_path: Path, target_day: date) -> str:
    if not template_path.is_file():
        raise WoonError(f"daily note template is missing: {template_path}")
    return template_path.read_text(encoding="utf-8").replace("{{date}}", target_day.isoformat())


def _completed_task_ids(text: str, target_day: date) -> set[str]:
    completed: set[str] = set()
    for line in text.splitlines():
        match = _MARKER.match(line)
        if match and match.group(1).casefold() == "x" and match.group(3) == target_day.isoformat():
            completed.add(match.group(2))
    return completed


def _replace_managed_tasks(text: str, tasks: tuple[DailyTask, ...], target_day: date) -> str:
    lines = [
        _START,
        *[
            f"- [{'x' if task.completed else ' '}] {task.title} "
            f"<!-- woon-task:{task.task_id}:{target_day.isoformat()} -->"
            for task in tasks
        ],
        _END,
    ]
    block = "\n".join(lines)
    if _START in text or _END in text:
        if _START not in text or _END not in text or text.index(_END) < text.index(_START):
            raise WoonError("daily note task markers are incomplete")
        return re.sub(
            rf"{re.escape(_START)}.*?{re.escape(_END)}",
            block,
            text,
            count=1,
            flags=re.DOTALL,
        )
    heading = "## 오늘의 할 일"
    if heading in text:
        return text.replace(heading, f"{heading}\n\n{block}", 1)
    insertion = f"{heading}\n\n{block}\n\n"
    focus = "## 오늘의 초점"
    if focus in text:
        return text.replace(focus, f"{insertion}{focus}", 1)
    return text.rstrip() + "\n\n" + insertion


def _record_operation(state_path: Path, *, operation: str, payload: dict[str, object]) -> None:
    state = _load_state(state_path)
    operations = state["operations"]
    assert isinstance(operations, dict)
    operations[operation] = payload
    atomic_write(state_path, encode_json(state), mode=0o600)


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "operations": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("task receipt state is unreadable") from error
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(raw.get("operations"), dict)
    ):
        raise WoonError("task receipt state is malformed")
    return raw


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
