"""Deterministic hard gates for one reconciled Markdown candidate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n", re.DOTALL)
H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FENCE = re.compile(r"^```", re.MULTILINE)
FENCED_BLOCK = re.compile(r"^```.*?^```\s*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
REPOSITORY_PATH = re.compile(
    r"(?:^|[\s`'\"])(scripts/[^\s`'\"),]+)",
    re.MULTILINE,
)
ABSOLUTE_LOCAL = re.compile(r"(?:^|[\s`'(\[])((?:/Users|/home)/[^\s`)'\]]+)")
INACTIVE_PARTS = frozenset(
    {".git", ".local", ".legacy-backup", "_quarantine", "catalog", "exports"}
)
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
    if target is not None:
        target_frontmatter, _ = _parse(target, "target", errors)
        for field in PROTECTED_FRONTMATTER:
            if target_frontmatter.get(field) != candidate_frontmatter.get(field):
                errors.append(f"protected frontmatter field changed: {field}")

    structural_prose = FENCED_BLOCK.sub("", candidate_body)
    link_prose = INLINE_CODE.sub("", structural_prose)
    headings = H1.findall(structural_prose)
    if len(headings) != 1:
        errors.append(f"candidate must contain exactly one H1, found {len(headings)}")
    title = candidate_frontmatter.get("title")
    if isinstance(title, str) and headings and headings[0].strip() != title.strip():
        errors.append("candidate H1 does not match frontmatter title")
    if len(FENCE.findall(candidate_body)) % 2:
        errors.append("candidate contains an unclosed fenced code block")
    if contains_absolute_local(candidate):
        errors.append("candidate exposes an absolute local path")
    if (
        candidate_frontmatter.get("publish") is True
        and candidate_frontmatter.get("access") == "public"
        and "projects/writing" in candidate_body
    ):
        errors.append("public candidate exposes the private writing locator")

    for link in sorted(_links(link_prose)):
        if not _wikilink_exists(root, relative_path, link):
            errors.append(f"wikilink does not resolve: {link}")
    for reference in unresolved_local_references(root, relative_path, candidate_body):
        errors.append(f"local file reference does not resolve: {reference}")
    return errors


def contains_absolute_local(text: str) -> bool:
    """Return whether Markdown exposes a machine-specific home path."""

    return ABSOLUTE_LOCAL.search(text) is not None


def unresolved_wikilinks(root: Path, relative_path: str, text: str) -> list[str]:
    """Return wikilinks that cannot resolve against an Obsidian Markdown tree."""

    return sorted(
        link
        for link in _links(_link_prose(text))
        if not _wikilink_exists(root, relative_path, link)
    )


def unresolved_local_references(root: Path, relative_path: str, text: str) -> list[str]:
    """Return actionable repository paths and Markdown links that do not exist."""

    prose = FENCED_BLOCK.sub("", text)
    references = set(REPOSITORY_PATH.findall(prose))
    for raw in MARKDOWN_LINK.findall(INLINE_CODE.sub("", prose)):
        value = raw.strip().split(maxsplit=1)[0].strip("<>")
        if not value or value.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if "/" not in value and "." not in value:
            continue
        references.add(value.split("#", 1)[0])
    return sorted(
        reference.rstrip(".:;")
        for reference in references
        if not _local_reference_exists(root, relative_path, reference.rstrip(".:;"))
    )


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


def _link_prose(text: str) -> str:
    return INLINE_CODE.sub("", FENCED_BLOCK.sub("", text))


def _wikilink_exists(root: Path, current_path: str, link: str) -> bool:
    normalized = link.removesuffix(".md")
    if Path(normalized).is_absolute() or ".." in Path(normalized).parts:
        return False
    direct = root / f"{normalized}.md"
    relative = root / Path(current_path).parent / f"{normalized}.md"
    current = (root / current_path).resolve()
    if (direct.resolve() == current or relative.resolve() == current) and current.is_file():
        return True
    if _active_file(root, direct) or _active_file(root, relative):
        return True
    basename = Path(normalized).name
    return any(path.stem == basename and _active_file(root, path) for path in root.rglob("*.md"))


def _local_reference_exists(root: Path, current_path: str, reference: str) -> bool:
    if "<" in reference or ">" in reference or "*" in reference or reference.startswith("repo://"):
        return True
    raw = Path(reference)
    candidates = [root / raw, root / Path(current_path).parent / raw]
    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        if _active_file(root, resolved) or resolved.is_dir():
            return True
    return False


def _active_file(root: Path, path: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    if not path.is_file() or any(part in INACTIVE_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() != ".md":
        return True
    match = FRONTMATTER.match(path.read_text(encoding="utf-8", errors="replace"))
    if match is None:
        return True
    try:
        metadata = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError:
        return True
    status = metadata.get("status") if isinstance(metadata, dict) else None
    return not (
        isinstance(status, str) and status.casefold() in {"archived", "deprecated", "retired"}
    )
