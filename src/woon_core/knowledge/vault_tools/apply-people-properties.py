#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path.cwd().resolve()

EXCLUDE_DIRS = {
    ".git",
    ".obsidian",
    "assets",
    "node_modules",
    "quartz",
}

PEOPLE = [
    {
        "link": '[[users/choi-woonyoung/README|최우녕]]',
        "self_file": "users/choi-woonyoung/README.md",
        "aliases": ["최우녕"],
    },
    {
        "link": '[[users/kim-heejun/README|김희준]]',
        "self_file": "users/kim-heejun/README.md",
        "aliases": ["김희준", "희준"],
    },
    {
        "link": '[[users/lee-minjeong/README|이민정]]',
        "self_file": "users/lee-minjeong/README.md",
        "aliases": ["이민정", "민정", "minjeong"],
        "writing_aliases": ["그녀", "그 사람"],
    },
]


def markdown_files() -> list[Path]:
    paths: list[Path] = []
    for path in ROOT.rglob("*.md"):
        rel_parts = path.relative_to(ROOT).parts
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        paths.append(path)
    return sorted(paths)


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    return text[: end + 4], text[end + 5 :]


def frontmatter_value(frontmatter: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", frontmatter, re.M)
    if not match:
        return None
    value = match.group(1).strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


def has_alias(text: str, alias: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9_-]+", alias):
        return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(alias)}(?![A-Za-z0-9_-])", text, re.I) is not None
    return alias in text


def people_for(path: Path, text: str, frontmatter: str) -> list[str]:
    rel = path.relative_to(ROOT).as_posix()
    if frontmatter_value(frontmatter, "publish") == "true":
        return []

    found: list[str] = []
    for person in PEOPLE:
        if rel == person["self_file"]:
            found.append(person["link"])
            continue
        aliases = list(person["aliases"])
        if any(has_alias(text, alias) for alias in aliases):
            found.append(person["link"])

    if rel == "maps/people-index.md":
        found = [person["link"] for person in PEOPLE]

    return found


def remove_people_field(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        if re.match(r"^people:\s*(?:.*)?$", lines[i]):
            i += 1
            while i < len(lines) and not re.match(r"^[A-Za-z0-9_-]+:\s*", lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def people_block(people: list[str]) -> list[str]:
    if not people:
        return ["people: []"]
    return ["people:"] + [f'  - "{link}"' for link in people]


def insert_people(lines: list[str], people: list[str]) -> list[str]:
    body = remove_people_field(lines[1:-1])
    insert_at = len(body)
    for index, line in enumerate(body):
        if line.startswith("related_to:"):
            insert_at = index
            break
    block = people_block(people)
    return ["---"] + body[:insert_at] + block + body[insert_at:] + ["---"]


def main() -> None:
    changed = 0
    skipped = 0
    linked = 0
    for path in markdown_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        split = split_frontmatter(text)
        if split is None:
            skipped += 1
            continue
        frontmatter, body = split
        people = people_for(path, text, frontmatter)
        linked += int(bool(people))
        lines = frontmatter.splitlines()
        updated_frontmatter = "\n".join(insert_people(lines, people)) + "\n"
        updated = updated_frontmatter + body
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1

    print(f"people_changed={changed}")
    print(f"people_linked_docs={linked}")
    print(f"people_skipped_no_frontmatter={skipped}")


if __name__ == "__main__":
    main()
