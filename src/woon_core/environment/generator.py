"""Render deterministic VS Code and JetBrains artifacts."""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from woon_core.environment.model import EnvironmentModel, load_model
from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.registry import Registry


@dataclass(frozen=True, slots=True)
class GenerateResult:
    target: str
    artifacts: int
    hash: str


def generate(root: Path, registry: Registry, target: str) -> GenerateResult:
    repository_path = registry.resolve(root, "env")
    artifacts = render(repository_path, target)
    if artifacts != render(repository_path, target):
        raise WoonError(f"generator is not deterministic for target {target!r}")
    manifest, aggregate = _make_manifest(target, artifacts)
    artifacts["manifest.json"] = manifest
    output_root = repository_path / "generated" / target
    for relative, data in sorted(artifacts.items()):
        atomic_write(output_root / relative, data)
    return GenerateResult(target=target, artifacts=len(artifacts), hash=aggregate)


def check(root: Path, registry: Registry, target: str) -> GenerateResult:
    repository_path = registry.resolve(root, "env")
    artifacts = render(repository_path, target)
    manifest, aggregate = _make_manifest(target, artifacts)
    artifacts["manifest.json"] = manifest
    output_root = repository_path / "generated" / target
    for relative, expected in sorted(artifacts.items()):
        try:
            actual = (output_root / relative).read_bytes()
        except OSError as error:
            raise WoonError(f"read generated artifact {relative}: {error}") from error
        if actual != expected:
            raise WoonError(f"generated artifact drift: {relative}")
    _audit_adapter_isolation(repository_path)
    return GenerateResult(target=target, artifacts=len(artifacts), hash=aggregate)


def render(repository_path: Path, target: str) -> dict[str, bytes]:
    model = load_model(repository_path, target)
    settings = _render_vscode_settings(repository_path, model)
    bindings = _render_vscode_bindings(model)
    environment_extensions = _strings(model.environment["extensions"])
    vscode_extensions = _mapping(model.vscode["extensions"])
    extension_bytes = (
        "\n".join(sorted(str(vscode_extensions[name]) for name in environment_extensions)) + "\n"
    ).encode()
    keymap = _render_jetbrains_keymap(model, target)
    jetbrains = model.jetbrains
    keymap_name = str(_mapping(jetbrains["keymap"])["name"])
    active_keymap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<application>\n  <component name="KeymapManager">\n'
        f'    <active_keymap name="{keymap_name}" />\n'
        "  </component>\n</application>\n"
    ).encode()
    jetbrains_extensions = _mapping(jetbrains["extensions"])
    plugins = sorted(
        str(jetbrains_extensions[name])
        for name in environment_extensions
        if name in jetbrains_extensions
    )
    plugin_bytes = (("\n".join(plugins) + "\n") if plugins else "").encode()
    return {
        "vscode/settings.json": settings,
        "vscode/keybindings.json": bindings,
        "vscode/extensions.txt": extension_bytes,
        "jetbrains/keymap.xml": keymap,
        "jetbrains/active-keymap.xml": active_keymap,
        "jetbrains/plugins.txt": plugin_bytes,
    }


def jetbrains_key(key: str) -> str:
    tokens = {"cmd": "meta", "[": "open_bracket", "]": "close_bracket"}
    return " ".join(tokens.get(part, part) for part in key.lower().split("+"))


def equal_jetbrains_keymaps(left: bytes, right: bytes) -> bool:
    try:
        return _normalized_keymap(left) == _normalized_keymap(right)
    except ElementTree.ParseError:
        return False


def _normalized_keymap(
    data: bytes,
) -> tuple[tuple[str, str], tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...]]:
    root = ElementTree.fromstring(data)
    metadata = (root.attrib.get("name", ""), root.attrib.get("parent", ""))
    actions: list[tuple[str, tuple[tuple[str, str, str], ...]]] = []
    for action in root.findall("action"):
        shortcuts = sorted(
            (
                shortcut.attrib.get("first-keystroke", ""),
                shortcut.attrib.get("second-keystroke", ""),
                shortcut.attrib.get("remove", ""),
            )
            for shortcut in action.findall("keyboard-shortcut")
        )
        actions.append((action.attrib.get("id", ""), tuple(shortcuts)))
    return metadata, tuple(sorted(actions))


def _render_vscode_settings(repository_path: Path, model: EnvironmentModel) -> bytes:
    try:
        settings = json.loads(
            (repository_path / "adapters/vscode/defaults.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"parse VS Code defaults: {error}") from error
    if not isinstance(settings, dict):
        raise WoonError("VS Code defaults must be an object")
    editor = _mapping(model.environment["editor"])
    for source, raw_target in _mapping(model.vscode["settings"]).items():
        section, name = source.split(".", 1)
        if section != "editor" or name not in editor or not isinstance(raw_target, str):
            raise WoonError(f"unsupported common setting path {source!r}")
        value: Any = editor[name]
        if raw_target == "editor.rulers":
            value = [value]
        settings[raw_target] = value
    languages = _mapping(model.environment["languages"])
    formatter_ids = _mapping(model.vscode["formatter_ids"])
    language_ids = _mapping(model.vscode["language_ids"])
    for language_name in sorted(languages):
        language = _mapping(languages[language_name])
        formatter = str(formatter_ids[str(language["formatter"])])
        for language_id in _strings(language_ids[language_name]):
            settings[f"[{language_id}]"] = {
                "editor.defaultFormatter": formatter,
                "editor.insertSpaces": True,
                "editor.tabSize": int(language["tab_size"]),
            }
    overlay = _mapping(_mapping(model.overlay["adapter_settings"]).get("vscode", {}))
    settings.update(overlay)
    return encode_json(settings)


def _render_vscode_bindings(model: EnvironmentModel) -> bytes:
    output: list[dict[str, str]] = []
    keybindings = _mapping(model.environment["keybindings"])
    actions = _mapping(model.vscode_actions["intents"])
    conflicts = _mapping(model.vscode_actions["remove_conflicts"])
    for intent in sorted(keybindings):
        key = str(keybindings[intent])
        for raw_binding in _list(actions[intent]):
            binding = _mapping(raw_binding)
            item = {"key": key, "command": str(binding["command"])}
            if when := binding.get("when"):
                item["when"] = str(when)
            output.append(item)
        for command in _strings(conflicts.get(intent, [])):
            output.append({"key": key, "command": f"-{command}"})
    return encode_json(output)


def _render_jetbrains_keymap(model: EnvironmentModel, target: str) -> bytes:
    keybindings = _mapping(model.environment["keybindings"])
    actions = _mapping(model.jetbrains_actions["intents"])
    overrides = _mapping(model.jetbrains_actions.get("key_overrides", {}))
    conflicts = _mapping(model.jetbrains_actions["remove_conflicts"])
    additional = _mapping(model.jetbrains_actions.get("additional_shortcuts", {}))
    by_action: dict[str, list[tuple[str, str, str]]] = {}
    for intent in sorted(keybindings):
        key = str(overrides.get(intent, jetbrains_key(str(keybindings[intent]))))
        for action in _strings(actions[intent]):
            by_action.setdefault(action, []).append((key, "", ""))
    for action_identifier, raw_keys in conflicts.items():
        for key in _strings(raw_keys):
            by_action.setdefault(action_identifier, []).append((key, "", "true"))
    for action_identifier, raw_shortcuts in additional.items():
        for raw_shortcut in _list(raw_shortcuts):
            shortcut = _mapping(raw_shortcut)
            by_action.setdefault(action_identifier, []).append(
                (str(shortcut["first"]), str(shortcut.get("second", "")), "")
            )

    keymap = _mapping(model.jetbrains["keymap"])
    root = ElementTree.Element(
        "keymap",
        {
            "version": "1",
            "name": str(keymap["name"]),
            "parent": str(_mapping(keymap["parents"])[target]),
        },
    )
    for action_identifier in sorted(by_action):
        action_element = ElementTree.SubElement(root, "action", {"id": action_identifier})
        for first, second, remove in sorted(by_action[action_identifier]):
            attributes = {"first-keystroke": first}
            if second:
                attributes["second-keystroke"] = second
            if remove:
                attributes["remove"] = remove
            ElementTree.SubElement(action_element, "keyboard-shortcut", attributes)
    ElementTree.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ElementTree.tostring(root) + b"\n"


def _make_manifest(target: str, artifacts: dict[str, bytes]) -> tuple[bytes, str]:
    file_hashes: dict[str, str] = {}
    aggregate = hashlib.sha256()
    for path, data in sorted(artifacts.items()):
        digest = hashlib.sha256(data).digest()
        file_hashes[path] = digest.hex()
        aggregate.update(path.encode())
        aggregate.update(digest)
    return encode_json(
        {"version": 1, "target": target, "files": file_hashes}
    ), aggregate.hexdigest()


def _audit_adapter_isolation(repository_path: Path) -> None:
    model = load_model(repository_path, _runtime_target())
    identifiers: set[str] = set()
    for bindings in _mapping(model.vscode_actions["intents"]).values():
        for binding in _list(bindings):
            identifiers.add(str(_mapping(binding)["command"]))
    for commands in _mapping(model.vscode_actions["remove_conflicts"]).values():
        identifiers.update(_strings(commands))
    for actions in _mapping(model.jetbrains_actions["intents"]).values():
        identifiers.update(_strings(actions))
    extensions = {".go", ".py", ".sh", ".ps1", ".json", ".yaml", ".yml", ".xml"}
    violations: list[str] = []
    for path in sorted(repository_path.rglob("*")):
        relative = path.relative_to(repository_path)
        if any(part in {".git", "generated", "fixtures"} for part in relative.parts):
            continue
        if (
            "adapters" in relative.parts
            or not path.is_file()
            or path.suffix.lower() not in extensions
        ):
            continue
        data = path.read_text(encoding="utf-8", errors="ignore")
        for identifier in identifiers:
            if len(identifier) >= 4 and identifier in data:
                violations.append(f"{relative.as_posix()}: {identifier}")
                break
    if violations:
        raise WoonError("IDE action IDs must stay under adapters: " + ", ".join(violations))


def _runtime_target() -> str:
    return (
        "macos" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux")
    )


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError("expected mapping")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise WoonError("expected list")
    return value


def _strings(value: object) -> list[str]:
    items = _list(value)
    if any(not isinstance(item, str) for item in items):
        raise WoonError("expected list of strings")
    return items
