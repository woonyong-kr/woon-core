#!/usr/bin/env python3
from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

ROOT = Path.cwd().resolve()
MAX_PARTS = 3

CHECK_ROOTS = {
    "README.md",
    "maps",
    "wiki",
    "inbox",
    "types",
}

SKIP_PARTS = {
    ".git",
    ".obsidian",
    "assets",
    "node_modules",
    "quartz",
    "scripts",
    "templates",
    "_sources",
}

PREFIX_MAX_PARTS = {
    # These are generated or purpose-owned collections rather than ad-hoc nesting.
    ("inbox", "calendar", "events"): 4,
    ("inbox", "tasks", "routines"): 4,
    # A goal is the explicit stop condition for a routine, so it is a
    # canonical task source rather than accidental nesting.
    ("inbox", "tasks", "goals"): 4,
    # The single Wiki keeps personal material under semantic collections.  The
    # extra level is deliberate ownership, not ad-hoc nesting.
    ("wiki", "personal"): 4,
    ("wiki", "personal", "projects"): 4,
    ("wiki", "personal", "interview"): 5,
    # Career keeps one human-readable hub and one record per application.  The
    # extra applications level is a declared collection, not a second canon.
    ("wiki", "personal", "career"): 4,
    ("wiki", "personal", "career", "applications"): 5,
}


@cache
def configured_protected_prefixes() -> frozenset[tuple[str, ...]]:
    """Load repository-owned depth exclusions instead of hard-coding private domains."""

    configuration = ROOT / ".woon/repository.yaml"
    if not configuration.is_file():
        return frozenset()
    payload = yaml.safe_load(configuration.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(".woon/repository.yaml must contain a mapping")
    values = payload.get("folder_depth_audit_ignored_roots", [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("folder_depth_audit_ignored_roots must be a string list")
    prefixes: set[tuple[str, ...]] = set()
    for value in values:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("folder depth ignored roots must be safe repository-relative paths")
        prefixes.add(path.parts)
    return frozenset(prefixes)


def is_under(path: Path, root: str) -> bool:
    if root.endswith(".md"):
        return path.as_posix() == root
    return path.as_posix() == root or path.as_posix().startswith(f"{root}/")


def in_scope(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_PARTS for part in rel.parts):
        return False
    if any(rel.parts[: len(prefix)] == prefix for prefix in configured_protected_prefixes()):
        return False
    return any(is_under(rel, root) for root in CHECK_ROOTS)


def maximum_parts_for(relative: Path) -> int:
    """Return the most specific declared depth contract for one path."""

    return next(
        (
            value
            for prefix, value in sorted(
                PREFIX_MAX_PARTS.items(), key=lambda item: len(item[0]), reverse=True
            )
            if relative.parts[: len(prefix)] == prefix
        ),
        MAX_PARTS,
    )


def main() -> int:
    violations: list[tuple[Path, int]] = []
    for path in sorted(ROOT.rglob("*.md")):
        if not in_scope(path):
            continue
        rel = path.relative_to(ROOT)
        # A more specific purpose-owned prefix must win over its parent.
        maximum = maximum_parts_for(rel)
        if len(rel.parts) > maximum:
            violations.append((rel, maximum))

    if violations:
        print(f"folder_depth_violations={len(violations)}")
        for rel, maximum in violations[:120]:
            print(f"depth>{maximum}: {rel.as_posix()}")
        if len(violations) > 120:
            print(f"depth_violation_more={len(violations) - 120}")
        return 1

    print(f"folder_depth_ok=max_parts:{MAX_PARTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
