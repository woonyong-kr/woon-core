from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.registry import Registry, Repository
from woon_core.skills import (
    ClaudeRoutingSelector,
    CodexRoutingSelector,
    evaluate_routing,
    install,
    plan,
    validate,
)
from woon_core.skills.service import CatalogSkill


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def fixture(tmp_path: Path) -> tuple[Path, Registry]:
    root = tmp_path / "workspace with spaces"
    repository = root / "woon-skills"
    write(
        repository / "profiles/core.yaml",
        "version: 1\nname: core\nmax_active: 20\nskills: [skills/common/demo]\n",
    )
    write(
        repository / "conflicts/effects.yaml",
        "version: 1\ndefault: [read]\nskills: {}\n",
    )
    write(repository / "conflicts/conflicts.yaml", "version: 1\ngroups: []\n")
    write(
        repository / "lock/sources.yaml",
        "version: 1\norigins:\n  skills:\n    path: skills\n    policy: maintained\n",
    )
    write(
        repository / "evals/profile-resolution.yaml",
        "version: 1\ncases:\n  - id: core\n    profiles: [core]\n"
        "    expect_skills: [skills/common/demo]\n",
    )
    write(
        repository / "evals/routing/config.yaml",
        "version: 1\nrepeat: 2\nthresholds:\n  primary_recall: 1.0\n"
        "  forbidden_selections: 0\n  agreement: 1.0\n",
    )
    write(
        repository / "evals/routing/common.yaml",
        "version: 1\ncases:\n  - id: demo-request\n"
        "    prompt: Run the demo workflow.\n    profiles: [core]\n"
        "    expect_primary: demo\n    allow_support: []\n    reject: []\n"
        "    max_selected: 1\n",
    )
    write(
        repository / "skills/common/demo/SKILL.md",
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


def test_plan_and_install_reject_evaluation_only_profile(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/profiles/eval.yaml",
        "version: 1\nname: eval\ninstallable: false\nmax_active: 20\n"
        "skills: [skills/common/demo]\n",
    )

    assert plan(root, registry, ["eval"], "").items[0].action == "selected"
    with pytest.raises(WoonError, match="not installable: eval"):
        plan(root, registry, ["eval"], "codex")
    with pytest.raises(WoonError, match="not installable: eval"):
        install(root, registry, ["eval"], "codex")


def test_install_rejects_profile_extending_evaluation_only_parent(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/profiles/eval.yaml",
        "version: 1\nname: eval\ninstallable: false\nmax_active: 20\n"
        "skills: [skills/common/demo]\n",
    )
    write(
        root / "woon-skills/profiles/derived.yaml",
        "version: 1\nname: derived\nextends: [eval]\nmax_active: 20\nskills: []\n",
    )

    with pytest.raises(WoonError, match="not installable: eval"):
        install(root, registry, ["derived"], "claude")


def test_validate_rejects_non_boolean_installable(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/profiles/core.yaml",
        "version: 1\nname: core\ninstallable: disabled\nmax_active: 20\n"
        "skills: [skills/common/demo]\n",
    )
    with pytest.raises(WoonError, match="invalid profile"):
        validate(root, registry, ["core"])


def test_validate_rejects_missing_conflict_member(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/conflicts/conflicts.yaml",
        "version: 1\ngroups:\n  - id: stale\n    mode: exclusive\n"
        "    members: [skills/common/demo, skills/common/missing]\n",
    )
    with pytest.raises(WoonError, match="missing skill"):
        validate(root, registry, ["core"])


def test_validate_rejects_profile_regression(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)
    write(
        root / "woon-skills/evals/profile-resolution.yaml",
        "version: 1\ncases:\n  - id: missing\n    profiles: [core]\n"
        "    expect_skills: [skills/common/missing]\n",
    )
    with pytest.raises(WoonError, match="expected missing skill"):
        validate(root, registry, ["core"])


def test_evaluate_routing_measures_repeatability(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)

    def selector(_catalog: object, prompts: dict[str, str]) -> dict[str, list[str]]:
        return {identifier: ["demo"] for identifier in prompts}

    result = evaluate_routing(root, registry, selector)
    assert result.passed
    assert result.repeat == 2
    assert result.primary_recall == 1.0
    assert result.agreement == 1.0


def test_evaluate_routing_rejects_unknown_skill(tmp_path: Path) -> None:
    root, registry = fixture(tmp_path)

    def selector(_catalog: object, prompts: dict[str, str]) -> dict[str, list[str]]:
        return {identifier: ["missing"] for identifier in prompts}

    with pytest.raises(WoonError, match="unavailable skill"):
        evaluate_routing(root, registry, selector, repeat=1)


def test_codex_selector_uses_isolated_strict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "demo"
    skill_path.mkdir()
    catalog = (CatalogSkill("skills/demo", "demo", "Test skill.", skill_path, "hash", ("read",)),)

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert "--ephemeral" in command
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        schema_path = Path(command[command.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        assert "uniqueItems" not in json.dumps(schema)
        assert schema["properties"]["cases"]["items"]["properties"]["skills"]["items"]["enum"] == [
            "demo"
        ]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"cases":[{"id":"case","skills":["demo"]}]}', encoding="utf-8")
        assert "Available skills:\n- demo: Test skill." in str(options["input"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("woon_core.skills.codex_router.subprocess.run", fake_run)
    assert CodexRoutingSelector()(catalog, {"case": "Run demo."}) == {"case": ["demo"]}


def test_claude_selector_disables_customization_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "demo"
    skill_path.mkdir()
    catalog = (CatalogSkill("skills/demo", "demo", "Test skill.", skill_path, "hash", ("read",)),)

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        assert "--safe-mode" in command
        assert "--no-session-persistence" in command
        assert command[command.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in command
        schema = json.loads(command[command.index("--json-schema") + 1])
        assert "uniqueItems" not in json.dumps(schema)
        assert schema["properties"]["cases"]["items"]["properties"]["skills"]["items"]["enum"] == [
            "demo"
        ]
        assert "Available skills:\n- demo: Test skill." in str(options["input"])
        output = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "structured_output": {"cases": [{"id": "case", "skills": ["demo"]}]},
            }
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr("woon_core.skills.claude_router.subprocess.run", fake_run)
    assert ClaudeRoutingSelector()(catalog, {"case": "Run demo."}) == {"case": ["demo"]}


def test_claude_selector_reports_authentication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "demo"
    skill_path.mkdir()
    catalog = (CatalogSkill("skills/demo", "demo", "Test skill.", skill_path, "hash", ("read",)),)
    output = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "result": "Not logged in · Please run /login",
        }
    )
    monkeypatch.setattr(
        "woon_core.skills.claude_router.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, output, ""),
    )

    with pytest.raises(WoonError, match="Not logged in"):
        ClaudeRoutingSelector()(catalog, {"case": "Run demo."})
