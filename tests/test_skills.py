from __future__ import annotations

from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.registry import Registry, Repository
from woon_core.skills import install, plan, validate


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fixture(tmp_path: Path) -> tuple[Path, Registry]:
    root = tmp_path / "workspace with spaces"
    repository = root / "woon-skills"
    write(
        repository / "profiles/core.yaml",
        "version: 1\nname: core\nmax_active: 20\nskills: [personal/demo]\n",
    )
    write(
        repository / "conflicts/effects.yaml",
        "version: 1\ndefault: [read]\nskills: {}\n",
    )
    write(repository / "conflicts/conflicts.yaml", "version: 1\ngroups: []\n")
    write(
        repository / "lock/sources.yaml",
        "version: 1\norigins:\n  personal:\n    path: personal\n    policy: maintained\n",
    )
    write(
        repository / "evals/routing.yaml",
        "version: 1\ncases:\n  - id: core\n    profiles: [core]\n"
        "    expect_skills: [personal/demo]\n",
    )
    write(
        repository / "personal/demo/SKILL.md",
        "---\nname: demo\ndescription: Test skill.\n---\n\n# Demo\n",
    )
    registry = Registry(
        version=1,
        repositories={
            "skills": Repository("https://github.com/example/woon-skills.git", "woon-skills")
        },
    )
    return root, registry


def test_install_detects_drift_and_repairs_missing_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, registry = fixture(tmp_path)
    target = tmp_path / "target with spaces/codex"
    monkeypatch.setenv("WOON_CODEX_SKILLS_HOME", str(target))
    assert plan(root, registry, ["core"], "codex").items[0].action == "install"
    install(root, registry, ["core"], "codex")
    assert plan(root, registry, ["core"], "codex").items[0].action == "unchanged"

    (target / "demo/SKILL.md").write_text("drift\n")
    assert plan(root, registry, ["core"], "codex").items[0].action == "update"
    result = install(root, registry, ["core"], "codex")
    assert result.backup is not None

    for path in (target / "demo").rglob("*"):
        if path.is_file():
            path.unlink()
    (target / "demo").rmdir()
    assert plan(root, registry, ["core"], "codex").items[0].action == "repair"
    install(root, registry, ["core"], "codex")


def test_install_refuses_unmanaged_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, registry = fixture(tmp_path)
    target = tmp_path / "codex"
    (target / "demo").mkdir(parents=True)
    monkeypatch.setenv("WOON_CODEX_SKILLS_HOME", str(target))
    assert plan(root, registry, ["core"], "codex").items[0].action == "blocked"
    with pytest.raises(WoonError, match="unmanaged"):
        install(root, registry, ["core"], "codex")


def test_validate_rejects_missing_conflict_member(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/conflicts/conflicts.yaml",
        "version: 1\ngroups:\n  - id: stale\n    mode: exclusive\n"
        "    members: [personal/demo, personal/missing]\n",
    )
    with pytest.raises(WoonError, match="missing skill"):
        validate(root, registry, ["core"])


def test_validate_rejects_routing_regression(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/evals/routing.yaml",
        "version: 1\ncases:\n  - id: missing\n    profiles: [core]\n"
        "    expect_skills: [personal/missing]\n",
    )
    with pytest.raises(WoonError, match="expected missing skill"):
        validate(root, registry, ["core"])
