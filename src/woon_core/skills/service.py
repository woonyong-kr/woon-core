"""Resolve, validate, and install profile-selected skills."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
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


@dataclass(frozen=True, slots=True)
class _Resolved:
    repository_path: Path
    profiles: tuple[str, ...]
    skills: tuple[CatalogSkill, ...]


def validate(root: Path, registry: Registry, profiles: list[str]) -> PlanResult:
    resolved = _load_resolved(root, registry, profiles)
    _validate_sources(resolved.repository_path)
    _validate_catalog(resolved.repository_path)
    _validate_routing_evals(resolved.repository_path)
    return PlanResult(
        profiles=resolved.profiles,
        items=tuple(
            PlanItem(skill.name, skill.reference, skill.hash, skill.effects, "selected")
            for skill in resolved.skills
        ),
        target=None,
    )


def plan(root: Path, registry: Registry, profiles: list[str], target_name: str) -> PlanResult:
    resolved = _load_resolved(root, registry, profiles)
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


def _load_resolved(root: Path, registry: Registry, requested: list[str]) -> _Resolved:
    repository_path = registry.resolve(root, "skills")
    profile_names, references, max_active = _resolve_profiles(
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
    return _Resolved(repository_path, tuple(profile_names), tuple(catalog))


def _resolve_profiles(
    repository_path: Path, requested: list[str]
) -> tuple[list[str], list[str], int]:
    profiles: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()
    selected_profiles: set[str] = set()
    selected_skills: set[str] = set()
    max_active = sys_max = 2**63 - 1

    def visit(name: str) -> None:
        nonlocal max_active
        if name in visiting:
            raise WoonError(f"profile cycle at {name!r}")
        if name in selected_profiles:
            return
        item = profiles.setdefault(name, load_yaml(repository_path / "profiles" / f"{name}.yaml"))
        item_max = item.get("max_active")
        if (
            item.get("version") != 1
            or item.get("name") != name
            or not isinstance(item_max, int)
            or item_max <= 0
        ):
            raise WoonError(f"invalid profile {name!r}")
        visiting.add(name)
        for parent in _strings(item.get("extends", [])):
            visit(parent)
        visiting.remove(name)
        selected_profiles.add(name)
        max_active = min(max_active, item_max)
        selected_skills.update(_strings(item.get("skills", [])))

    for name in requested:
        visit(name)
    return (
        sorted(selected_profiles),
        sorted(selected_skills),
        max_active if max_active != sys_max else 0,
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
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir() or not (entry / "SKILL.md").exists():
                continue
            reference = f"{origin_path}/{entry.name}"
            metadata = _read_frontmatter(entry / "SKILL.md")
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


def _validate_routing_evals(repository_path: Path) -> None:
    evaluations = load_yaml(repository_path / "evals/routing.yaml")
    cases = _list(evaluations.get("cases"))
    if evaluations.get("version") != 1 or not cases:
        raise WoonError("routing evals require version 1 and at least one case")
    seen: set[str] = set()
    for raw_case in cases:
        case = _mapping(raw_case)
        identifier = str(case.get("id", ""))
        if not identifier or identifier in seen:
            raise WoonError(f"routing eval case IDs must be non-empty and unique: {identifier!r}")
        seen.add(identifier)
        profiles = _strings(case.get("profiles"))
        _, references, _ = _resolve_profiles(repository_path, profiles)
        selected = set(references)
        for expected in _strings(case.get("expect_skills")):
            if expected not in selected:
                raise WoonError(f"routing eval {identifier!r} expected missing skill {expected!r}")
        for rejected in _strings(case.get("reject_skills", [])):
            if rejected in selected:
                raise WoonError(f"routing eval {identifier!r} selected rejected skill {rejected!r}")


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
        return (
            Path(os.environ.get("WOON_CODEX_SKILLS_HOME", Path.home() / ".codex/skills"))
            .expanduser()
            .resolve()
        )
    if target == "claude":
        return (
            Path(os.environ.get("WOON_CLAUDE_SKILLS_HOME", Path.home() / ".claude/skills"))
            .expanduser()
            .resolve()
        )
    raise WoonError(f"unknown skills target {target!r}")


def _read_install_manifest(target: Path) -> dict[str, Any]:
    path = target / INSTALL_MANIFEST_NAME
    if not path.exists():
        return {"version": 1, "skills": {}}
    try:
        manifest = json.loads(path.read_text())
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
