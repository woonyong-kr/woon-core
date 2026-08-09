from __future__ import annotations

from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.workspace import discover, initialize


def test_discover_accepts_same_root_from_multiple_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WOON_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    workspace = discover(str(tmp_path))
    assert workspace.root == tmp_path
    assert workspace.source == "--root+WOON_HOME"


def test_discover_rejects_ambiguous_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    monkeypatch.setenv("WOON_HOME", str(other))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    with pytest.raises(WoonError, match="ambiguous workspace roots"):
        discover(str(tmp_path))


def test_initialize_persists_portable_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_home = tmp_path / "config"
    root = tmp_path / "workspace with spaces"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.chdir(tmp_path)
    assert initialize(str(root)) == root
    assert (root / ".woon-root").read_text() == "version: 1\n"
    assert discover(str(root)).root == root
