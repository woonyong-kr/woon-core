"""Bounded, read-only context bundles assembled from the Woon search index."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.service import KnowledgeService
from woon_core.knowledge.wiki_tree import (
    WikiTreeNode,
    load_wiki_tree,
    normalize_identity,
    split_markdown,
    strip_generated_wiki_views,
)


@dataclass(frozen=True, slots=True)
class ContextItem:
    query: str
    document_id: str
    relative_path: str
    title: str
    source_type: str
    revision: str
    heading: str
    text: str
    role: str = "match"


@dataclass(frozen=True, slots=True)
class ContextBundle:
    queries: tuple[str, ...]
    items: tuple[ContextItem, ...]
    total_chars: int
    truncated: bool

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "queries": list(self.queries),
            "items": [asdict(item) for item in self.items],
            "total_chars": self.total_chars,
            "truncated": self.truncated,
        }


def build_context_bundle(
    service: KnowledgeService,
    queries: tuple[str, ...],
    *,
    max_items: int = 12,
    max_chars: int = 20_000,
    per_query: int = 5,
) -> ContextBundle:
    """Return deduplicated excerpts without creating a second knowledge store."""

    normalized = tuple(dict.fromkeys(" ".join(query.split()) for query in queries if query.strip()))
    if not normalized:
        raise WoonError("context bundle requires at least one non-empty query")
    if not 1 <= max_items <= 50:
        raise WoonError("context bundle max_items must be between 1 and 50")
    if not 1_000 <= max_chars <= 200_000:
        raise WoonError("context bundle max_chars must be between 1000 and 200000")
    if not 1 <= per_query <= 20:
        raise WoonError("context bundle per_query must be between 1 and 20")

    items: list[ContextItem] = []
    seen: set[tuple[str, str, str]] = set()
    total = 0
    truncated = False
    for query in normalized:
        for hit in service.search(query, per_query):
            key = (hit.document_id, hit.revision, hit.chunk_id)
            if key in seen:
                continue
            excerpt = service.read_excerpt(hit.document_id, hit.chunk_id)
            text = excerpt.text.strip()
            if not text:
                continue
            if len(items) >= max_items or total + len(text) > max_chars:
                truncated = True
                continue
            seen.add(key)
            items.append(
                ContextItem(
                    query=query,
                    document_id=hit.document_id,
                    relative_path=hit.relative_path,
                    title=hit.title,
                    source_type=hit.source_type,
                    revision=hit.revision,
                    heading=excerpt.heading,
                    text=text,
                )
            )
            total += len(text)
    return ContextBundle(normalized, tuple(items), total, truncated)


def build_wiki_context_bundle(
    vault: Path,
    subject: str,
    *,
    max_items: int = 24,
    max_chars: int = 30_000,
) -> ContextBundle:
    """Build one bounded AI context from the canonical Wiki tree.

    The bundle follows the same hierarchy a human sees: ancestors, current
    page, direct children, current-page history, and evidence sections. It is
    a read-only projection and therefore cannot become a second source.
    """

    if not subject.strip():
        raise WoonError("Wiki context requires one subject")
    if not 1 <= max_items <= 100:
        raise WoonError("Wiki context max_items must be between 1 and 100")
    if not 1_000 <= max_chars <= 200_000:
        raise WoonError("Wiki context max_chars must be between 1000 and 200000")

    nodes, texts, issues = load_wiki_tree(vault)
    if issues:
        raise WoonError("Wiki context requires a valid tree: " + "; ".join(issues[:5]))
    current = _resolve_wiki_subject(nodes, subject)
    by_path = {node.relative_path: node for node in nodes}
    ancestors: list[WikiTreeNode] = []
    cursor = current
    while cursor.parent_path is not None:
        cursor = by_path[cursor.parent_path]
        ancestors.append(cursor)
    ancestors.reverse()
    direct_children = tuple(node for node in nodes if node.parent_path == current.relative_path)
    children = _ordered_wiki_children(current, direct_children)

    candidates: list[ContextItem] = []
    for node in ancestors:
        candidates.append(
            _tree_item(
                node,
                texts[node.relative_path],
                "ancestor",
                "상위 키워드",
                node.summary,
            )
        )
    _, current_body = split_markdown(strip_generated_wiki_views(texts[current.relative_path]))
    candidates.append(
        _tree_item(
            current,
            texts[current.relative_path],
            "current",
            "현재 문서",
            current_body.strip(),
        )
    )
    for index, group in enumerate(current.navigation_groups, start=1):
        grouped = tuple(
            node
            for child_id in group.child_ids
            for node in children
            if node.canonical_id == child_id
        )
        candidates.append(
            _navigation_group_item(
                current,
                texts[current.relative_path],
                index,
                group.label,
                grouped,
            )
        )
    for node in children:
        candidates.append(
            _tree_item(node, texts[node.relative_path], "child", "바로 아래 키워드", node.summary)
        )
    # Entity roots own both current knowledge and their dated chronology.
    # Focused children may still contribute evidence, but a dates-only history
    # child is no longer required.
    context_nodes = (current, *children)
    for node in context_nodes:
        source = texts[node.relative_path]
        _, body = split_markdown(strip_generated_wiki_views(source))
        timeline = _managed_body(
            source,
            "<!-- woon-wiki-timeline:start -->",
            "<!-- woon-wiki-timeline:end -->",
        )
        if timeline:
            candidates.append(_tree_item(node, source, "history", "판단 및 변경 이력", timeline))
        evidence = _evidence_sections(body)
        if evidence:
            candidates.append(_tree_item(node, source, "evidence", "근거와 출처", evidence))

    items: list[ContextItem] = []
    total = 0
    truncated = False
    for candidate in candidates:
        if not candidate.text:
            continue
        if len(items) >= max_items or total + len(candidate.text) > max_chars:
            truncated = True
            continue
        items.append(candidate)
        total += len(candidate.text)
    return ContextBundle((subject.strip(),), tuple(items), total, truncated)


def _ordered_wiki_children(
    parent: WikiTreeNode, direct: tuple[WikiTreeNode, ...]
) -> tuple[WikiTreeNode, ...]:
    """Use exactly the same explicit group order exposed by the human Wiki view."""

    by_id = {node.canonical_id: node for node in direct}
    if parent.navigation_groups:
        return tuple(
            by_id[child_id]
            for group in parent.navigation_groups
            for child_id in group.child_ids
            if child_id in by_id
        )
    return tuple(
        sorted(
            direct,
            key=lambda node: (
                node.sequence is None,
                node.sequence if node.sequence is not None else 0,
                node.title.casefold(),
            ),
        )
    )


def _navigation_group_item(
    parent: WikiTreeNode,
    source: str,
    index: int,
    label: str,
    children: tuple[WikiTreeNode, ...],
) -> ContextItem:
    """Project a complete group index before bounded child summaries are added."""

    rows = "\n".join(
        f"- {position}. {node.title} ({node.canonical_id})"
        for position, node in enumerate(children, start=1)
    )
    return ContextItem(
        query=parent.title,
        document_id=f"{parent.canonical_id}#navigation-{index}",
        relative_path=parent.relative_path,
        title=label,
        source_type="canonical-wiki-navigation",
        revision=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        heading=f"탐색 단계 {index}. {label}",
        text=rows,
        role="navigation-group",
    )


def _resolve_wiki_subject(nodes: tuple[WikiTreeNode, ...], subject: str) -> WikiTreeNode:
    wanted_path = subject.strip().removesuffix(".md")
    if not wanted_path.startswith("wiki/"):
        wanted_path = f"wiki/{wanted_path}"
    wanted_identity = normalize_identity(subject)
    matches = [
        node
        for node in nodes
        if node.relative_path.removesuffix(".md") == wanted_path
        or node.canonical_id == subject.strip().removesuffix(".md")
        or wanted_identity
        in {normalize_identity(value) for value in (node.title, *node.aliases, *node.keywords)}
    ]
    if len(matches) != 1:
        detail = "not found" if not matches else "ambiguous"
        raise WoonError(f"Wiki context subject is {detail}: {subject.strip()}")
    return matches[0]


def _tree_item(node: WikiTreeNode, source: str, role: str, heading: str, text: str) -> ContextItem:
    return ContextItem(
        query=node.title,
        document_id=node.canonical_id,
        relative_path=node.relative_path,
        title=node.title,
        source_type="canonical-wiki",
        revision=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        heading=heading,
        text=text.strip(),
        role=role,
    )


def _managed_body(text: str, start: str, end: str) -> str:
    match = re.search(re.escape(start) + r"(?P<body>.*?)" + re.escape(end), text, re.DOTALL)
    return match.group("body").strip() if match else ""


def _evidence_sections(body: str) -> str:
    sections: list[str] = []
    matches = list(re.finditer(r"(?m)^#{2,3} (?P<title>.+?)\s*$", body))
    for index, match in enumerate(matches):
        if not re.search(
            r"근거|출처|검증|개인 기여|evidence|source|reference",
            match.group("title"),
            re.I,
        ):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append(body[match.start() : end].strip())
    return "\n\n".join(sections)
