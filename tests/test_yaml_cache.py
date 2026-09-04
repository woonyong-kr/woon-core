from __future__ import annotations

import os
from pathlib import Path

from woon_core.knowledge.yaml_cache import load_yaml_file, load_yaml_text


def test_load_yaml_file_invalidates_from_current_bytes(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("value: one\n", encoding="utf-8")
    original_stat = path.stat()

    first = load_yaml_file(path)
    first["value"] = "mutated"
    assert load_yaml_file(path) == {"value": "one"}

    path.write_text("value: two\n", encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert load_yaml_file(path) == {"value": "two"}


def test_load_yaml_text_returns_isolated_values() -> None:
    first = load_yaml_text("nested:\n  items:\n    - one\n")
    first["nested"]["items"].append("mutated")

    assert load_yaml_text("nested:\n  items:\n    - one\n") == {"nested": {"items": ["one"]}}
