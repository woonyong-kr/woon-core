"""Workspace discovery and initialization."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, load_yaml

MARKER_NAME = ".woon-root"


@dataclass(frozen=True, slots=True)
class Workspace:
    root: Path
    source: str


def discover(cli_root: str = "") -> Workspace:
    candidates: list[tuple[str, str]] = []

    def add(root: str, source: str) -> None:
        if root.strip():
            candidates.append((root, source))

    add(cli_root, "--root")
    add(os.environ.get("WOON_HOME", ""), "WOON_HOME")
    add(_read_configured_root(), "config")
    marker = _find_marker()
    if marker is not None:
        add(str(marker), MARKER_NAME)
    if not candidates:
        add(str(Path.home() / "workspace" / "woon"), "default")

    resolved: dict[Path, list[str]] = {}
    for raw_root, source in candidates:
        root = _canonical(raw_root)
        resolved.setdefault(root, []).append(source)
    if len(resolved) != 1:
        descriptions = [
            f"{root} ({', '.join(sources)})" for root, sources in sorted(resolved.items())
        ]
        raise WoonError(f"ambiguous workspace roots: {'; '.join(descriptions)}")
    root, sources = next(iter(resolved.items()))
    return Workspace(root=root, source="+".join(sources))


def initialize(path: str) -> Path:
    root = _canonical(path)
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / MARKER_NAME, b"version: 1\n")
    config_path = _platform_config_path()
    data = yaml.safe_dump({"root": str(root)}, allow_unicode=True, sort_keys=True).encode()
    atomic_write(config_path, data, mode=0o600)
    return root


def _read_configured_root() -> str:
    path = _platform_config_path()
    if not path.exists():
        return ""
    value = load_yaml(path).get("root", "")
    if not isinstance(value, str):
        raise WoonError(f"{path}: root must be a string")
    return value


def _platform_config_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if not base:
            raise WoonError("APPDATA is not set")
        return Path(base) / "Woon" / "config.yaml"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "woon" / "config.yaml"


def _find_marker() -> Path | None:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / MARKER_NAME).exists():
            return candidate
    return None


def _canonical(path: str) -> Path:
    return Path(path).expanduser().resolve(strict=False)
