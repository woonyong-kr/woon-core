"""Command-line entry point."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TextIO

from woon_core import __version__
from woon_core.calendar.cli import run_calendar
from woon_core.career.cli import run_career
from woon_core.context import Compiler
from woon_core.environment import apply as apply_environment
from woon_core.environment import check as check_environment
from woon_core.environment import doctor as doctor_environment
from woon_core.environment import generate as generate_environment
from woon_core.environment import plan as plan_environment
from woon_core.environment import verify as verify_environment
from woon_core.environment.machine import runtime_target
from woon_core.environment.python_ide import PythonIdeStatus
from woon_core.environment.python_ide import apply as apply_python_ide
from woon_core.environment.python_ide import doctor as doctor_python_ide
from woon_core.environment.python_ide import plan as plan_python_ide
from woon_core.environment.python_ide import verify as verify_python_ide
from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.answer_citation_evaluation import evaluate_answer_citations
from woon_core.knowledge.codex_daily_digest import (
    entries_from_records as daily_digest_entries_from_records,
)
from woon_core.knowledge.codex_daily_digest import (
    migrate_legacy_daily_digests,
    record_codex_daily_digest,
    record_daily_digest_from_codex_ledger,
)
from woon_core.knowledge.codex_knowledge import (
    entries_from_records as codex_knowledge_entries_from_records,
)
from woon_core.knowledge.codex_knowledge import record_codex_knowledge_entries
from woon_core.knowledge.codex_quality_review import run_codex_quality_reviews
from woon_core.knowledge.codex_quality_revision import (
    apply_codex_quality_revisions,
    create_codex_quality_revision_proposals,
)
from woon_core.knowledge.codex_source_archive import (
    bundle_from_record as codex_source_bundle_from_record,
)
from woon_core.knowledge.codex_source_archive import record_codex_source_bundle
from woon_core.knowledge.content_quality_evaluation import evaluate_content_quality
from woon_core.knowledge.content_quality_review_plan import (
    assemble_content_quality_reviews,
    create_content_quality_review_plan,
    rebase_content_quality_review_plan,
)
from woon_core.knowledge.evaluation import evaluate as evaluate_knowledge
from woon_core.knowledge.factory import build_knowledge_service, resolve_knowledge_vault
from woon_core.knowledge.mail_schedule_automation import (
    record_mail_schedule_candidates,
    submissions_from_records,
)
from woon_core.knowledge.novel_wiki_projection import (
    apply_novel_wiki_projection,
    prepare_novel_wiki_projection,
)
from woon_core.knowledge.obsidian_plugins import ObsidianPluginService
from woon_core.knowledge.ollama_quality_review import run_ollama_quality_reviews
from woon_core.knowledge.orchestration import (
    load_orchestrator_settings,
    verify_codex_automation_registry,
)
from woon_core.knowledge.reconciliation import (
    audit_reconciliation,
    reconcile_catalog,
)
from woon_core.knowledge.research_intake import (
    create_research_intake_plan,
    export_notebooklm_artifact,
    write_research_intake_plan,
)
from woon_core.knowledge.schedule_apply import (
    apply_policy_authorized_schedule_candidate,
    receipt_record,
)
from woon_core.knowledge.second_brain_runtime import record_governance_preflight
from woon_core.knowledge.source_archive import archive_private_source_corpus
from woon_core.knowledge.source_catalog import (
    load_source_catalog,
    plan_source_catalog,
    write_source_catalog,
)
from woon_core.knowledge.wiki_tree import apply_wiki_tree_refresh, prepare_wiki_tree_refresh
from woon_core.people.cli import run_people
from woon_core.registry import Registry
from woon_core.skills import ClaudeRoutingSelector, CodexRoutingSelector, evaluate_routing
from woon_core.skills import doctor as doctor_skills
from woon_core.skills import install as install_skills
from woon_core.skills import plan as plan_skills
from woon_core.skills import validate as validate_skills
from woon_core.tasks.cli import run_tasks
from woon_core.workspace import Workspace, discover, initialize

USAGE = """woon - deterministic control plane for the Woon development system

Usage:
  woon init --root <path>
  woon doctor [--root <path>]
  woon repo sync [--root <path>]
  woon resolve <repo-id|repo://id/path> [--root <path>]
  woon context generate [--all|repo-id...] [--root <path>]
  woon context check [--all|repo-id...] [--root <path>]
  woon env generate [--target <macos|windows|linux>]
  woon env doctor [--all]
  woon env plan [--all]
  woon env apply [--all]
  woon env verify [--all]
  woon env check [--target <macos|windows|linux>]
  woon env python-ide <doctor|plan|apply|verify> --project <path>
  woon skills plan --profile <names> [--target <codex|claude>]
  woon skills validate --profile <names>
  woon skills install --profile <names> --target <codex|claude>
  woon skills eval-routing [--executor <all|codex|claude>] [--repeat <count>]
  woon skills doctor
  woon tasks find <query> [--date <YYYY-MM-DD>] [--vault <path>]
  woon tasks upsert-recurring --id <task-id> --title <text> --purpose <text>
    --area <area> [--start-date <YYYY-MM-DD>] [--vault <path>]
  woon tasks materialize [--date <YYYY-MM-DD>] [--vault <path>]
  woon tasks complete --id <task-id> [--date <YYYY-MM-DD>] [--vault <path>]
  woon people find <query> [--vault <path>]
  woon people documents <person-id> [--vault <path>]
  woon people upsert --id <person-id> --title <text> --kind <kind>
    --relationship <text> --purpose <text> --basis <explicit-request|repeated-evidence>
    [--vault <path>]
  woon people link --document <relative-markdown-path> --person <person-id>
    --roles <comma-separated-roles> --evidence <text> [--vault <path>]
  woon people identify --person <person-id> --identifiers <comma-separated-identifiers>
    --evidence <text> [--context <comma-separated-terms>] [--vault <path>]
  woon people private-history-sync --novel-root <local-path> [--vault <path>]
  woon calendar refresh [--vault <path>]
  woon calendar migrate-legacy [--vault <path>]
  woon calendar upsert --id <stable-id> --title <text> --start <ISO8601>
    --end <ISO8601> --category <career|learning|creative|life|relationship|health|admin>
    [--location <text>] [--notes <text>] [--vault <path>]
  woon career <create|analyze|evaluate|approve-draft|attach-pdf|mark-reviewed|
    mark-ready|reopen|outcome|context|show> [options]
  woon knowledge index [--vault <path>]
  woon knowledge search <query> [--limit <1..20>] [--vault <path>]
  woon knowledge get <canonical-id> [--vault <path>]
  woon knowledge audit [--vault <path>]
  woon knowledge vault-tool <name> [tool-options...] [--vault <path>]
  woon knowledge obsidian-plugin <status|install|remove-detected-mindmaps|
    install-local-build|
    configure-prisma-calendar|configure-full-calendar-remastered|
    configure-notion-bases-calendar|configure-link-calendar|
    attest-link-calendar-runtime|retire>
    [--plugin <approved-plugin-id>...] [--source-dir <path>] [--version <semver>]
    [--attested-check <operator-confirmed-ui-check>...]
    [--vault <path>]
  woon knowledge history <canonical-id> [--limit <1..100>] [--vault <path>]
  woon knowledge migrate-compiled [--vault <path>]
  woon knowledge initialize-curation [--vault <path>]
  woon knowledge refresh-provisional-curation [--vault <path>]
  woon knowledge reconcile-superseded-revisions [--vault <path>]
  woon knowledge compile [--force] [--vault <path>]
  woon knowledge compile-audit [--vault <path>]
  woon knowledge refresh-wiki-tree [--vault <path>]
  woon knowledge project-novel [--day <YYYY-MM-DD>] [--vault <path>]
  woon knowledge evaluate --cases <path> [--output <path>] [--vault <path>]
  woon knowledge evaluate-answers --cases <path> --answers <path>
    [--output <path>] [--vault <path>]
  woon knowledge evaluate-quality --reviews <path> --standard <path> --prompt <path>
    [--output <path>] [--vault <path>]
  woon knowledge quality-review-plan --standard <path> --prompt <path> --output <directory>
    [--batch-size <1..64>] [--max-batch-chars <4000..200000>]
    [--standard-uri <repo-uri>] [--prompt-uri <repo-uri>]
    [--vault <path>]
  woon knowledge rebase-quality-review-plan --prior-plan <path> --prior-results <directory>
    --standard <path> --prompt <path> --output <directory> --results <directory>
    [--batch-size <1..64>] [--max-batch-chars <4000..200000>]
    [--standard-uri <repo-uri>] [--prompt-uri <repo-uri>]
    [--vault <path>]
  woon knowledge assemble-quality-reviews --plan <path> --results <directory>
    --standard <path> --evaluator-name <name> --evaluator-version <version>
    --output <path> [--vault <path>]
  woon knowledge review-quality-ollama --plan <path> --results <directory>
    [--model <name>] [--batch <batch-id>...] [--timeout-seconds <30..3600>]
    [--max-attempts <1..5>] [--context-tokens <4096..32768>]
    [--adaptive-context <true|false>]
    [--continue-on-error <true|false>]
  woon knowledge review-quality-codex --plan <path> --results <directory>
    [--model <name>] [--codex-binary <path>] [--batch <batch-id>...]
    [--timeout-seconds <30..3600>] [--max-attempts <1..3>]
    [--continue-on-error <true|false>]
  woon knowledge revise-quality-codex --plan <path> --reviews <directory> --output <directory>
    [--model <name>] [--codex-binary <path>] [--page <page-id>...]
    [--timeout-seconds <30..3600>]
    [--max-attempts <1..3>] [--continue-on-error <true|false>] [--vault <path>]
  woon knowledge apply-quality-revisions --plan <path> --reviews <directory>
    --proposals <directory> [--proposals <retry-directory>...]
    [--duplicate-policy error|first-valid] [--vault <path>]
  woon knowledge research-intake-plan --purpose <text> [--zotero <CSL-JSON>]
    [--notebooklm-manifest <JSON>] [--output <path>]
  woon knowledge notebooklm-export --artifact-id <id> --kind <kind>
    --source-ref <doi-or-arxiv> [--source-ref <doi-or-arxiv>...]
    --tool-revision <40-hex-commit> --output <markdown> --manifest <JSON>
    [--nlm <binary>]
  woon knowledge source-plan --source <path> --source-name <name>
    [--vault <path>] [--output <relative-path>]
  woon knowledge source-reconcile --source <path> --source-name <name>
    [--vault <path>] [--limit <count>] [--model <model>] [--state <state>]
  woon knowledge source-audit --source <path> --source-name <name> [--vault <path>]
  woon knowledge source-archive --source <path> --source-name <name>
    --wiki-subject <wiki/path.md> [--vault <path>]
  woon knowledge validate-orchestrator [--vault <path>]
    [--automation-root <path>]
  woon knowledge governance-preflight [--vault <path>]
    [--automation-root <path>]
  woon knowledge record-mail-schedule-candidates --run-token <mail-kst-YYYYMMDD-HHMM>
    [--candidates-json <json-array>] [--vault <path>]
  woon knowledge record-codex-daily-record --day <YYYY-MM-DD> --entries-json <json-array>
    [--vault <path>]
  woon knowledge record-codex-knowledge-entries --source-range <safe-token> --day <YYYY-MM-DD>
    --entries-json <json-array> [--vault <path>]
  woon knowledge record-codex-source --bundle-file <local-JSON> [--vault <path>]
  woon knowledge materialize-codex-daily-record --day <YYYY-MM-DD> [--vault <path>]
  woon knowledge migrate-legacy-daily-digests [--vault <path>]
  woon knowledge schedule-apply --candidate <local-JSON>
    [--vault <path>]
  woon version
"""

RESEARCH_INTAKE_USAGE = """usage: woon knowledge research-intake-plan --purpose <text>
  [--zotero <CSL-JSON>]
  [--notebooklm-manifest <JSON>] [--output <path>]

Builds an offline review plan. Zotero exports contribute bibliographic metadata only;
NotebookLM Markdown remains review-required until evidence-backed claims are compiled.
"""

NOTEBOOKLM_EXPORT_USAGE = """usage: woon knowledge notebooklm-export
  --artifact-id <id> --kind <kind>
  --source-ref <doi-or-arxiv> [--source-ref <doi-or-arxiv>...]
  --tool-revision <40-hex-commit> --output <markdown> --manifest <JSON>
  [--nlm <binary>]

Downloads one already-generated NotebookLM artifact as Markdown through a pinned nlm
client and writes a review-required manifest. It does not upload sources or modify Wiki.
"""


def main() -> None:
    try:
        run(sys.argv[1:], sys.stdout)
    except WoonError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def run(raw_arguments: list[str], output: TextIO) -> None:
    root, arguments = _parse_global(raw_arguments)
    if not arguments:
        output.write(USAGE)
        return
    command, *remaining = arguments
    if command in {"version", "--version", "-v"}:
        print(__version__, file=output)
    elif command in {"help", "--help", "-h"}:
        output.write(USAGE)
    elif command == "init":
        if remaining:
            raise WoonError("init takes no positional arguments")
        if not root:
            raise WoonError("init requires --root")
        initialized = initialize(root)
        print(
            f"status: initialized\nroot: {initialized}\n"
            "next_actions:\n  - woon doctor\n  - woon repo sync",
            file=output,
        )
    elif command == "doctor":
        workspace = discover(root)
        registry = Registry.load(workspace.root)
        missing = registry.missing(workspace.root)
        print(
            f"status: ok\nroot: {workspace.root}\nsource: {workspace.source}\n"
            f"repositories: {len(registry.repositories)}\nmissing: {len(missing)}",
            file=output,
        )
        for identifier in missing:
            print(f"  - {identifier}", file=output)
    elif command == "resolve":
        if len(remaining) != 1:
            raise WoonError("resolve requires one repository ID or repo URI")
        workspace, registry = _load(root)
        print(registry.resolve(workspace.root, remaining[0]), file=output)
    elif command == "repo":
        if remaining != ["sync"]:
            raise WoonError("usage: woon repo sync")
        workspace, registry = _load(root)
        result = registry.sync(workspace.root)
        print(f"status: ok\ncloned: {result.cloned}\nexisting: {result.existing}", file=output)
    elif command == "context":
        _run_context(root, remaining, output)
    elif command == "env":
        _run_environment(root, remaining, output)
    elif command == "skills":
        _run_skills(root, remaining, output)
    elif command == "tasks":
        run_tasks(remaining, output)
    elif command == "people":
        run_people(remaining, output)
    elif command == "calendar":
        run_calendar(remaining, output)
    elif command == "career":
        run_career(remaining, output)
    elif command == "knowledge":
        _run_knowledge(remaining, output)
    else:
        raise WoonError(f"unknown command {command!r}")


def _run_context(root: str, arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError("usage: woon context <generate|check> [--all|repo-id]")
    command, *targets = arguments
    all_repositories = not targets or targets == ["--all"]
    identifiers = [] if all_repositories else targets
    if any(identifier.startswith("-") for identifier in identifiers):
        raise WoonError("unknown context option")
    workspace, registry = _load(root)
    compiler = Compiler(workspace.root, registry)
    if command == "generate":
        result = compiler.generate(all_repositories, identifiers)
    elif command == "check":
        result = compiler.check(all_repositories, identifiers)
    else:
        raise WoonError(f"unknown context command {command!r}")
    suffix = "\npath_violations: 0" if command == "check" else ""
    print(
        f"status: ok\nrepositories: {result.repositories}\nartifacts: {result.artifacts}{suffix}",
        file=output,
    )


def _run_environment(root: str, arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError("usage: woon env <doctor|plan|generate|check|apply|verify> [--all]")
    command, *raw_options = arguments
    if command == "python-ide":
        _run_python_ide(root, raw_options, output)
        return
    target, options = _parse_target(raw_options)
    if options == ["--all"]:
        options = []
    if options:
        raise WoonError(f"unexpected env arguments: {' '.join(options)}")
    workspace, registry = _load(root)
    if command == "doctor":
        statuses = doctor_environment(workspace.root, registry, target)
        print(f"status: ok\ntarget: {target}\ninstallations: {len(statuses)}", file=output)
        for status in statuses:
            print(
                f"  - name: {status.name}\n    path: {status.path}\n"
                f"    running: {str(status.running).lower()}",
                file=output,
            )
            if status.extension_command:
                print(
                    f"    extension_command: {status.extension_command}\n"
                    f"    extension_command_available: "
                    f"{str(status.command_available).lower()}",
                    file=output,
                )
    elif command in {"generate", "check"}:
        environment_operation = generate_environment if command == "generate" else check_environment
        generation_result = environment_operation(workspace.root, registry, target)
        print(
            f"status: ok\ntarget: {target}\nartifacts: {generation_result.artifacts}\n"
            f"hash: {generation_result.hash}",
            file=output,
        )
    elif command == "plan":
        plan_result = plan_environment(workspace.root, registry, target)
        print(
            f"status: ok\ntarget: {target}\noperations: {len(plan_result.operations)}\n"
            f"changes: {plan_result.changes}",
            file=output,
        )
        for planned_operation in plan_result.operations:
            if planned_operation.changed:
                print(
                    f"  - {planned_operation.target}/{planned_operation.kind}: "
                    f"{planned_operation.destination}",
                    file=output,
                )
    elif command == "apply":
        apply_result = apply_environment(workspace.root, registry, target)
        print(
            f"status: ok\ntarget: {target}\napplied: {apply_result.applied}",
            file=output,
        )
        if apply_result.backup_path:
            print(f"backup: {apply_result.backup_path}", file=output)
    elif command == "verify":
        verify_result = verify_environment(workspace.root, registry, target)
        print(
            f"status: ok\ntarget: {target}\nverified: {len(verify_result.operations)}",
            file=output,
        )
    else:
        raise WoonError(f"unknown env command {command!r}")


def _run_python_ide(root: str, arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError("usage: woon env python-ide <doctor|plan|apply|verify> --project <path>")
    command, *options = arguments
    project = _parse_python_ide_options(options)
    workspace, registry = _load(root)
    if command == "doctor":
        status = doctor_python_ide(workspace.root, registry, project)
        _print_python_ide_status(status, output)
        return
    if command == "plan":
        result = plan_python_ide(workspace.root, registry, project)
        _print_python_ide_status(result.status, output)
        print(f"operations: {len(result.operations)}", file=output)
        for operation in result.operations:
            print(f"  - {' '.join(operation)}", file=output)
        return
    if command == "apply":
        status = apply_python_ide(workspace.root, registry, project)
        _print_python_ide_status(status, output)
        return
    if command == "verify":
        status = verify_python_ide(workspace.root, registry, project)
        _print_python_ide_status(status, output)
        return
    raise WoonError(f"unknown python-ide command {command!r}")


def _print_python_ide_status(status: PythonIdeStatus, output: TextIO) -> None:
    project = status.project
    environment = status.environment
    interpreter = status.interpreter
    uv_available = status.uv_available
    pip_available = status.pip_available
    print(
        f"status: ok\nproject: {project}\nenvironment: {environment}\n"
        f"interpreter: {interpreter}\nuv_available: {str(uv_available).lower()}\n"
        f"pip_available: {str(pip_available).lower()}",
        file=output,
    )


def _run_skills(root: str, arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError("usage: woon skills <plan|validate|install|eval-routing|doctor>")
    command, *options = arguments
    if command == "doctor":
        if options:
            raise WoonError("skills doctor takes no options")
        print("status: ok", file=output)
        for name, path in doctor_skills().items():
            print(f"{name}: {path}", file=output)
        return
    if command == "eval-routing":
        repeat, executor = _parse_routing_options(options)
        workspace, registry = _load(root)
        failed = False
        for executor_name, selector in _routing_selectors(executor):
            evaluation = evaluate_routing(
                workspace.root,
                registry,
                selector,
                repeat=repeat,
            )
            status = "ok" if evaluation.passed else "failed"
            print(
                f"executor: {executor_name}\nstatus: {status}\n"
                f"cases: {len(evaluation.cases)}\nrepeat: {evaluation.repeat}\n"
                f"primary_recall: {evaluation.primary_recall:.4f}\n"
                f"forbidden_selections: {evaluation.forbidden_selections}\n"
                f"agreement: {evaluation.agreement:.4f}",
                file=output,
            )
            for case in evaluation.cases:
                if not case.passed:
                    selections = " | ".join(
                        ", ".join(run) if run else "(none)" for run in case.selections
                    )
                    print(
                        f"  - failed: {case.identifier} (selected: {selections})",
                        file=output,
                    )
            failed = failed or not evaluation.passed
        if failed:
            raise WoonError("semantic routing evaluation did not meet thresholds")
        return
    profiles, target = _parse_skills_options(options)
    workspace, registry = _load(root)
    if command == "validate":
        result = validate_skills(workspace.root, registry, profiles)
    elif command == "plan":
        result = plan_skills(workspace.root, registry, profiles, target)
    elif command == "install":
        installed = install_skills(workspace.root, registry, profiles, target)
        print(
            f"status: ok\ntarget: {installed.target}\ninstalled: {installed.installed}\n"
            f"updated: {installed.updated}\nretired: {installed.retired}\n"
            f"unchanged: {installed.unchanged}",
            file=output,
        )
        if installed.backup:
            print(f"backup: {installed.backup}", file=output)
        return
    else:
        raise WoonError(f"unknown skills command {command!r}")
    print(
        f"status: ok\nprofiles: {','.join(result.profiles)}\nskills: {len(result.items)}",
        file=output,
    )
    if result.target:
        print(f"target: {result.target}", file=output)
    for item in result.items:
        print(f"  - {item.name}: {item.action} [{','.join(item.effects)}]", file=output)


def _run_knowledge(arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError("usage: woon knowledge <index|search|get|audit|history|compile|evaluate>")
    command, *raw_options = arguments
    if command == "vault-tool":
        _run_vault_tool(raw_options, output)
        return
    if command == "obsidian-plugin":
        _run_obsidian_plugin(raw_options, output)
        return
    if command == "source-plan":
        _run_knowledge_source_plan(raw_options, output)
        return
    if command == "source-reconcile":
        _run_knowledge_source_reconcile(raw_options, output)
        return
    if command == "source-audit":
        _run_knowledge_source_audit(raw_options, output)
        return
    if command == "source-archive":
        _run_knowledge_source_archive(raw_options, output)
        return
    if command == "project-novel":
        _run_novel_wiki_projection(raw_options, output)
        return
    if command == "refresh-wiki-tree":
        _run_wiki_tree_refresh(raw_options, output)
        return
    if command in {
        "migrate-compiled",
        "initialize-curation",
        "refresh-provisional-curation",
        "reconcile-superseded-revisions",
        "compile",
        "compile-audit",
    }:
        _run_compiled_knowledge(command, raw_options, output)
        return
    if command == "evaluate":
        _run_knowledge_evaluation(raw_options, output)
        return
    if command == "evaluate-answers":
        _run_answer_citation_evaluation(raw_options, output)
        return
    if command == "evaluate-quality":
        _run_content_quality_evaluation(raw_options, output)
        return
    if command == "quality-review-plan":
        _run_content_quality_review_plan(raw_options, output)
        return
    if command == "rebase-quality-review-plan":
        _run_content_quality_review_rebase(raw_options, output)
        return
    if command == "assemble-quality-reviews":
        _run_content_quality_review_assembly(raw_options, output)
        return
    if command == "review-quality-ollama":
        _run_ollama_quality_review(raw_options, output)
        return
    if command == "review-quality-codex":
        _run_codex_quality_review(raw_options, output)
        return
    if command == "revise-quality-codex":
        _run_codex_quality_revision(raw_options, output)
        return
    if command == "apply-quality-revisions":
        _run_codex_quality_revision_apply(raw_options, output)
        return
    if command == "research-intake-plan":
        _run_research_intake_plan(raw_options, output)
        return
    if command == "notebooklm-export":
        _run_notebooklm_export(raw_options, output)
        return
    if command == "validate-orchestrator":
        _run_second_brain_orchestrator_validation(raw_options, output)
        return
    if command == "governance-preflight":
        _run_governance_preflight(raw_options, output)
        return
    if command == "record-mail-schedule-candidates":
        _run_mail_schedule_candidate_recording(raw_options, output)
        return
    if command == "record-codex-daily-record":
        _run_codex_daily_digest_recording(raw_options, output)
        return
    if command == "record-codex-knowledge-entries":
        _run_codex_knowledge_entry_recording(raw_options, output)
        return
    if command == "record-codex-source":
        _run_codex_source_recording(raw_options, output)
        return
    if command == "materialize-codex-daily-record":
        _run_codex_daily_digest_materialization(raw_options, output)
        return
    if command == "migrate-legacy-daily-digests":
        _run_legacy_daily_digest_migration(raw_options, output)
        return
    if command == "schedule-apply":
        _run_schedule_apply(raw_options, output)
        return
    local_commands = {"index", "search", "get", "audit", "history"}
    if command not in local_commands:
        raise WoonError(f"unknown knowledge command {command!r}")
    vault, options = _parse_knowledge_options(raw_options)
    settings, service = build_knowledge_service(vault)
    if command == "index":
        if options:
            raise WoonError("knowledge index takes no positional arguments")
        count = service.reindex()
        print(
            f"status: ok\nadapter: {settings.search_adapter}\nindexed: {count}",
            file=output,
        )
    elif command == "search":
        query = " ".join(options).strip()
        if not query:
            raise WoonError("knowledge search requires a query")
        results = service.search(query, _knowledge_limit(raw_options, 5))
        print(
            json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2),
            file=output,
        )
    elif command == "get":
        if len(options) != 1:
            raise WoonError("knowledge get requires one canonical ID")
        document = service.get(options[0])
        print(
            json.dumps(
                {
                    "metadata": asdict(document.metadata),
                    "body": document.body,
                    "relative_path": document.relative_path,
                    "revision": document.revision,
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=output,
        )
    elif command == "audit":
        if options:
            raise WoonError("knowledge audit takes no positional arguments")
        errors = service.audit()
        print(
            json.dumps(
                {"status": "ok" if not errors else "invalid", "errors": errors},
                ensure_ascii=False,
                indent=2,
            ),
            file=output,
        )
        if errors:
            raise WoonError(f"knowledge audit found {len(errors)} errors")
    elif command == "history":
        if len(options) != 1:
            raise WoonError("knowledge history requires one canonical ID")
        entries = service.history(options[0], _knowledge_limit(raw_options, 20))
        print(
            json.dumps([asdict(item) for item in entries], ensure_ascii=False, indent=2),
            file=output,
        )


def _default_codex_automation_root() -> Path:
    """Return the active Codex automation registry without hard-coding a home."""

    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return codex_home / "automations"


def _run_second_brain_orchestrator_validation(arguments: list[str], output: TextIO) -> None:
    """Validate policy shape without creating locks, receipts, or tasks."""

    # A policy that says a heartbeat is active is not enough evidence that the
    # corresponding Codex automation is actually registered.  Resolve the
    # same registry location Codex uses by default, while retaining an
    # explicit override for isolated tests and non-default CODEX_HOME setups.
    automation_root = _default_codex_automation_root()
    automation_root_seen = False
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--automation-root":
            raw_options.append(option)
            index += 1
            continue
        if automation_root_seen or index + 1 >= len(arguments):
            raise WoonError("--automation-root requires exactly one path")
        automation_root = Path(arguments[index + 1]).expanduser()
        automation_root_seen = True
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options:
        raise WoonError("knowledge validate-orchestrator takes no positional arguments")
    settings = load_orchestrator_settings(vault or resolve_knowledge_vault())
    verified = verify_codex_automation_registry(settings, automation_root)
    print(
        json.dumps(
            {
                "status": "ok",
                "policy_sha256": settings.policy_sha256,
                "timezone": settings.timezone,
                "automations": [
                    {
                        "id": item.automation_id,
                        "mode": item.mode,
                        "status": item.status,
                        "task_thread_id": item.task_thread_id,
                    }
                    for item in settings.automations
                ],
                "codex_registry_root": str(automation_root),
                "codex_registry_verified": list(verified),
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=output,
    )


def _run_governance_preflight(arguments: list[str], output: TextIO) -> None:
    """Run the current policy gate immediately, without waiting for a heartbeat.

    The command verifies the live Codex heartbeat registry and the vault health
    audit before it can write the governance receipt/checkpoint.  Its receipt
    carries digests only; it never stores the checked instruction text.
    """

    automation_root = _default_codex_automation_root()
    automation_root_seen = False
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--automation-root":
            raw_options.append(option)
            index += 1
            continue
        if automation_root_seen or index + 1 >= len(arguments):
            raise WoonError("--automation-root requires exactly one path")
        automation_root = Path(arguments[index + 1]).expanduser()
        automation_root_seen = True
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options:
        raise WoonError("knowledge governance-preflight takes no positional arguments")
    settings = load_orchestrator_settings(vault or resolve_knowledge_vault())
    verified = verify_codex_automation_registry(settings, automation_root)
    input_sha256, output_sha256 = _governance_preflight_evidence(
        settings.vault,
        settings.policy_document,
        settings.wiki_contract,
        automation_root,
        verified,
    )
    result = record_governance_preflight(
        settings, input_sha256=input_sha256, output_sha256=output_sha256
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "checks": ["instruction-inventory", "automation-registry", "vault-health"],
                "receipt_recorded": not result.replayed,
                "replayed": result.replayed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=output,
    )


def _governance_preflight_evidence(
    vault: Path,
    policy_document: Path,
    wiki_contract: Path,
    automation_root: Path,
    verified: tuple[str, ...],
) -> tuple[str, str]:
    """Return evidence digests after bounded, non-mutating governance checks.

    Any vault-health failure blocks the receipt. Legacy generated daily
    fragments are migrated explicitly before automation is allowed to run.
    """

    audit_script = Path(__file__).parent / "knowledge" / "vault_tools" / "audit-vault-health.py"
    if not audit_script.is_file():
        raise WoonError("second-brain governance health audit script is missing")
    try:
        audit = subprocess.run(
            [sys.executable, str(audit_script)],
            cwd=vault,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        raise WoonError("second-brain governance health audit could not start") from error
    if audit.returncode != 0:
        raise WoonError("second-brain governance health audit failed")

    workspace = vault.parent
    inventory: list[Path] = [
        vault / "config" / "second-brain-orchestrator.yaml",
        policy_document,
        wiki_contract,
    ]
    inventory.extend(sorted((vault / "docs").glob("*.md")))
    inventory.extend(
        sorted(
            path
            for path in (vault / "config").rglob("*")
            if path.is_file() and path.suffix in {".json", ".md", ".yaml", ".yml"}
        )
    )
    for repository_name in ("woon-core", "woon-knowledge", "woon-skills"):
        repository = workspace / repository_name
        if not repository.is_dir():
            continue
        inventory.extend(_active_instruction_files(repository))
    inventory.extend(_governance_skill_inventory(workspace))
    inventory.extend(sorted(automation_root.glob("*/automation.toml")))
    digest = hashlib.sha256()
    retired_markers = ("ai-reference", "_quarantine", "woon-brain", "codex-write-vault")
    for path in sorted({item.resolve() for item in inventory}):
        if not path.is_file():
            raise WoonError("second-brain governance inventory file is missing")
        try:
            content = path.read_bytes()
        except OSError as error:
            raise WoonError("second-brain governance inventory file is unreadable") from error
        if path.name in {"AGENTS.md", "CLAUDE.md"}:
            text = content.decode("utf-8", errors="strict").lower()
            if any(marker in text for marker in retired_markers):
                raise WoonError("second-brain governance found a retired instruction reference")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\n")
    output = hashlib.sha256()
    output.update(audit.stdout.encode("utf-8"))
    output.update("\n".join(verified).encode("utf-8"))
    return digest.hexdigest(), output.hexdigest()


def _active_instruction_files(repository: Path) -> tuple[Path, ...]:
    """Return live instructions without reactivating archived source evidence."""

    ignored_parts = {".git", ".local", "node_modules", "archive", "_sources"}
    candidates = (*repository.rglob("AGENTS.md"), *repository.rglob("CLAUDE.md"))
    return tuple(
        sorted(
            path
            for path in candidates
            if not ignored_parts.intersection(path.relative_to(repository).parts)
        )
    )


def _governance_skill_inventory(
    workspace: Path, *, installed_root: Path | None = None
) -> tuple[Path, ...]:
    """Return canonical skills and reject drift in active installed copies."""

    repository = workspace / "woon-skills"
    if not repository.is_dir():
        return ()
    catalog = repository / "catalog.json"
    if not catalog.is_file():
        raise WoonError("second-brain governance skill catalog is missing")
    canonical_skills = tuple(sorted((repository / "skills").rglob("SKILL.md")))
    if not canonical_skills:
        raise WoonError("second-brain governance canonical skill inventory is empty")
    canonical = tuple(
        sorted(
            path
            for root in (
                repository / "skills",
                repository / "profiles",
                repository / "conflicts",
                repository / "standards",
                repository / "evals",
            )
            for path in root.rglob("*")
            if path.is_file() and path.suffix in {".json", ".md", ".py", ".sh", ".yaml", ".yml"}
        )
    )
    active_root = installed_root or (Path.home() / ".codex/skills")
    installed: list[Path] = []
    for source in canonical_skills:
        active = active_root / source.parent.name / "SKILL.md"
        if not active.exists():
            continue
        if not active.is_file() or active.read_bytes() != source.read_bytes():
            raise WoonError(f"second-brain governance installed skill drift: {source.parent.name}")
        installed.append(active)
    return (catalog, *canonical, *sorted(installed))


_VAULT_TOOL_SCRIPTS = {
    "assess-document-cohesion": "assess-document-cohesion.py",
    "audit-folder-depth": "audit-folder-depth.py",
    "audit-source-assets": "audit-source-assets.py",
    "audit-vault-health": "audit-vault-health.py",
    "convert-svg-diagrams-to-mermaid": "convert-svg-diagrams-to-mermaid.py",
    "normalize-section-headings": "normalize-section-headings.py",
    "personalize-section-headings": "personalize-section-headings.py",
    "remove-ascii-box-diagrams": "remove-ascii-box-diagrams.py",
}
_VAULT_TOOL_SHELL_SCRIPTS = {
    "fetch-transformer-explainer": "fetch-transformer-explainer.sh",
    "evaluate-llm-wiki": "evaluate-llm-wiki.mjs",
}


def _run_vault_tool(arguments: list[str], output: TextIO) -> None:
    """Run one core-owned maintenance tool against an explicit Vault."""

    if not arguments:
        choices = ", ".join(sorted(_VAULT_TOOL_SCRIPTS | _VAULT_TOOL_SHELL_SCRIPTS))
        raise WoonError(f"knowledge vault-tool requires one of: {choices}")
    tool, *raw_options = arguments
    script_name = _VAULT_TOOL_SCRIPTS.get(tool)
    shell_script = _VAULT_TOOL_SHELL_SCRIPTS.get(tool)
    if script_name is None and shell_script is None:
        raise WoonError(f"unknown knowledge vault tool {tool!r}")
    vault, options = _parse_knowledge_options(raw_options)
    target = vault or resolve_knowledge_vault()
    if script_name is not None:
        script = Path(__file__).parent / "knowledge" / "vault_tools" / script_name
        command = [sys.executable, str(script), *options]
    else:
        if shell_script is None:
            raise WoonError(f"core-owned vault tool is missing: {tool}")
        script = Path(__file__).parents[2] / "scripts" / "knowledge-vault" / shell_script
        executable = "node" if script.suffix == ".mjs" else "bash"
        command = [executable, str(script), *options]
    if not script.is_file():
        raise WoonError(f"core-owned vault tool is missing: {tool}")
    try:
        result = subprocess.run(
            command,
            cwd=target,
            capture_output=True,
            check=False,
            env={**os.environ, "VAULT_DIR": str(target)},
            text=True,
        )
    except OSError as error:
        raise WoonError(f"knowledge vault tool could not start: {tool}") from error
    output.write(result.stdout)
    if result.returncode != 0:
        detail = result.stderr.strip() or f"knowledge vault tool failed: {tool}"
        raise WoonError(detail)


def _run_obsidian_plugin(arguments: list[str], output: TextIO) -> None:
    """Manage approved Obsidian releases with filesystem receipts, not UI automation."""

    if not arguments:
        raise WoonError(
            "knowledge obsidian-plugin requires status, install, install-local-build, "
            "remove-detected-mindmaps, "
            "configure-prisma-calendar, configure-full-calendar-remastered, "
            "configure-notion-bases-calendar, configure-link-calendar, "
            "attest-link-calendar-runtime, or retire"
        )
    action, *raw_options = arguments
    plugin_ids: list[str] = []
    options: list[str] = []
    local_options: dict[str, str] = {}
    attested_checks: list[str] = []
    index = 0
    while index < len(raw_options):
        option = raw_options[index]
        if option not in {"--plugin", "--source-dir", "--version", "--attested-check"}:
            options.append(option)
            index += 1
            continue
        if index + 1 >= len(raw_options):
            raise WoonError(f"{option} requires one value")
        value = raw_options[index + 1]
        if option == "--plugin":
            plugin_ids.append(value)
        elif option == "--attested-check":
            attested_checks.append(value)
        elif option in local_options:
            raise WoonError(f"{option} may only be provided once")
        else:
            local_options[option] = value
        index += 2
    vault, remaining = _parse_knowledge_options(options)
    if remaining:
        raise WoonError("unexpected obsidian-plugin argument: " + " ".join(remaining))
    service = ObsidianPluginService(vault or resolve_knowledge_vault())
    if action == "status":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("obsidian-plugin status does not accept install options")
        result = service.status()
    elif action == "install":
        if local_options or attested_checks:
            raise WoonError("obsidian-plugin install does not accept local build options")
        result = service.install(plugin_ids)
    elif action == "install-local-build":
        if attested_checks:
            raise WoonError("install-local-build does not accept --attested-check")
        if len(plugin_ids) != 1:
            raise WoonError("install-local-build requires exactly one --plugin")
        missing = sorted({"--source-dir", "--version"}.difference(local_options))
        if missing:
            raise WoonError("install-local-build requires " + ", ".join(missing))
        result = service.install_local_build(
            plugin_ids[0],
            Path(local_options["--source-dir"]),
            local_options["--version"],
        )
    elif action == "remove-detected-mindmaps":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("remove-detected-mindmaps discovers targets from installed manifests")
        result = service.remove_detected_mindmaps()
    elif action == "configure-prisma-calendar":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("configure-prisma-calendar does not accept --plugin")
        result = service.configure_prisma_calendar()
    elif action == "configure-full-calendar-remastered":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("configure-full-calendar-remastered does not accept --plugin")
        result = service.configure_full_calendar_remastered()
    elif action == "configure-notion-bases-calendar":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("configure-notion-bases-calendar does not accept --plugin")
        result = service.configure_notion_bases_calendar()
    elif action == "configure-link-calendar":
        if plugin_ids or local_options or attested_checks:
            raise WoonError("configure-link-calendar does not accept --plugin")
        result = service.configure_link_calendar()
    elif action == "attest-link-calendar-runtime":
        if plugin_ids or local_options:
            raise WoonError(
                "attest-link-calendar-runtime accepts only --attested-check and --vault"
            )
        result = service.attest_link_calendar_runtime(attested_checks)
    elif action == "retire":
        if local_options or attested_checks:
            raise WoonError("obsidian-plugin retire does not accept local build options")
        result = service.retire(plugin_ids)
    else:
        raise WoonError(f"unknown obsidian-plugin action {action!r}")
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_mail_schedule_candidate_recording(arguments: list[str], output: TextIO) -> None:
    """Record one minimized mail polling window through the same MCP-owned service."""

    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--run-token", "--candidates-json"}:
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if (
        options
        or "--run-token" not in values
        or set(values).difference({"--run-token", "--candidates-json"})
    ):
        raise WoonError(
            "knowledge record-mail-schedule-candidates requires --run-token "
            "and optional --candidates-json"
        )
    raw_candidates = values.get("--candidates-json", "[]")
    try:
        parsed = json.loads(raw_candidates)
    except json.JSONDecodeError as error:
        raise WoonError("mail candidates must be a JSON array") from error
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise WoonError("mail candidates must be a JSON array of objects")
    result = record_mail_schedule_candidates(
        vault or resolve_knowledge_vault(),
        run_token=values["--run-token"],
        submissions=submissions_from_records(parsed),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_codex_daily_digest_recording(arguments: list[str], output: TextIO) -> None:
    """Record one transcript-free block in the canonical daily record."""

    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--day", "--entries-json"}:
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options or set(values) != {"--day", "--entries-json"}:
        raise WoonError("knowledge record-codex-daily-record requires --day and --entries-json")
    try:
        target_day = date.fromisoformat(values["--day"])
    except ValueError as error:
        raise WoonError("Codex daily digest day must be YYYY-MM-DD") from error
    try:
        parsed = json.loads(values["--entries-json"])
    except json.JSONDecodeError as error:
        raise WoonError("Codex daily digest entries must be a JSON array") from error
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise WoonError("Codex daily digest entries must be a JSON array of objects")
    result = record_codex_daily_digest(
        vault or resolve_knowledge_vault(),
        day=target_day,
        entries=daily_digest_entries_from_records(parsed),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_codex_knowledge_entry_recording(arguments: list[str], output: TextIO) -> None:
    """Record one sanitized Codex conclusion batch through the owned service."""

    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--source-range", "--day", "--entries-json", "--input-state"}:
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    required = {"--source-range", "--day", "--entries-json"}
    if (
        options
        or not required.issubset(values)
        or set(values).difference(required | {"--input-state"})
    ):
        raise WoonError(
            "knowledge record-codex-knowledge-entries requires --source-range, --day "
            "and --entries-json"
        )
    try:
        target_day = date.fromisoformat(values["--day"])
    except ValueError as error:
        raise WoonError("Codex knowledge day must be YYYY-MM-DD") from error
    try:
        parsed = json.loads(values["--entries-json"])
    except json.JSONDecodeError as error:
        raise WoonError("Codex knowledge entries must be a JSON array") from error
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise WoonError("Codex knowledge entries must be a JSON array of objects")
    result = record_codex_knowledge_entries(
        vault or resolve_knowledge_vault(),
        source_range=values["--source-range"],
        day=target_day,
        entries=codex_knowledge_entries_from_records(parsed),
        input_state=values.get("--input-state", "processed"),  # type: ignore[arg-type]
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_codex_source_recording(arguments: list[str], output: TextIO) -> None:
    """Archive one allowed conversation bundle from a private local JSON file."""

    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--bundle-file":
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError("--bundle-file requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options or set(values) != {"--bundle-file"}:
        raise WoonError("knowledge record-codex-source requires --bundle-file")
    bundle_file = Path(values["--bundle-file"]).expanduser().resolve()
    if not bundle_file.is_file() or bundle_file.is_symlink():
        raise WoonError("Codex source bundle file is missing or unsafe")
    try:
        parsed = json.loads(bundle_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("Codex source bundle file is unreadable") from error
    if not isinstance(parsed, dict):
        raise WoonError("Codex source bundle must be a JSON object")
    result = record_codex_source_bundle(
        vault or resolve_knowledge_vault(),
        codex_source_bundle_from_record(parsed),
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_codex_daily_digest_materialization(arguments: list[str], output: TextIO) -> None:
    """Materialize one daily record block from the local minimized ledger."""

    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--day":
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError("--day requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options or set(values) != {"--day"}:
        raise WoonError("knowledge materialize-codex-daily-record requires --day")
    try:
        target_day = date.fromisoformat(values["--day"])
    except ValueError as error:
        raise WoonError("Codex daily digest day must be YYYY-MM-DD") from error
    result = record_daily_digest_from_codex_ledger(
        vault or resolve_knowledge_vault(),
        day=target_day,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_legacy_daily_digest_migration(arguments: list[str], output: TextIO) -> None:
    """Migrate generated digest files into their sole daily-record owner."""

    vault, options = _parse_knowledge_options(arguments)
    if options:
        raise WoonError("knowledge migrate-legacy-daily-digests takes no positional arguments")
    result = migrate_legacy_daily_digests(vault or resolve_knowledge_vault())
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _run_schedule_apply(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    raw_options: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--candidate":
            raw_options.append(option)
            index += 1
            continue
        if option in values or index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault, options = _parse_knowledge_options(raw_options)
    if options or set(values) != {"--candidate"}:
        raise WoonError("knowledge schedule-apply requires --candidate")
    receipt = apply_policy_authorized_schedule_candidate(
        vault or resolve_knowledge_vault(), Path(values["--candidate"])
    )
    print(json.dumps(receipt_record(receipt), ensure_ascii=False, indent=2), file=output)


def _run_novel_wiki_projection(arguments: list[str], output: TextIO) -> None:
    """Project the Wiki-internal Novel source archive into one keyword tree."""

    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--vault", "--day"}:
            raise WoonError(f"unexpected knowledge project-novel argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault = Path(values.get("--vault", resolve_knowledge_vault())).expanduser().resolve()
    try:
        projection_day = date.fromisoformat(values.get("--day", date.today().isoformat()))
    except ValueError as error:
        raise WoonError("knowledge project-novel --day must be YYYY-MM-DD") from error
    source = vault / "wiki/private/_sources/novel"
    report = prepare_novel_wiki_projection(vault, source, projection_day=projection_day)
    apply_novel_wiki_projection(vault, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "source_root": source.relative_to(vault).as_posix(),
                "category_count": report.category_count,
                "source_count": report.source_count,
                "event_count": report.event_count,
                "judgment_count": report.judgment_count,
                "relation_count": report.relation_count,
                "changed_count": report.changed_count,
                "retired_stale_pages": len(report.stale_pages),
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=output,
    )


def _run_wiki_tree_refresh(arguments: list[str], output: TextIO) -> None:
    """Refresh every generated navigation block after validating the whole tree."""

    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option != "--vault":
            raise WoonError(f"unexpected knowledge refresh-wiki-tree argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    vault = Path(values.get("--vault", resolve_knowledge_vault())).expanduser().resolve()
    report = prepare_wiki_tree_refresh(vault)
    if report.issues:
        raise WoonError("Wiki tree refresh rejected: " + "; ".join(report.issues[:12]))
    apply_wiki_tree_refresh(vault, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "document_count": report.document_count,
                "changed_count": report.changed_count,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=output,
    )


def _run_compiled_knowledge(command: str, arguments: list[str], output: TextIO) -> None:
    """Run explicit source-schema lifecycle actions outside normal retrieval."""

    force = False
    raw_options: list[str] = []
    for option in arguments:
        if option == "--force":
            if command != "compile":
                raise WoonError("--force is supported only by knowledge compile")
            force = True
        else:
            raw_options.append(option)
    vault, options = _parse_knowledge_options(raw_options)
    if options:
        raise WoonError(f"knowledge {command} takes no positional arguments")
    _, service = build_knowledge_service(vault)
    if command == "migrate-compiled":
        migration = service.migrate_compiled_wiki()
        print(json.dumps(asdict(migration), ensure_ascii=False, indent=2), file=output)
        return
    if command == "initialize-curation":
        count = service.initialize_compiled_wiki_curation()
        print(json.dumps({"curations": count}, ensure_ascii=False, indent=2), file=output)
        return
    if command == "refresh-provisional-curation":
        count = service.refresh_provisional_compiled_wiki_curation()
        print(json.dumps({"refreshed": count}, ensure_ascii=False, indent=2), file=output)
        return
    if command == "reconcile-superseded-revisions":
        report = service.reconcile_superseded_compiled_wiki_revisions()
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2), file=output)
        return
    if command == "compile":
        compilation = service.compile(force=force)
        print(json.dumps(asdict(compilation), ensure_ascii=False, indent=2), file=output)
        return
    audit = service.compilation_audit()
    print(json.dumps(asdict(audit), ensure_ascii=False, indent=2), file=output)
    if not audit.complete:
        raise WoonError(f"compiled Wiki audit found {len(audit.errors)} errors")


def _run_research_intake_plan(arguments: list[str], output: TextIO) -> None:
    if arguments in (["--help"], ["-h"]):
        output.write(RESEARCH_INTAKE_USAGE)
        return
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--purpose", "--zotero", "--notebooklm-manifest", "--output"}:
            raise WoonError(f"unexpected knowledge research-intake-plan argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    if "--purpose" not in values:
        raise WoonError("knowledge research-intake-plan requires --purpose")
    plan = create_research_intake_plan(
        purpose=values["--purpose"],
        zotero_export=(
            Path(values["--zotero"]).expanduser().resolve() if "--zotero" in values else None
        ),
        notebooklm_manifest=(
            Path(values["--notebooklm-manifest"]).expanduser().resolve()
            if "--notebooklm-manifest" in values
            else None
        ),
    )
    if "--output" in values:
        write_research_intake_plan(plan, Path(values["--output"]))
    print(json.dumps(plan, ensure_ascii=False, indent=2), file=output)


def _run_notebooklm_export(arguments: list[str], output: TextIO) -> None:
    if arguments in (["--help"], ["-h"]):
        output.write(NOTEBOOKLM_EXPORT_USAGE)
        return
    values: dict[str, str] = {}
    source_refs: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--artifact-id",
            "--kind",
            "--source-ref",
            "--tool-revision",
            "--output",
            "--manifest",
            "--nlm",
        }:
            raise WoonError(f"unexpected knowledge notebooklm-export argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        value = arguments[index + 1]
        if option == "--source-ref":
            source_refs.append(value)
        elif option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = value
        index += 2
    required = {"--artifact-id", "--kind", "--tool-revision", "--output", "--manifest"}
    missing = sorted(required.difference(values))
    if missing or not source_refs:
        details = missing + ([] if source_refs else ["--source-ref"])
        raise WoonError("knowledge notebooklm-export requires " + ", ".join(details))
    result = export_notebooklm_artifact(
        artifact_id=values["--artifact-id"],
        kind=values["--kind"],
        source_refs=tuple(source_refs),
        tool_revision=values["--tool-revision"],
        output_markdown=Path(values["--output"]),
        manifest_output=Path(values["--manifest"]),
        nlm_binary=values.get("--nlm", "nlm"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_knowledge_evaluation(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--vault", "--cases", "--output"}:
            raise WoonError(f"unexpected knowledge evaluate argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    if "--cases" not in values:
        raise WoonError("knowledge evaluate requires --cases")
    vault = Path(values.get("--vault", ".")).expanduser().resolve()
    result = evaluate_knowledge(vault, Path(values["--cases"]).expanduser().resolve())
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if "--output" in values:
        destination = Path(values["--output"]).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, (rendered + "\n").encode("utf-8"), mode=0o600)
    print(rendered, file=output)
    if not result["passed"]:
        raise WoonError("knowledge retrieval evaluation did not meet thresholds")


def _run_answer_citation_evaluation(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--vault", "--cases", "--answers", "--output"}:
            raise WoonError(f"unexpected knowledge evaluate-answers argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    missing = sorted({"--cases", "--answers"}.difference(values))
    if missing:
        raise WoonError("knowledge evaluate-answers requires " + ", ".join(missing))
    result = evaluate_answer_citations(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--cases"]).expanduser().resolve(),
        Path(values["--answers"]).expanduser().resolve(),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if "--output" in values:
        destination = Path(values["--output"]).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, (rendered + "\n").encode("utf-8"), mode=0o600)
    print(rendered, file=output)
    if not result["passed"]:
        raise WoonError("knowledge answer/citation evaluation did not meet all checks")


def _run_content_quality_evaluation(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--vault", "--reviews", "--standard", "--prompt", "--output"}:
            raise WoonError(f"unexpected knowledge evaluate-quality argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    missing = sorted({"--reviews", "--standard", "--prompt"}.difference(values))
    if missing:
        raise WoonError("knowledge evaluate-quality requires " + ", ".join(missing))
    result = evaluate_content_quality(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--reviews"]).expanduser().resolve(),
        Path(values["--standard"]).expanduser().resolve(),
        Path(values["--prompt"]).expanduser().resolve(),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if "--output" in values:
        destination = Path(values["--output"]).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(destination, (rendered + "\n").encode("utf-8"), mode=0o600)
    print(rendered, file=output)
    if not result["passed"]:
        raise WoonError("knowledge content quality evaluation did not meet all checks")


def _run_content_quality_review_plan(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--vault",
            "--standard",
            "--standard-uri",
            "--prompt",
            "--prompt-uri",
            "--output",
            "--batch-size",
            "--max-batch-chars",
        }:
            raise WoonError(f"unexpected knowledge quality-review-plan argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    missing = sorted({"--standard", "--prompt", "--output"}.difference(values))
    if missing:
        raise WoonError("knowledge quality-review-plan requires " + ", ".join(missing))
    batch_size = _quality_review_batch_size(values.get("--batch-size", "1"))
    max_batch_chars = _quality_review_max_batch_chars(values.get("--max-batch-chars", "24000"))
    result = create_content_quality_review_plan(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--standard"]).expanduser().resolve(),
        values.get("--standard-uri", "repo://skills/standards/learning-writing-harness.md"),
        Path(values["--prompt"]).expanduser().resolve(),
        values.get("--prompt-uri", "repo://skills/standards/learning-quality-review-prompt.md"),
        Path(values["--output"]).expanduser().resolve(),
        batch_size,
        max_batch_chars,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_content_quality_review_rebase(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--vault",
            "--prior-plan",
            "--prior-results",
            "--standard",
            "--standard-uri",
            "--prompt",
            "--prompt-uri",
            "--output",
            "--results",
            "--batch-size",
            "--max-batch-chars",
        }:
            raise WoonError(f"unexpected knowledge rebase-quality-review-plan argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    required = {
        "--prior-plan",
        "--prior-results",
        "--standard",
        "--prompt",
        "--output",
        "--results",
    }
    missing = sorted(required.difference(values))
    if missing:
        raise WoonError("knowledge rebase-quality-review-plan requires " + ", ".join(missing))
    result = rebase_content_quality_review_plan(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--prior-plan"]).expanduser().resolve(),
        Path(values["--prior-results"]).expanduser().resolve(),
        Path(values["--standard"]).expanduser().resolve(),
        values.get("--standard-uri", "repo://skills/standards/learning-writing-harness.md"),
        Path(values["--prompt"]).expanduser().resolve(),
        values.get("--prompt-uri", "repo://skills/standards/learning-quality-review-prompt.md"),
        Path(values["--output"]).expanduser().resolve(),
        Path(values["--results"]).expanduser().resolve(),
        _quality_review_batch_size(values.get("--batch-size", "2")),
        _quality_review_max_batch_chars(values.get("--max-batch-chars", "24000")),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_content_quality_review_assembly(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--vault",
            "--plan",
            "--results",
            "--standard",
            "--evaluator-name",
            "--evaluator-version",
            "--output",
        }:
            raise WoonError(f"unexpected knowledge assemble-quality-reviews argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    required = {
        "--plan",
        "--results",
        "--standard",
        "--evaluator-name",
        "--evaluator-version",
        "--output",
    }
    missing = sorted(required.difference(values))
    if missing:
        raise WoonError("knowledge assemble-quality-reviews requires " + ", ".join(missing))
    result = assemble_content_quality_reviews(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--plan"]).expanduser().resolve(),
        Path(values["--results"]).expanduser().resolve(),
        Path(values["--standard"]).expanduser().resolve(),
        values["--evaluator-name"],
        values["--evaluator-version"],
        Path(values["--output"]).expanduser().resolve(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_ollama_quality_review(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    batch_ids: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--plan",
            "--results",
            "--model",
            "--batch",
            "--timeout-seconds",
            "--max-attempts",
            "--context-tokens",
            "--adaptive-context",
            "--continue-on-error",
        }:
            raise WoonError(f"unexpected knowledge review-quality-ollama argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        value = arguments[index + 1]
        if option == "--batch":
            batch_ids.append(value)
        elif option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = value
        index += 2
    missing = sorted({"--plan", "--results"}.difference(values))
    if missing:
        raise WoonError("knowledge review-quality-ollama requires " + ", ".join(missing))
    timeout = _ollama_quality_timeout(values.get("--timeout-seconds", "600"))
    max_attempts = _ollama_quality_max_attempts(values.get("--max-attempts", "3"))
    context_tokens = _ollama_quality_context_tokens(values.get("--context-tokens", "32768"))
    adaptive_context = _boolean_option(
        values.get("--adaptive-context", "false"), "Ollama quality review adaptive_context"
    )
    continue_on_error = _boolean_option(
        values.get("--continue-on-error", "false"), "Ollama quality review continue_on_error"
    )
    result = run_ollama_quality_reviews(
        Path(values["--plan"]).expanduser().resolve(),
        Path(values["--results"]).expanduser().resolve(),
        model=values.get("--model", "qwen3:4b-instruct"),
        timeout_seconds=timeout,
        max_attempts=max_attempts,
        context_tokens=context_tokens,
        adaptive_context=adaptive_context,
        continue_on_error=continue_on_error,
        batch_ids=tuple(batch_ids),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_codex_quality_review(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    batch_ids: list[str] = []
    allowed = {
        "--plan",
        "--results",
        "--model",
        "--codex-binary",
        "--batch",
        "--timeout-seconds",
        "--max-attempts",
        "--continue-on-error",
    }
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in allowed:
            raise WoonError(f"unexpected knowledge review-quality-codex argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        value = arguments[index + 1]
        if option == "--batch":
            batch_ids.append(value)
        elif option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = value
        index += 2
    missing = sorted({"--plan", "--results"}.difference(values))
    if missing:
        raise WoonError("knowledge review-quality-codex requires " + ", ".join(missing))
    result = run_codex_quality_reviews(
        Path(values["--plan"]).expanduser().resolve(),
        Path(values["--results"]).expanduser().resolve(),
        model=values.get("--model"),
        codex_binary=values.get("--codex-binary", "codex"),
        timeout_seconds=_ollama_quality_timeout(values.get("--timeout-seconds", "900")),
        max_attempts=_codex_quality_max_attempts(values.get("--max-attempts", "1")),
        continue_on_error=_boolean_option(
            values.get("--continue-on-error", "false"), "Codex quality review continue_on_error"
        ),
        batch_ids=tuple(batch_ids),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_codex_quality_revision(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    page_ids: list[str] = []
    allowed = {
        "--vault",
        "--plan",
        "--reviews",
        "--output",
        "--model",
        "--codex-binary",
        "--page",
        "--timeout-seconds",
        "--max-attempts",
        "--continue-on-error",
    }
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in allowed:
            raise WoonError(f"unexpected knowledge revise-quality-codex argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        if option == "--page":
            page_ids.append(arguments[index + 1])
        elif option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = arguments[index + 1]
        index += 2
    required = {"--plan", "--reviews", "--output"}
    missing = sorted(required.difference(values))
    if missing:
        raise WoonError("knowledge revise-quality-codex requires " + ", ".join(missing))
    result = create_codex_quality_revision_proposals(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--plan"]).expanduser().resolve(),
        Path(values["--reviews"]).expanduser().resolve(),
        Path(values["--output"]).expanduser().resolve(),
        model=values.get("--model"),
        codex_binary=values.get("--codex-binary", "codex"),
        timeout_seconds=_ollama_quality_timeout(values.get("--timeout-seconds", "900")),
        max_attempts=_codex_quality_max_attempts(values.get("--max-attempts", "1")),
        continue_on_error=_boolean_option(
            values.get("--continue-on-error", "false"), "Codex quality revision continue_on_error"
        ),
        page_ids=tuple(page_ids),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_codex_quality_revision_apply(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    proposal_dirs: list[str] = []
    page_ids: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--vault",
            "--plan",
            "--reviews",
            "--proposals",
            "--duplicate-policy",
            "--page",
        }:
            raise WoonError(f"unexpected knowledge apply-quality-revisions argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires exactly one value")
        if option == "--proposals":
            proposal_dirs.append(arguments[index + 1])
        elif option == "--page":
            page_ids.append(arguments[index + 1])
        elif option in values:
            raise WoonError(f"{option} requires exactly one value")
        else:
            values[option] = arguments[index + 1]
        index += 2
    required = {"--plan", "--reviews"}
    missing = sorted(required.difference(values))
    if missing or not proposal_dirs:
        if not proposal_dirs:
            missing.append("--proposals")
        raise WoonError("knowledge apply-quality-revisions requires " + ", ".join(missing))
    result = apply_codex_quality_revisions(
        Path(values.get("--vault", ".")).expanduser().resolve(),
        Path(values["--plan"]).expanduser().resolve(),
        Path(values["--reviews"]).expanduser().resolve(),
        tuple(Path(path).expanduser().resolve() for path in proposal_dirs),
        duplicate_policy=values.get("--duplicate-policy", "error"),
        page_ids=tuple(page_ids),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _ollama_quality_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError as error:
        raise WoonError("Ollama quality review timeout must be an integer") from error
    if not 30 <= timeout <= 3600:
        raise WoonError("Ollama quality review timeout must be between 30 and 3600 seconds")
    return timeout


def _ollama_quality_max_attempts(value: str) -> int:
    try:
        max_attempts = int(value)
    except ValueError as error:
        raise WoonError("Ollama quality review max_attempts must be an integer") from error
    if not 1 <= max_attempts <= 5:
        raise WoonError("Ollama quality review max_attempts must be between 1 and 5")
    return max_attempts


def _codex_quality_max_attempts(value: str) -> int:
    try:
        max_attempts = int(value)
    except ValueError as error:
        raise WoonError("Codex quality review max_attempts must be an integer") from error
    if not 1 <= max_attempts <= 3:
        raise WoonError("Codex quality review max_attempts must be between 1 and 3")
    return max_attempts


def _ollama_quality_context_tokens(value: str) -> int:
    try:
        context_tokens = int(value)
    except ValueError as error:
        raise WoonError("Ollama quality review context_tokens must be an integer") from error
    if not 4096 <= context_tokens <= 32768:
        raise WoonError("Ollama quality review context_tokens must be between 4096 and 32768")
    return context_tokens


def _boolean_option(value: str, label: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise WoonError(f"{label} must be true or false")


def _quality_review_batch_size(value: str) -> int:
    try:
        batch_size = int(value)
    except ValueError as error:
        raise WoonError("quality review batch size must be an integer") from error
    if not 1 <= batch_size <= 64:
        raise WoonError("quality review batch size must be between 1 and 64")
    return batch_size


def _quality_review_max_batch_chars(value: str) -> int:
    try:
        maximum = int(value)
    except ValueError as error:
        raise WoonError("quality review max_batch_chars must be an integer") from error
    if not 4_000 <= maximum <= 200_000:
        raise WoonError("quality review max_batch_chars must be between 4000 and 200000")
    return maximum


def _run_knowledge_source_plan(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--source", "--source-name", "--vault", "--output"}:
            raise WoonError(f"unexpected source-plan argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires a value")
        value = arguments[index + 1]
        if option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = value
        index += 2
    if "--source" not in values or "--source-name" not in values:
        raise WoonError("source-plan requires --source and --source-name")
    target = Path(values.get("--vault", ".")).expanduser().resolve()
    _reject_self_source_catalog(Path(values["--source"]), target)
    relative_output = Path(
        values.get("--output", f"catalog/sources/{values['--source-name']}.yaml")
    )
    if relative_output.is_absolute() or ".." in relative_output.parts:
        raise WoonError("source-plan --output must be a safe relative path")
    destination = (target / relative_output).resolve()
    try:
        destination.relative_to(target)
    except ValueError as error:
        raise WoonError("source-plan output escapes the target vault") from error
    plan = plan_source_catalog(
        Path(values["--source"]),
        target,
        values["--source-name"],
        previous_records=(
            load_source_catalog(destination).records if destination.is_file() else ()
        ),
    )
    write_source_catalog(plan, destination)
    print(
        json.dumps(
            {
                "status": "ok",
                "source": plan.source_name,
                "records": len(plan.records),
                "excluded": len(plan.excluded),
                "summary": plan.summary,
                "output": relative_output.as_posix(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=output,
    )


def _run_knowledge_source_reconcile(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {
            "--source",
            "--source-name",
            "--vault",
            "--limit",
            "--model",
            "--state",
            "--max-attempts",
            "--reasoning-effort",
        }:
            raise WoonError(f"unexpected source-reconcile argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    if "--source" not in values or "--source-name" not in values:
        raise WoonError("source-reconcile requires --source and --source-name")
    try:
        limit = int(values.get("--limit", "1"))
    except ValueError as error:
        raise WoonError("source-reconcile --limit must be an integer") from error
    try:
        max_attempts = int(values.get("--max-attempts", "3"))
    except ValueError as error:
        raise WoonError("source-reconcile --max-attempts must be an integer") from error
    reasoning_effort = values.get("--reasoning-effort", "high")
    if reasoning_effort not in {"low", "medium", "high"}:
        raise WoonError("source-reconcile --reasoning-effort must be low, medium, or high")
    state = values.get("--state")
    if state is not None and state not in {"merge-required", "semantic-match", "new"}:
        raise WoonError("source-reconcile --state must be merge-required, semantic-match, or new")
    target = Path(values.get("--vault", ".")).expanduser().resolve()
    _reject_self_source_catalog(Path(values["--source"]), target)
    settings, _ = build_knowledge_service(target)
    if settings.compiled_wiki is not None:
        raise WoonError(
            "source-reconcile writes Markdown directly and is disabled for a compiled Wiki; "
            "capture source and claim/page-spec records, then run knowledge compile"
        )
    name = values["--source-name"]
    summary = reconcile_catalog(
        Path(values["--source"]),
        target,
        target / f"catalog/sources/{name}.yaml",
        target / f"catalog/reconciliation/{name}.yaml",
        limit=limit,
        model=values.get("--model", "gpt-5.6-terra"),
        max_attempts=max_attempts,
        reasoning_effort=reasoning_effort,
        states=(state,) if state is not None else ("merge-required", "semantic-match", "new"),
    )
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2), file=output)


def _run_knowledge_source_audit(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--source", "--source-name", "--vault"}:
            raise WoonError(f"unexpected source-audit argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    if "--source" not in values or "--source-name" not in values:
        raise WoonError("source-audit requires --source and --source-name")
    target = Path(values.get("--vault", ".")).expanduser().resolve()
    _reject_self_source_catalog(Path(values["--source"]), target)
    name = values["--source-name"]
    audit = audit_reconciliation(
        Path(values["--source"]),
        target,
        target / f"catalog/sources/{name}.yaml",
        target / f"catalog/reconciliation/{name}.yaml",
    )
    print(json.dumps(asdict(audit), ensure_ascii=False, indent=2), file=output)
    if not audit.complete:
        raise WoonError(
            f"source reconciliation is incomplete: pending={audit.pending}, "
            f"failed={audit.failed}, errors={len(audit.errors)}"
        )


def _run_knowledge_source_archive(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--source", "--source-name", "--wiki-subject", "--vault"}:
            raise WoonError(f"unexpected source-archive argument: {option}")
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    required = {"--source", "--source-name", "--wiki-subject"}
    if not required.issubset(values):
        raise WoonError("source-archive requires --source, --source-name, and --wiki-subject")
    target = Path(values.get("--vault", ".")).expanduser().resolve()
    result = archive_private_source_corpus(
        Path(values["--source"]),
        target,
        values["--source-name"],
        values["--wiki-subject"],
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), file=output)


def _reject_self_source_catalog(source: Path, target: Path) -> None:
    """Keep the current vault out of the external-corpus import workflow."""

    resolved_source = source.expanduser().resolve()
    resolved_target = target.expanduser().resolve()
    try:
        resolved_source.relative_to(resolved_target)
        overlaps = True
    except ValueError:
        try:
            resolved_target.relative_to(resolved_source)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        raise WoonError(
            "current vault self-source catalog is retired; use knowledge compile-audit "
            "and audit-vault-health instead"
        )


def _parse_knowledge_options(arguments: list[str]) -> tuple[Path | None, list[str]]:
    vault: Path | None = None
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option in {"--vault", "--limit"}:
            if index + 1 >= len(arguments):
                raise WoonError(f"{option} requires a value")
            if option == "--vault":
                vault = Path(arguments[index + 1])
            index += 2
            continue
        remaining.append(option)
        index += 1
    return vault, remaining


def _knowledge_limit(arguments: list[str], default: int) -> int:
    if "--limit" not in arguments:
        return default
    index = arguments.index("--limit")
    try:
        return int(arguments[index + 1])
    except (IndexError, ValueError) as error:
        raise WoonError("--limit requires an integer") from error


def _parse_global(arguments: list[str]) -> tuple[str, list[str]]:
    root = ""
    clean: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] != "--root":
            clean.append(arguments[index])
            index += 1
            continue
        if index + 1 >= len(arguments) or not arguments[index + 1].strip():
            raise WoonError("--root requires a path")
        root = arguments[index + 1]
        index += 2
    return root, clean


def _parse_target(arguments: list[str]) -> tuple[str, list[str]]:
    target = runtime_target()
    remaining: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] != "--target":
            remaining.append(arguments[index])
            index += 1
            continue
        if index + 1 >= len(arguments):
            raise WoonError("--target requires macos, windows, or linux")
        target = arguments[index + 1]
        index += 2
    if target not in {"macos", "windows", "linux"}:
        raise WoonError(f"unsupported target {target!r}")
    return target, remaining


def _parse_python_ide_options(arguments: list[str]) -> Path:
    if len(arguments) != 2 or arguments[0] != "--project" or not arguments[1].strip():
        raise WoonError("python-ide requires --project <path>")
    return Path(arguments[1])


def _parse_skills_options(arguments: list[str]) -> tuple[list[str], str]:
    profiles: list[str] = []
    target = ""
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--profile", "--target"} or index + 1 >= len(arguments):
            raise WoonError(f"unexpected skills argument {option!r}")
        value = arguments[index + 1]
        if option == "--profile":
            profiles.extend(part.strip() for part in value.split(",") if part.strip())
        else:
            target = value
        index += 2
    return sorted(profiles), target


def _parse_routing_options(arguments: list[str]) -> tuple[int | None, str]:
    repeat: int | None = None
    executor = "all"
    seen: set[str] = set()
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if (
            option not in {"--repeat", "--executor"}
            or option in seen
            or index + 1 >= len(arguments)
        ):
            raise WoonError(
                "usage: woon skills eval-routing [--executor <all|codex|claude>] [--repeat <count>]"
            )
        seen.add(option)
        value = arguments[index + 1]
        if option == "--executor":
            if value not in {"all", "codex", "claude"}:
                raise WoonError("--executor must be all, codex, or claude")
            executor = value
        else:
            try:
                repeat = int(value)
            except ValueError as error:
                raise WoonError("--repeat must be a positive integer") from error
            if repeat <= 0:
                raise WoonError("--repeat must be a positive integer")
        index += 2
    return repeat, executor


def _routing_selectors(
    executor: str,
) -> list[tuple[str, CodexRoutingSelector | ClaudeRoutingSelector]]:
    selectors: dict[str, CodexRoutingSelector | ClaudeRoutingSelector] = {
        "codex": CodexRoutingSelector(),
        "claude": ClaudeRoutingSelector(),
    }
    names = ("codex", "claude") if executor == "all" else (executor,)
    return [(name, selectors[name]) for name in names]


def _load(root: str) -> tuple[Workspace, Registry]:
    workspace = discover(root)
    return workspace, Registry.load(workspace.root)
