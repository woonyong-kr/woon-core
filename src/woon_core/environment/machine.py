"""Plan, apply, and verify generated IDE configuration."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from woon_core.environment.generator import check, equal_jetbrains_keymaps
from woon_core.errors import WoonError
from woon_core.io import atomic_write, load_yaml
from woon_core.registry import Registry


@dataclass(frozen=True, slots=True)
class TargetStatus:
    name: str
    path: Path
    running: bool
    extension_command: str
    command_available: bool


@dataclass(frozen=True, slots=True)
class Operation:
    kind: str
    target: str
    artifact: str
    source: Path
    destination: str
    changed: bool


@dataclass(frozen=True, slots=True)
class PlanResult:
    target: str
    operations: tuple[Operation, ...]
    changes: int


@dataclass(frozen=True, slots=True)
class ApplyResult:
    target: str
    applied: int
    backup_path: Path | None


@dataclass(frozen=True, slots=True)
class _DiscoveredTarget:
    name: str
    path: Path
    process_patterns: tuple[str, ...]
    files: dict[str, str]
    extension_artifact: str
    extension_command: str
    extension_list_args: tuple[str, ...]
    extension_install_args: tuple[str, ...]
    extension_uninstall_args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackupRecord:
    destination: Path
    backup: Path
    existed: bool


def doctor(root: Path, registry: Registry, target: str) -> list[TargetStatus]:
    statuses: list[TargetStatus] = []
    for item in _discover(root, registry, target):
        statuses.append(
            TargetStatus(
                name=item.name,
                path=item.path,
                running=_any_process_running(item.process_patterns),
                extension_command=item.extension_command,
                command_available=(
                    not item.extension_command or shutil.which(item.extension_command) is not None
                ),
            )
        )
    return statuses


def plan(root: Path, registry: Registry, target: str) -> PlanResult:
    try:
        check(root, registry, target)
    except WoonError as error:
        raise WoonError(f"generated artifacts are not current: {error}") from error
    repository_path = registry.resolve(root, "env")
    operations: list[Operation] = []
    for item in _discover(root, registry, target):
        for artifact, destination_name in sorted(item.files.items()):
            source = repository_path / "generated" / target / artifact.removesuffix("#platform")
            destination = item.path / destination_name
            operations.append(
                Operation(
                    kind="file",
                    target=item.name,
                    artifact=artifact,
                    source=source,
                    destination=str(destination),
                    changed=differs_semantically(artifact, source, destination),
                )
            )
        if item.extension_artifact:
            if not item.extension_command:
                raise WoonError(f"extension command is not configured for {item.name} on {target}")
            installed = _list_extensions(item.extension_command, item.extension_list_args)
            source = repository_path / "generated" / target / item.extension_artifact
            for extension in _read_lines(source):
                operations.append(
                    Operation(
                        kind="extension",
                        target=item.name,
                        artifact=item.extension_artifact,
                        source=source,
                        destination=extension,
                        changed=extension.lower() not in installed,
                    )
                )
    operations.sort(key=lambda item: (item.target, item.kind, item.destination))
    return PlanResult(
        target=target,
        operations=tuple(operations),
        changes=sum(operation.changed for operation in operations),
    )


def apply(root: Path, registry: Registry, target: str) -> ApplyResult:
    current_target = runtime_target()
    if target != current_target:
        raise WoonError(f"apply target {target!r} does not match current OS {current_target!r}")
    for status in doctor(root, registry, target):
        if status.running:
            raise WoonError(f"{status.name} is running; close the IDE before apply")
    result = plan(root, registry, target)
    if result.changes == 0:
        return ApplyResult(target=target, applied=0, backup_path=None)

    repository_path = registry.resolve(root, "env")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = repository_path / "backups" / timestamp
    records: list[BackupRecord] = []
    installed: list[tuple[_DiscoveredTarget, str]] = []
    changed_files = [
        operation
        for operation in result.operations
        if operation.changed and operation.kind == "file"
    ]
    try:
        for index, operation in enumerate(changed_files):
            records.append(backup_destination(backup_root, index, Path(operation.destination)))
        targets = {item.name: item for item in _discover(root, registry, target)}
        for operation in result.operations:
            if not operation.changed or operation.kind != "extension":
                continue
            item = targets[operation.target]
            args = _substitute_id(item.extension_install_args, operation.destination)
            _run_extension_command(item.extension_command, args, "install", operation.destination)
            installed.append((item, operation.destination))
        for operation in changed_files:
            atomic_write(Path(operation.destination), operation.source.read_bytes())
        verify(root, registry, target)
    except BaseException as error:
        rollback_errors = rollback(records)
        rollback_errors.extend(_rollback_extensions(installed))
        if rollback_errors:
            raise WoonError(
                f"apply failed: {error}; rollback failed: {'; '.join(rollback_errors)}"
            ) from error
        raise WoonError(f"apply failed and changes were rolled back: {error}") from error
    return ApplyResult(
        target=target,
        applied=len(changed_files) + len(installed),
        backup_path=backup_root,
    )


def verify(root: Path, registry: Registry, target: str) -> PlanResult:
    result = plan(root, registry, target)
    if result.changes:
        destinations = [item.destination for item in result.operations if item.changed]
        raise WoonError(
            f"semantic verification failed for {result.changes} files: " + ", ".join(destinations)
        )
    return result


def differs_semantically(artifact: str, source: Path, destination: Path) -> bool:
    expected = source.read_bytes()
    try:
        actual = destination.read_bytes()
    except FileNotFoundError:
        return True
    normalized_artifact = artifact.removesuffix("#platform")
    if normalized_artifact.endswith(".json"):
        try:
            expected_value: object = json.loads(expected)
            actual_value: object = json.loads(actual)
            return expected_value != actual_value
        except json.JSONDecodeError:
            return True
    if normalized_artifact == "jetbrains/keymap.xml":
        return not equal_jetbrains_keymaps(expected, actual)
    if normalized_artifact == "jetbrains/active-keymap.xml":
        return _active_keymap_name(expected) != _active_keymap_name(actual)
    return expected != actual


def backup_destination(root: Path, index: int, destination: Path) -> BackupRecord:
    backup = root / f"{index:04d}"
    if not destination.exists():
        return BackupRecord(destination=destination, backup=backup, existed=False)
    atomic_write(backup, destination.read_bytes())
    return BackupRecord(destination=destination, backup=backup, existed=True)


def rollback(records: list[BackupRecord]) -> list[str]:
    failures: list[str] = []
    for record in reversed(records):
        try:
            if record.existed:
                atomic_write(record.destination, record.backup.read_bytes())
            else:
                record.destination.unlink(missing_ok=True)
        except OSError as error:
            failures.append(str(error))
    return failures


def runtime_target() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def _discover(root: Path, registry: Registry, target: str) -> list[_DiscoveredTarget]:
    repository_path = registry.resolve(root, "env")
    config = load_yaml(repository_path / "adapters/installations.yaml")
    if config.get("version") != 1:
        raise WoonError(f"unsupported installations version {config.get('version')}")
    raw_targets = _mapping(config.get("targets"))
    result: list[_DiscoveredTarget] = []
    for name, raw_definition in sorted(raw_targets.items()):
        definition = _mapping(raw_definition)
        config_paths = _mapping(definition.get("config_paths"))
        if target not in config_paths:
            raise WoonError(f"target {name!r} has no {target} config path")
        files = {str(key): str(value) for key, value in _mapping(definition.get("files")).items()}
        platform_files = _mapping(definition.get("platform_files", {}))
        for artifact, destination in _mapping(platform_files.get(target, {})).items():
            if files.get(str(artifact)) == str(destination):
                continue
            files[f"{artifact}#platform"] = str(destination)
        process_patterns = _mapping(definition.get("process_patterns"))
        extension_commands = _mapping(definition.get("extension_commands", {}))
        for pattern in _strings(config_paths[target]):
            expanded = _expand_path(pattern)
            matches = (
                [Path(value) for value in glob.glob(str(expanded))]
                if _has_glob(pattern)
                else [expanded]
            )
            for path in matches:
                required_child = str(definition.get("require_child", ""))
                if required_child and not (path / required_child).is_dir():
                    continue
                if not path.exists():
                    continue
                result.append(
                    _DiscoveredTarget(
                        name=name,
                        path=path,
                        process_patterns=tuple(_strings(process_patterns.get(target, []))),
                        files=files,
                        extension_artifact=str(definition.get("extension_artifact", "")),
                        extension_command=str(extension_commands.get(target, "")),
                        extension_list_args=tuple(
                            _strings(definition.get("extension_list_args", []))
                        ),
                        extension_install_args=tuple(
                            _strings(definition.get("extension_install_args", []))
                        ),
                        extension_uninstall_args=tuple(
                            _strings(definition.get("extension_uninstall_args", []))
                        ),
                    )
                )
    return sorted(result, key=lambda item: (item.name, str(item.path)))


def _active_keymap_name(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    for component in root.findall("component"):
        if component.attrib.get("name") == "KeymapManager":
            active = component.find("active_keymap")
            return "" if active is None else active.attrib.get("name", "")
    return ""


def _expand_path(value: str) -> Path:
    expanded = value.replace("%HOME%", str(Path.home()))
    if "%APPDATA%" in expanded:
        app_data = os.environ.get("APPDATA")
        if not app_data:
            raise WoonError(f"APPDATA is required for path {value!r}")
        expanded = expanded.replace("%APPDATA%", app_data)
    return Path(expanded)


def _any_process_running(patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        command = (
            ["tasklist", "/FI", f"IMAGENAME eq {pattern}"]
            if sys.platform == "win32"
            else ["pgrep", "-f", pattern]
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if (
            result.returncode == 0
            and result.stdout.strip()
            and (sys.platform != "win32" or pattern.lower() in result.stdout.lower())
        ):
            return True
    return False


def _list_extensions(command: str, args: tuple[str, ...]) -> set[str]:
    if shutil.which(command) is None:
        raise WoonError(f"extension command not found: {command}")
    result = subprocess.run([command, *args], check=True, capture_output=True, text=True)
    return {
        line.strip().split("@", 1)[0].lower() for line in result.stdout.splitlines() if line.strip()
    }


def _read_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def _substitute_id(args: tuple[str, ...], identifier: str) -> list[str]:
    return [argument.replace("%ID%", identifier) for argument in args]


def _run_extension_command(command: str, args: list[str], action: str, identifier: str) -> None:
    result = subprocess.run([command, *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise WoonError(
            f"{action} extension {identifier}: exit {result.returncode}: {result.stderr.strip()}"
        )


def _rollback_extensions(records: list[tuple[_DiscoveredTarget, str]]) -> list[str]:
    failures: list[str] = []
    for item, identifier in reversed(records):
        try:
            _run_extension_command(
                item.extension_command,
                _substitute_id(item.extension_uninstall_args, identifier),
                "uninstall",
                identifier,
            )
        except WoonError as error:
            failures.append(str(error))
    return failures


def _has_glob(value: str) -> bool:
    return any(character in value for character in "*?[")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError("expected mapping")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WoonError("expected list of strings")
    return value
