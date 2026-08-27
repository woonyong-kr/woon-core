#!/usr/bin/env python3
"""Report repeated content and document-boundary review candidates.

The report is advisory. Length and similarity can locate candidates, but they do
not prove that a document must be split or merged.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

VAULT = Path.cwd().resolve()
CONFIG = VAULT / "config/canonical-knowledge.yaml"
FRONTMATTER = re.compile(r"\A---\n(?P<yaml>.*?)\n---\n", re.DOTALL)
FENCED_BLOCK = re.compile(r"^```.*?^```\s*$", re.MULTILINE | re.DOTALL)
GENERATED_BLOCK = re.compile(r"<!-- generated:.*?-->.*?<!-- /generated:.*?-->", re.DOTALL)
BREADCRUMB = re.compile(r"<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->", re.DOTALL)
WORD = re.compile(r"[가-힣A-Za-z0-9_]+")


@dataclass(frozen=True)
class Settings:
    """Tunable candidate thresholds loaded from the vault configuration."""

    roots: tuple[str, ...]
    repeated_paragraph_min_chars: int
    semantic_shingle_words: int
    semantic_jaccard_review: float
    semantic_containment_review: float
    common_shingle_document_limit: int
    long_plain_chars_review: int
    long_h2_review: int
    short_plain_chars_review: int
    short_h2_max: int


def _load_settings() -> Settings:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    values = raw.get("quality", {}).get("cohesion", {})
    if not isinstance(values, dict):
        raise ValueError("quality.cohesion must be a mapping")
    return Settings(
        roots=tuple(values["roots"]),
        repeated_paragraph_min_chars=int(values["repeated_paragraph_min_chars"]),
        semantic_shingle_words=int(values["semantic_shingle_words"]),
        semantic_jaccard_review=float(values["semantic_jaccard_review"]),
        semantic_containment_review=float(values["semantic_containment_review"]),
        common_shingle_document_limit=int(values["common_shingle_document_limit"]),
        long_plain_chars_review=int(values["long_plain_chars_review"]),
        long_h2_review=int(values["long_h2_review"]),
        short_plain_chars_review=int(values["short_plain_chars_review"]),
        short_h2_max=int(values["short_h2_max"]),
    )


def _relative(path: Path) -> str:
    return path.relative_to(VAULT).as_posix()


def _frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER.match(text)
    if match is None:
        return {}
    loaded = yaml.safe_load(match.group("yaml")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _visible_body(text: str) -> str:
    match = FRONTMATTER.match(text)
    body = text[match.end() :] if match else text
    body = BREADCRUMB.sub("", body)
    return GENERATED_BLOCK.sub("", body)


def _is_source_archive(path: Path) -> bool:
    return "_sources" in path.relative_to(VAULT).parts


def _documents(settings: Settings) -> dict[Path, str]:
    documents: dict[Path, str] = {}
    for root_name in settings.roots:
        root = VAULT / root_name
        for path in root.rglob("*.md"):
            if _is_source_archive(path):
                continue
            if path.name in {"README.md", "index.md"}:
                continue
            text = path.read_text(encoding="utf-8")
            metadata = _frontmatter(text)
            if metadata.get("publish") is not True:
                continue
            documents[path] = _visible_body(text)
    return dict(sorted(documents.items()))


def _words(text: str, *, keep_code: bool) -> list[str]:
    source = text if keep_code else FENCED_BLOCK.sub(" ", text)
    return WORD.findall(source.casefold())


def _shingles(words: list[str], size: int) -> set[tuple[str, ...]]:
    if len(words) < size:
        return set()
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def _paragraph_kind(paragraph: str) -> str:
    # A blank line inside a fence can leave this paragraph starting at a code
    # comment or statement rather than the opening fence. Any fence delimiter
    # still means the repeated unit is code/output, not teaching prose.
    if "```" in paragraph:
        return "code-or-output"
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if lines and all(line.startswith(("- [", "- `", "- [[")) for line in lines):
        return "source-or-link-list"
    if len(lines) >= 5 and all(line.startswith(("- ", "1. ", "2. ")) for line in lines):
        return "structure-list"
    return "prose"


def _paragraphs_with_kind(body: str) -> list[tuple[str, str]]:
    """Keep a fenced block intact before splitting ordinary prose paragraphs."""

    paragraphs: list[tuple[str, str]] = []
    parts = re.split(r"(^```.*?^```\s*$)", body, flags=re.MULTILINE | re.DOTALL)
    for index, part in enumerate(parts):
        if not part.strip():
            continue
        if index % 2:
            paragraphs.append(("code-or-output", part.strip()))
            continue
        for paragraph in re.split(r"\n\s*\n", part):
            if paragraph.strip():
                paragraphs.append((_paragraph_kind(paragraph), paragraph.strip()))
    return paragraphs


def _repeated_paragraphs(documents: dict[Path, str], settings: Settings) -> list[dict[str, Any]]:
    owners: dict[str, set[Path]] = defaultdict(set)
    samples: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for path, body in documents.items():
        for kind, paragraph in _paragraphs_with_kind(body):
            normalized = re.sub(r"\s+", " ", paragraph).strip().casefold()
            if len(normalized) < settings.repeated_paragraph_min_chars:
                continue
            owners[normalized].add(path)
            samples[normalized] = paragraph.strip()
            kinds[normalized] = kind
    groups = []
    for normalized, paths in owners.items():
        if len(paths) < 2:
            continue
        sample = samples[normalized]
        groups.append(
            {
                "kind": kinds[normalized],
                "paths": [_relative(path) for path in sorted(paths)],
                "preview": re.sub(r"\s+", " ", sample)[:240],
            }
        )
    return sorted(groups, key=lambda item: (-len(item["paths"]), item["paths"]))


def _similar_documents(documents: dict[Path, str], settings: Settings) -> list[dict[str, Any]]:
    shingle_sets = {
        path: _shingles(_words(body, keep_code=False), settings.semantic_shingle_words)
        for path, body in documents.items()
    }
    inverted: dict[tuple[str, ...], list[Path]] = defaultdict(list)
    for path, shingles in shingle_sets.items():
        for shingle in shingles:
            inverted[shingle].append(path)

    shared: Counter[tuple[Path, Path]] = Counter()
    for paths in inverted.values():
        if not 1 < len(paths) <= settings.common_shingle_document_limit:
            continue
        for left, right in itertools.combinations(sorted(paths), 2):
            shared[(left, right)] += 1

    candidates = []
    for (left, right), intersection in shared.items():
        left_set = shingle_sets[left]
        right_set = shingle_sets[right]
        if not left_set or not right_set:
            continue
        jaccard = intersection / (len(left_set) + len(right_set) - intersection)
        containment = intersection / min(len(left_set), len(right_set))
        if (
            jaccard < settings.semantic_jaccard_review
            and containment < settings.semantic_containment_review
        ):
            continue
        candidates.append(
            {
                "left": _relative(left),
                "right": _relative(right),
                "jaccard": round(jaccard, 3),
                "containment": round(containment, 3),
            }
        )
    return sorted(
        candidates,
        key=lambda item: (-max(item["jaccard"], item["containment"]), item["left"]),
    )


def _boundary_candidates(
    documents: dict[Path, str], settings: Settings
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    long_documents = []
    short_documents = []
    for path, body in documents.items():
        plain_chars = len("".join(_words(body, keep_code=True)))
        h2_count = sum(line.startswith("## ") for line in body.splitlines())
        item = {"path": _relative(path), "plain_chars": plain_chars, "h2": h2_count}
        if plain_chars > settings.long_plain_chars_review and h2_count >= settings.long_h2_review:
            long_documents.append(item)
        if plain_chars < settings.short_plain_chars_review and h2_count <= settings.short_h2_max:
            short_documents.append(item)
    return (
        sorted(long_documents, key=lambda item: -item["plain_chars"]),
        sorted(short_documents, key=lambda item: item["plain_chars"]),
    )


def main() -> int:
    """Print a deterministic advisory report without changing documents."""

    settings = _load_settings()
    documents = _documents(settings)
    repeated = _repeated_paragraphs(documents, settings)
    similar = _similar_documents(documents, settings)
    long_documents, short_documents = _boundary_candidates(documents, settings)
    repeated_by_kind = Counter(item["kind"] for item in repeated)
    report = {
        "decision": "review-only; never split or merge from a threshold alone",
        "documents_scanned": len(documents),
        "repeated_paragraph_groups": len(repeated),
        "repeated_paragraph_groups_by_kind": dict(sorted(repeated_by_kind.items())),
        "semantic_overlap_candidates": len(similar),
        "long_multi_section_candidates": len(long_documents),
        "short_fragment_candidates": len(short_documents),
        "candidates": {
            "repeated_paragraphs": repeated,
            "semantic_overlap": similar,
            "long_multi_section": long_documents,
            "short_fragments": short_documents,
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
