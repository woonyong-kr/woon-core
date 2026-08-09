"""Load and validate the canonical IDE configuration model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from woon_core import __version__
from woon_core.errors import WoonError
from woon_core.io import load_yaml


@dataclass(frozen=True, slots=True)
class EnvironmentModel:
    environment: dict[str, Any]
    vscode: dict[str, Any]
    vscode_actions: dict[str, Any]
    jetbrains: dict[str, Any]
    jetbrains_actions: dict[str, Any]
    overlay: dict[str, Any]


def load_model(repository_path: Path, target: str) -> EnvironmentModel:
    lock = load_yaml(repository_path / "lock/toolchain.yaml")
    _validate_toolchain(lock)
    model = EnvironmentModel(
        environment=load_yaml(repository_path / "config/env.yaml"),
        vscode=load_yaml(repository_path / "adapters/vscode/adapter.yaml"),
        vscode_actions=load_yaml(repository_path / "adapters/vscode/actions.yaml"),
        jetbrains=load_yaml(repository_path / "adapters/jetbrains/adapter.yaml"),
        jetbrains_actions=load_yaml(repository_path / "adapters/jetbrains/actions.yaml"),
        overlay=load_yaml(repository_path / "overlays" / f"{target}.yaml"),
    )
    _validate_model(model, target)
    return model


def _validate_toolchain(lock: dict[str, Any]) -> None:
    serialization = _mapping(lock.get("serialization"), "serialization")
    if (
        lock.get("version") != 1
        or lock.get("generator") != f"woon-core@{__version__}"
        or lock.get("schema") != "env.schema.json@1"
    ):
        raise WoonError(f"toolchain lock does not match woon-core {__version__} and env schema 1")
    if serialization != {
        "json_indent": 2,
        "json_key_order": "lexical",
        "xml_indent": 2,
        "line_ending": "lf",
    }:
        raise WoonError("unsupported serialization lock")


def _validate_model(model: EnvironmentModel, target: str) -> None:
    documents = (
        model.environment,
        model.vscode,
        model.vscode_actions,
        model.jetbrains,
        model.jetbrains_actions,
        model.overlay,
    )
    if any(document.get("version") != 1 for document in documents):
        raise WoonError("all environment schemas must use version 1")

    environment = model.environment
    editor = _mapping(environment.get("editor"), "editor")
    languages = _mapping(environment.get("languages"), "languages")
    keybindings = _mapping(environment.get("keybindings"), "keybindings")
    extensions = _string_list(environment.get("extensions"), "extensions")
    settings = _mapping(model.vscode.get("settings"), "vscode settings")
    formatter_ids = _mapping(model.vscode.get("formatter_ids"), "formatter_ids")
    language_ids = _mapping(model.vscode.get("language_ids"), "language_ids")
    vscode_extensions = _mapping(model.vscode.get("extensions"), "vscode extensions")
    vscode_intents = _mapping(model.vscode_actions.get("intents"), "vscode intents")
    jetbrains_intents = _mapping(model.jetbrains_actions.get("intents"), "jetbrains intents")

    keymap = _mapping(model.jetbrains.get("keymap"), "jetbrains keymap")
    parents = _mapping(keymap.get("parents"), "jetbrains parents")
    if target not in parents:
        raise WoonError(f"JetBrains parent keymap missing for target {target!r}")

    common_targets: set[str] = set()
    for source, raw_target in settings.items():
        parts = source.split(".")
        if len(parts) != 2 or parts[0] != "editor" or parts[1] not in editor:
            raise WoonError(f"adapter maps undeclared common setting {source!r}")
        if not isinstance(raw_target, str) or not raw_target:
            raise WoonError(f"adapter target for {source!r} must be a string")
        common_targets.add(raw_target)
    if len(editor) != len(settings):
        raise WoonError("every editor setting must have exactly one VS Code adapter mapping")
    overlay_settings = _mapping(model.overlay.get("adapter_settings"), "adapter_settings")
    vscode_overlay = _mapping(overlay_settings.get("vscode", {}), "vscode overlay")
    overlap = common_targets.intersection(vscode_overlay)
    if overlap:
        raise WoonError(f"platform overlay may not override common setting {min(overlap)!r}")

    for name, raw_language in languages.items():
        language = _mapping(raw_language, f"language {name}")
        formatter = language.get("formatter")
        if not isinstance(formatter, str) or not formatter or int(language.get("tab_size", 0)) <= 0:
            raise WoonError(f"language {name!r} requires formatter and positive tab_size")
        if not _string_list(language_ids.get(name), f"language IDs for {name}"):
            raise WoonError(f"language {name!r} has no VS Code language ID")
        if formatter not in formatter_ids:
            raise WoonError(f"formatter {formatter!r} has no VS Code mapping")

    for intent in keybindings:
        if not _list(vscode_intents.get(intent), f"VS Code intent {intent}"):
            raise WoonError(f"VS Code action mapping missing for intent {intent!r}")
        if not _string_list(jetbrains_intents.get(intent), f"JetBrains intent {intent}"):
            raise WoonError(f"JetBrains action mapping missing for intent {intent!r}")
    for intent in vscode_intents:
        if intent not in keybindings:
            raise WoonError(f"VS Code adapter has undeclared intent {intent!r}")
    for intent in jetbrains_intents:
        if intent not in keybindings:
            raise WoonError(f"JetBrains adapter has undeclared intent {intent!r}")

    seen: set[str] = set()
    for extension in extensions:
        if extension in seen:
            raise WoonError(f"duplicate extension {extension!r}")
        seen.add(extension)
        if extension not in vscode_extensions:
            raise WoonError(f"VS Code extension mapping missing for {extension!r}")


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise WoonError(f"{name} must be a mapping")
    return value


def _list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise WoonError(f"{name} must be a list")
    return value


def _string_list(value: object, name: str) -> list[str]:
    items = _list(value, name)
    if any(not isinstance(item, str) for item in items):
        raise WoonError(f"{name} must contain strings")
    return items
