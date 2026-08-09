"""Deterministic hard gates for one reconciled Markdown candidate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n", re.DOTALL)
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```", re.MULTILINE)
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
ABSOLUTE_LOCAL = re.compile(r"(?:^|[\s`'(\[])((?:/Users|/home)/[^\s`)'\]]+)")
PROTECTED_FRONTMATTER = (
    "type",
    "canonical_id",
    "title",
    "status",
    "publish",
    "access",
)


def validate_markdown_candidate(
    root: Path,
    relative_path: str,
    target: str | None,
    candidate: str,
) -> list[str]:
    """Return hard-gate violations without changing source or target files."""

    errors: list[str] = []
    candidate_frontmatter, candidate_body = _parse(candidate, "candidate", errors)
    target_frontmatter: dict[str, Any] = {}
    target_body = ""
    if target is not None:
        target_frontmatter, target_body = _parse(target, "target", errors)
        for field in PROTECTED_FRONTMATTER:
            if target_frontmatter.get(field) != candidate_frontmatter.get(field):
                errors.append(f"protected frontmatter field changed: {field}")

    headings = H1.findall(candidate_body)
    if len(headings) != 1:
        errors.append(f"candidate must contain exactly one H1, found {len(headings)}")
    title = candidate_frontmatter.get("title")
    if isinstance(title, str) and headings and headings[0].strip() != title.strip():
        errors.append("candidate H1 does not match frontmatter title")
    if len(FENCE.findall(candidate_body)) % 2:
        errors.append("candidate contains an unclosed fenced code block")
    if ABSOLUTE_LOCAL.search(candidate):
        errors.append("candidate exposes an absolute local path")

    old_links = _links(target_body) if target is not None else set()
    for link in sorted(_links(candidate_body).difference(old_links)):
        if not _wikilink_exists(root, relative_path, link):
            errors.append(f"new wikilink does not resolve: {link}")
    return errors


def unresolved_wikilinks(root: Path, relative_path: str, text: str) -> list[str]:
    """Return wikilinks that cannot resolve against an Obsidian Markdown tree."""

    return sorted(link for link in _links(text) if not _wikilink_exists(root, relative_path, link))


def _parse(
    text: str,
    label: str,
    errors: list[str],
) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER.match(text)
    if match is None:
        errors.append(f"{label} is missing YAML frontmatter")
        return {}, text
    try:
        loaded = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as error:
        errors.append(f"{label} has invalid YAML frontmatter: {error}")
        return {}, text[match.end() :]
    if not isinstance(loaded, dict):
        errors.append(f"{label} frontmatter must be a mapping")
        return {}, text[match.end() :]
    return loaded, text[match.end() :]


def _links(text: str) -> set[str]:
    links: set[str] = set()
    for raw in WIKILINK.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            links.add(target)
    return links


def _wikilink_exists(root: Path, current_path: str, link: str) -> bool:
    normalized = link.removesuffix(".md")
    if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
        return False
    direct = root / f"{normalized}.md"
    relative = root / Path(current_path).parent / f"{normalized}.md"
    if direct.is_file() or relative.is_file():
        return True
    basename = Path(normalized).name
    return any(path.stem == basename for path in root.rglob("*.md"))
