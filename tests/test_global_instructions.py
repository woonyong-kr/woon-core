from __future__ import annotations

from pathlib import Path

import pytest

from woon_core.context.global_instructions import (
    END_MARKER,
    START_MARKER,
    apply,
    check,
    merge_managed_block,
    render_managed_block,
)
from woon_core.errors import WoonError


def write_fixture(root: Path, source: str = "# Woon\n\n- use catalog\n") -> None:
    (root / "woon-core/config").mkdir(parents=True, exist_ok=True)
    (root / "woon-core/config/global-agents.md").write_text(source, encoding="utf-8")
    (root / "woon-core/registry").mkdir(parents=True, exist_ok=True)
    (root / "woon-core/registry/repositories.yaml").write_text(
        "version: 1\nrepositories:\n"
        "  core:\n"
        "    remote: https://github.com/example/core.git\n"
        "    directory: woon-core\n",
        encoding="utf-8",
    )


def test_apply_preserves_human_instructions_and_is_idempotent(tmp_path: Path) -> None:
    write_fixture(tmp_path)
    target = tmp_path / "home/AGENTS.md"
    target.parent.mkdir()
    target.write_text("# Human rules\n\nKeep this.\n", encoding="utf-8")

    first = apply(tmp_path, target)
    second = apply(tmp_path, target)

    body = target.read_text(encoding="utf-8")
    assert first.changed is True
    assert second.changed is False
    assert body.startswith("# Human rules\n\nKeep this.\n")
    assert body.count(START_MARKER) == 1
    assert body.count(END_MARKER) == 1
    assert "use catalog" in body
    check(tmp_path, target)


def test_apply_replaces_only_managed_block(tmp_path: Path) -> None:
    write_fixture(tmp_path, "# Version one\n")
    target = tmp_path / "AGENTS.md"
    apply(tmp_path, target)
    write_fixture(tmp_path, "# Version two\n")

    result = apply(tmp_path, target)

    body = target.read_text(encoding="utf-8")
    assert result.changed is True
    assert "Version one" not in body
    assert "Version two" in body
    assert body.count(START_MARKER) == 1


@pytest.mark.parametrize(
    "existing",
    [
        f"{START_MARKER}\nmissing end\n",
        f"{END_MARKER}\nmissing start\n",
        f"{START_MARKER}\na\n{START_MARKER}\nb\n{END_MARKER}\n{END_MARKER}\n",
    ],
)
def test_merge_rejects_malformed_markers(existing: str) -> None:
    with pytest.raises(WoonError, match="malformed"):
        merge_managed_block(existing, render_managed_block("valid"))


def test_render_rejects_recursive_markers() -> None:
    with pytest.raises(WoonError, match="must not contain"):
        render_managed_block(START_MARKER)


def test_check_detects_drift(tmp_path: Path) -> None:
    write_fixture(tmp_path)
    target = tmp_path / "AGENTS.md"
    apply(tmp_path, target)
    target.write_text(
        target.read_text(encoding="utf-8").replace("use catalog", "skip catalog"),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="drift"):
        check(tmp_path, target)
