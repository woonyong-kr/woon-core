"""Read-only Markdown corpus adapter for Wiki and reference documents."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.knowledge.domain import IndexedDocument

HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
GENERATED_BLOCK = re.compile(
    r"<!-- (?:breadcrumb|recent-docs):start -->.*?<!-- (?:breadcrumb|recent-docs):end -->",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class CorpusRoot:
    """One configured Markdown tree and the label exposed in search results."""

    path: Path
    source_type: str


class MarkdownKnowledgeCorpus:
    """Scan Markdown roots without treating them as writable canonical files."""

    def __init__(
        self,
        vault: Path,
        roots: tuple[CorpusRoot, ...],
        exclusions: tuple[str, ...],
    ) -> None:
        self._vault = vault
        self._roots = roots
        self._exclusions = exclusions

    def list_documents(self) -> list[IndexedDocument]:
        documents: list[IndexedDocument] = []
        for path, relative, source_type in self._paths():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            documents.append(_parse(relative, text, source_type))
        return documents

    def state_token(self) -> tuple[tuple[str, int, int, int, int], ...]:
        """Return metadata-only file state for a bounded freshness fast path."""

        states: list[tuple[str, int, int, int, int]] = []
        for path, relative, _ in self._paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            states.append((relative, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
        return tuple(states)

    def _paths(self) -> list[tuple[Path, str, str]]:
        paths: list[tuple[Path, str, str]] = []
        seen: set[str] = set()
        for root in self._roots:
            if not root.path.is_dir():
                continue
            for path in sorted(root.path.rglob("*.md")):
                relative = path.relative_to(self._vault).as_posix()
                if relative in seen or self._excluded(relative):
                    continue
                seen.add(relative)
                paths.append((path, relative, root.source_type))
        return paths

    def _excluded(self, relative_path: str) -> bool:
        return any(
            fnmatch.fnmatchcase(relative_path, pattern)
            or fnmatch.fnmatchcase(f"/{relative_path}", pattern)
            for pattern in self._exclusions
        )


def _parse(relative_path: str, text: str, source_type: str) -> IndexedDocument:
    raw: dict[str, Any] = {}
    body = text
    match = FRONTMATTER.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, dict):
            raw = loaded
        body = text[match.end() :]
    body = GENERATED_BLOCK.sub("", body).strip()
    title = _string(raw.get("title")) or _heading(body) or Path(relative_path).stem
    summary = _string(raw.get("summary")) or _summary(body, title)
    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return IndexedDocument(
        document_id=relative_path,
        canonical_id=None,
        title=title,
        summary=summary,
        body=body.strip() + "\n",
        relative_path=relative_path,
        revision=revision,
        source_type=source_type,
    )


def _string(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _heading(body: str) -> str:
    match = HEADING.search(body)
    return match.group(1).strip() if match else ""


def _summary(body: str, title: str) -> str:
    paragraphs = re.split(r"\n\s*\n", body)
    for paragraph in paragraphs:
        if "상위 링크:" in paragraph:
            continue
        normalized = " ".join(
            line.strip()
            for line in paragraph.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "<!--", "```"))
        )
        if normalized and normalized != title:
            return normalized[:240]
    return title
