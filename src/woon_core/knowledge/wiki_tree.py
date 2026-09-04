"""Canonical parent tree and generated views for the Woon Wiki.

The Markdown page remains the only human-readable source of truth.  This
module derives navigation blocks from page metadata and never stores a second
map.  All preparation functions are read-only; callers apply the complete
batch atomically after reviewing the report.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.identity import is_book_scoped_canonical_id, validate_canonical_id
from woon_core.knowledge.learning_checkpoint import strip_learning_checkpoint
from woon_core.knowledge.yaml_cache import load_yaml_text

OVERVIEW_START = "<!-- woon-wiki-overview:start -->"
OVERVIEW_END = "<!-- woon-wiki-overview:end -->"
CHILDREN_START = "<!-- woon-wiki-children:start -->"
CHILDREN_END = "<!-- woon-wiki-children:end -->"
BOOK_READER_NAVIGATION_START = "<!-- woon-book-reader-navigation:start -->"
BOOK_READER_NAVIGATION_END = "<!-- woon-book-reader-navigation:end -->"
LATEST_START = "<!-- woon-wiki-latest:start -->"
LATEST_END = "<!-- woon-wiki-latest:end -->"
TIMELINE_START = "<!-- woon-wiki-timeline:start -->"
TIMELINE_END = "<!-- woon-wiki-timeline:end -->"
SOURCE_INDEX_START = "<!-- woon-wiki-source-index:start -->"
SOURCE_INDEX_END = "<!-- woon-wiki-source-index:end -->"

NODE_KINDS = {"root", "hub", "topic", "entity", "detail", "decision"}
VIEW_MODES = {"tree", "linear", "project", "topic-timeline", "article"}
TREE_VIEW_KINDS = {"root", "hub", "topic", "entity"}
FLATTEN_GROUP_MAX_CHILDREN = 20
UNGROUPED_NAVIGATION_EXCEPTIONS = {
    "wiki/README.md",
    "wiki/books/README.md",
    "wiki/books/ai-machine-learning.md",
    "wiki/books/programming-languages.md",
    "wiki/concepts/README.md",
}
LIFECYCLE_STATES = {
    "idea",
    "planned",
    "active",
    "paused",
    "completed",
    "cancelled",
    "archived",
}
TEMPORAL_ENTITY_KINDS = {"project", "person", "career", "application"}
LEGACY_TREE_FIELDS = {"parent_topics", "parent_moc", "map_role", "mindmap_role"}
_WIKILINK_RE = re.compile(r"^\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]$")
_BODY_WIKILINK_RE = re.compile(r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
WIKI_SOURCE_ARCHIVE_PARTS = ("private", "_sources")


@dataclass(frozen=True, slots=True)
class WikiNavigationGroup:
    label: str
    child_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WikiOrderedReaderSection:
    kind: str
    label: str


@dataclass(frozen=True, slots=True)
class WikiTreeNode:
    path: Path
    relative_path: str
    title: str
    summary: str
    canonical_id: str
    node_kind: str
    parent_path: str | None
    keywords: tuple[str, ...]
    aliases: tuple[str, ...]
    view_mode: str
    entity_kind: str
    updated: date
    sequence: float | None
    knowledge_state: str
    navigation_groups: tuple[WikiNavigationGroup, ...]
    ordered_reader_sections: tuple[WikiOrderedReaderSection, ...]
    lifecycle_status: str
    started_on: date | None
    ended_on: date | None
    occurred_on: date | None
    include_in_latest: bool


@dataclass(frozen=True, slots=True)
class WikiTreeReport:
    document_count: int
    changed_count: int
    pages: dict[Path, bytes]
    issues: tuple[str, ...]


def is_wiki_source_archive(path: Path, wiki_root: Path) -> bool:
    """Return whether ``path`` belongs to the raw source boundary inside Wiki.

    Raw evidence is physically owned by the Wiki vault, but it is not a second
    human-readable Wiki tree. Only accepted projections outside ``_sources``
    participate in keyword navigation, Graph, compilation, and page policy.
    """

    try:
        # ``iter_wiki_pages`` passes paths produced directly below the already
        # resolved vault root. Resolving both operands for every Markdown file
        # turns a full audit into thousands of redundant filesystem walks.
        # Keep the common case lexical and resolve only an actual symlink.
        relative = path.relative_to(wiki_root)
        if path.is_symlink():
            relative = path.resolve().relative_to(wiki_root.resolve())
    except ValueError:
        return False
    return relative.parts[:2] == WIKI_SOURCE_ARCHIVE_PARTS


def iter_wiki_pages(wiki_root: Path) -> tuple[Path, ...]:
    """List canonical human Wiki pages while excluding the raw source archive."""

    return tuple(
        path
        for path in sorted(wiki_root.rglob("*.md"))
        if not is_wiki_source_archive(path, wiki_root)
    )


def is_compact_link_canonical_id(canonical_id: object) -> bool:
    """Return whether a canonical page intentionally contains links only."""

    value = str(canonical_id or "")
    return value.startswith("resources/") or (
        value.startswith("private/novel/")
        and ("/source-" in value or value.startswith("private/novel/people/"))
    )


def is_compact_link_page(node: WikiTreeNode) -> bool:
    """Return whether a detail page intentionally contains links without a callout."""

    return is_compact_link_canonical_id(node.canonical_id)


def prepare_wiki_tree_refresh(
    vault: Path, *, canonical_prefix: str | None = None
) -> WikiTreeReport:
    """Regenerate compact navigation and latest blocks from canonical metadata."""

    root = vault.expanduser().resolve()
    nodes, texts, issues = load_wiki_tree(root)
    scope = canonical_prefix.strip().strip("/") if canonical_prefix is not None else ""
    if canonical_prefix is not None and not scope:
        raise WoonError("Wiki tree refresh canonical prefix must not be empty")
    relevant_issues = issues
    if scope:
        path_marker = f"wiki/{scope}"
        relevant_issues = tuple(issue for issue in issues if scope in issue or path_marker in issue)
    if relevant_issues:
        return WikiTreeReport(len(nodes), 0, {}, relevant_issues)
    children = _children_by_parent(nodes)
    related = _related_neighbors(nodes, texts)
    pages: dict[Path, bytes] = {}
    changed = 0
    by_path = {node.relative_path: node for node in nodes}
    selected_nodes = tuple(
        node
        for node in nodes
        if not scope or node.canonical_id == scope or node.canonical_id.startswith(f"{scope}/")
    )
    if scope and not selected_nodes:
        raise WoonError(f"Wiki tree refresh canonical prefix was not found: {scope}")
    for node in selected_nodes:
        refreshed = render_wiki_tree_view(
            texts[node.relative_path],
            node=node,
            nodes=by_path,
            children=children,
            texts=texts,
            related=related,
        )
        encoded = refreshed.encode("utf-8")
        pages[node.path] = encoded
        if encoded != node.path.read_bytes():
            changed += 1
    return WikiTreeReport(len(nodes), changed, pages, ())


def apply_wiki_tree_refresh(vault: Path, report: WikiTreeReport) -> None:
    """Apply one validated tree projection atomically with full rollback."""

    if report.issues:
        raise WoonError("cannot apply an invalid Wiki tree refresh")
    root = vault.expanduser().resolve()
    snapshots: list[tuple[Path, bytes, int]] = []
    changed: list[tuple[Path, bytes, int]] = []
    for path, content in sorted(report.pages.items(), key=lambda item: item[0].as_posix()):
        resolved = path.resolve()
        if not resolved.is_relative_to(root / "wiki") or not resolved.is_file():
            raise WoonError("Wiki tree refresh target must be an existing Wiki page")
        previous = resolved.read_bytes()
        mode = resolved.stat().st_mode & 0o777
        snapshots.append((resolved, previous, mode))
        if previous != content:
            changed.append((resolved, content, mode))
    try:
        for path, content, mode in changed:
            atomic_write(path, content, mode=mode)
    except BaseException:
        for path, previous, mode in reversed(snapshots):
            atomic_write(path, previous, mode=mode)
        raise


def load_wiki_tree(
    vault: Path,
) -> tuple[tuple[WikiTreeNode, ...], dict[str, str], tuple[str, ...]]:
    """Load and validate the active Wiki parent graph without mutating files."""

    root = vault.expanduser().resolve()
    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        raise WoonError("Wiki root is missing")
    nodes: list[WikiTreeNode] = []
    texts: dict[str, str] = {}
    issues: list[str] = []
    canonical: dict[str, str] = {}
    identities: dict[str, str] = {}
    book_roots = _book_root_ids(wiki_root)
    for path in iter_wiki_pages(wiki_root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        texts[relative] = text
        try:
            metadata, _ = split_markdown(text)
        except WoonError as error:
            issues.append(f"{relative}: {error}")
            continue
        title = _required_text(metadata, "title", relative, issues)
        summary = _required_text(metadata, "summary", relative, issues)
        canonical_id = _required_text(metadata, "canonical_id", relative, issues)
        node_kind = _required_text(metadata, "node_kind", relative, issues)
        view_mode = _required_text(metadata, "view_mode", relative, issues)
        entity_kind = _optional_text(metadata.get("entity_kind"), "entity_kind", relative, issues)
        keywords = _string_list(metadata.get("keywords"), "keywords", relative, issues)
        aliases = _string_list(metadata.get("aliases", []), "aliases", relative, issues)
        navigation_groups = _navigation_groups(metadata.get("navigation_groups"), relative, issues)
        ordered_reader_sections = _ordered_reader_sections(
            metadata.get("ordered_reader_sections"), relative, issues
        )
        state = _required_text(metadata, "knowledge_state", relative, issues)
        updated = _date_value(metadata.get("updated"), "updated", relative, issues)
        if node_kind and node_kind not in NODE_KINDS:
            issues.append(f"{relative}: unsupported node_kind {node_kind!r}")
        if node_kind == "entity" and not entity_kind:
            issues.append(f"{relative}: entity requires entity_kind")
        if node_kind in {"root", "hub"} and _visible_navigation_body(text):
            issues.append(
                f"{relative}: navigation page body must contain only generated keyword links"
            )
        if view_mode and view_mode not in VIEW_MODES:
            issues.append(f"{relative}: unsupported view_mode {view_mode!r}")
        for field in LEGACY_TREE_FIELDS.intersection(metadata):
            issues.append(f"{relative}: legacy Wiki tree field remains: {field}")
        parent_path: str | None = None
        parent = metadata.get("parent")
        if relative == "wiki/README.md":
            if node_kind != "root":
                issues.append("wiki/README.md: root must use node_kind root")
            if parent not in {None, ""}:
                issues.append("wiki/README.md: root must not have parent")
        else:
            if node_kind == "root":
                issues.append(f"{relative}: only wiki/README.md may be root")
            parent_path = wikilink_path(parent, relative, issues)
        sequence = _optional_number(metadata.get("sequence"), relative, issues)
        lifecycle_status = _optional_text(
            metadata.get("lifecycle_status"), "lifecycle_status", relative, issues
        ).casefold()
        started_on = _optional_date(metadata.get("started_on"), "started_on", relative, issues)
        ended_on = _optional_date(metadata.get("ended_on"), "ended_on", relative, issues)
        occurred_on = _optional_date(metadata.get("occurred_on"), "occurred_on", relative, issues)
        include_in_latest = metadata.get("include_in_latest", True)
        if not isinstance(include_in_latest, bool):
            issues.append(f"{relative}: include_in_latest must be a boolean")
            include_in_latest = True
        issues.extend(
            _temporal_issues(
                relative,
                lifecycle_status=lifecycle_status,
                started_on=started_on,
                ended_on=ended_on,
                occurred_on=occurred_on,
            )
        )
        if canonical_id:
            try:
                validate_canonical_id(canonical_id)
            except WoonError as error:
                issues.append(f"{relative}: {error}")
            key = canonical_id.casefold()
            if key in canonical:
                issues.append(
                    f"{relative}: duplicate canonical_id with {canonical[key]}: {canonical_id}"
                )
            canonical[key] = relative
        # Chapter and section names such as "신경망 학습" legitimately repeat a
        # general concept title.  Their canonical identity is the full path below one
        # verified book edition, so they must not compete with the global concept tree.
        book_scoped_identity = any(
            canonical_id.startswith(f"{book_root}/") for book_root in book_roots
        )
        for identity in () if book_scoped_identity else (title, *aliases, *keywords):
            normalized = normalize_identity(identity)
            if not normalized:
                continue
            previous = identities.get(normalized)
            if previous is not None and previous != relative:
                issues.append(f"{relative}: duplicate identity {identity!r} with {previous}")
            else:
                identities[normalized] = relative
        nodes.append(
            WikiTreeNode(
                path=path,
                relative_path=relative,
                title=title,
                summary=summary,
                canonical_id=canonical_id,
                node_kind=node_kind,
                parent_path=parent_path,
                keywords=keywords,
                aliases=aliases,
                view_mode=view_mode,
                entity_kind=entity_kind,
                updated=updated,
                sequence=sequence,
                knowledge_state=state,
                navigation_groups=navigation_groups,
                ordered_reader_sections=ordered_reader_sections,
                lifecycle_status=lifecycle_status,
                started_on=started_on,
                ended_on=ended_on,
                occurred_on=occurred_on,
                include_in_latest=include_in_latest,
            )
        )
    by_path = {node.relative_path: node for node in nodes}
    for node in nodes:
        if node.parent_path is not None and node.parent_path not in by_path:
            issues.append(f"{node.relative_path}: parent is missing: {node.parent_path}")
    issues.extend(_cycle_and_reachability_issues(nodes))
    issues.extend(_navigation_group_issues(nodes, texts))
    issues.extend(_domain_tree_issues(nodes, texts))
    return tuple(nodes), texts, tuple(dict.fromkeys(issues))


def _book_root_ids(wiki_root: Path) -> frozenset[str]:
    """Discover book entities before validating descendant display identities."""

    roots: set[str] = set()
    for path in iter_wiki_pages(wiki_root):
        try:
            metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, WoonError):
            continue
        canonical_id = metadata.get("canonical_id")
        if (
            metadata.get("node_kind") == "entity"
            and metadata.get("entity_kind") == "book"
            and isinstance(canonical_id, str)
            and canonical_id.strip()
        ):
            roots.add(canonical_id.strip())
    return frozenset(roots)


def render_wiki_tree_view(
    text: str,
    *,
    node: WikiTreeNode,
    nodes: dict[str, WikiTreeNode],
    children: dict[str, tuple[WikiTreeNode, ...]],
    texts: dict[str, str],
    related: dict[str, tuple[WikiTreeNode, ...]] | None = None,
) -> str:
    """Render all Core-owned navigation blocks for one already validated node."""

    descendants = _descendants(node.relative_path, children)
    direct = children.get(node.relative_path, ())
    # Structural properties belong to frontmatter/Obsidian Properties.  A
    # generated callout repeated title, kind, state, parent, and child count
    # without helping a reader understand the subject, so every Wiki page now
    # keeps only its authored semantic summary and navigation in the body.
    updated = _normalize_reader_headings(
        _normalize_h1_spacing(
            _strip_ordered_book_navigation(_strip_marker_block(text, OVERVIEW_START, OVERVIEW_END))
        )
    )
    # Latest indexes are projections. Remove every stale variant first and
    # rebuild only the heading that still has rows in the current graph.
    updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
    updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
    authored = updated
    authored_without_children = _strip_section(
        authored, "하위 키워드", CHILDREN_START, CHILDREN_END
    )
    book_map_kind = _book_navigation_kind(node, tuple(nodes.values()))

    if node.entity_kind == "book" and not node.navigation_groups:
        updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
        updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
        updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
        return updated.rstrip() + "\n"

    direct_already_authored = bool(direct) and all(
        _contains_wikilink_to(authored_without_children, item.relative_path) for item in direct
    )
    show_tree = (
        (book_map_kind is not None or node.node_kind in TREE_VIEW_KINDS)
        and (bool(descendants) or bool(node.ordered_reader_sections))
        and not (book_map_kind is None and node.node_kind == "entity" and direct_already_authored)
    )
    if show_tree:
        if book_map_kind is not None and node.ordered_reader_sections:
            updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
            updated = _render_ordered_book_reader_sections(updated, node, direct, texts)
            updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
            updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
            return _normalize_h1_spacing(updated).rstrip() + "\n"
        child_rows = [
            CHILDREN_START,
            *_render_navigation_children(
                node,
                direct,
                children,
                texts,
                include_sequence=node.view_mode == "linear" and book_map_kind is None,
                book_map_kind=book_map_kind,
            ),
            CHILDREN_END,
        ]
        if book_map_kind is not None:
            updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
            updated = _replace_or_insert_after_h1(
                updated, CHILDREN_START, CHILDREN_END, "\n".join(child_rows)
            )
        else:
            updated = _replace_or_append_section(
                updated,
                "하위 키워드",
                CHILDREN_START,
                CHILDREN_END,
                "\n".join(child_rows),
            )
        if (
            node.node_kind in {"root", "hub"}
            or book_map_kind is not None
            or is_book_scoped_canonical_id(node.canonical_id)
        ):
            updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
        else:
            direct_paths = {item.relative_path for item in direct}
            latest = sorted(
                (item for item in descendants if item.relative_path not in direct_paths),
                key=lambda item: (-item.updated.toordinal(), item.title),
            )[:10]
            latest_heading = "최신 하위 문서"
            if not latest and node.node_kind == "entity" and related:
                latest = sorted(
                    (
                        item
                        for item in related.get(node.relative_path, ())
                        if item.relative_path not in direct_paths
                        and item.include_in_latest
                        and not _contains_wikilink_to(authored, item.relative_path)
                    ),
                    key=lambda item: (-item.updated.toordinal(), item.title.casefold()),
                )[:10]
                latest_heading = "최신 관련 문서"
            if latest:
                latest_rows = [
                    LATEST_START,
                    *(
                        _render_keyword_link(item, include_sequence=False, label=label)
                        for item, label in zip(
                            latest, _distinct_keyword_labels(latest), strict=True
                        )
                    ),
                    LATEST_END,
                ]
                updated = _replace_or_append_section(
                    updated,
                    latest_heading,
                    LATEST_START,
                    LATEST_END,
                    "\n".join(latest_rows),
                )
                updated = _normalize_latest_heading(updated, latest_heading)
            else:
                updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
    elif (
        node.node_kind == "entity"
        and related
        and any(
            item.include_in_latest and not _contains_wikilink_to(authored, item.relative_path)
            for item in related.get(node.relative_path, ())
        )
    ):
        updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
        latest = sorted(
            (
                item
                for item in related[node.relative_path]
                if item.include_in_latest
                and not _contains_wikilink_to(authored, item.relative_path)
            ),
            key=lambda item: (-item.updated.toordinal(), item.title.casefold()),
        )[:10]
        latest_rows = [
            LATEST_START,
            *(
                _render_keyword_link(item, include_sequence=False, label=label)
                for item, label in zip(latest, _distinct_keyword_labels(latest), strict=True)
            ),
            LATEST_END,
        ]
        updated = _replace_or_append_section(
            updated, "최신 관련 문서", LATEST_START, LATEST_END, "\n".join(latest_rows)
        )
        updated = _normalize_latest_heading(updated, "최신 관련 문서")
    else:
        updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
        updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
        updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
    return _normalize_h1_spacing(updated).rstrip() + "\n"


def strip_generated_wiki_views(text: str) -> str:
    """Remove derived view blocks before computing a compiler projection."""

    updated = _strip_ordered_book_navigation(text)
    updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
    updated = _strip_section(updated, "원자료", SOURCE_INDEX_START, SOURCE_INDEX_END)
    updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
    updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
    return _strip_marker_block(updated, OVERVIEW_START, OVERVIEW_END).rstrip() + "\n"


def preserve_generated_wiki_views(existing: str, rendered: str) -> str:
    """Carry exact derived views across a compiler render when they already exist."""

    updated = strip_generated_wiki_views(rendered)
    overview = _optional_marker_with_trailing_space(existing, OVERVIEW_START, OVERVIEW_END)
    if overview is not None:
        h1 = re.search(r"(?m)^# .+?\s*$", updated)
        if h1 is None:
            raise WoonError("Wiki document requires one H1")
        updated = updated[: h1.end()].rstrip() + "\n\n" + overview + updated[h1.end() :].lstrip()
    for heading, start, end in (("하위 키워드", CHILDREN_START, CHILDREN_END),):
        block = _optional_marker_block(existing, start, end)
        if block is not None:
            if _managed_navigation_uses_h2(block):
                updated = _strip_section(updated, heading, start, end)
                updated = _replace_or_insert_after_h1(updated, start, end, block)
            else:
                updated = _replace_or_append_section(updated, heading, start, end, block)
    source_index = _optional_marker_block(rendered, SOURCE_INDEX_START, SOURCE_INDEX_END)
    if source_index is None:
        source_index = _optional_marker_block(existing, SOURCE_INDEX_START, SOURCE_INDEX_END)
    if source_index is not None:
        updated = _replace_or_append_section(
            updated, "원자료", SOURCE_INDEX_START, SOURCE_INDEX_END, source_index
        )
    latest = _optional_marker_block(existing, LATEST_START, LATEST_END)
    if latest is not None:
        heading = "최신 관련 문서" if "## 최신 관련 문서" in existing else "최신 하위 문서"
        updated = _replace_or_append_section(updated, heading, LATEST_START, LATEST_END, latest)
        updated = _normalize_latest_heading(updated, heading)
    updated = _preserve_ordered_book_navigation(existing, updated)
    return updated.rstrip() + "\n"


def _preserve_ordered_book_navigation(existing: str, rendered: str) -> str:
    """Restore exact mixed-depth navigation blocks at their source-order anchors."""

    pattern = re.compile(
        rf"(?ms){re.escape(BOOK_READER_NAVIGATION_START)}\n.*?"
        rf"{re.escape(BOOK_READER_NAVIGATION_END)}"
    )
    matches = list(pattern.finditer(existing))
    if not matches:
        return rendered
    updated = _strip_ordered_book_navigation(rendered)
    insertions: list[tuple[int, str]] = []
    for match in matches:
        trailing = existing[match.end() :]
        next_heading = re.search(r"(?m)^## (?P<label>\S.*?)\s*$", trailing)
        if next_heading is None or next_heading.group("label").strip() == "원자료":
            offset = _before_source_index_offset(updated)
        else:
            offset = _exact_h2_offset(updated, next_heading.group("label").strip())
        insertions.append((offset, match.group(0)))
    for offset, block in reversed(insertions):
        before = updated[:offset].rstrip()
        after = updated[offset:].lstrip("\n")
        updated = f"{before}\n\n{block}\n\n{after}"
    return updated


def split_markdown(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?", text, flags=re.DOTALL)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")
    try:
        metadata = load_yaml_text(match.group("yaml")) or {}
    except yaml.YAMLError as error:
        raise WoonError(f"invalid Wiki frontmatter: {error}") from error
    if not isinstance(metadata, dict):
        raise WoonError("Wiki frontmatter must be a mapping")
    return metadata, text[match.end() :]


def render_markdown(metadata: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_text}---\n\n{body.lstrip()}".rstrip() + "\n"


def parent_link(relative_path: str, title: str) -> str:
    return f"[[{_without_suffix(relative_path)}|{title}]]"


def normalize_identity(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


def wikilink_path(value: object, relative: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{relative}: non-root Wiki requires one parent wikilink")
        return None
    match = _WIKILINK_RE.fullmatch(value.strip())
    if match is None:
        issues.append(f"{relative}: parent must be one Wiki wikilink")
        return None
    target = match.group("target").strip()
    if target.startswith("/") or ".." in Path(target).parts:
        issues.append(f"{relative}: parent must stay inside wiki/")
        return None
    if not target.endswith(".md"):
        target += ".md"
    if not target.startswith("wiki/"):
        issues.append(f"{relative}: parent must target wiki/**/*.md")
        return None
    return Path(target).as_posix()


def _required_text(metadata: dict[str, Any], key: str, relative: str, issues: list[str]) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{relative}: {key} must be a non-empty string")
        return ""
    return value.strip()


def _string_list(value: object, key: str, relative: str, issues: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        issues.append(f"{relative}: {key} must be a string list")
        return ()
    normalized = tuple(item.strip() for item in value)
    if len(set(item.casefold() for item in normalized)) != len(normalized):
        issues.append(f"{relative}: {key} must not contain duplicates")
    return normalized


def _navigation_groups(
    value: object, relative: str, issues: list[str]
) -> tuple[WikiNavigationGroup, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        issues.append(f"{relative}: navigation_groups must be a list")
        return ()
    groups: list[WikiNavigationGroup] = []
    labels: set[str] = set()
    for index, item in enumerate(value, start=1):
        location = f"{relative}: navigation_groups[{index}]"
        if not isinstance(item, dict):
            issues.append(f"{location} must be a mapping")
            continue
        label = item.get("label")
        child_ids = item.get("children")
        if not isinstance(label, str) or not label.strip():
            issues.append(f"{location}.label must be a non-empty string")
            continue
        normalized_label = label.strip().casefold()
        if normalized_label in labels:
            issues.append(f"{relative}: navigation_groups labels must not repeat")
            continue
        labels.add(normalized_label)
        if (
            not isinstance(child_ids, list)
            or not child_ids
            or not all(isinstance(child_id, str) and child_id.strip() for child_id in child_ids)
        ):
            issues.append(f"{location}.children must be a non-empty canonical_id list")
            continue
        normalized_ids = tuple(child_id.strip() for child_id in child_ids)
        if len(set(normalized_ids)) != len(normalized_ids):
            issues.append(f"{location}.children must not contain duplicates")
            continue
        groups.append(WikiNavigationGroup(label=label.strip(), child_ids=normalized_ids))
    return tuple(groups)


def _ordered_reader_sections(
    value: object, relative: str, issues: list[str]
) -> tuple[WikiOrderedReaderSection, ...]:
    """Parse the explicit source order for a mixed-depth book chapter."""

    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        issues.append(f"{relative}: ordered_reader_sections must be a non-empty list")
        return ()
    sections: list[WikiOrderedReaderSection] = []
    identities: set[tuple[str, str]] = set()
    for index, item in enumerate(value, start=1):
        location = f"{relative}: ordered_reader_sections[{index}]"
        if not isinstance(item, dict) or set(item) != {"kind", "label"}:
            issues.append(f"{location} must contain exactly kind and label")
            continue
        kind = item.get("kind")
        label = item.get("label")
        if kind not in {"source-body", "navigation-group", "toc-heading"}:
            issues.append(f"{location}.kind must be source-body, navigation-group, or toc-heading")
            continue
        if not isinstance(label, str) or not label.strip():
            issues.append(f"{location}.label must be a non-empty string")
            continue
        identity = (kind, label.strip().casefold())
        if identity in identities:
            issues.append(f"{relative}: ordered_reader_sections must not repeat entries")
            continue
        identities.add(identity)
        sections.append(WikiOrderedReaderSection(kind=kind, label=label.strip()))
    return tuple(sections)


def _date_value(value: object, key: str, relative: str, issues: list[str]) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    issues.append(f"{relative}: {key} must be YYYY-MM-DD")
    return date.min


def _optional_date(value: object, key: str, relative: str, issues: list[str]) -> date | None:
    if value in {None, ""}:
        return None
    parsed = _date_value(value, key, relative, issues)
    return None if parsed == date.min else parsed


def _temporal_issues(
    relative: str,
    *,
    lifecycle_status: str,
    started_on: date | None,
    ended_on: date | None,
    occurred_on: date | None,
) -> list[str]:
    issues: list[str] = []
    if not lifecycle_status:
        if any(value is not None for value in (started_on, ended_on, occurred_on)):
            issues.append(f"{relative}: temporal dates require lifecycle_status")
        return issues
    if lifecycle_status not in LIFECYCLE_STATES:
        issues.append(f"{relative}: unsupported lifecycle_status {lifecycle_status!r}")
    if occurred_on is not None and any(value is not None for value in (started_on, ended_on)):
        issues.append(f"{relative}: occurred_on cannot be combined with a date range")
    if started_on is not None and ended_on is not None and ended_on < started_on:
        issues.append(f"{relative}: ended_on cannot precede started_on")
    if lifecycle_status in {"completed", "cancelled", "archived"} and not (ended_on or occurred_on):
        issues.append(f"{relative}: closed lifecycle requires ended_on or occurred_on")
    if lifecycle_status in {"idea", "planned", "active", "paused"} and ended_on is not None:
        issues.append(f"{relative}: open lifecycle cannot have ended_on")
    return issues


def _optional_number(value: object, relative: str, issues: list[str]) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    issues.append(f"{relative}: sequence must be numeric")
    return None


def _optional_text(value: object, key: str, relative: str, issues: list[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{relative}: {key} must be a non-empty string when present")
        return ""
    return value.strip()


def _domain_tree_issues(nodes: list[WikiTreeNode], texts: dict[str, str]) -> list[str]:
    """Validate the user-facing book, resource, and people navigation boundaries."""

    by_path = {node.relative_path: node for node in nodes}
    by_id = {node.canonical_id: node for node in nodes}
    children = _children_by_parent(tuple(nodes))
    issues: list[str] = []
    root = "wiki/README.md"
    books_path = "wiki/books/README.md"
    resources_path = "wiki/resources/README.md"
    people_path = "wiki/people/README.md"

    books = by_path.get(books_path)
    if books is not None:
        if books.parent_path != root:
            issues.append(f"{books_path}: books must be a direct child of Wiki root")
        if books.title != "책" or books.keywords[:1] != ("책",):
            issues.append(f"{books_path}: books root must use the visible keyword '책'")
        for genre in children.get(books_path, ()):
            if genre.node_kind != "hub":
                issues.append(f"{genre.relative_path}: direct children of books must be genre hubs")
            for book in children.get(genre.relative_path, ()):
                if book.node_kind != "entity" or book.entity_kind != "book":
                    issues.append(
                        f"{book.relative_path}: direct children of a book genre "
                        "must be book entities"
                    )

    resources = by_path.get(resources_path)
    if resources is not None:
        if resources.parent_path != root:
            issues.append(f"{resources_path}: resources must be a direct child of Wiki root")
        if resources.title != "리소스" or resources.keywords[:1] != ("리소스",):
            issues.append(f"{resources_path}: resources root must use the visible keyword '리소스'")
        for keyword in children.get(resources_path, ()):
            grouped = children.get(keyword.relative_path, ())
            if keyword.node_kind != "topic":
                issues.append(
                    f"{keyword.relative_path}: direct children of resources must be category topics"
                )
            if grouped:
                issues.append(
                    f"{keyword.relative_path}: resource category topics must link raw sources "
                    "directly and must not own intermediate Wiki children"
                )
            issues.extend(_resource_link_index_issues(keyword, texts[keyword.relative_path]))

    for node in nodes:
        book_map_kind = _book_navigation_kind(node, nodes)
        if node.ordered_reader_sections:
            issues.extend(
                _ordered_reader_section_issues(
                    node,
                    texts[node.relative_path],
                    book_map_kind=book_map_kind,
                )
            )
        if book_map_kind is not None and node.navigation_groups:
            authored_map_body = _book_map_authored_body(texts[node.relative_path])
            if (
                authored_map_body
                and not node.ordered_reader_sections
                and book_map_kind != "section-root"
            ):
                issues.append(
                    f"{node.relative_path}: book map authored body must be empty; "
                    "keep source explanation and runnable examples on leaf pages"
                )
        chapter_match = re.fullmatch(r"(.+/chapter-\d{2})/.+", node.canonical_id)
        if chapter_match is not None:
            owner = by_id.get(chapter_match.group(1))
            if owner is None:
                if not _is_root_direct_book_section(node, nodes):
                    issues.append(
                        f"{node.relative_path}: numbered lesson is missing its owning chapter "
                        f"{chapter_match.group(1)}"
                    )
            elif not _has_ancestor(node, owner.relative_path, by_path):
                issues.append(
                    f"{node.relative_path}: numbered lesson must stay below {owner.relative_path}"
                )

        if re.fullmatch(r".+/chapter-\d{2}", node.canonical_id):
            direct = children.get(node.relative_path, ())
            authored = _strip_ordered_book_navigation(
                _strip_section(
                    texts[node.relative_path], "하위 키워드", CHILDREN_START, CHILDREN_END
                )
            )
            duplicated = tuple(
                child.canonical_id
                for child in direct
                if _contains_wikilink_to(authored, child.relative_path)
            )
            if duplicated:
                issues.append(
                    f"{node.relative_path}: chapter body must not duplicate managed lesson links: "
                    + ", ".join(duplicated)
                )

        if node.node_kind == "entity" and node.entity_kind not in {"book", "resource"}:
            if node.entity_kind in TEMPORAL_ENTITY_KINDS and not node.lifecycle_status:
                issues.append(f"{node.relative_path}: temporal entity requires lifecycle_status")
            issues.extend(
                _entity_root_issues(
                    node,
                    texts[node.relative_path],
                    children.get(node.relative_path, ()),
                    texts,
                )
            )
        if node.entity_kind == "book":
            if not _has_ancestor(node, books_path, by_path):
                issues.append(f"{node.relative_path}: book entity must stay below the books root")
            if node.navigation_groups:
                authored = _strip_section(
                    texts[node.relative_path], "하위 키워드", CHILDREN_START, CHILDREN_END
                )
                duplicated = tuple(
                    child.canonical_id
                    for child in children.get(node.relative_path, ())
                    if _contains_wikilink_to(authored, child.relative_path)
                )
                if duplicated:
                    issues.append(
                        f"{node.relative_path}: book body must not duplicate managed "
                        "chapter links: " + ", ".join(duplicated)
                    )
            issues.extend(_book_link_index_issues(node, texts[node.relative_path]))
        if node.entity_kind == "resource":
            issues.append(
                f"{node.relative_path}: resource entity cards are retired; index the raw link "
                "on one resource keyword topic"
            )

    if people_path in by_path:
        for child in children.get(people_path, ()):
            if child.node_kind != "entity" or child.entity_kind != "person":
                issues.append(
                    f"{child.relative_path}: direct children of people must be person entities"
                )
    return issues


def _ordered_reader_section_issues(
    node: WikiTreeNode,
    text: str,
    *,
    book_map_kind: str | None,
) -> list[str]:
    """Validate the opt-in mixed-depth chapter projection without fallback."""

    issues: list[str] = []
    if book_map_kind != "chapter-root":
        return [f"{node.relative_path}: ordered_reader_sections are allowed only on a book chapter"]
    kinds = {section.kind for section in node.ordered_reader_sections}
    if "toc-heading" in kinds:
        metadata, _ = split_markdown(text)
        if "source-body" in kinds or not kinds.issubset({"toc-heading", "navigation-group"}):
            return [
                f"{node.relative_path}: toc-heading may combine only with navigation-group "
                "entries on a toc-only chapter"
            ]
        if metadata.get("content_state") != "toc-only":
            issues.append(
                f"{node.relative_path}: toc-heading is allowed only on a toc-only chapter"
            )
        navigation_labels = [group.label for group in node.navigation_groups]
        ordered_navigation = [
            section.label
            for section in node.ordered_reader_sections
            if section.kind == "navigation-group"
        ]
        if ordered_navigation != navigation_labels:
            issues.append(
                f"{node.relative_path}: toc-only ordered navigation-group entries must match "
                "navigation_groups exactly and in source order"
            )
        if _book_map_authored_body(text):
            issues.append(
                f"{node.relative_path}: toc-heading chapter must not contain authored prose, "
                "code, images, or links"
            )
        return issues
    if not node.navigation_groups:
        return [f"{node.relative_path}: ordered_reader_sections require navigation_groups"]
    if kinds != {"source-body", "navigation-group"}:
        issues.append(
            f"{node.relative_path}: ordered_reader_sections require both source-body and "
            "navigation-group entries"
        )
    navigation_labels = [group.label for group in node.navigation_groups]
    ordered_navigation = [
        section.label
        for section in node.ordered_reader_sections
        if section.kind == "navigation-group"
    ]
    if ordered_navigation != navigation_labels:
        issues.append(
            f"{node.relative_path}: ordered navigation-group entries must match "
            "navigation_groups exactly and in source order"
        )
    source_labels = [
        section.label for section in node.ordered_reader_sections if section.kind == "source-body"
    ]
    authored = strip_generated_wiki_views(text)
    positions: list[int] = []
    for label in source_labels:
        matches = list(re.finditer(rf"(?m)^## {re.escape(label)}\s*$", authored))
        if len(matches) != 1:
            issues.append(
                f"{node.relative_path}: ordered source-body H2 must occur exactly once: {label}"
            )
            continue
        positions.append(matches[0].start())
    if positions != sorted(positions):
        issues.append(
            f"{node.relative_path}: ordered source-body H2 order differs from reader body"
        )
    return issues


def _navigation_group_issues(nodes: list[WikiTreeNode], texts: dict[str, str]) -> list[str]:
    """Require one visible grouping axis whenever a page owns multiple children.

    A few compact catalog roots intentionally render a two-level bullet tree from
    their canonical parent relations. Every other multi-child page must name the
    reading axis explicitly so the human view and AI context cannot degrade into
    an alphabetic or creation-order link dump.
    """

    children = _children_by_parent(tuple(nodes))
    all_nodes = tuple(nodes)
    issues: list[str] = []
    for parent in nodes:
        direct = children.get(parent.relative_path, ())
        book_map_kind = _book_navigation_kind(parent, all_nodes)
        supports_groups = parent.node_kind in TREE_VIEW_KINDS
        if len(direct) > 1 and not parent.navigation_groups:
            missing_sequence = tuple(child for child in direct if child.sequence is None)
            if missing_sequence:
                issues.append(
                    f"{parent.relative_path}: ordered navigation requires sequence on every "
                    "direct child: " + ", ".join(child.canonical_id for child in missing_sequence)
                )
            sequence_counts = Counter(
                child.sequence for child in direct if child.sequence is not None
            )
            repeated = sorted(sequence for sequence, count in sequence_counts.items() if count > 1)
            if repeated:
                issues.append(
                    f"{parent.relative_path}: direct child sequence values must be unique: "
                    + ", ".join(_format_sequence(sequence) for sequence in repeated)
                )
        requires_groups = (
            supports_groups
            and len(direct) > 1
            and parent.relative_path not in UNGROUPED_NAVIGATION_EXCEPTIONS
        ) or (book_map_kind is not None and bool(direct))
        if requires_groups and not parent.navigation_groups:
            if book_map_kind is not None:
                issues.append(
                    f"{parent.relative_path}: book map requires navigation_groups for every "
                    "source part, appendix, or section topic"
                )
            else:
                issues.append(
                    f"{parent.relative_path}: {len(direct)} direct children require explicit "
                    "navigation groups instead of one flat list"
                )
        if not parent.navigation_groups:
            continue
        if not supports_groups:
            issues.append(
                f"{parent.relative_path}: navigation_groups are allowed only on root, hub, "
                "topic, or entity pages"
            )
            continue
        direct_by_id = {child.canonical_id: child for child in direct}
        if book_map_kind == "section-root" and (
            len(parent.navigation_groups) != 1
            or parent.navigation_groups[0].label.strip().casefold()
            != parent.title.strip().casefold()
        ):
            issues.append(
                f"{parent.relative_path}: a root-direct source section may own exactly one "
                "navigation group whose label matches the section title"
            )
        listed = [child_id for group in parent.navigation_groups for child_id in group.child_ids]
        duplicates = sorted(
            {child_id for child_id in listed if listed.count(child_id) > 1}, key=str.casefold
        )
        if duplicates:
            issues.append(
                f"{parent.relative_path}: navigation_groups repeat direct children: "
                + ", ".join(duplicates)
            )
        unknown = sorted(set(listed) - set(direct_by_id), key=str.casefold)
        if unknown:
            issues.append(
                f"{parent.relative_path}: navigation_groups contain non-direct children: "
                + ", ".join(unknown)
            )
        missing = sorted(set(direct_by_id) - set(listed), key=str.casefold)
        if missing:
            issues.append(
                f"{parent.relative_path}: navigation_groups omit direct children: "
                + ", ".join(missing)
            )
        for group in parent.navigation_groups:
            chapter_topic_map = (
                book_map_kind == "chapter-root"
                or re.fullmatch(r".+/chapter-\d+", parent.canonical_id) is not None
            )
            if chapter_topic_map and len(group.child_ids) > 1:
                repeated_topic = tuple(
                    child_id
                    for child_id in group.child_ids
                    if (child := direct_by_id.get(child_id)) is not None
                    and child.title.strip().casefold() == group.label.strip().casefold()
                    and not _has_direct_reader_content(texts[child.relative_path])
                )
                if repeated_topic:
                    issues.append(
                        f"{parent.relative_path}: chapter H2 topic must not be repeated as "
                        "a wrapper child: " + ", ".join(repeated_topic)
                    )
            if chapter_topic_map and re.fullmatch(
                r"(?:\d+장|부록\s+[A-Za-z0-9]+)", group.label.strip()
            ):
                issues.append(
                    f"{parent.relative_path}: book map H2 must be a meaningful topic keyword, "
                    f"not the repeated container label {group.label!r}"
                )
            link_count = 0
            for child_id in group.child_ids:
                child = direct_by_id.get(child_id)
                if child is None:
                    continue
                if parent.canonical_id == "resources/README" and child.node_kind == "topic":
                    row_count = len(_resource_link_rows(texts[child.relative_path]))
                    link_count += row_count if row_count <= FLATTEN_GROUP_MAX_CHILDREN else 1
                else:
                    link_count += 1
            if link_count > FLATTEN_GROUP_MAX_CHILDREN and book_map_kind != "book-root":
                issues.append(
                    f"{parent.relative_path}: navigation group {group.label!r} has "
                    f"{link_count} links; maximum is {FLATTEN_GROUP_MAX_CHILDREN}"
                )
    return issues


def _has_direct_reader_content(text: str) -> bool:
    """Distinguish a source-owning section page from one navigation shell.

    A section heading can own introductory source prose or code before its
    numbered subsections.  Such a page is not a wrapper merely because its
    title repeats the H2 topic on the chapter map.  Managed projections,
    previous/next links, headings, and link-only lists do not establish source
    ownership by themselves.
    """

    _, body = split_markdown(text)
    authored = strip_generated_wiki_views(body)
    authored = re.sub(r"(?m)^#\s+.+$", "", authored, count=1)
    authored = re.sub(
        r"(?ms)^##\s+(?:이전과 다음|이전·다음)\s*$.*?(?=^##\s|\Z)",
        "",
        authored,
    )
    if re.search(r"(?ms)```[^\n]*\n.+?\n```", authored):
        return True
    if re.search(r"(?m)^\|.+\|\s*$", authored):
        return True
    authored = re.sub(r"!?\[\[[^\]]+\]\]", "", authored)
    authored = re.sub(r"(?m)^\s{0,3}#{1,6}\s+.*$", "", authored)
    authored = re.sub(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", authored)
    semantic = re.sub(r"[^0-9A-Za-z가-힣]+", "", authored)
    return len(semantic) >= 20


def _book_map_authored_body(text: str) -> str:
    """Return reader prose left on a generated book map after owned views are removed."""

    _, body = split_markdown(text)
    authored = strip_generated_wiki_views(body)
    authored = re.sub(r"(?m)^#\s+.+$", "", authored, count=1)
    authored = re.sub(
        r"(?ms)^##\s+(?:이전과 다음|이전·다음)\s*$.*?(?=^##\s|\Z)",
        "",
        authored,
    )
    authored = re.sub(r"<!--.*?-->", "", authored, flags=re.DOTALL)
    return authored.strip()


def _format_sequence(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _has_ancestor(node: WikiTreeNode, ancestor: str, by_path: dict[str, WikiTreeNode]) -> bool:
    current = node
    while current.parent_path is not None:
        if current.parent_path == ancestor:
            return True
        parent = by_path.get(current.parent_path)
        if parent is None:
            return False
        current = parent
    return False


def _book_link_index_issues(node: WikiTreeNode, text: str) -> list[str]:
    """Require a book-shaped contents list without synthetic study horizons."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = strip_learning_checkpoint(body)
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    if re.search(r"(?m)^#{2,3}\s+(?:책 전체 학습 해상도|2주|1달|5달)(?:\s|·|$)", body):
        return [
            f"{node.relative_path}: book page must follow the verified table of contents "
            "without 2주·1달·5달 study horizons"
        ]
    # Once source-owned groups exist, Core owns the visible map. The authored
    # body may contain an introduction, but direct chapter links are rejected
    # separately as duplicate projection input.
    if node.navigation_groups:
        return []
    rights_safe_toc = node.knowledge_state == "목차 확인됨"
    group_open = False
    group_has_link = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and re.fullmatch(r"##\s+.+", stripped):
            if group_open and not group_has_link:
                return [
                    f"{node.relative_path}: every book contents group must contain at least "
                    "one direct hyperlink row"
                ]
            group_open = True
            group_has_link = False
            continue
        if re.fullmatch(r"-\s+\[\[[^\]]+\]\]", stripped):
            target = stripped.split("[[", 1)[1].split("]]", 1)[0].split("|", 1)[0]
            if target.startswith("#") or "#" in target:
                return [
                    f"{node.relative_path}: book contents must link to chapter pages, not anchors"
                ]
            if not group_open:
                return [f"{node.relative_path}: book contents links require an H2 topic heading"]
            if indent != 0:
                return [f"{node.relative_path}: book contents links must be flat bullets below H2"]
            group_has_link = True
            continue
        if rights_safe_toc and re.fullmatch(r"-\s+[^\[].*", stripped):
            if not group_open:
                return [f"{node.relative_path}: book contents rows require an H2 topic heading"]
            if indent != 0:
                return [f"{node.relative_path}: book contents rows must be flat bullets below H2"]
            group_has_link = True
            continue
        return [
            f"{node.relative_path}: book page body must contain only headings and hyperlink rows"
        ]
    if group_open and not group_has_link:
        return [
            f"{node.relative_path}: every book contents group must contain at least one "
            "direct hyperlink row"
        ]
    return []


def _resource_link_index_issues(node: WikiTreeNode, text: str) -> list[str]:
    """Require resource pages to contain links, optionally under one text keyword level."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    group_open = False
    group_has_link = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0 and re.fullmatch(r"-\s+[^\[\n][^\n]*", stripped):
            if group_open and not group_has_link:
                return [
                    f"{node.relative_path}: every resource keyword must contain at least "
                    "one indented hyperlink row"
                ]
            group_open = True
            group_has_link = False
            continue
        is_wikilink = re.fullmatch(r"-\s+\[\[[^\]]+\]\]", stripped) is not None
        is_web_link = re.fullmatch(r"-\s+\[[^\]]+\]\(https://[^\s)]+\)", stripped) is not None
        if (is_wikilink or is_web_link) and indent == 0:
            if group_open and not group_has_link:
                return [
                    f"{node.relative_path}: every resource keyword must contain at least "
                    "one indented hyperlink row"
                ]
            group_open = False
            group_has_link = False
            continue
        if (is_wikilink or is_web_link) and indent > 0 and group_open:
            group_has_link = True
            continue
        return [
            f"{node.relative_path}: resource topic body must contain only hyperlink rows "
            "or one text keyword level"
        ]
    if group_open and not group_has_link:
        return [
            f"{node.relative_path}: every resource keyword must contain at least one "
            "indented hyperlink row"
        ]
    return []


def _resource_link_rows(text: str) -> tuple[str, ...]:
    """Return validated resource rows for projection under a topic label."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return tuple(
        line.strip()
        for line in body.splitlines()
        if line.strip() and line.strip() != "## 하위 키워드"
    )


def _entity_root_issues(
    node: WikiTreeNode,
    text: str,
    direct: tuple[WikiTreeNode, ...],
    texts: dict[str, str],
) -> list[str]:
    """Keep current knowledge and a readable chronology on the entity root.

    A separate history child used to be mandatory.  That split made people and
    project pages harder to scan and produced navigation-only documents.  The
    canonical entity now owns its dated history inline; focused children remain
    valid only when they have an independent subject, not merely because they
    contain dates.
    """

    _, body = split_markdown(strip_generated_wiki_views(text))
    semantic = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    semantic = re.sub(r"<!--.*?-->", "", semantic, flags=re.DOTALL).strip()
    issues: list[str] = []
    if not any(
        line.strip() and not line.lstrip().startswith("#") for line in semantic.splitlines()
    ):
        issues.append(f"{node.relative_path}: entity root must contain current subject knowledge")
    return issues


def _cycle_and_reachability_issues(nodes: list[WikiTreeNode]) -> list[str]:
    by_path = {node.relative_path: node for node in nodes}
    root = "wiki/README.md"
    issues: list[str] = []
    if root not in by_path:
        return ["wiki/README.md: Wiki root is missing"]
    for node in nodes:
        if node.relative_path == root:
            continue
        seen = {node.relative_path}
        current = node
        while current.parent_path is not None:
            if current.parent_path in seen:
                issues.append(f"{node.relative_path}: parent cycle detected")
                break
            seen.add(current.parent_path)
            parent = by_path.get(current.parent_path)
            if parent is None:
                break
            if parent.relative_path == root:
                break
            current = parent
        else:
            issues.append(f"{node.relative_path}: parent chain does not reach wiki/README.md")
    return issues


def _children_by_parent(nodes: tuple[WikiTreeNode, ...]) -> dict[str, tuple[WikiTreeNode, ...]]:
    grouped: dict[str, list[WikiTreeNode]] = {}
    for node in nodes:
        if node.parent_path is not None:
            grouped.setdefault(node.parent_path, []).append(node)
    return {
        parent: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.sequence is None,
                    item.sequence if item.sequence is not None else 0,
                    item.title.casefold(),
                ),
            )
        )
        for parent, items in grouped.items()
    }


def _related_neighbors(
    nodes: tuple[WikiTreeNode, ...], texts: dict[str, str]
) -> dict[str, tuple[WikiTreeNode, ...]]:
    """Derive entity-related pages from canonical links and person references."""

    by_path = {node.relative_path: node for node in nodes}
    by_stem: dict[str, WikiTreeNode | None] = {}
    for node in nodes:
        stem = Path(node.relative_path).stem.casefold()
        by_stem[stem] = node if stem not in by_stem else None
    links: dict[str, set[str]] = {node.relative_path: set() for node in nodes}
    for node in nodes:
        metadata, body = split_markdown(strip_generated_wiki_views(texts[node.relative_path]))
        values: list[str] = []
        for field in ("related_to", "people"):
            raw = metadata.get(field, [])
            if isinstance(raw, list):
                values.extend(value for value in raw if isinstance(value, str))
        values.extend(match.group(0) for match in _BODY_WIKILINK_RE.finditer(body))
        for value in values:
            match = _BODY_WIKILINK_RE.fullmatch(value.strip())
            if match is None:
                continue
            target = match.group("target").strip()
            if not target.endswith(".md"):
                target += ".md"
            target = Path(target).as_posix()
            candidate = by_path.get(target)
            if candidate is None and "/" not in target.removesuffix(".md"):
                candidate = by_stem.get(Path(target).stem.casefold())
            if (
                candidate is None
                or candidate.relative_path == node.relative_path
                or node.node_kind in {"root", "hub"}
                or candidate.node_kind in {"root", "hub"}
            ):
                continue
            links[node.relative_path].add(candidate.relative_path)
            links[candidate.relative_path].add(node.relative_path)
    return {
        relative: tuple(by_path[target] for target in sorted(targets))
        for relative, targets in links.items()
        if targets
    }


def _descendants(
    relative_path: str, children: dict[str, tuple[WikiTreeNode, ...]]
) -> tuple[WikiTreeNode, ...]:
    result: list[WikiTreeNode] = []
    stack = list(reversed(children.get(relative_path, ())))
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(reversed(children.get(node.relative_path, ())))
    return tuple(result)


def _render_subtree(
    relative_path: str,
    children: dict[str, tuple[WikiTreeNode, ...]],
    *,
    depth: int = 0,
) -> list[str]:
    rows: list[str] = []
    for child in children.get(relative_path, ()):
        prefix = "  " * depth + "- "
        sequence = f"{child.sequence:g}. " if child.sequence is not None else ""
        rows.append(
            f"{prefix}{sequence}"
            f"[[{_without_suffix(child.relative_path)}|{child.title}]] — {child.summary}"
        )
        rows.extend(_render_subtree(child.relative_path, children, depth=depth + 1))
    return rows


def _render_keyword_link(
    node: WikiTreeNode, *, include_sequence: bool = True, label: str | None = None
) -> str:
    sequence = f"{node.sequence:g}. " if include_sequence and node.sequence is not None else ""
    display_label = label or _compact_keyword_label(node.keywords[0])
    period = _temporal_period(node)
    suffix = f" · {period}" if period else ""
    return f"- {sequence}[[{_without_suffix(node.relative_path)}|{display_label}]]{suffix}"


def _temporal_period(node: WikiTreeNode) -> str:
    if node.occurred_on is not None:
        return node.occurred_on.isoformat()
    if node.started_on is not None and node.ended_on is not None:
        return f"{node.started_on.isoformat()} → {node.ended_on.isoformat()}"
    if node.started_on is not None:
        return f"{node.started_on.isoformat()} →"
    if node.ended_on is not None:
        return f"→ {node.ended_on.isoformat()}"
    return ""


def _distinct_keyword_labels(nodes: Sequence[WikiTreeNode]) -> tuple[str, ...]:
    """Keep compact labels unless siblings would become indistinguishable."""

    compact = tuple(_compact_keyword_label(node.keywords[0]) for node in nodes)
    compact_counts = Counter(label.casefold() for label in compact)
    candidates = tuple(
        node.keywords[0].split(" — ", maxsplit=1)[1].strip()
        if compact_counts[label.casefold()] > 1 and " — " in node.keywords[0]
        else label
        for node, label in zip(nodes, compact, strict=True)
    )
    candidate_counts = Counter(label.casefold() for label in candidates)
    return tuple(
        node.title if candidate_counts[label.casefold()] > 1 else label
        for node, label in zip(nodes, candidates, strict=True)
    )


def _render_navigation_children(
    parent: WikiTreeNode,
    direct: tuple[WikiTreeNode, ...],
    children: dict[str, tuple[WikiTreeNode, ...]],
    texts: dict[str, str],
    *,
    include_sequence: bool,
    book_map_kind: str | None = None,
) -> tuple[str, ...]:
    """Render one direct-child map without exposing grandchildren."""

    if parent.navigation_groups:
        return _render_explicit_navigation_groups(
            parent,
            direct,
            children,
            texts,
            include_sequence=include_sequence,
            topic_headings=book_map_kind is not None,
            suppress_owner_heading=book_map_kind == "section-root",
        )

    rows: list[str] = []
    labels = dict(
        zip(
            (child.relative_path for child in direct),
            _distinct_keyword_labels(direct),
            strict=True,
        )
    )
    for child in direct:
        grouped = children.get(child.relative_path, ())
        flatten = (
            parent.node_kind == "hub"
            and child.node_kind == "hub"
            and 0 < len(grouped) <= FLATTEN_GROUP_MAX_CHILDREN
        )
        if not flatten:
            rows.append(
                _render_keyword_link(
                    child,
                    include_sequence=include_sequence,
                    label=labels[child.relative_path],
                )
            )
            continue
        rows.append(f"- {_compact_keyword_label(child.keywords[0])}")
        grouped_labels = _distinct_keyword_labels(grouped)
        rows.extend(
            "  " + _render_keyword_link(item, include_sequence=include_sequence, label=label)
            for item, label in zip(grouped, grouped_labels, strict=True)
        )
    return tuple(rows)


def _render_ordered_book_reader_sections(
    text: str,
    parent: WikiTreeNode,
    direct: tuple[WikiTreeNode, ...],
    texts: dict[str, str],
) -> str:
    """Interleave generated deep links between exact authored source sections."""

    sections = parent.ordered_reader_sections
    if "toc-heading" in {section.kind for section in sections}:
        direct_by_id = {child.canonical_id: child for child in direct}
        direct_labels = dict(
            zip(
                (child.canonical_id for child in direct),
                _distinct_keyword_labels(direct),
                strict=True,
            )
        )
        groups = {group.label: group for group in parent.navigation_groups}
        toc_rows = [BOOK_READER_NAVIGATION_START]
        for section in sections:
            if section.kind == "toc-heading":
                toc_rows.append(f"## {section.label}")
            else:
                toc_rows.extend(
                    _render_explicit_navigation_group(
                        parent,
                        groups[section.label],
                        direct_by_id,
                        direct_labels,
                        texts,
                        include_sequence=False,
                        topic_headings=True,
                    )
                )
        toc_rows.append(BOOK_READER_NAVIGATION_END)
        return _replace_or_insert_after_h1(
            text,
            BOOK_READER_NAVIGATION_START,
            BOOK_READER_NAVIGATION_END,
            "\n".join(toc_rows),
        )

    direct_by_id = {child.canonical_id: child for child in direct}
    direct_labels = dict(
        zip(
            (child.canonical_id for child in direct),
            _distinct_keyword_labels(direct),
            strict=True,
        )
    )
    groups = {group.label: group for group in parent.navigation_groups}
    insertions: list[tuple[int, str]] = []
    index = 0
    while index < len(sections):
        section = sections[index]
        if section.kind == "source-body":
            index += 1
            continue
        run: list[WikiOrderedReaderSection] = []
        while index < len(sections) and sections[index].kind == "navigation-group":
            run.append(sections[index])
            index += 1
        next_source = next(
            (item for item in sections[index:] if item.kind == "source-body"),
            None,
        )
        offset = (
            _exact_h2_offset(text, next_source.label)
            if next_source is not None
            else _before_source_index_offset(text)
        )
        rows: list[str] = [BOOK_READER_NAVIGATION_START]
        for item in run:
            group = groups[item.label]
            rows.extend(
                _render_explicit_navigation_group(
                    parent,
                    group,
                    direct_by_id,
                    direct_labels,
                    texts,
                    include_sequence=False,
                    topic_headings=True,
                )
            )
        rows.append(BOOK_READER_NAVIGATION_END)
        insertions.append((offset, "\n".join(rows)))

    updated = text
    for offset, block in reversed(insertions):
        before = updated[:offset].rstrip()
        after = updated[offset:].lstrip("\n")
        updated = f"{before}\n\n{block}\n\n{after}"
    return updated


def _exact_h2_offset(text: str, label: str) -> int:
    matches = list(re.finditer(rf"(?m)^## {re.escape(label)}\s*$", text))
    if len(matches) != 1:
        raise WoonError("ordered book reader source-body heading must occur exactly once: " + label)
    return matches[0].start()


def _before_source_index_offset(text: str) -> int:
    match = re.search(r"(?m)^## 원자료\s*$", text)
    return match.start() if match is not None else len(text)


def _strip_ordered_book_navigation(text: str) -> str:
    pattern = re.compile(
        rf"(?ms)\n*{re.escape(BOOK_READER_NAVIGATION_START)}\n.*?"
        rf"{re.escape(BOOK_READER_NAVIGATION_END)}\n*"
    )
    return pattern.sub("\n\n", text)


def _render_explicit_navigation_groups(
    parent: WikiTreeNode,
    direct: tuple[WikiTreeNode, ...],
    children: dict[str, tuple[WikiTreeNode, ...]],
    texts: dict[str, str],
    *,
    include_sequence: bool,
    topic_headings: bool = False,
    suppress_owner_heading: bool = False,
) -> tuple[str, ...]:
    """Render group labels without changing canonical parent relations.

    Book roots and chapter roots use source-owned H2 labels. Other Wiki maps
    retain the established two-level bullet projection.
    """

    direct_by_id = {child.canonical_id: child for child in direct}
    direct_labels = dict(
        zip(
            (child.canonical_id for child in direct),
            _distinct_keyword_labels(direct),
            strict=True,
        )
    )
    rows: list[str] = []
    for group in parent.navigation_groups:
        rows.extend(
            _render_explicit_navigation_group(
                parent,
                group,
                direct_by_id,
                direct_labels,
                texts,
                include_sequence=include_sequence,
                topic_headings=topic_headings,
                suppress_owner_heading=suppress_owner_heading,
            )
        )
    return tuple(rows)


def _render_explicit_navigation_group(
    parent: WikiTreeNode,
    group: WikiNavigationGroup,
    direct_by_id: dict[str, WikiTreeNode],
    direct_labels: dict[str, str],
    texts: dict[str, str],
    *,
    include_sequence: bool,
    topic_headings: bool,
    suppress_owner_heading: bool = False,
) -> tuple[str, ...]:
    """Render one navigation group while preserving the established projection."""

    omit_heading = (
        suppress_owner_heading
        and topic_headings
        and group.label.strip().casefold() == parent.title.strip().casefold()
    )
    rows = [] if omit_heading else [f"## {group.label}" if topic_headings else f"- {group.label}"]
    for child_id in group.child_ids:
        child = direct_by_id[child_id]
        if (
            topic_headings
            and len(group.child_ids) > 1
            and child.title.strip() == group.label.strip()
        ):
            # A source section introduction is still a canonical page, but
            # its title is already represented by the H2 topic keyword.
            # Repeating the same title as a link makes a book map look like
            # an accidental extra depth level.
            continue
        if parent.canonical_id == "resources/README" and child.node_kind == "topic":
            resource_rows = _resource_link_rows(texts[child.relative_path])
            if len(resource_rows) <= FLATTEN_GROUP_MAX_CHILDREN:
                rows.extend(row if topic_headings else f"  {row}" for row in resource_rows)
                continue
        link = _render_keyword_link(
            child,
            include_sequence=include_sequence,
            label=child.title if topic_headings else direct_labels[child_id],
        )
        rows.append(link if topic_headings else f"  {link}")
    return tuple(rows)


def _book_navigation_kind(node: WikiTreeNode, nodes: Sequence[WikiTreeNode]) -> str | None:
    """Classify every book-scoped map that owns a source TOC projection."""

    if node.entity_kind == "book":
        return "book-root"
    book_roots = tuple(
        candidate.canonical_id for candidate in nodes if candidate.entity_kind == "book"
    )
    for book_root in book_roots:
        prefix = f"{book_root}/"
        if not node.canonical_id.startswith(prefix):
            continue
        relative = node.canonical_id.removeprefix(prefix)
        if re.fullmatch(r"(?:chapter-\d+|appendix-[a-z0-9-]+)", relative):
            return "chapter-root"
        if node.navigation_groups:
            book = next(candidate for candidate in nodes if candidate.canonical_id == book_root)
            if node.parent_path == book.relative_path:
                return "section-root"
            return "nested-book-map"
    return None


def _is_root_direct_book_section(
    node: WikiTreeNode,
    nodes: Sequence[WikiTreeNode],
) -> bool:
    """Accept preserved chapter-scoped IDs under an explicit root section index.

    The canonical ID remains stable (``.../chapter-03/3-3``), while the
    canonical parent and visible owner move directly to the book root.  This is
    deliberately narrower than accepting a missing chapter page in general.
    """

    by_path = {candidate.relative_path: candidate for candidate in nodes}
    current = node
    while current.parent_path is not None:
        parent = by_path.get(current.parent_path)
        if parent is None:
            return False
        if parent.entity_kind == "book":
            chapter_match = re.match(r"^(.+/chapter-(?P<number>\d{2}))/", current.canonical_id)
            title_match = re.match(r"^(?P<number>\d+)\.(?P<section>\d+)\s+", current.title)
            if chapter_match is None or title_match is None:
                return False
            if int(chapter_match.group("number")) != int(title_match.group("number")):
                return False
            return any(
                re.match(rf"^{int(title_match.group('number'))}장(?:\s|$)", group.label)
                and current.canonical_id in group.child_ids
                for group in parent.navigation_groups
            )
        current = parent
    return False


def _managed_navigation_uses_h2(block: str) -> bool:
    """Return whether a managed map owns source topic headings itself."""

    return re.search(r"(?m)^##\s+\S", block) is not None


def _compact_keyword_label(value: str) -> str:
    label = value.split(" — ", maxsplit=1)[0].strip()
    label = re.sub(r"\s+\([A-Za-z][A-Za-z0-9 .+/#-]*\)$", "", label).strip()
    if label.endswith(" 탐색"):
        label = label.removesuffix(" 탐색").rstrip()
    return label


def _contains_wikilink_to(text: str, relative_path: str) -> bool:
    target = Path(relative_path).with_suffix("").as_posix()
    aliases = (target, Path(target).name)
    return any(
        re.search(rf"!?\[\[{re.escape(alias)}(?:[|#\]])", text) is not None for alias in aliases
    )


def _normalize_h1_spacing(text: str) -> str:
    return re.sub(r"(?m)^(# .+?)\n{3,}", r"\1\n\n", text, count=1)


def _normalize_reader_headings(text: str) -> str:
    """Replace conversation-shaped headings with durable reader semantics.

    Wiki tree refresh is the shared Core-owned projection pass for compiled,
    conversation-grown, and private source-derived pages.  Keeping this narrow
    migration here means a legacy generated page cannot permanently block the
    governance preflight before the writer gets a chance to run.
    """

    replacements = {
        "현재 이해": "핵심 정리",
        "남긴 의도": "판단 기준",
        "다음 질문": "다음 검증",
        "연결": "관련 문서",
    }
    updated = text
    for old, new in replacements.items():
        updated = re.sub(
            rf"(?m)^## {re.escape(old)}[ \t]*$",
            f"## {new}",
            updated,
        )
    return updated


def _visible_navigation_body(text: str) -> str:
    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return body.strip()


def _replace_or_insert_after_h1(text: str, start: str, end: str, block: str) -> str:
    count_start, count_end = text.count(start), text.count(end)
    if count_start != count_end or count_start > 1:
        raise WoonError(f"malformed managed markers: {start}")
    if count_start:
        return re.sub(
            re.escape(start) + r".*?" + re.escape(end), block, text, count=1, flags=re.DOTALL
        )
    h1 = re.search(r"(?m)^# .+?\s*$", text)
    if h1 is None:
        raise WoonError("Wiki document requires one H1")
    return text[: h1.end()].rstrip() + "\n\n" + block + "\n\n" + text[h1.end() :].lstrip()


def _replace_or_append_section(text: str, heading: str, start: str, end: str, block: str) -> str:
    count_start, count_end = text.count(start), text.count(end)
    if count_start != count_end or count_start > 1:
        raise WoonError(f"malformed managed markers: {start}")
    if count_start:
        return re.sub(
            re.escape(start) + r".*?" + re.escape(end), block, text, count=1, flags=re.DOTALL
        )
    empty_heading = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n\s*(?=^## |\Z)")
    if empty_heading.search(text):
        return empty_heading.sub(f"## {heading}\n\n{block}\n", text, count=1)
    return text.rstrip() + f"\n\n## {heading}\n\n{block}\n"


def _normalize_latest_heading(text: str, heading: str) -> str:
    return re.sub(
        rf"(?m)^## 최신 (?:하위|관련) 문서\s*$\n(?=\s*{re.escape(LATEST_START)})",
        f"## {heading}\n",
        text,
        count=1,
    )


def _strip_section(text: str, heading: str, start: str, end: str) -> str:
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise WoonError(f"malformed managed markers: {start}")
    if start not in text:
        return re.sub(
            rf"(?ms)^## {re.escape(heading)}\s*\n\s*(?=^## |\Z)",
            "",
            text,
            count=1,
        )
    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\s*\n+{re.escape(start)}.*?{re.escape(end)}\s*\n?"
    )
    updated, count = pattern.subn("", text, count=1)
    if count == 0:
        updated = _strip_marker_block(text, start, end)
    return updated


def _strip_marker_block(text: str, start: str, end: str) -> str:
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise WoonError(f"malformed managed markers: {start}")
    return re.sub(
        r"\n?" + re.escape(start) + r".*?" + re.escape(end) + r"\n?",
        "\n",
        text,
        count=1,
        flags=re.DOTALL,
    )


def _optional_marker_block(text: str, start: str, end: str) -> str | None:
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise WoonError(f"malformed managed markers: {start}")
    if start not in text:
        return None
    match = re.search(re.escape(start) + r".*?" + re.escape(end), text, flags=re.DOTALL)
    return match.group(0) if match is not None else None


def _optional_marker_with_trailing_space(text: str, start: str, end: str) -> str | None:
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise WoonError(f"malformed managed markers: {start}")
    if start not in text:
        return None
    match = re.search(
        re.escape(start) + r".*?" + re.escape(end) + r"\s*",
        text,
        flags=re.DOTALL,
    )
    return match.group(0) if match is not None else None


def _without_suffix(relative_path: str) -> str:
    return Path(relative_path).with_suffix("").as_posix()
