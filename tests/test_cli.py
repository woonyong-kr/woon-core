import hashlib
import json
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from woon_core import cli
from woon_core.calendar.constants import LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS
from woon_core.calendar.manual_schedule import UserScheduleRequest
from woon_core.calendar.migration import LegacyCalendarMigrationResult
from woon_core.calendar.projection import CalendarProjectionResult
from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge.book_contract import (
    PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256,
    PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
    book_promotion_contract_fields,
)
from woon_core.knowledge.book_rights import BookRightsRestorationReport
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    CompileReport,
    CuratedRevisionReport,
    RevisionReconciliationReport,
    StagedBookAsset,
    VerifiedBookPage,
    VerifiedBookPreflightReport,
    VerifiedBookUpdateReport,
)
from woon_core.knowledge.mail_schedule_automation import MailScheduleRecordResult
from woon_core.knowledge.orchestration import OrchestratorSettings
from woon_core.knowledge.schedule_bridge import ScheduleReceipt
from woon_core.skills import RoutingCaseResult, RoutingEvalResult


def test_version() -> None:
    output = StringIO()
    run(["version"], output)
    assert output.getvalue().strip() == "0.5.6"


def test_unknown_command_fails() -> None:
    with pytest.raises(WoonError, match="unknown command"):
        run(["unknown"], StringIO())


def test_governance_skill_inventory_rejects_installed_copy_drift(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "woon-skills/skills/knowledge/archive/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: archive\ndescription: canonical\n---\n", encoding="utf-8")
    catalog = workspace / "woon-skills/catalog.json"
    catalog.write_text("{}\n", encoding="utf-8")
    installed = tmp_path / "installed/archive/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("---\nname: archive\ndescription: stale\n---\n", encoding="utf-8")

    with pytest.raises(WoonError, match="installed skill drift: archive"):
        cli._governance_skill_inventory(workspace, installed_root=tmp_path / "installed")

    installed.write_bytes(source.read_bytes())
    inventory = cli._governance_skill_inventory(workspace, installed_root=tmp_path / "installed")
    assert inventory == (catalog, source, installed)


def test_active_instruction_files_excludes_archived_source_evidence(tmp_path: Path) -> None:
    repository = tmp_path / "woon-knowledge"
    active = repository / "AGENTS.md"
    nested = repository / "docs/CLAUDE.md"
    archived = repository / "wiki/private/_sources/legacy/AGENTS.md"
    local = repository / ".local/snapshot/CLAUDE.md"
    for path in (active, nested, archived, local):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("instruction", encoding="utf-8")

    assert cli._active_instruction_files(repository) == (active, nested)


def test_retired_daily_digest_commands_are_not_cli_entrypoints() -> None:
    with pytest.raises(WoonError, match="unknown knowledge command"):
        run(["knowledge", "materialize-codex-daily-digest"], StringIO())
    with pytest.raises(WoonError, match="unknown knowledge command"):
        run(["knowledge", "record-codex-daily-digest"], StringIO())


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


def test_knowledge_configure_link_calendar_uses_the_receipt_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def configure_link_calendar(self) -> dict[str, str]:
            return {"action": "configure-link-calendar"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()

    run(
        [
            "knowledge",
            "obsidian-plugin",
            "configure-link-calendar",
            "--vault",
            str(tmp_path),
        ],
        output,
    )

    assert captured == {"vault": tmp_path}
    assert '"action": "configure-link-calendar"' in output.getvalue()


def test_knowledge_attest_link_calendar_runtime_records_manual_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class PluginService:
        def __init__(self, vault: Path) -> None:
            captured["vault"] = vault

        def attest_link_calendar_runtime(self, checks: list[str]) -> dict[str, str]:
            captured["checks"] = checks
            return {"action": "attest-link-calendar-runtime"}

    monkeypatch.setattr(cli, "ObsidianPluginService", PluginService)
    output = StringIO()
    arguments = ["knowledge", "obsidian-plugin", "attest-link-calendar-runtime"]
    for check in LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS:
        arguments.extend(("--attested-check", check))
    arguments.extend(("--vault", str(tmp_path)))

    run(arguments, output)

    assert captured == {
        "vault": tmp_path,
        "checks": list(LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS),
    }
    assert '"action": "attest-link-calendar-runtime"' in output.getvalue()


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
            wiki_contract=policy,
            timezone="Asia/Seoul",
            checkpoint_path=tmp_path / ".local/checkpoint.yaml",
            receipt_directory=tmp_path / ".local/receipts",
            lock_directory=tmp_path / ".local/locks",
            policy_sha256="a" * 64,
            automations=(),
        )

    monkeypatch.setattr(cli, "load_orchestrator_settings", fake_load)
    automation_root = tmp_path / "codex-home" / "automations"
    monkeypatch.setenv("CODEX_HOME", str(automation_root.parent))
    monkeypatch.setattr(cli, "verify_codex_automation_registry", lambda _settings, root: ())
    output = StringIO()
    run(["knowledge", "validate-orchestrator", "--vault", str(tmp_path)], output)

    assert captured == {"vault": tmp_path}
    assert '"status": "ok"' in output.getvalue()
    assert f'"codex_registry_root": "{automation_root}"' in output.getvalue()
    assert not (tmp_path / ".local").exists()


def test_knowledge_validate_orchestrator_uses_codex_home_registry_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "docs/second-brain-operating-model.md"
    policy.parent.mkdir()
    policy.write_text("# policy\n", encoding="utf-8")
    settings = OrchestratorSettings(
        vault=tmp_path,
        policy_document=policy,
        wiki_contract=policy,
        timezone="Asia/Seoul",
        checkpoint_path=tmp_path / ".local/checkpoint.yaml",
        receipt_directory=tmp_path / ".local/receipts",
        lock_directory=tmp_path / ".local/locks",
        policy_sha256="a" * 64,
        automations=(),
    )
    captured: dict[str, Path] = {}
    monkeypatch.setattr(cli, "load_orchestrator_settings", lambda _vault: settings)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-codex"))

    def verify(actual: OrchestratorSettings, root: Path) -> tuple[str, ...]:
        assert actual is settings
        captured["root"] = root
        return ()

    monkeypatch.setattr(cli, "verify_codex_automation_registry", verify)
    run(["knowledge", "validate-orchestrator", "--vault", str(tmp_path)], StringIO())

    assert captured == {"root": tmp_path / "isolated-codex" / "automations"}


def test_knowledge_validate_orchestrator_can_verify_registered_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "docs/second-brain-operating-model.md"
    policy.parent.mkdir()
    policy.write_text("# policy\n", encoding="utf-8")
    settings = OrchestratorSettings(
        vault=tmp_path,
        policy_document=policy,
        wiki_contract=policy,
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


def test_knowledge_compile_accepts_exact_page_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    captured: dict[str, object] = {}

    class Service:
        def compile(self, *, force: bool = False, page_ids: tuple[str, ...] = ()) -> CompileReport:
            captured["force"] = force
            captured["page_ids"] = page_ids
            return CompileReport(compiled=2, unchanged=0, page_ids=page_ids)

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), Service()),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "compile",
            "--force",
            "--page",
            "ai/evaluation-benchmarks",
            "--page",
            "security/oauth2",
            "--vault",
            str(vault),
        ],
        output,
    )

    assert captured == {
        "force": True,
        "page_ids": ("ai/evaluation-benchmarks", "security/oauth2"),
    }
    assert json.loads(output.getvalue())["page_ids"] == [
        "ai/evaluation-benchmarks",
        "security/oauth2",
    ]


def test_knowledge_book_promote_parses_verified_pages_and_uses_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coverage_path = vault / "catalog/book-coverage/kotlin.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_path.write_text("{}\n", encoding="utf-8")
    coverage_replacement = {
        "schema_version": 3,
        "book_id": "books/kotlin",
        "workflow_phase": "source-landed",
        "translation_required": False,
        "nodes": [],
    }
    payload = tmp_path / "promotion.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "books/kotlin/chapter-01",
                        "title": "1장 Kotlin",
                        "body": "## 설명\n\n실행 결과를 확인한다.\n",
                        "statement": "실행 결과를 설명한다.",
                        "current_use": "Kotlin 첫 장을 학습할 때 사용한다.",
                        "source_locator": "source://kotlin#pdf-page=1",
                        "source_sha256": "a" * 64,
                        "frontmatter": {
                            "access": "local-only",
                            "parent": "[[wiki/books/kotlin|Kotlin]]",
                        },
                        "expected_revision": None,
                    }
                ],
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": hashlib.sha256(b"{}\n").hexdigest(),
                    "replacement": coverage_replacement,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[VerifiedBookPage] = []
    coverage_calls: list[BookCoverageManifestUpdate] = []

    class FakeService:
        def promote_verified_book_pages(
            self,
            pages: tuple[VerifiedBookPage, ...],
            coverage_manifest: BookCoverageManifestUpdate,
            staged_assets: tuple[object, ...],
        ) -> CuratedRevisionReport:
            assert staged_assets == ()
            calls.extend(pages)
            coverage_calls.append(coverage_manifest)
            return CuratedRevisionReport(curated=1, compiled=1, unchanged=0, page_ids=("one",))

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), FakeService()),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "book-promote",
            "--input",
            str(payload),
            "--vault",
            str(vault),
        ],
        output,
    )

    assert len(calls) == 1
    assert calls[0].page_id == "books/kotlin/chapter-01"
    assert len(coverage_calls) == 1
    assert coverage_calls[0].replacement == coverage_replacement
    assert '"compiled": 1' in output.getvalue()


def test_book_promotion_coverage_parser_accepts_new_manifest_with_null_revision(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "catalog/book-coverage").mkdir(parents=True)

    update = cli._parse_book_coverage_manifest_update(  # noqa: SLF001
        {
            "mode": "replace",
            "relative_path": "catalog/book-coverage/new-book.json",
            "expected_sha256": None,
            "replacement": {"book_id": "books/new-book"},
        },
        vault,
    )
    assert update == BookCoverageManifestUpdate(
        mode="replace",
        relative_path="catalog/book-coverage/new-book.json",
        expected_sha256=None,
        replacement={"book_id": "books/new-book"},
    )


def test_book_promotion_coverage_parser_accepts_explicit_merge_scope(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    base_relative_path = "catalog/book-coverage/kotlin.json"
    base_path = vault / base_relative_path
    base_path.parent.mkdir(parents=True)
    base_bytes = b'{"book_id":"books/kotlin","nodes":[]}\n'
    base_path.write_bytes(base_bytes)
    base_sha256 = hashlib.sha256(base_bytes).hexdigest()
    scope_root_id = "books/kotlin/chapter-02"
    replacement = {
        "schema_version": 2,
        "book_id": "books/kotlin",
        "coverage_scope": {
            "root_id": scope_root_id,
            "base_relative_path": base_relative_path,
            "base_sha256": base_sha256,
        },
    }

    update = cli._parse_book_coverage_manifest_update(  # noqa: SLF001
        {
            "mode": "merge-scope",
            "relative_path": "catalog/book-coverage-scopes/kotlin/chapter-02.json",
            "expected_sha256": None,
            "base_relative_path": base_relative_path,
            "base_expected_sha256": base_sha256,
            "scope_root_id": scope_root_id,
            "replacement": replacement,
        },
        vault,
    )

    assert update == BookCoverageManifestUpdate(
        mode="merge-scope",
        relative_path="catalog/book-coverage-scopes/kotlin/chapter-02.json",
        expected_sha256=None,
        replacement=replacement,
        base_relative_path=base_relative_path,
        base_expected_sha256=base_sha256,
        scope_root_id=scope_root_id,
    )


def test_book_promotion_coverage_parser_rejects_existing_manifest_with_null_revision(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    path = vault / "catalog/book-coverage/new-book.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(WoonError, match="existing book promotion coverage manifest requires"):
        cli._parse_book_coverage_manifest_update(  # noqa: SLF001
            {
                "mode": "replace",
                "relative_path": "catalog/book-coverage/new-book.json",
                "expected_sha256": None,
                "replacement": {"book_id": "books/new-book"},
            },
            vault,
        )


def test_knowledge_book_promote_preflights_without_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coverage_path = vault / "catalog/book-coverage/kotlin.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_bytes = b'{}\n'
    coverage_path.write_bytes(coverage_bytes)
    staged_path = tmp_path / "staged-source-assets/figure.png"
    staged_path.parent.mkdir()
    staged_bytes = b"source figure"
    staged_path.write_bytes(staged_bytes)
    staged_sha256 = hashlib.sha256(staged_bytes).hexdigest()
    payload = tmp_path / "preview.json"
    payload.write_text(
        json.dumps(
            {
                "apply": False,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "books/kotlin/chapter-01",
                        "title": "1장 Kotlin",
                        "body": "## 설명\n\n실행 결과를 확인한다.\n",
                        "statement": "실행 결과를 설명한다.",
                        "current_use": "Kotlin 첫 장을 학습할 때 사용한다.",
                        "source_locator": "source://kotlin#pdf-page=1",
                        "source_sha256": "a" * 64,
                        "frontmatter": {"access": "local-only"},
                        "expected_revision": None,
                    }
                ],
                "staged_assets": [
                    {
                        "staging_relative_path": "staged-source-assets/figure.png",
                        "archive_relative_path": (
                            "wiki/private/_sources/knowledge/local-only/"
                            "kotlin/images/figure.png"
                        ),
                        "sha256": staged_sha256,
                        "size": len(staged_bytes),
                        "provenance": "embedded-original-byte-identical",
                        "source_entry_locator": "source://kotlin#images/figure.png",
                    }
                ],
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
                    "replacement": {
                        "schema_version": 3,
                        "workflow_phase": "source-landed",
                        "translation_required": False,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    preflight_calls: list[tuple[VerifiedBookPage, ...]] = []

    class FakeService:
        def promote_verified_book_pages(self, *_args: object) -> None:
            raise AssertionError("apply:false must not call the mutating service method")

        def preflight_verified_book_update(
            self,
            pages: tuple[VerifiedBookPage, ...],
            replacements: dict[str, str],
            expected_revisions: dict[str, str],
            body_sha256: dict[str, str],
            coverage_manifest: BookCoverageManifestUpdate,
            staged_assets: tuple[StagedBookAsset, ...],
            ) -> VerifiedBookPreflightReport:
            preflight_calls.append(pages)
            assert replacements == {}
            assert expected_revisions == {}
            assert body_sha256 == {}
            assert len(staged_assets) == 1
            assert staged_assets[0].staging_path == staged_path
            return VerifiedBookPreflightReport(
                ready=True,
                page_count=len(pages),
                retirement_count=0,
                coverage_mode=coverage_manifest.mode,
                coverage_path=coverage_manifest.relative_path,
                base_manifest_preserved=False,
                staged_asset_count=1,
            )

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), FakeService()),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "book-promote",
            "--input",
            str(payload),
            "--vault",
            str(vault),
        ],
        output,
    )

    assert len(preflight_calls) == 1
    assert preflight_calls[0][0].page_id == "books/kotlin/chapter-01"
    result = json.loads(output.getvalue())
    assert result["ready"] is True
    assert result["applied"] is False
    assert result["retirement_count"] == 0
    assert result["staged_asset_count"] == 1


@pytest.mark.parametrize(
    "staging_relative_path",
    ("../figure.png", "/tmp/figure.png"),
)
def test_book_promote_rejects_staged_asset_path_traversal(
    tmp_path: Path, staging_relative_path: str
) -> None:
    input_path = tmp_path / "promotion.json"
    input_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(WoonError, match="staging_relative_path is unsafe"):
        cli._parse_staged_book_assets(  # noqa: SLF001
            [
                {
                    "staging_relative_path": staging_relative_path,
                    "archive_relative_path": (
                        "wiki/private/_sources/knowledge/local-only/"
                        "kotlin/images/figure.png"
                    ),
                    "sha256": "a" * 64,
                    "size": 1,
                    "provenance": "embedded-original-byte-identical",
                    "source_entry_locator": "source://kotlin#images/figure.png",
                }
            ],
            input_path,
        )


def test_book_promote_parses_75_regular_staged_assets(tmp_path: Path) -> None:
    input_path = tmp_path / "promotion.json"
    input_path.write_text("{}\n", encoding="utf-8")
    staged_root = tmp_path / "staged-source-assets"
    staged_root.mkdir()
    raw: list[dict[str, object]] = []
    for index in range(75):
        name = f"figure-{index:02}.png"
        content = f"source-{index}".encode()
        (staged_root / name).write_bytes(content)
        raw.append(
            {
                "staging_relative_path": f"staged-source-assets/{name}",
                "archive_relative_path": (
                    "wiki/private/_sources/knowledge/local-only/"
                    f"kotlin/images/{name}"
                ),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
                "provenance": "embedded-original-byte-identical",
                "source_entry_locator": f"source://kotlin#images/{name}",
            }
        )

    parsed = cli._parse_staged_book_assets(raw, input_path)  # noqa: SLF001

    assert len(parsed) == 75
    assert all(asset.staging_path.is_file() for asset in parsed)


def test_book_promote_rejects_symlinked_staged_asset(tmp_path: Path) -> None:
    input_path = tmp_path / "promotion.json"
    input_path.write_text("{}\n", encoding="utf-8")
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    staged = tmp_path / "staged-source-assets/figure.png"
    staged.parent.mkdir()
    staged.symlink_to(source)

    with pytest.raises(WoonError, match="must not use symlinks"):
        cli._parse_staged_book_assets(  # noqa: SLF001
            [
                {
                    "staging_relative_path": "staged-source-assets/figure.png",
                    "archive_relative_path": (
                        "wiki/private/_sources/knowledge/local-only/"
                        "kotlin/images/figure.png"
                    ),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "size": source.stat().st_size,
                    "provenance": "embedded-original-byte-identical",
                    "source_entry_locator": "source://kotlin#images/figure.png",
                }
            ],
            input_path,
        )


def test_knowledge_book_promote_rejects_legacy_payload_without_contract(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "legacy.json"
    payload.write_text('{"apply": true, "pages": []}\n', encoding="utf-8")

    with pytest.raises(WoonError, match="input is legacy"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


def test_knowledge_book_promote_rejects_v3_payload_before_service_call(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "v3-book-promotion.json"
    contract = book_promotion_contract_fields()
    contract["payload_schema_version"] = 3
    contract["book_contract"] = {"version": 3, "sha256": "a" * 64}
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                **contract,
                "pages": [{}],
                "coverage_manifest": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="payload_schema_version mismatch: expected=7 actual=3"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


def test_knowledge_book_promote_rejects_implicit_schema_v4_replacement(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "v4-book-promotion.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                "payload_schema_version": 4,
                "book_contract": {"version": 4, "sha256": "a" * 64},
                "pages": [{}],
                "coverage_manifest": {
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": "b" * 64,
                    "replacement": {},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="payload_schema_version mismatch: expected=7 actual=4"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


def test_knowledge_book_promote_rejects_stale_contract_hash(tmp_path: Path) -> None:
    payload = tmp_path / "stale.json"
    contract_fields = book_promotion_contract_fields()
    contract = contract_fields["book_contract"]
    assert isinstance(contract, dict)
    contract["sha256"] = "0" * 64
    payload.write_text(
        json.dumps({"apply": True, **contract_fields, "pages": []}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="book contract hash mismatch"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


def test_knowledge_book_promote_rejects_unknown_schema_fields(tmp_path: Path) -> None:
    payload = tmp_path / "unknown-field.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [],
                "generated_by": "stale-agent",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="fields are invalid for the current payload schema"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


@pytest.mark.parametrize(
    ("replacement_phase", "replacement_translation", "message"),
    (
        ("translated", False, "workflow_phase must match"),
        ("source-landed", True, "translation_required must match"),
    ),
)
def test_knowledge_book_promote_binds_v7_workflow_to_coverage(
    tmp_path: Path,
    replacement_phase: str,
    replacement_translation: bool,
    message: str,
) -> None:
    payload = tmp_path / "workflow-mismatch.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(
                    workflow_phase="source-landed",
                    translation_required=False,
                ),
                "pages": [
                    {
                        "page_id": "books/kotlin",
                        "title": "Kotlin",
                        "body": "",
                        "statement": "책 목차다.",
                        "current_use": "책을 탐색한다.",
                        "source_locator": "source://kotlin#toc",
                        "source_sha256": "a" * 64,
                        "frontmatter": {"navigation_groups": [{"label": "1부", "children": []}]},
                        "expected_revision": None,
                    }
                ],
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": None,
                    "replacement": {
                        "schema_version": 3,
                        "workflow_phase": replacement_phase,
                        "translation_required": replacement_translation,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match=message):
        run(
            [
                "knowledge",
                "book-promote",
                "--input",
                str(payload),
                "--vault",
                str(tmp_path / "vault"),
            ],
            StringIO(),
        )


def test_knowledge_book_promote_rejects_missing_atomic_coverage_manifest(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "missing-coverage.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [{}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="fields are invalid for the current payload schema"):
        run(["knowledge", "book-promote", "--input", str(payload)], StringIO())


def test_knowledge_book_promote_retire_rejects_legacy_payload_without_contract(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "legacy-retire.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                "pages": [],
                "retire_replacements": {},
                "retirement_expected_revisions": {},
                "retirement_body_sha256": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="input is legacy"):
        run(["knowledge", "book-promote-retire", "--input", str(payload)], StringIO())


@pytest.mark.parametrize("apply", (False, True))
def test_knowledge_book_promote_retire_uses_one_atomic_service_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coverage_path = vault / "catalog/book-coverage/kotlin.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_bytes = b'{"old": true}\n'
    coverage_path.write_bytes(coverage_bytes)
    input_path = tmp_path / "atomic-book-update.json"
    staged_bytes = b"verified figure bytes"
    staged_path = tmp_path / "staged-source-assets/figure.png"
    staged_path.parent.mkdir()
    staged_path.write_bytes(staged_bytes)
    staged_sha256 = hashlib.sha256(staged_bytes).hexdigest()
    input_path.write_text(
        json.dumps(
            {
                "apply": apply,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "books/kotlin",
                        "title": "Kotlin",
                        "body": "책 목차다.\n",
                        "statement": "책 목차를 설명한다.",
                        "current_use": "책을 탐색할 때 사용한다.",
                        "source_locator": "source://kotlin#toc",
                        "source_sha256": "a" * 64,
                        "frontmatter": {"access": "local-only"},
                        "expected_revision": "root-revision",
                    }
                ],
                "retire_replacements": {"books/kotlin/part-01": "books/kotlin"},
                "retirement_expected_revisions": {"books/kotlin/part-01": "part-revision"},
                "retirement_body_sha256": {"books/kotlin/part-01": "b" * 64},
                "retirement_image_replacements": {
                    "books/kotlin/part-01": {
                        "wiki/private/_sources/knowledge/local-only/kotlin/images/old.png": (
                            "wiki/private/_sources/knowledge/local-only/kotlin/images/figure.png"
                        )
                    }
                },
                "staged_assets": [
                    {
                        "staging_relative_path": "staged-source-assets/figure.png",
                        "archive_relative_path": (
                            "wiki/private/_sources/knowledge/local-only/"
                            "kotlin/images/figure.png"
                        ),
                        "sha256": staged_sha256,
                        "size": len(staged_bytes),
                        "provenance": "embedded-original-byte-identical",
                        "source_entry_locator": "source://kotlin#images/figure.png",
                    }
                ],
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
                    "replacement": {
                        "schema_version": 3,
                        "workflow_phase": "source-landed",
                        "translation_required": False,
                        "new": True,
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[
        tuple[
            tuple[VerifiedBookPage, ...],
            dict[str, str],
            dict[str, str],
            dict[str, str],
            BookCoverageManifestUpdate | None,
            tuple[StagedBookAsset, ...],
            dict[str, dict[str, str]],
        ]
    ] = []

    class FakeService:
        def _capture(
            self,
            pages: tuple[VerifiedBookPage, ...],
            replacements: dict[str, str],
            expected_revisions: dict[str, str],
            body_sha256: dict[str, str],
            coverage_manifest: BookCoverageManifestUpdate | None,
            staged_assets: tuple[StagedBookAsset, ...],
            retirement_image_replacements: dict[str, dict[str, str]],
        ) -> None:
            calls.append(
                (
                    pages,
                    replacements,
                    expected_revisions,
                    body_sha256,
                    coverage_manifest,
                    staged_assets,
                    retirement_image_replacements,
                )
            )

        def apply_verified_book_update(
            self,
            pages: tuple[VerifiedBookPage, ...],
            replacements: dict[str, str],
            expected_revisions: dict[str, str],
            body_sha256: dict[str, str],
            coverage_manifest: BookCoverageManifestUpdate | None,
            staged_assets: tuple[StagedBookAsset, ...],
            *,
            retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        ) -> VerifiedBookUpdateReport:
            self._capture(
                pages,
                replacements,
                expected_revisions,
                body_sha256,
                coverage_manifest,
                staged_assets,
                retirement_image_replacements or {},
            )
            return VerifiedBookUpdateReport(
                curated=1,
                retired=1,
                compiled=2,
                unchanged=0,
                page_ids=("books/kotlin",),
                retired_page_ids=("books/kotlin/part-01",),
                replacement_ids=("books/kotlin",),
            )

        def preflight_verified_book_update(
            self,
            pages: tuple[VerifiedBookPage, ...],
            replacements: dict[str, str],
            expected_revisions: dict[str, str],
            body_sha256: dict[str, str],
            coverage_manifest: BookCoverageManifestUpdate,
            staged_assets: tuple[StagedBookAsset, ...],
            *,
            retirement_image_replacements: dict[str, dict[str, str]] | None = None,
        ) -> VerifiedBookPreflightReport:
            self._capture(
                pages,
                replacements,
                expected_revisions,
                body_sha256,
                coverage_manifest,
                staged_assets,
                retirement_image_replacements or {},
            )
            return VerifiedBookPreflightReport(
                ready=True,
                page_count=len(pages),
                retirement_count=len(replacements),
                coverage_mode=coverage_manifest.mode,
                coverage_path=coverage_manifest.relative_path,
                base_manifest_preserved=False,
                staged_asset_count=len(staged_assets),
            )

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), FakeService()),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "book-promote-retire",
            "--input",
            str(input_path),
            "--vault",
            str(vault),
        ],
        output,
    )

    assert len(calls) == 1
    assert calls[0][0][0].expected_revision == "root-revision"
    assert calls[0][1] == {"books/kotlin/part-01": "books/kotlin"}
    assert calls[0][2] == {"books/kotlin/part-01": "part-revision"}
    assert calls[0][3] == {"books/kotlin/part-01": "b" * 64}
    assert calls[0][4] == BookCoverageManifestUpdate(
        mode="replace",
        relative_path="catalog/book-coverage/kotlin.json",
        expected_sha256=hashlib.sha256(coverage_bytes).hexdigest(),
        replacement={
            "schema_version": 3,
            "workflow_phase": "source-landed",
            "translation_required": False,
            "new": True,
        },
    )
    assert len(calls[0][5]) == 1
    assert calls[0][5][0].staging_path == staged_path
    assert calls[0][5][0].sha256 == staged_sha256
    assert calls[0][6] == {
        "books/kotlin/part-01": {
            "wiki/private/_sources/knowledge/local-only/kotlin/images/old.png": (
                "wiki/private/_sources/knowledge/local-only/kotlin/images/figure.png"
            )
        }
    }
    if apply:
        assert '"retired": 1' in output.getvalue()
    else:
        assert '"ready": true' in output.getvalue()
        assert '"staged_asset_count": 1' in output.getvalue()


def test_knowledge_book_rights_restore_uses_one_atomic_private_service_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    coverage_path = vault / "catalog/book-coverage/book.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_bytes = b'{}\n'
    coverage_path.write_bytes(coverage_bytes)
    source_hash = "a" * 64
    input_path = tmp_path / "restore.json"
    input_path.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "personal/book",
                        "title": "Book",
                        "body": "원문 본문이다.\n",
                        "statement": "책 원문을 보존한다.",
                        "current_use": "책을 읽을 때 사용한다.",
                        "source_locator": "source://book#page=1",
                        "source_sha256": source_hash,
                        "frontmatter": {"access": "local-only"},
                        "expected_revision": None,
                    }
                ],
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/book.json",
                    "expected_sha256": hashlib.sha256(coverage_bytes).hexdigest(),
                    "replacement": {
                        "schema_version": 3,
                        "book_id": "personal/book",
                        "workflow_phase": "source-landed",
                        "translation_required": False,
                        "edition": {"source_sha256": source_hash},
                    },
                },
                "rights_restore": {
                    "schema_version": 1,
                    "rights_contract": {
                        "version": PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
                        "sha256": PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256,
                    },
                    "book_id": "personal/book",
                    "rights_evidence": {
                        "source_archive_relative_path": (
                            "wiki/private/_sources/knowledge/local-only/book/Book.pdf"
                        ),
                        "source_archive_sha256": source_hash,
                        "notice_locator": "판권면, PDF 3쪽",
                        "notice_sha256": "b" * 64,
                        "authorization_receipt_locator": "conversation://task/turn-1",
                        "authorization_receipt_sha256": "c" * 64,
                        "ownership_basis": "user-purchased-copy",
                        "authorized_on": "2026-09-03",
                        "authorized_scope": "source-landed-private-local-only",
                        "decision": "user-authorized-private",
                        "restrictions": [
                            "external-transmission-prohibited",
                            "model-training-prohibited",
                            "publication-prohibited",
                            "redistribution-prohibited",
                        ],
                    },
                    "book_intake": {
                        "relative_path": "catalog/book-intake/official-books.json",
                        "expected_sha256": "d" * 64,
                        "bundle_id": "book",
                    },
                    "quarantine_manifests": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[tuple[object, ...]] = []

    class FakeService:
        def apply_book_rights_restoration(self, *args: object) -> BookRightsRestorationReport:
            calls.append(args)
            return BookRightsRestorationReport(
                ready=True,
                applied=True,
                page_count=1,
                coverage_mode="replace",
                coverage_path="catalog/book-coverage/book.json",
                intake_relative_path="catalog/book-intake/official-books.json",
                quarantine_manifest_count=0,
                staged_asset_count=0,
                unchanged_asset_count=0,
            )

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda actual_vault: (SimpleNamespace(vault=actual_vault), FakeService()),
    )
    output = StringIO()

    run(
        [
            "knowledge",
            "book-rights-restore",
            "--input",
            str(input_path),
            "--vault",
            str(vault),
        ],
        output,
    )

    assert len(calls) == 1
    assert '"rights_status": "user-authorized-private"' in output.getvalue()


@pytest.mark.parametrize(
    "body_sha256",
    (
        {},
        {
            "books/kotlin/part-01": "b" * 64,
            "books/kotlin/part-02": "c" * 64,
        },
    ),
)
def test_knowledge_book_promote_retire_requires_exact_retirement_body_keys(
    tmp_path: Path,
    body_sha256: dict[str, str],
) -> None:
    input_path = tmp_path / "invalid-atomic-book-update.json"
    input_path.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [{}],
                "retire_replacements": {"books/kotlin/part-01": "books/kotlin"},
                "retirement_expected_revisions": {"books/kotlin/part-01": "part-revision"},
                "retirement_body_sha256": body_sha256,
                "coverage_manifest": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="retirement_body_sha256 must match replacements"):
        run(["knowledge", "book-promote-retire", "--input", str(input_path)], StringIO())


@pytest.mark.parametrize(
    ("coverage_manifest", "message"),
    (
        (
            {
                "mode": "replace",
                "relative_path": "catalog/book-coverage/kotlin.json",
                "expected_sha256": "a" * 64,
                "replacement": {},
                "unexpected": True,
            },
            "coverage_manifest fields are invalid",
        ),
        (
            {
                "mode": "replace",
                "relative_path": "catalog/book-coverage/../outside.json",
                "expected_sha256": "a" * 64,
                "replacement": {},
            },
            "path must be one JSON file",
        ),
    ),
)
def test_knowledge_book_promote_retire_rejects_unsafe_coverage_manifest(
    tmp_path: Path,
    coverage_manifest: dict[str, object],
    message: str,
) -> None:
    vault = tmp_path / "vault"
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    input_path = tmp_path / "invalid-coverage-update.json"
    input_path.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "books/kotlin",
                        "title": "Kotlin",
                        "body": "책 목차다.\n",
                        "statement": "책 목차를 설명한다.",
                        "current_use": "책을 탐색할 때 사용한다.",
                        "source_locator": "source://kotlin#toc",
                        "source_sha256": "a" * 64,
                        "frontmatter": {"access": "local-only"},
                        "expected_revision": "root-revision",
                    }
                ],
                "retire_replacements": {"books/kotlin/part-01": "books/kotlin"},
                "retirement_expected_revisions": {"books/kotlin/part-01": "part-revision"},
                "retirement_body_sha256": {"books/kotlin/part-01": "b" * 64},
                "coverage_manifest": coverage_manifest,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match=message):
        run(
            [
                "knowledge",
                "book-promote-retire",
                "--input",
                str(input_path),
                "--vault",
                str(vault),
            ],
            StringIO(),
        )


def test_knowledge_book_promote_retire_rejects_symlinked_coverage_manifest(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    manifest_root = vault / "catalog/book-coverage"
    manifest_root.mkdir(parents=True)
    real = tmp_path / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    linked = manifest_root / "kotlin.json"
    linked.symlink_to(real)
    input_path = tmp_path / "symlinked-coverage-update.json"
    input_path.write_text(
        json.dumps(
            {
                "apply": True,
                **book_promotion_contract_fields(),
                "pages": [
                    {
                        "page_id": "books/kotlin",
                        "title": "Kotlin",
                        "body": "책 목차다.\n",
                        "statement": "책 목차를 설명한다.",
                        "current_use": "책을 탐색할 때 사용한다.",
                        "source_locator": "source://kotlin#toc",
                        "source_sha256": "a" * 64,
                        "frontmatter": {"access": "local-only"},
                        "expected_revision": "root-revision",
                    }
                ],
                "retire_replacements": {"books/kotlin/part-01": "books/kotlin"},
                "retirement_expected_revisions": {"books/kotlin/part-01": "part-revision"},
                "retirement_body_sha256": {"books/kotlin/part-01": "b" * 64},
                "coverage_manifest": {
                    "mode": "replace",
                    "relative_path": "catalog/book-coverage/kotlin.json",
                    "expected_sha256": hashlib.sha256(real.read_bytes()).hexdigest(),
                    "replacement": {},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="path must not use symlinks"):
        run(
            [
                "knowledge",
                "book-promote-retire",
                "--input",
                str(input_path),
                "--vault",
                str(vault),
            ],
            StringIO(),
        )


def test_knowledge_refresh_wiki_tree_validates_and_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = SimpleNamespace(document_count=9, changed_count=2, issues=())
    calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "prepare_wiki_tree_refresh",
        lambda actual, *, canonical_prefix=None: calls.extend((actual, canonical_prefix)) or report,
    )
    monkeypatch.setattr(
        cli,
        "apply_wiki_tree_refresh",
        lambda actual, prepared: calls.extend((actual, prepared)),
    )
    monkeypatch.setattr(
        cli,
        "resolve_knowledge_vault",
        lambda: pytest.fail("explicit --vault must not resolve the default vault"),
    )

    output = StringIO()
    run(["knowledge", "refresh-wiki-tree", "--vault", str(vault)], output)

    assert calls == [vault.resolve(), None, vault.resolve(), report]
    assert '"document_count": 9' in output.getvalue()
    assert '"changed_count": 2' in output.getvalue()


def test_knowledge_refresh_wiki_tree_forwards_canonical_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = SimpleNamespace(document_count=9, changed_count=2, issues=())
    calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "prepare_wiki_tree_refresh",
        lambda actual, *, canonical_prefix=None: calls.extend((actual, canonical_prefix)) or report,
    )
    monkeypatch.setattr(
        cli,
        "apply_wiki_tree_refresh",
        lambda actual, prepared: calls.extend((actual, prepared)),
    )

    output = StringIO()
    run(
        [
            "knowledge",
            "refresh-wiki-tree",
            "--vault",
            str(vault),
            "--canonical-prefix",
            "personal/kotlin-in-action",
        ],
        output,
    )

    assert calls == [
        vault.resolve(),
        "personal/kotlin-in-action",
        vault.resolve(),
        report,
    ]
    assert '"canonical_prefix": "personal/kotlin-in-action"' in output.getvalue()


def test_knowledge_project_novel_does_not_resolve_default_for_explicit_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = SimpleNamespace(
        category_count=1,
        source_count=2,
        event_count=3,
        judgment_count=4,
        relation_count=5,
        changed_count=0,
        stale_pages=(),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        cli,
        "prepare_novel_wiki_projection",
        lambda actual, source, projection_day: (
            calls.extend((actual, source, projection_day)) or report
        ),
    )
    monkeypatch.setattr(
        cli,
        "apply_novel_wiki_projection",
        lambda actual, prepared: calls.extend((actual, prepared)),
    )
    monkeypatch.setattr(
        cli,
        "resolve_knowledge_vault",
        lambda: pytest.fail("explicit --vault must not resolve the default vault"),
    )

    output = StringIO()
    run(
        [
            "knowledge",
            "project-novel",
            "--vault",
            str(vault),
            "--day",
            "2026-08-27",
        ],
        output,
    )

    assert calls == [
        vault.resolve(),
        vault.resolve() / "wiki/private/_sources/novel",
        date(2026, 8, 27),
        vault.resolve(),
        report,
    ]
    assert '"changed_count": 0' in output.getvalue()


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
    report = tmp_path / "report.json"
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
            "--output",
            str(report),
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
    assert report.stat().st_mode & 0o777 == 0o600


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
