"""Command-line entry point."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from woon_core import __version__
from woon_core.context import Compiler
from woon_core.environment import apply as apply_environment
from woon_core.environment import check as check_environment
from woon_core.environment import doctor as doctor_environment
from woon_core.environment import generate as generate_environment
from woon_core.environment import plan as plan_environment
from woon_core.environment import verify as verify_environment
from woon_core.environment.machine import runtime_target
from woon_core.errors import WoonError
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.reconciliation import audit_reconciliation, reconcile_catalog
from woon_core.knowledge.source_catalog import (
    load_source_catalog,
    plan_source_catalog,
    write_source_catalog,
)
from woon_core.registry import Registry
from woon_core.skills import ClaudeRoutingSelector, CodexRoutingSelector, evaluate_routing
from woon_core.skills import doctor as doctor_skills
from woon_core.skills import install as install_skills
from woon_core.skills import plan as plan_skills
from woon_core.skills import validate as validate_skills
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
  woon skills plan --profile <names> [--target <codex|claude>]
  woon skills validate --profile <names>
  woon skills install --profile <names> --target <codex|claude>
  woon skills eval-routing [--executor <all|codex|claude>] [--repeat <count>]
  woon skills doctor
  woon knowledge index [--vault <path>]
  woon knowledge search <query> [--limit <1..20>] [--vault <path>]
  woon knowledge get <canonical-id> [--vault <path>]
  woon knowledge audit [--vault <path>]
  woon knowledge history <canonical-id> [--limit <1..100>] [--vault <path>]
  woon knowledge migrate-compiled [--vault <path>]
  woon knowledge compile [--force] [--vault <path>]
  woon knowledge compile-audit [--vault <path>]
  woon knowledge source-plan --source <path> --source-name <name> [--protect <glob>]
    [--vault <path>] [--output <relative-path>]
  woon knowledge source-reconcile --source <path> --source-name <name>
    [--vault <path>] [--limit <count>] [--model <model>] [--state <state>]
  woon knowledge source-audit --source <path> --source-name <name> [--vault <path>]
  woon version
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
                    print(f"  - failed: {case.identifier}", file=output)
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
        raise WoonError("usage: woon knowledge <index|search|get|audit|history|compile>")
    command, *raw_options = arguments
    if command == "source-plan":
        _run_knowledge_source_plan(raw_options, output)
        return
    if command == "source-reconcile":
        _run_knowledge_source_reconcile(raw_options, output)
        return
    if command == "source-audit":
        _run_knowledge_source_audit(raw_options, output)
        return
    if command in {"migrate-compiled", "compile", "compile-audit"}:
        _run_compiled_knowledge(command, raw_options, output)
        return
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
    else:
        raise WoonError(f"unknown knowledge command {command!r}")


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
    if command == "compile":
        compilation = service.compile(force=force)
        print(json.dumps(asdict(compilation), ensure_ascii=False, indent=2), file=output)
        return
    audit = service.compilation_audit()
    print(json.dumps(asdict(audit), ensure_ascii=False, indent=2), file=output)
    if not audit.complete:
        raise WoonError(f"compiled Wiki audit found {len(audit.errors)} errors")


def _run_knowledge_source_plan(arguments: list[str], output: TextIO) -> None:
    values: dict[str, str] = {}
    protected: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option not in {"--source", "--source-name", "--vault", "--output", "--protect"}:
            raise WoonError(f"unexpected source-plan argument: {option}")
        if index + 1 >= len(arguments):
            raise WoonError(f"{option} requires a value")
        value = arguments[index + 1]
        if option == "--protect":
            protected.append(value)
        elif option in values:
            raise WoonError(f"{option} may only be provided once")
        else:
            values[option] = value
        index += 2
    if "--source" not in values or "--source-name" not in values:
        raise WoonError("source-plan requires --source and --source-name")
    target = Path(values.get("--vault", ".")).expanduser().resolve()
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
        protected_patterns=tuple(protected),
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
