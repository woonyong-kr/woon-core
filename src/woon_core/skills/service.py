"""Resolve, validate, and install profile-selected skills."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, load_yaml
from woon_core.registry import Registry

INSTALL_MANIFEST_NAME = ".woon-installed.json"
ALLOWED_EFFECTS = {
    "read",
    "write",
    "process",
    "network",
    "commit",
    "push",
    "merge",
    "release",
    "delete",
}


@dataclass(frozen=True, slots=True)
class CatalogSkill:
    reference: str
    name: str
    description: str
    path: Path
    hash: str
    effects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanItem:
    name: str
    source: str
    hash: str
    effects: tuple[str, ...]
    action: str


@dataclass(frozen=True, slots=True)
class PlanResult:
    profiles: tuple[str, ...]
    items: tuple[PlanItem, ...]
    target: Path | None


@dataclass(frozen=True, slots=True)
class InstallResult:
    installed: int
    updated: int
    retired: int
    unchanged: int
    target: Path
    backup: Path | None


RoutingSelector = Callable[
    [tuple[CatalogSkill, ...], dict[str, str]],
    dict[str, list[str]],
]


@dataclass(frozen=True, slots=True)
class RoutingCaseResult:
    identifier: str
    primary: str
    selections: tuple[tuple[str, ...], ...]
    passed: bool


@dataclass(frozen=True, slots=True)
class RoutingEvalResult:
    repeat: int
    primary_recall: float
    forbidden_selections: int
    agreement: float
    passed: bool
    cases: tuple[RoutingCaseResult, ...]


@dataclass(frozen=True, slots=True)
class _Resolved:
    repository_path: Path
    profiles: tuple[str, ...]
    skills: tuple[CatalogSkill, ...]
    non_installable_profiles: tuple[str, ...]


def validate(root: Path, registry: Registry, profiles: list[str]) -> PlanResult:
    resolved = _load_resolved(root, registry, profiles)
    _validate_sources(resolved.repository_path)
    _validate_catalog(resolved.repository_path)
    _validate_profile_evals(resolved.repository_path)
    _load_routing_evals(resolved.repository_path)
    return PlanResult(
        profiles=resolved.profiles,
        items=tuple(
            PlanItem(skill.name, skill.reference, skill.hash, skill.effects, "selected")
            for skill in resolved.skills
        ),
        target=None,
    )


def evaluate_routing(
    root: Path,
    registry: Registry,
    selector: RoutingSelector,
    repeat: int | None = None,
) -> RoutingEvalResult:
    repository_path = registry.resolve(root, "skills")
    config, cases = _load_routing_evals(repository_path)
    configured_repeat = _positive_int(config.get("repeat"), "routing repeat")
    run_count = repeat if repeat is not None else configured_repeat
    if run_count <= 0:
        raise WoonError("routing repeat must be positive")

    thresholds = _mapping(config.get("thresholds"))
    required_recall = _ratio(thresholds.get("primary_recall"), "primary_recall")
    required_agreement = _ratio(thresholds.get("agreement"), "agreement")
    allowed_forbidden = _non_negative_int(
        thresholds.get("forbidden_selections"), "forbidden_selections"
    )

    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(tuple(_strings(case.get("profiles"))), []).append(case)

    selections_by_case: dict[str, list[tuple[str, ...]]] = {str(case["id"]): [] for case in cases}
    for profiles, group in grouped.items():
        resolved = _load_resolved(root, registry, list(profiles))
        prompts = {str(case["id"]): str(case["prompt"]) for case in group}
        available = {skill.name for skill in resolved.skills}
        for _ in range(run_count):
            selection_response = selector(resolved.skills, prompts)
            if set(selection_response) != set(prompts):
                raise WoonError("routing selector returned missing or unexpected case IDs")
            for identifier, names in selection_response.items():
                if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                    raise WoonError(f"routing selector returned invalid result for {identifier!r}")
                unknown = set(names).difference(available)
                if unknown:
                    raise WoonError(
                        f"routing selector returned unavailable skill {min(unknown)!r} "
                        f"for {identifier!r}"
                    )
                selections_by_case[identifier].append(tuple(sorted(set(names))))

    primary_hits = 0
    forbidden_count = 0
    agreements = 0
    results: list[RoutingCaseResult] = []
    for case in cases:
        identifier = str(case["id"])
        primary = str(case["expect_primary"])
        allowed = {primary, *_strings(case.get("allow_support", []))}
        rejected = set(_strings(case.get("reject", [])))
        max_selected = _positive_int(case.get("max_selected"), f"{identifier} max_selected")
        runs = selections_by_case[identifier]
        run_passes: list[bool] = []
        for selected_names in runs:
            selected_set = set(selected_names)
            primary_hits += int(primary in selected_set)
            forbidden_count += len(selected_set.intersection(rejected))
            run_passes.append(
                primary in selected_set
                and not selected_set.intersection(rejected)
                and selected_set.issubset(allowed)
                and len(selected_set) <= max_selected
            )
        agrees = len(set(runs)) == 1
        agreements += int(agrees)
        results.append(RoutingCaseResult(identifier, primary, tuple(runs), all(run_passes)))

    total_runs = len(cases) * run_count
    primary_recall = primary_hits / total_runs
    agreement = agreements / len(cases)
    passed = (
        primary_recall >= required_recall
        and forbidden_count <= allowed_forbidden
        and agreement >= required_agreement
        and all(result.passed for result in results)
    )
    return RoutingEvalResult(
        repeat=run_count,
        primary_recall=primary_recall,
        forbidden_selections=forbidden_count,
        agreement=agreement,
        passed=passed,
        cases=tuple(results),
    )


def plan(root: Path, registry: Registry, profiles: list[str], target_name: str) -> PlanResult:
    resolved = _load_resolved(root, registry, profiles)
    if target_name:
        _require_installable(resolved)
    _validate_sources(resolved.repository_path)
    target = _target_path(target_name) if target_name else None
    manifest = _read_install_manifest(target) if target else {"skills": {}}
    managed_skills = _mapping(manifest.get("skills"))
    selected: set[str] = set()
    items: list[PlanItem] = []
    for skill in resolved.skills:
        action = "selected"
        if target:
            destination = target / skill.name
            actual_hash, exists = _installed_hash(destination)
            managed_hash = managed_skills.get(skill.name)
            if managed_hash is None and exists:
                action = "blocked"
            elif managed_hash is None:
                action = "install"
            elif not exists:
                action = "repair"
            elif managed_hash != skill.hash or actual_hash != skill.hash:
                action = "update"
            else:
                action = "unchanged"
        selected.add(skill.name)
        items.append(PlanItem(skill.name, skill.reference, skill.hash, skill.effects, action))
    for name, digest in managed_skills.items():
        if name in selected:
            continue
        _, exists = _installed_hash(target / name) if target else ("", False)
        items.append(PlanItem(name, "", str(digest), (), "retire" if exists else "forget"))
    return PlanResult(resolved.profiles, tuple(sorted(items, key=lambda item: item.name)), target)


def install(root: Path, registry: Registry, profiles: list[str], target_name: str) -> InstallResult:
    if not target_name:
        raise WoonError("skills install requires --target codex or claude")
    resolved = _load_resolved(root, registry, profiles)
    _require_installable(resolved)
    installation_plan = plan(root, registry, profiles, target_name)
    assert installation_plan.target is not None
    target = installation_plan.target
    target.mkdir(parents=True, exist_ok=True)
    for item in installation_plan.items:
        if item.action == "blocked":
            raise WoonError(f"refusing to overwrite unmanaged skill {target / item.name}")

    staging = Path(tempfile.mkdtemp(prefix=".woon-staging-", dir=target))
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_root = target / ".woon-backups" / timestamp
    by_name = {skill.name: skill for skill in resolved.skills}
    moved: list[tuple[Path, Path]] = []
    installed_paths: list[Path] = []
    manifest_path = target / INSTALL_MANIFEST_NAME
    previous_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    counts = {"installed": 0, "updated": 0, "retired": 0, "unchanged": 0}
    try:
        for item in installation_plan.items:
            if item.action in {"install", "update", "repair"}:
                shutil.copytree(
                    by_name[item.name].path,
                    staging / item.name,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"),
                )
        for item in installation_plan.items:
            destination = target / item.name
            if item.action == "unchanged":
                counts["unchanged"] += 1
            elif item.action in {"install", "update", "repair"}:
                if item.action == "update":
                    backup = backup_root / item.name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    destination.replace(backup)
                    moved.append((destination, backup))
                    counts["updated"] += 1
                elif item.action == "install":
                    counts["installed"] += 1
                else:
                    counts["updated"] += 1
                (staging / item.name).replace(destination)
                installed_paths.append(destination)
            elif item.action == "retire":
                backup = backup_root / item.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(backup)
                moved.append((destination, backup))
                counts["retired"] += 1
            elif item.action == "forget":
                counts["retired"] += 1
        manifest = {
            "version": 1,
            "profiles": list(resolved.profiles),
            "skills": {skill.name: skill.hash for skill in resolved.skills},
        }
        atomic_write(
            manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )
    except BaseException as error:
        for path in reversed(installed_paths):
            shutil.rmtree(path, ignore_errors=True)
        for live, backup in reversed(moved):
            if backup.exists():
                backup.replace(live)
        if previous_manifest is None:
            manifest_path.unlink(missing_ok=True)
        else:
            atomic_write(manifest_path, previous_manifest)
        raise WoonError(f"skill installation rolled back: {error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return InstallResult(
        installed=counts["installed"],
        updated=counts["updated"],
        retired=counts["retired"],
        unchanged=counts["unchanged"],
        target=target,
        backup=backup_root if moved else None,
    )


def doctor() -> dict[str, Path]:
    return {target: _target_path(target) for target in ("codex", "claude")}


def _require_installable(resolved: _Resolved) -> None:
    if resolved.non_installable_profiles:
        names = ", ".join(resolved.non_installable_profiles)
        raise WoonError(f"profile is not installable: {names}")


def _load_resolved(root: Path, registry: Registry, requested: list[str]) -> _Resolved:
    repository_path = registry.resolve(root, "skills")
    profile_names, references, max_active, non_installable_profiles = _resolve_profiles(
        repository_path, requested or ["core"]
    )
    if len(references) > max_active:
        raise WoonError(f"resolved profile has {len(references)} skills, budget is {max_active}")
    effects = load_yaml(repository_path / "conflicts/effects.yaml")
    if effects.get("version") != 1:
        raise WoonError(f"unsupported effects schema version {effects.get('version')}")
    default_effects = _effects(effects.get("default"), "default")
    effect_overrides = _mapping(effects.get("skills"))
    for reference, declared in effect_overrides.items():
        _effects(declared, reference)

    catalog: list[CatalogSkill] = []
    seen_names: dict[str, str] = {}
    for reference in references:
        path = _safe_skill_path(repository_path, reference)
        metadata = _read_frontmatter(path / "SKILL.md")
        name = str(metadata["name"])
        description = str(metadata["description"])
        if name != PurePosixPath(reference).name:
            raise WoonError(f"{reference} frontmatter name {name!r} does not match directory")
        if len(description.strip()) > 180:
            raise WoonError(f"{reference} description exceeds 180 characters")
        if previous := seen_names.get(name):
            raise WoonError(f"duplicate active skill name {name!r}: {previous} and {reference}")
        seen_names[name] = reference
        declared = _effects(effect_overrides.get(reference, default_effects), reference)
        catalog.append(
            CatalogSkill(
                reference=reference,
                name=name,
                description=description,
                path=path,
                hash=_hash_directory(path),
                effects=tuple(declared),
            )
        )
    _validate_conflicts(repository_path, references)
    return _Resolved(
        repository_path,
        tuple(profile_names),
        tuple(catalog),
        tuple(non_installable_profiles),
    )


def _resolve_profiles(
    repository_path: Path, requested: list[str]
) -> tuple[list[str], list[str], int, list[str]]:
    profiles: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()
    selected_profiles: set[str] = set()
    selected_skills: set[str] = set()
    non_installable_profiles: set[str] = set()
    max_active = sys_max = 2**63 - 1

    def visit(name: str) -> None:
        nonlocal max_active
        if name in visiting:
            raise WoonError(f"profile cycle at {name!r}")
        if name in selected_profiles:
            return
        item = profiles.setdefault(name, load_yaml(repository_path / "profiles" / f"{name}.yaml"))
        item_max = item.get("max_active")
        installable = item.get("installable", True)
        if (
            item.get("version") != 1
            or item.get("name") != name
            or not isinstance(item_max, int)
            or item_max <= 0
            or not isinstance(installable, bool)
        ):
            raise WoonError(f"invalid profile {name!r}")
        visiting.add(name)
        for parent in _strings(item.get("extends", [])):
            visit(parent)
        visiting.remove(name)
        selected_profiles.add(name)
        if not installable:
            non_installable_profiles.add(name)
        max_active = min(max_active, item_max)
        selected_skills.update(_strings(item.get("skills", [])))

    for name in requested:
        visit(name)
    return (
        sorted(selected_profiles),
        sorted(selected_skills),
        max_active if max_active != sys_max else 0,
        sorted(non_installable_profiles),
    )


def _validate_conflicts(repository_path: Path, active: list[str]) -> None:
    conflicts = load_yaml(repository_path / "conflicts/conflicts.yaml")
    if conflicts.get("version") != 1:
        raise WoonError(f"unsupported conflicts schema version {conflicts.get('version')}")
    active_set = set(active)
    for raw_group in _list(conflicts.get("groups")):
        group = _mapping(raw_group)
        matched = [member for member in _strings(group.get("members")) if member in active_set]
        if len(matched) < 2:
            continue
        mode = group.get("mode")
        if mode == "exclusive":
            raise WoonError(f"profile conflict {group.get('id')!r}: {', '.join(matched)}")
        if mode == "explicit-policy":
            raise WoonError(
                f"profile conflict {group.get('id')!r} requires option "
                f"{group.get('required_option')!r}"
            )
        raise WoonError(f"unknown conflict mode {mode!r}")


def _validate_sources(repository_path: Path) -> None:
    sources = load_yaml(repository_path / "lock/sources.yaml")
    if sources.get("version") != 1:
        raise WoonError(f"unsupported sources schema version {sources.get('version')}")
    for name, raw_origin in _mapping(sources.get("origins")).items():
        origin = _mapping(raw_origin)
        raw_path = str(origin.get("path", ""))
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts:
            raise WoonError(f"origin {name!r} has unsafe path")
        if not (repository_path / Path(*path.parts)).exists():
            raise WoonError(f"origin {name!r} path does not exist")
        if origin.get("upstream") and (
            len(str(origin.get("commit", ""))) != 40 or origin.get("update_policy") != "review-pr"
        ):
            raise WoonError(f"vendor origin {name!r} requires a commit lock and review-pr policy")


def _validate_catalog(repository_path: Path) -> None:
    sources = load_yaml(repository_path / "lock/sources.yaml")
    references: set[str] = set()
    names: dict[str, str] = {}
    for _origin_name, raw_origin in sorted(_mapping(sources.get("origins")).items()):
        origin_path = str(_mapping(raw_origin)["path"])
        directory = repository_path / origin_path
        for skill_file in sorted(directory.rglob("SKILL.md")):
            entry = skill_file.parent
            relative = entry.relative_to(directory).as_posix()
            reference = f"{origin_path}/{relative}"
            metadata = _read_frontmatter(skill_file)
            name = str(metadata["name"])
            if name != entry.name:
                raise WoonError(f"{reference} frontmatter name {name!r} does not match directory")
            if previous := names.get(name):
                raise WoonError(
                    f"duplicate catalog skill name {name!r}: {previous} and {reference}"
                )
            names[name] = reference
            references.add(reference)

    conflicts = load_yaml(repository_path / "conflicts/conflicts.yaml")
    seen_groups: set[str] = set()
    for raw_group in _list(conflicts.get("groups")):
        group = _mapping(raw_group)
        identifier = str(group.get("id", ""))
        if not identifier or identifier in seen_groups:
            raise WoonError(f"conflict group IDs must be non-empty and unique: {identifier!r}")
        seen_groups.add(identifier)
        members = _strings(group.get("members"))
        if len(members) < 2:
            raise WoonError(f"conflict group {identifier!r} requires at least two members")
        for reference in members:
            if reference not in references:
                raise WoonError(
                    f"conflict group {identifier!r} references missing skill {reference!r}"
                )
        if group.get("preferred") and group["preferred"] not in members:
            raise WoonError(f"conflict group {identifier!r} preferred skill is not a member")
    effects = load_yaml(repository_path / "conflicts/effects.yaml")
    for reference in _mapping(effects.get("skills")):
        if reference not in references:
            raise WoonError(f"effects reference missing skill {reference!r}")


def _validate_profile_evals(repository_path: Path) -> None:
    evaluations = load_yaml(repository_path / "evals/profile-resolution.yaml")
    cases = _list(evaluations.get("cases"))
    if evaluations.get("version") != 1 or not cases:
        raise WoonError("profile evals require version 1 and at least one case")
    seen: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case)
        identifier = str(case.get("id", ""))
        if not identifier or identifier in seen:
            raise WoonError(f"profile eval case IDs must be non-empty and unique: {identifier!r}")
        seen.add(identifier)
        profiles = _strings(case.get("profiles"))
        _, references, _, _ = _resolve_profiles(repository_path, profiles)
        selected = set(references)
        for expected in _strings(case.get("expect_skills")):
            if expected not in selected:
                raise WoonError(f"profile eval {identifier!r} expected missing skill {expected!r}")
        for rejected in _strings(case.get("reject_skills", [])):
            if rejected in selected:
                raise WoonError(f"profile eval {identifier!r} selected rejected skill {rejected!r}")


def _load_routing_evals(repository_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    directory = repository_path / "evals/routing"
    config = load_yaml(directory / "config.yaml")
    if config.get("version") != 1:
        raise WoonError("routing config requires version 1")
    _positive_int(config.get("repeat"), "routing repeat")
    thresholds = _mapping(config.get("thresholds"))
    _ratio(thresholds.get("primary_recall"), "primary_recall")
    _ratio(thresholds.get("agreement"), "agreement")
    _non_negative_int(thresholds.get("forbidden_selections"), "forbidden_selections")

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "config.yaml":
            continue
        document = load_yaml(path)
        raw_cases = _list(document.get("cases"))
        if document.get("version") != 1 or not raw_cases:
            raise WoonError(f"routing eval file {path.name!r} requires version 1 and cases")
        for raw_case in raw_cases:
            case = _mapping(raw_case)
            identifier = str(case.get("id", ""))
            prompt = str(case.get("prompt", "")).strip()
            profiles = _strings(case.get("profiles"))
            primary = str(case.get("expect_primary", ""))
            if not identifier or identifier in seen or not prompt or not profiles or not primary:
                raise WoonError(f"invalid or duplicate routing case {identifier!r}")
            seen.add(identifier)
            _, references, _, _ = _resolve_profiles(repository_path, profiles)
            available: set[str] = set()
            for reference in references:
                metadata = _read_frontmatter(
                    _safe_skill_path(repository_path, reference) / "SKILL.md"
                )
                available.add(str(metadata["name"]))
            support = _strings(case.get("allow_support", []))
            rejected = _strings(case.get("reject", []))
            declared = {primary, *support, *rejected}
            missing = declared.difference(available)
            if missing:
                raise WoonError(
                    f"routing case {identifier!r} references unavailable skill {min(missing)!r}"
                )
            if set(rejected).intersection({primary, *support}):
                raise WoonError(f"routing case {identifier!r} both allows and rejects a skill")
            max_selected = _positive_int(case.get("max_selected"), f"{identifier} max_selected")
            if max_selected > len({primary, *support}):
                raise WoonError(f"routing case {identifier!r} max_selected exceeds allowed skills")
            cases.append(case)
    if not cases:
        raise WoonError("routing evals require at least one case file")
    return config, cases


def _read_frontmatter(path: Path) -> dict[str, Any]:
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise WoonError("missing YAML frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise WoonError("unterminated YAML frontmatter")
    metadata = yaml.safe_load(normalized[4:end])
    if (
        not isinstance(metadata, dict)
        or not metadata.get("name")
        or not str(metadata.get("description", "")).strip()
    ):
        raise WoonError("frontmatter requires name and description")
    return metadata


def _safe_skill_path(repository_path: Path, reference: str) -> Path:
    relative = PurePosixPath(reference)
    if (
        relative.is_absolute()
        or relative in {PurePosixPath("."), PurePosixPath("..")}
        or ".." in relative.parts
    ):
        raise WoonError(f"unsafe skill reference {reference!r}")
    path = repository_path / Path(*relative.parts)
    if not (path / "SKILL.md").exists():
        raise WoonError(f"skill {reference!r} does not contain SKILL.md")
    return path


def _hash_directory(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _effects(value: object, reference: str) -> list[str]:
    effects = _strings(value)
    if len(set(effects)) != len(effects):
        raise WoonError(f"{reference} declares duplicate side effect")
    unknown = set(effects).difference(ALLOWED_EFFECTS)
    if unknown:
        raise WoonError(f"{reference} declares unknown side effect {min(unknown)!r}")
    return effects


def _target_path(target: str) -> Path:
    if target == "codex":
        return _configured_skills_path(
            direct_variable="WOON_CODEX_SKILLS_HOME",
            executor_home_variable="CODEX_HOME",
            default_home=Path.home() / ".codex",
        )
    if target == "claude":
        return _configured_skills_path(
            direct_variable="WOON_CLAUDE_SKILLS_HOME",
            executor_home_variable="CLAUDE_CONFIG_DIR",
            default_home=Path.home() / ".claude",
        )
    raise WoonError(f"unknown skills target {target!r}")


def _configured_skills_path(
    *, direct_variable: str, executor_home_variable: str, default_home: Path
) -> Path:
    direct = os.environ.get(direct_variable)
    if direct:
        return Path(direct).expanduser().resolve()
    executor_home = os.environ.get(executor_home_variable)
    home = Path(executor_home).expanduser() if executor_home else default_home
    return (home / "skills").resolve()


def _read_install_manifest(target: Path) -> dict[str, Any]:
    path = target / INSTALL_MANIFEST_NAME
    if not path.exists():
        return {"version": 1, "skills": {}}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"invalid install manifest in {target}: {error}") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != 1
        or not isinstance(manifest.get("skills"), dict)
    ):
        raise WoonError(f"invalid install manifest in {target}")
    return manifest


def _installed_hash(path: Path) -> tuple[str, bool]:
    if not path.exists() and not path.is_symlink():
        return "", False
    if path.is_symlink() or not path.is_dir():
        raise WoonError(f"installed skill path is not a directory: {path}")
    return _hash_directory(path), True


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError("expected mapping")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise WoonError("expected list")
    return value


def _strings(value: object) -> list[str]:
    items = _list(value)
    if any(not isinstance(item, str) for item in items):
        raise WoonError("expected list of strings")
    return items


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WoonError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WoonError(f"{field} must be a non-negative integer")
    return value


def _ratio(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WoonError(f"{field} must be a number from 0 to 1")
    ratio = float(value)
    if not 0 <= ratio <= 1:
        raise WoonError(f"{field} must be a number from 0 to 1")
    return ratio
