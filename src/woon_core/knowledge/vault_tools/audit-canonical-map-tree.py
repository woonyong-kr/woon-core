#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path


VAULT = Path.cwd().resolve()
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
RECENT_DOCS_RE = re.compile(
    r"<!-- recent-docs:start -->.*?<!-- recent-docs:end -->",
    re.S,
)
USER_ROOTS = ["README.md", "maps", "wiki"]
SKIP_DIRS = {
    ".git",
    ".obsidian",
    ".drawio-backup",
    "quartz",
    "scripts",
    "templates",
    "types",
    "assets",
    "sources",
    "inbox",
}

TOP_NAV = {
    "README.md",
}
CANONICAL_SUBJECTS = {
    "maps/os-moc.md",
    "maps/pintos-moc.md",
    "maps/ai-neural-network-moc.md",
    "maps/ai-llm-moc.md",
}
AUXILIARY_ROLES = {"global-index", "code-index", "visual-index", "local-index"}
AUXILIARY_PATH_PREFIXES = ("maps/book-",)
AUXILIARY_PATHS = {
    "maps/books-index.md",
    "maps/resource-index.md",
    "maps/people-index.md",
    "maps/portfolio-project-moc.md",
}


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def iter_markdown() -> list[Path]:
    files: list[Path] = []
    for root in USER_ROOTS:
        path = VAULT / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                if set(item.relative_to(VAULT).parts) & SKIP_DIRS:
                    continue
                files.append(item)
    return sorted(set(files))


def clean_value(value: str):
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}

    data: dict[str, object] = {}
    current_key: str | None = None
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("- ", "  - ")) and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(clean_value(line.lstrip()[2:]))
            continue
        match = re.match(r"^([A-Za-z0-9가-힣_-]+):\s*(.*)$", line)
        if not match:
            continue
        current_key = match.group(1)
        value = match.group(2).strip()
        data[current_key] = [] if value == "" else clean_value(value)
    return data


def strip_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def strip_managed_blocks(text: str) -> str:
    return RECENT_DOCS_RE.sub("", text)


def link_keys(path: Path) -> set[str]:
    r = rel(path)
    no_ext = r[:-3] if r.endswith(".md") else r
    keys = {no_ext, path.stem}
    if path.name in {"README.md", "index.md"}:
        if path.parent == VAULT:
            keys.update({"README", "index"})
        else:
            keys.add(path.parent.name)
            keys.add(no_ext.removesuffix("/README").removesuffix("/index"))
    return {key for key in keys if key}


def build_index(files: list[Path]) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in files:
        for key in link_keys(path):
            index.setdefault(key, []).append(path)
    return index


def resolve(target: str, index: dict[str, list[Path]]) -> list[Path]:
    target = target.strip().split("#", 1)[0]
    if target.endswith(".md"):
        target = target[:-3]
    if target.startswith("./"):
        target = target[2:]
    return index.get(target, [])


def is_wiki_concept(path: Path) -> bool:
    r = rel(path)
    return r.startswith("wiki/") and path.name not in {"README.md", "index.md"}


def is_auxiliary_map(path: Path, role: str | None) -> bool:
    r = rel(path)
    return (
        role in AUXILIARY_ROLES
        or r in AUXILIARY_PATHS
        or any(r.startswith(prefix) for prefix in AUXILIARY_PATH_PREFIXES)
    )


def main() -> int:
    files = iter_markdown()
    index = build_index(files)
    map_files = sorted((VAULT / "maps").glob("*.md"))
    root = VAULT / "README.md"
    if root.exists():
        map_files = [root] + map_files

    texts = {path: path.read_text(encoding="utf-8") for path in map_files}
    metadata = {path: parse_frontmatter(texts[path]) for path in map_files}
    outgoing: dict[Path, set[Path]] = {path: set() for path in map_files}
    wiki_concept_links: dict[Path, set[Path]] = {path: set() for path in map_files}

    for path in map_files:
        visible_text = strip_managed_blocks(strip_fences(texts[path]))
        for target in WIKILINK_RE.findall(visible_text):
            for match in resolve(target, index):
                outgoing[path].add(match)
                if is_wiki_concept(match):
                    wiki_concept_links[path].add(match)

    issues: dict[str, list[str]] = {
        "top_nav_direct_wiki_links": [],
        "canonical_subject_too_many_direct_wiki_links": [],
        "canonical_map_high_overlap": [],
    }

    for path in map_files:
        r = rel(path)
        direct_count = len(wiki_concept_links[path])
        if r in TOP_NAV and direct_count:
            sample = ", ".join(rel(item) for item in sorted(wiki_concept_links[path])[:8])
            issues["top_nav_direct_wiki_links"].append(
                f"{r}: {direct_count} direct wiki links ({sample})"
            )
        if r in CANONICAL_SUBJECTS and direct_count > 4:
            sample = ", ".join(rel(item) for item in sorted(wiki_concept_links[path])[:8])
            issues["canonical_subject_too_many_direct_wiki_links"].append(
                f"{r}: {direct_count} direct wiki links ({sample})"
            )

    canonical = [
        path
        for path in map_files
        if rel(path).startswith("maps/")
        and not is_auxiliary_map(path, metadata[path].get("map_role"))
    ]
    for idx, left in enumerate(canonical):
        for right in canonical[idx + 1 :]:
            shared = outgoing[left] & outgoing[right]
            union = outgoing[left] | outgoing[right]
            if not union:
                continue
            jaccard = len(shared) / len(union)
            if len(shared) >= 16 and jaccard >= 0.40:
                sample = ", ".join(rel(item) for item in sorted(shared)[:8])
                issues["canonical_map_high_overlap"].append(
                    f"{rel(left)} <-> {rel(right)}: shared={len(shared)}, jaccard={jaccard:.2f} ({sample})"
                )

    issues = {key: value for key, value in issues.items() if value}
    report = {
        "maps_scanned": len(map_files),
        "top_nav": sorted(TOP_NAV),
        "canonical_subjects": sorted(CANONICAL_SUBJECTS),
        "issue_counts": {key: len(value) for key, value in issues.items()},
        "issues": issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
