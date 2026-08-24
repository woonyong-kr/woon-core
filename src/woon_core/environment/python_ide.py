"""Keep a ``uv`` project environment compatible with JetBrains package inspection."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from woon_core.environment.machine import runtime_target
from woon_core.environment.model import load_model
from woon_core.errors import WoonError
from woon_core.registry import Registry

_POLICY = {
    "manager": "uv",
    "environment": ".venv",
    "package_inspection": "pip",
    "sync": "locked-all-groups",
}


@dataclass(frozen=True, slots=True)
class PythonIdePolicy:
    manager: str
    environment: str
    package_inspection: str
    sync: str


@dataclass(frozen=True, slots=True)
class PythonIdeStatus:
    project: Path
    environment: Path
    interpreter: Path
    uv_available: bool
    pip_available: bool


@dataclass(frozen=True, slots=True)
class PythonIdePlan:
    status: PythonIdeStatus
    operations: tuple[tuple[str, ...], ...]


def doctor(root: Path, registry: Registry, project: Path) -> PythonIdeStatus:
    """Report whether one locked ``uv`` project can satisfy IDE package inspection."""

    policy = _load_policy(root, registry)
    resolved_project = project.expanduser().resolve()
    _validate_project_layout(resolved_project)
    environment = resolved_project / policy.environment
    executable = "Scripts/python.exe" if runtime_target() == "windows" else "bin/python"
    interpreter = environment / executable
    return PythonIdeStatus(
        project=resolved_project,
        environment=environment,
        interpreter=interpreter,
        uv_available=shutil.which(policy.manager) is not None,
        pip_available=_has_pip(interpreter, resolved_project),
    )


def plan(root: Path, registry: Registry, project: Path) -> PythonIdePlan:
    """Return the explicit sync and compatibility operations without changing the project."""

    status = doctor(root, registry, project)
    return PythonIdePlan(
        status=status,
        operations=(
            ("uv", "sync", "--locked", "--all-groups"),
            ("uv", "pip", "install", "--python", str(status.interpreter), "pip"),
        ),
    )


def apply(root: Path, registry: Registry, project: Path) -> PythonIdeStatus:
    """Synchronize a locked project, then restore the ``pip`` bridge JetBrains invokes."""

    result = plan(root, registry, project)
    if not result.status.uv_available:
        raise WoonError("Python IDE bootstrap requires uv on PATH")
    for operation in result.operations:
        _run(operation, result.status.project)
    return verify(root, registry, project)


def verify(root: Path, registry: Registry, project: Path) -> PythonIdeStatus:
    """Fail closed when JetBrains cannot use ``python -m pip list`` in the project venv."""

    status = doctor(root, registry, project)
    if not status.pip_available:
        raise WoonError(
            f"JetBrains package inspection is unavailable for {status.project}; "
            "run 'woon env python-ide apply --project <path>'"
        )
    return status


def _load_policy(root: Path, registry: Registry) -> PythonIdePolicy:
    repository_path = registry.resolve(root, "env")
    environment = load_model(repository_path, runtime_target()).environment
    raw_policy = environment.get("python_ide")
    if not isinstance(raw_policy, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in raw_policy.items()
    ):
        raise WoonError("python_ide must be a string mapping")
    if raw_policy != _POLICY:
        raise WoonError("unsupported python_ide policy")
    return PythonIdePolicy(**raw_policy)


def _validate_project_layout(project: Path) -> None:
    if not project.is_dir():
        raise WoonError(f"Python IDE project does not exist: {project}")
    for name in ("pyproject.toml", "uv.lock"):
        if not (project / name).is_file():
            raise WoonError(f"Python IDE project requires {name}: {project}")


def _has_pip(interpreter: Path, project: Path) -> bool:
    if not interpreter.is_file():
        return False
    try:
        result = subprocess.run(
            [str(interpreter), "-m", "pip", "--version"],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def _run(operation: tuple[str, ...], project: Path) -> None:
    try:
        result = subprocess.run(
            operation,
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise WoonError(f"run {' '.join(operation)}: {error}") from error
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        suffix = f": {details}" if details else ""
        raise WoonError(f"run {' '.join(operation)}: exit {result.returncode}{suffix}")
