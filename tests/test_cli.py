from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core import cli
from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.skills import RoutingCaseResult, RoutingEvalResult


def test_version() -> None:
    output = StringIO()
    run(["version"], output)
    assert output.getvalue().strip() == "0.5.4"


def test_unknown_command_fails() -> None:
    with pytest.raises(WoonError, match="unknown command"):
        run(["unknown"], StringIO())


def test_skills_eval_routing_reports_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    result = RoutingEvalResult(
        repeat=2,
        primary_recall=1.0,
        forbidden_selections=0,
        agreement=1.0,
        passed=True,
        cases=(RoutingCaseResult("case", "demo", (("demo",), ("demo",)), True),),
    )
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _root: (SimpleNamespace(root=Path("/workspace")), object()),
    )
    monkeypatch.setattr(cli, "evaluate_routing", lambda *_args, **_kwargs: result)

    output = StringIO()
    run(["skills", "eval-routing", "--executor", "codex", "--repeat", "2"], output)
    assert "executor: codex" in output.getvalue()
    assert "status: ok" in output.getvalue()
    assert "primary_recall: 1.0000" in output.getvalue()


def test_skills_eval_routing_defaults_to_both_executors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = RoutingEvalResult(
        repeat=1,
        primary_recall=1.0,
        forbidden_selections=0,
        agreement=1.0,
        passed=True,
        cases=(RoutingCaseResult("case", "demo", (("demo",),), True),),
    )
    monkeypatch.setattr(
        cli,
        "_load",
        lambda _root: (SimpleNamespace(root=Path("/workspace")), object()),
    )
    calls: list[str] = []

    def fake_evaluate(*args: object, **_kwargs: object) -> RoutingEvalResult:
        calls.append(type(args[2]).__name__)
        return result

    monkeypatch.setattr(cli, "evaluate_routing", fake_evaluate)
    output = StringIO()
    run(["skills", "eval-routing", "--repeat", "1"], output)

    assert calls == ["CodexRoutingSelector", "ClaudeRoutingSelector"]
    assert "executor: codex" in output.getvalue()
    assert "executor: claude" in output.getvalue()


def test_skills_eval_routing_rejects_invalid_repeat() -> None:
    with pytest.raises(WoonError, match="positive integer"):
        run(["skills", "eval-routing", "--repeat", "0"], StringIO())


def test_skills_eval_routing_rejects_invalid_executor() -> None:
    with pytest.raises(WoonError, match="all, codex, or claude"):
        run(["skills", "eval-routing", "--executor", "other"], StringIO())
