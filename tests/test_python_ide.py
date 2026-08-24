from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from woon_core import cli
from woon_core.environment import python_ide
from woon_core.environment.python_ide import PythonIdePlan, PythonIdeStatus
from woon_core.errors import WoonError
from woon_core.registry import Registry
from woon_core.workspace import Workspace


def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    interpreter = tmp_path / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    return tmp_path


def status(target: Path, pip_available: bool) -> PythonIdeStatus:
    return PythonIdeStatus(
        project=target,
        environment=target / ".venv",
        interpreter=target / ".venv/bin/python",
        uv_available=True,
        pip_available=pip_available,
    )


def test_plan_declares_locked_sync_then_pip_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = project(tmp_path)
    monkeypatch.setattr(python_ide, "doctor", lambda *_args: status(target, False))

    result = python_ide.plan(Path("/workspace"), cast(Registry, object()), target)

    assert result.operations == (
        ("uv", "sync", "--locked", "--all-groups"),
        ("uv", "pip", "install", "--python", str(target / ".venv/bin/python"), "pip"),
    )


def test_apply_runs_sync_then_restores_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = project(tmp_path)
    result_status = status(target, True)
    result_plan = PythonIdePlan(
        result_status,
        (
            ("uv", "sync", "--locked", "--all-groups"),
            ("uv", "pip", "install", "--python", str(result_status.interpreter), "pip"),
        ),
    )
    commands: list[tuple[str, ...]] = []

    def fake_run(command: tuple[str, ...], _project: Path) -> None:
        commands.append(command)

    monkeypatch.setattr(python_ide, "plan", lambda *_args: result_plan)
    monkeypatch.setattr(python_ide, "_run", fake_run)
    monkeypatch.setattr(python_ide, "verify", lambda *_args: result_status)

    result = python_ide.apply(Path("/workspace"), cast(Registry, object()), target)

    assert result.pip_available is True
    assert ("uv", "sync", "--locked", "--all-groups") in commands
    assert (
        "uv",
        "pip",
        "install",
        "--python",
        str(target / ".venv/bin/python"),
        "pip",
    ) in commands
    assert commands.index(("uv", "sync", "--locked", "--all-groups")) < commands.index(
        ("uv", "pip", "install", "--python", str(target / ".venv/bin/python"), "pip")
    )


def test_verify_requires_pip_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = project(tmp_path)
    monkeypatch.setattr(python_ide, "doctor", lambda *_args: status(target, False))

    with pytest.raises(WoonError, match="package inspection"):
        python_ide.verify(Path("/workspace"), cast(Registry, object()), target)


def test_cli_python_ide_plan_reports_explicit_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    result_status = PythonIdeStatus(
        project=Path("/project"),
        environment=Path("/project/.venv"),
        interpreter=Path("/project/.venv/bin/python"),
        uv_available=True,
        pip_available=False,
    )
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _root: (
            Workspace(Path("/workspace"), "test"),
            cast(Registry, object()),
        ),
    )
    monkeypatch.setattr(
        cli,
        "plan_python_ide",
        lambda *_args: PythonIdePlan(
            result_status,
            (("uv", "sync", "--locked", "--all-groups"),),
        ),
    )

    from io import StringIO

    output = StringIO()
    cli.run(["env", "python-ide", "plan", "--project", "/project"], output)

    assert "pip_available: false" in output.getvalue()
    assert "uv sync --locked --all-groups" in output.getvalue()
