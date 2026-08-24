#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd().resolve()
MAX_PARTS = 3

CHECK_ROOTS = {
    "README.md",
    "maps",
    "wiki",
    "sources",
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
}

PROTECTED_PREFIXES = {
    ("sources", "imports"),
}

PREFIX_MAX_PARTS = {
    # These are generated or purpose-owned collections rather than ad-hoc nesting.
    ("inbox", "calendar", "events"): 4,
    ("inbox", "tasks", "routines"): 4,
    # A goal is the explicit stop condition for a routine, so it is a
    # canonical task source rather than accidental nesting.
    ("inbox", "tasks", "goals"): 4,
    ("maps", "context-graph"): 4,
    # The single Wiki keeps personal material under semantic collections.  The
    # extra level is deliberate ownership, not ad-hoc nesting.
    ("wiki", "personal", "projects"): 4,
    ("wiki", "personal", "interview"): 5,
    # Private raw originals stay below their domain folder so they are never
    # confused with searchable or publishable Wiki material.
    ("sources", "private", "writing"): 4,
}


def is_under(path: Path, root: str) -> bool:
    if root.endswith(".md"):
        return path.as_posix() == root
    return path.as_posix() == root or path.as_posix().startswith(f"{root}/")


def in_scope(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_PARTS for part in rel.parts):
        return False
    if any(rel.parts[: len(prefix)] == prefix for prefix in PROTECTED_PREFIXES):
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
