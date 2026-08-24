#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path

VAULT = Path.cwd().resolve()
ROOT = VAULT / "README.md"
LOCAL_ROOT = VAULT / "maps/local-private-index.md"
USER_FACING_ROOTS = [
    "README.md",
    "maps",
    "wiki",
]
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
SKIP_PREFIXES = {
    ("wiki", "canonical"),
}
WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
ALLOWED_MAP_ROLES = {
    "home",
    "global-index",
    "subject-index",
    "topic-index",
    "visual-index",
    "code-index",
    "local-index",
}


def rel(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def iter_user_facing_markdown() -> list[Path]:
    files: list[Path] = []
    for root in USER_FACING_ROOTS:
        path = VAULT / root
        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                relative = item.relative_to(VAULT)
                parts = set(relative.parts)
                if parts & SKIP_DIRS:
                    continue
                if any(relative.parts[: len(prefix)] == prefix for prefix in SKIP_PREFIXES):
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


def strip_fenced_blocks(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def link_keys(path: Path) -> set[str]:
    r = rel(path)
    no_ext = r[:-3] if r.endswith(".md") else r
    keys = {no_ext, path.stem}
    if path.name in {"README.md", "index.md"}:
        if path.parent == VAULT:
            keys.add("README")
            keys.add("index")
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


def first_wikilink(value) -> str | None:
    if isinstance(value, str):
        match = WIKILINK_RE.search(value)
        return match.group(1) if match else None
    if isinstance(value, list):
        for item in value:
            result = first_wikilink(item)
            if result:
                return result
    return None


def domain_tags(fm: dict[str, object]) -> list[str]:
    tags = fm.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        return []
    return sorted(t for t in tags if isinstance(t, str) and t.startswith("domain:"))


def main() -> int:
    files = iter_user_facing_markdown()
    texts = {path: path.read_text(encoding="utf-8") for path in files}
    metadata = {path: parse_frontmatter(texts[path]) for path in files}
    index = build_index(files)
    graph: dict[Path, set[Path]] = {path: set() for path in files}
    issues: dict[str, list[str]] = {
        "root_missing": [],
        "unreachable_from_root": [],
        "missing_breadcrumb": [],
        "map_missing_role": [],
        "map_unknown_role": [],
        "map_missing_domain": [],
        "wiki_missing_domain": [],
        "wiki_missing_parent_moc": [],
        "wiki_parent_moc_not_map": [],
        "public_links_to_non_public": [],
        "root_duplicate_wikilinks": [],
    }

    for path in files:
        link_text = strip_fenced_blocks(texts[path]).replace("\\|", "|")
        for target in WIKILINK_RE.findall(link_text):
            for match in resolve(target, index):
                graph[path].add(match)

    if ROOT in texts:
        root_link_counts: dict[Path, int] = {}
        root_text = strip_fenced_blocks(texts[ROOT]).replace("\\|", "|")
        for target in WIKILINK_RE.findall(root_text):
            for match in resolve(target, index):
                root_link_counts[match] = root_link_counts.get(match, 0) + 1
        issues["root_duplicate_wikilinks"] = [
            f"{rel(path)}: {count} occurrences"
            for path, count in sorted(root_link_counts.items(), key=lambda item: rel(item[0]))
            if count > 1
        ]

    def reachable_from(start: Path) -> set[Path]:
        reachable: set[Path] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(sorted(graph.get(current, set()) - reachable))
        return reachable

    if ROOT not in graph:
        issues["root_missing"].append("README.md")
    else:
        public_reachable = reachable_from(ROOT)
        local_reachable = reachable_from(LOCAL_ROOT) if LOCAL_ROOT in graph else set()

        for path in files:
            access = metadata[path].get("access")
            required_reachable = local_reachable if access == "local-only" else public_reachable
            if path not in required_reachable:
                issues["unreachable_from_root"].append(rel(path))

    for path in files:
        fm = metadata[path]
        r = rel(path)
        doc_type = fm.get("type")
        publish = fm.get("publish") is True
        access = fm.get("access")

        if "<!-- breadcrumb:start -->" not in texts[path]:
            issues["missing_breadcrumb"].append(r)

        if path.parent.name == "maps" or r == "README.md":
            role = fm.get("map_role")
            if not role:
                issues["map_missing_role"].append(r)
            elif role not in ALLOWED_MAP_ROLES:
                issues["map_unknown_role"].append(f"{r}: {role}")
            if publish and not domain_tags(fm):
                issues["map_missing_domain"].append(r)

        if publish and doc_type == "Wiki":
            if not domain_tags(fm):
                issues["wiki_missing_domain"].append(r)
            parent_target = first_wikilink(fm.get("parent_moc"))
            if not parent_target:
                issues["wiki_missing_parent_moc"].append(r)
            else:
                matches = resolve(parent_target, index)
                if not any(rel(match).startswith("maps/") for match in matches):
                    issues["wiki_parent_moc_not_map"].append(f"{r} -> {parent_target}")

        if access == "public":
            for target in graph[path]:
                target_access = metadata[target].get("access")
                if metadata[target].get("publish") is not True or target_access != "public":
                    issues["public_links_to_non_public"].append(f"{r} -> {rel(target)}")

    issues = {key: value for key, value in issues.items() if value}
    report = {
        "root": "README.md",
        "user_facing_files": len(files),
        "issue_counts": {key: len(value) for key, value in issues.items()},
        "issues": issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
