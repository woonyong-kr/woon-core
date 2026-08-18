"""Parsing helpers for the public ``woon tasks`` command."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import TextIO

from woon_core.errors import WoonError
from woon_core.tasks.factory import build_task_service


def run_tasks(arguments: list[str], output: TextIO) -> None:
    """Run a bounded task command against one explicit or configured vault."""

    if not arguments:
        raise WoonError("usage: woon tasks <find|upsert-recurring|materialize|complete>")
    command, *options = arguments
    values, positionals = _options(options)
    vault = Path(values.pop("--vault")).expanduser() if "--vault" in values else None
    service = build_task_service(vault)
    if command == "find":
        if len(positionals) != 1 or set(values).difference({"--date"}):
            raise WoonError("tasks find requires one query and optional --date")
        tasks = service.find(positionals[0], on_date=_day(values.get("--date")))
        for task in tasks:
            output.write(f"{'[x]' if task.completed else '[ ]'} {task.title} ({task.task_id})\n")
        return
    if command == "materialize":
        if positionals or set(values).difference({"--date"}):
            raise WoonError("tasks materialize accepts only optional --date")
        materialization = service.materialize_due(on_date=_day(values.get("--date")))
        output.write(
            f"status: ok\nday: {materialization.day}\n"
            f"daily_note: {materialization.daily_relative_path}\n"
            f"tasks: {len(materialization.tasks)}\n"
        )
        return
    if command == "complete":
        if positionals or "--id" not in values or set(values).difference({"--id", "--date"}):
            raise WoonError("tasks complete requires --id and optional --date")
        completion = service.complete(task_id=values["--id"], on_date=_day(values.get("--date")))
        output.write(
            f"status: ok\nday: {completion.day}\n"
            f"daily_note: {completion.daily_relative_path}\n"
        )
        return
    if command == "upsert-recurring":
        required = {"--id", "--title", "--purpose", "--area"}
        allowed = required | {"--start-date"}
        if positionals or not required.issubset(values) or set(values).difference(allowed):
            raise WoonError(
                "tasks upsert-recurring requires --id --title --purpose --area "
                "and optional --start-date"
            )
        upsert = service.upsert_recurring_todo(
            task_id=values["--id"],
            title=values["--title"],
            purpose=values["--purpose"],
            area=values["--area"],
            start_date=_day(values.get("--start-date")),
        )
        output.write(
            f"status: ok\ncreated: {str(upsert.created).lower()}\n"
            f"changed: {str(upsert.changed).lower()}\nroutine: {upsert.routine.relative_path}\n"
        )
        return
    raise WoonError(f"unknown tasks command {command!r}")


def _options(arguments: list[str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if not option.startswith("--"):
            positionals.append(option)
            index += 1
            continue
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    return values, positionals


def _day(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise WoonError("task date must use YYYY-MM-DD") from error
