import json
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from woon_core import cli
from woon_core.calendar.manual_schedule import UserScheduleRequest
from woon_core.calendar.migration import LegacyCalendarMigrationResult
from woon_core.calendar.projection import CalendarProjectionResult
from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge.codex_daily_digest import CodexDailyDigestResult
from woon_core.knowledge.compiled_wiki import RevisionReconciliationReport
from woon_core.knowledge.mail_schedule_automation import MailScheduleRecordResult
from woon_core.knowledge.orchestration import OrchestratorSettings
from woon_core.knowledge.schedule_bridge import ScheduleReceipt
from woon_core.skills import RoutingCaseResult, RoutingEvalResult


def test_version() -> None:
    output = StringIO()
    run(["version"], output)
    assert output.getvalue().strip() == "0.5.4"


def test_daily_digest_repair_gate_accepts_only_the_requested_missing_fragment() -> None:
    payload = {
        "issues": {
            "daily_digest_projection_violations": [
                "inbox/daily/2026-08-17.md: missing daily digest inbox/daily-digests/2026-08-17.md"
            ],
            "broken_links": [],
        }
    }

    assert cli._is_only_missing_daily_digest_repair(json.dumps(payload), date(2026, 8, 17))
    assert not cli._is_only_missing_daily_digest_repair(json.dumps(payload), date(2026, 8, 16))
    payload["issues"]["broken_links"] = ["brain/home.md: missing target"]
    assert not cli._is_only_missing_daily_digest_repair(json.dumps(payload), date(2026, 8, 17))


def test_daily_digest_repair_records_governance_before_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = SimpleNamespace(vault=tmp_path, policy_document=tmp_path / "policy.md")
    calls: list[str] = []
    monkeypatch.setattr(cli, "resolve_knowledge_vault", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_orchestrator_settings", lambda vault: settings)
    monkeypatch.setattr(cli, "verify_codex_automation_registry", lambda *_: ("daily",))
    monkeypatch.setattr(
        cli,
        "_governance_preflight_evidence",
        lambda *args, **kwargs: ("a" * 64, "b" * 64),
    )
    monkeypatch.setattr(
        cli,
        "record_governance_preflight",
        lambda *_args, **_kwargs: calls.append("governance"),
    )
    monkeypatch.setattr(
        cli,
        "record_codex_daily_digest",
        lambda *_args, **_kwargs: (
            calls.append("digest")
            or CodexDailyDigestResult(
                day="2026-08-17",
                entry_count=0,
                receipt_id="receipt",
                replayed=False,
                relative_path="inbox/daily-digests/2026-08-17.md",
            )
        ),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "record-codex-daily-digest",
            "--vault",
            str(tmp_path),
            "--day",
            "2026-08-17",
            "--entries-json",
            "[]",
            "--repair-missing-digest",
        ],
        output,
    )

    assert calls == ["governance", "digest"]
    assert '"entry_count": 0' in output.getvalue()


def test_unknown_command_fails() -> None:
    with pytest.raises(WoonError, match="unknown command"):
        run(["unknown"], StringIO())


def test_calendar_migrate_legacy_uses_the_native_calendar_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "woon_core.calendar.cli.migrate_legacy_owned_calendar",
        lambda vault: LegacyCalendarMigrationResult(True, "Woon 일정", "event-001"),
    )
    output = StringIO()

    run(["calendar", "migrate-legacy", "--vault", str(tmp_path)], output)

    assert output.getvalue() == (
        "status: ok\nmigrated: true\ncalendar_name: Woon 일정\ncalendar_event_id: event-001\n"
    )


def test_calendar_refresh_reports_markdown_and_ics_projection_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CalendarService:
        def refresh(self) -> CalendarProjectionResult:
            return CalendarProjectionResult(
                changed=False,
                event_count=10,
                relative_path="inbox/calendar/events",
                ics_relative_path="inbox/calendar/apple-calendar.ics",
                start_at=datetime(2026, 8, 18, 0, 0, tzinfo=UTC),
                end_at=datetime(2026, 8, 18, 1, 0, tzinfo=UTC),
            )

    monkeypatch.setattr(
        "woon_core.calendar.cli.build_calendar_projection_service", lambda vault: CalendarService()
    )
    output = StringIO()

    run(["calendar", "refresh", "--vault", str(tmp_path)], output)

    assert output.getvalue() == (
        "status: ok\nchanged: false\nevents: 10\n"
        "calendar_markdown: inbox/calendar/events\n"
        "calendar_ics: inbox/calendar/apple-calendar.ics\n"
    )


def test_knowledge_configure_full_calendar_uses_the_receipt_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def configure_full_calendar_remastered(self) -> dict[str, str]:
            return {"action": "configure-full-calendar-remastered"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()

    run(
        [
            "knowledge",
            "obsidian-plugin",
            "configure-full-calendar-remastered",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured == {"vault": tmp_path}
    assert '"action": "configure-full-calendar-remastered"' in output.getvalue()


def test_knowledge_configure_notion_bases_uses_the_receipt_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def configure_notion_bases_calendar(self) -> dict[str, str]:
            return {"action": "configure-notion-bases-calendar"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()

    run(
        [
            "knowledge",
            "obsidian-plugin",
            "configure-notion-bases-calendar",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured == {"vault": tmp_path}
    assert '"action": "configure-notion-bases-calendar"' in output.getvalue()


def test_knowledge_configure_simple_calendar_uses_the_receipt_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def configure_simple_calendar(self) -> dict[str, str]:
            return {"action": "configure-simple-calendar"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()

    run(
        [
            "knowledge",
            "obsidian-plugin",
            "configure-simple-calendar",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured == {"vault": tmp_path}
    assert '"action": "configure-simple-calendar"' in output.getvalue()


def test_knowledge_install_local_build_uses_the_receipt_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    source = tmp_path / "build"

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def install_local_build(
            self, plugin_id: str, source_directory: Path, expected_version: str
        ) -> dict[str, str]:
            captured.update(
                plugin_id=plugin_id,
                source_directory=source_directory,
                expected_version=expected_version,
            )
            return {"action": "install-local-build"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()

    run(
        [
            "knowledge",
            "obsidian-plugin",
            "install-local-build",
            "--plugin",
            "context-graph",
            "--source-dir",
            str(source),
            "--version",
            "0.4.1",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured == {
        "vault": tmp_path,
        "plugin_id": "context-graph",
        "source_directory": source,
        "expected_version": "0.4.1",
    }
    assert '"action": "install-local-build"' in output.getvalue()


def test_calendar_upsert_uses_one_user_authorized_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def upsert(vault: Path, request: object) -> ScheduleReceipt:
        captured.update(vault=vault, request=request)
        return ScheduleReceipt(
            candidate_id="user-calendar:sample-event",
            lifecycle="create",
            idempotency_key="user-calendar:sample-event",
            calendar_event_id="event-001",
        )

    monkeypatch.setattr("woon_core.calendar.cli.apply_user_authorized_schedule", upsert)
    output = StringIO()
    run(
        [
            "calendar",
            "upsert",
            "--id",
            "sample-event",
            "--title",
            "면접 동행",
            "--start",
            "2026-08-19T11:00:00+09:00",
            "--end",
            "2026-08-19T12:00:00+09:00",
            "--category",
            "relationship",
            "--location",
            "센터필드 East 타워",
            "--notes",
            "신분증을 지참한다.",
            "--display-category",
            "false",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured["vault"] == tmp_path
    request = cast(UserScheduleRequest, captured["request"])
    assert request.event_id == "sample-event"
    assert request.location == "센터필드 East 타워"
    assert request.display_category is False
    assert "calendar_event_id: event-001" in output.getvalue()


def test_knowledge_validate_orchestrator_has_no_runtime_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "docs/second-brain-operating-model.md"
    policy.parent.mkdir()
    policy.write_text("# policy\n", encoding="utf-8")
    captured: dict[str, Path] = {}

    def fake_load(vault: Path) -> OrchestratorSettings:
        captured["vault"] = vault
        return OrchestratorSettings(
            vault=vault,
            policy_document=policy,
            timezone="Asia/Seoul",
            checkpoint_path=tmp_path / ".local/checkpoint.yaml",
            receipt_directory=tmp_path / ".local/receipts",
            lock_directory=tmp_path / ".local/locks",
            policy_sha256="a" * 64,
            automations=(),
        )

    monkeypatch.setattr(cli, "load_orchestrator_settings", fake_load)
    output = StringIO()
    run(["knowledge", "validate-orchestrator", "--vault", str(tmp_path)], output)

    assert captured == {"vault": tmp_path}
    assert '"status": "ok"' in output.getvalue()
    assert not (tmp_path / ".local").exists()


def test_knowledge_validate_orchestrator_can_verify_registered_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "docs/second-brain-operating-model.md"
    policy.parent.mkdir()
    policy.write_text("# policy\n", encoding="utf-8")
    settings = OrchestratorSettings(
        vault=tmp_path,
        policy_document=policy,
        timezone="Asia/Seoul",
        checkpoint_path=tmp_path / ".local/checkpoint.yaml",
        receipt_directory=tmp_path / ".local/receipts",
        lock_directory=tmp_path / ".local/locks",
        policy_sha256="a" * 64,
        automations=(),
    )
    captured: dict[str, Path] = {}
    monkeypatch.setattr(cli, "load_orchestrator_settings", lambda _vault: settings)

    def verify(actual: OrchestratorSettings, root: Path) -> tuple[str, ...]:
        assert actual is settings
        captured["root"] = root
        return ("mail-schedule-candidates",)

    monkeypatch.setattr(cli, "verify_codex_automation_registry", verify)
    registry = tmp_path / "automations"
    output = StringIO()
    run(
        [
            "knowledge",
            "validate-orchestrator",
            "--vault",
            str(tmp_path),
            "--automation-root",
            str(registry),
        ],
        output,
    )

    assert captured == {"root": registry}
    assert '"codex_registry_verified": [' in output.getvalue()


def test_knowledge_schedule_apply_requires_one_policy_authorized_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "brain/review/schedule-apply/candidate-001.json"
    captured: dict[str, object] = {}

    def apply(vault: Path, path: Path) -> ScheduleReceipt:
        captured.update(vault=vault, path=path)
        return ScheduleReceipt(
            candidate_id="candidate-001",
            lifecycle="create",
            idempotency_key="schedule-001",
            calendar_event_id="event-001",
        )

    monkeypatch.setattr(cli, "apply_policy_authorized_schedule_candidate", apply)
    output = StringIO()
    run(
        [
            "knowledge",
            "schedule-apply",
            "--vault",
            str(tmp_path),
            "--candidate",
            str(candidate),
        ],
        output,
    )

    assert captured == {"vault": tmp_path, "path": candidate}
    assert '"calendar_event_id": "event-001"' in output.getvalue()


def test_knowledge_records_empty_mail_window_through_the_local_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def record(vault: Path, *, run_token: str, submissions: tuple[object, ...]):
        captured.update(vault=vault, run_token=run_token, submissions=submissions)
        return MailScheduleRecordResult(candidate_count=0, receipt_id="a" * 64, replayed=False)

    monkeypatch.setattr(cli, "record_mail_schedule_candidates", record)
    output = StringIO()

    run(
        [
            "knowledge",
            "record-mail-schedule-candidates",
            "--vault",
            str(tmp_path),
            "--run-token",
            "mail-kst-20260817-0625",
        ],
        output,
    )

    assert captured == {
        "vault": tmp_path,
        "run_token": "mail-kst-20260817-0625",
        "submissions": (),
    }
    assert '"candidate_count": 0' in output.getvalue()


@pytest.mark.parametrize("command", ["source-plan", "source-audit"])
def test_knowledge_rejects_current_vault_as_external_source(tmp_path: Path, command: str) -> None:
    arguments = [
        "knowledge",
        command,
        "--source",
        str(tmp_path),
        "--source-name",
        "vault",
        "--vault",
        str(tmp_path),
    ]

    with pytest.raises(WoonError, match="self-source catalog is retired"):
        run(arguments, StringIO())


@pytest.mark.parametrize("source_kind", ["child", "parent", "symlink-child"])
def test_knowledge_rejects_external_source_path_that_overlaps_vault(
    tmp_path: Path, source_kind: str
) -> None:
    vault = tmp_path / "vault"
    source = vault / "sources"
    source.mkdir(parents=True)
    if source_kind == "child":
        candidate = source
    elif source_kind == "parent":
        candidate = tmp_path
    else:
        candidate = tmp_path / "source-link"
        candidate.symlink_to(source, target_is_directory=True)

    with pytest.raises(WoonError, match="self-source catalog is retired"):
        run(
            [
                "knowledge",
                "source-audit",
                "--source",
                str(candidate),
                "--source-name",
                "external",
                "--vault",
                str(vault),
            ],
            StringIO(),
        )


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


def test_knowledge_reconcile_superseded_revisions_uses_compiler_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    service = SimpleNamespace(
        reconcile_superseded_compiled_wiki_revisions=lambda: RevisionReconciliationReport(
            archived_sources=2,
            superseded_claims=2,
        )
    )
    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), service),
    )

    output = StringIO()
    run(["knowledge", "reconcile-superseded-revisions", "--vault", str(vault)], output)

    assert '"archived_sources": 2' in output.getvalue()
    assert '"superseded_claims": 2' in output.getvalue()


def test_knowledge_evaluate_uses_explicit_cases_and_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases.json"
    cases.write_text("{}", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    captured: dict[str, Path] = {}

    def fake_evaluate(actual_vault: Path, actual_cases: Path) -> dict[str, object]:
        captured["vault"] = actual_vault
        captured["cases"] = actual_cases
        return {"passed": True, "metrics": {"recall_at_k": 1.0}}

    monkeypatch.setattr(cli, "evaluate_knowledge", fake_evaluate)

    output = StringIO()
    run(["knowledge", "evaluate", "--vault", str(vault), "--cases", str(cases)], output)

    assert captured == {"vault": vault.resolve(), "cases": cases.resolve()}
    assert '"passed": true' in output.getvalue()


def test_knowledge_evaluate_answers_uses_explicit_payload_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases.json"
    answers = tmp_path / "answers.json"
    vault = tmp_path / "vault"
    cases.write_text("{}", encoding="utf-8")
    answers.write_text("{}", encoding="utf-8")
    vault.mkdir()
    captured: dict[str, Path] = {}

    def fake_evaluate(
        actual_vault: Path, actual_cases: Path, actual_answers: Path
    ) -> dict[str, object]:
        captured["vault"] = actual_vault
        captured["cases"] = actual_cases
        captured["answers"] = actual_answers
        return {"passed": True, "mechanical": {"passed": True}}

    monkeypatch.setattr(cli, "evaluate_answer_citations", fake_evaluate)

    output = StringIO()
    run(
        [
            "knowledge",
            "evaluate-answers",
            "--vault",
            str(vault),
            "--cases",
            str(cases),
            "--answers",
            str(answers),
        ],
        output,
    )

    assert captured == {
        "vault": vault.resolve(),
        "cases": cases.resolve(),
        "answers": answers.resolve(),
    }
    assert '"passed": true' in output.getvalue()


def test_knowledge_evaluate_quality_uses_explicit_review_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    vault = tmp_path / "vault"
    reviews.write_text("{}", encoding="utf-8")
    standard.write_text("standard", encoding="utf-8")
    prompt.write_text("prompt", encoding="utf-8")
    vault.mkdir()
    captured: dict[str, Path] = {}

    def fake_evaluate(
        actual_vault: Path, actual_reviews: Path, actual_standard: Path, actual_prompt: Path
    ) -> dict[str, object]:
        captured["vault"] = actual_vault
        captured["reviews"] = actual_reviews
        captured["standard"] = actual_standard
        captured["prompt"] = actual_prompt
        return {"passed": True, "coverage": {"compiled_pages": 1}}

    monkeypatch.setattr(cli, "evaluate_content_quality", fake_evaluate)

    output = StringIO()
    run(
        [
            "knowledge",
            "evaluate-quality",
            "--vault",
            str(vault),
            "--reviews",
            str(reviews),
            "--standard",
            str(standard),
            "--prompt",
            str(prompt),
        ],
        output,
    )

    assert captured == {
        "vault": vault.resolve(),
        "reviews": reviews.resolve(),
        "standard": standard.resolve(),
        "prompt": prompt.resolve(),
    }
    assert '"passed": true' in output.getvalue()


def test_quality_review_plan_uses_immutable_input_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    output_dir = tmp_path / "plan"
    vault.mkdir()
    standard.write_text("standard", encoding="utf-8")
    prompt.write_text("prompt", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_plan(*arguments: object) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"compiled_pages": 2, "batches": 1}

    monkeypatch.setattr(cli, "create_content_quality_review_plan", fake_plan)

    output = StringIO()
    run(
        [
            "knowledge",
            "quality-review-plan",
            "--vault",
            str(vault),
            "--standard",
            str(standard),
            "--prompt",
            str(prompt),
            "--output",
            str(output_dir),
            "--batch-size",
            "4",
            "--max-batch-chars",
            "12000",
        ],
        output,
    )

    assert captured["arguments"] == (
        vault.resolve(),
        standard.resolve(),
        "repo://skills/standards/learning-writing-harness.md",
        prompt.resolve(),
        "repo://skills/standards/learning-quality-review-prompt.md",
        output_dir.resolve(),
        4,
        12000,
    )
    assert '"compiled_pages": 2' in output.getvalue()


def test_quality_review_plan_defaults_to_one_page_per_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    output_dir = tmp_path / "plan"
    vault.mkdir()
    standard.write_text("standard", encoding="utf-8")
    prompt.write_text("prompt", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_plan(*arguments: object) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"compiled_pages": 1, "batches": 1}

    monkeypatch.setattr(cli, "create_content_quality_review_plan", fake_plan)

    run(
        [
            "knowledge",
            "quality-review-plan",
            "--vault",
            str(vault),
            "--standard",
            str(standard),
            "--prompt",
            str(prompt),
            "--output",
            str(output_dir),
        ],
        StringIO(),
    )

    assert captured["arguments"][-2:] == (1, 24_000)


def test_quality_review_assembly_uses_explicit_evaluator_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    standard = tmp_path / "standard.md"
    output_path = tmp_path / "reviews.json"
    vault.mkdir()
    results.mkdir()
    plan.write_text("{}", encoding="utf-8")
    standard.write_text("standard", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_assemble(*arguments: object) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"compiled_pages": 2, "reviews": 2}

    monkeypatch.setattr(cli, "assemble_content_quality_reviews", fake_assemble)

    output = StringIO()
    run(
        [
            "knowledge",
            "assemble-quality-reviews",
            "--vault",
            str(vault),
            "--plan",
            str(plan),
            "--results",
            str(results),
            "--standard",
            str(standard),
            "--evaluator-name",
            "local-judge",
            "--evaluator-version",
            "1.0",
            "--output",
            str(output_path),
        ],
        output,
    )

    assert captured["arguments"] == (
        vault.resolve(),
        plan.resolve(),
        results.resolve(),
        standard.resolve(),
        "local-judge",
        "1.0",
        output_path.resolve(),
    )
    assert '"reviews": 2' in output.getvalue()


def test_ollama_quality_review_uses_explicit_batch_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    plan.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_review(*arguments: object, **keywords: object) -> dict[str, object]:
        captured["arguments"] = arguments
        captured["keywords"] = keywords
        return {"reviewed_pages": 1}

    monkeypatch.setattr(cli, "run_ollama_quality_reviews", fake_review)

    output = StringIO()
    run(
        [
            "knowledge",
            "review-quality-ollama",
            "--plan",
            str(plan),
            "--results",
            str(results),
            "--model",
            "qwen3:4b-instruct",
            "--batch",
            "quality-001",
            "--timeout-seconds",
            "720",
            "--adaptive-context",
            "true",
        ],
        output,
    )

    assert captured == {
        "arguments": (plan.resolve(), results.resolve()),
        "keywords": {
            "model": "qwen3:4b-instruct",
            "timeout_seconds": 720,
            "max_attempts": 3,
            "context_tokens": 32_768,
            "adaptive_context": True,
            "continue_on_error": False,
            "batch_ids": ("quality-001",),
        },
    }
    assert '"reviewed_pages": 1' in output.getvalue()


def test_codex_quality_review_uses_chatgpt_subscription_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    plan.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_review(*arguments: object, **keywords: object) -> dict[str, object]:
        captured["arguments"] = arguments
        captured["keywords"] = keywords
        return {"reviewed_pages": 3}

    monkeypatch.setattr(cli, "run_codex_quality_reviews", fake_review)
    output = StringIO()
    run(
        [
            "knowledge",
            "review-quality-codex",
            "--plan",
            str(plan),
            "--results",
            str(results),
            "--batch",
            "quality-001",
            "--timeout-seconds",
            "720",
        ],
        output,
    )

    assert captured == {
        "arguments": (plan.resolve(), results.resolve()),
        "keywords": {
            "model": None,
            "codex_binary": "codex",
            "timeout_seconds": 720,
            "max_attempts": 1,
            "continue_on_error": False,
            "batch_ids": ("quality-001",),
        },
    }
    assert '"reviewed_pages": 3' in output.getvalue()
