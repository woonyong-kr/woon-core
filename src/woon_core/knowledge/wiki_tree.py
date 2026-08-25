"""Canonical parent tree and generated views for the Woon Wiki.

The Markdown page remains the only human-readable source of truth.  This
module derives navigation blocks from page metadata and never stores a second
map.  All preparation functions are read-only; callers apply the complete
batch atomically after reviewing the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write

OVERVIEW_START = "<!-- woon-wiki-overview:start -->"
OVERVIEW_END = "<!-- woon-wiki-overview:end -->"
CHILDREN_START = "<!-- woon-wiki-children:start -->"
CHILDREN_END = "<!-- woon-wiki-children:end -->"
LATEST_START = "<!-- woon-wiki-latest:start -->"
LATEST_END = "<!-- woon-wiki-latest:end -->"
TIMELINE_START = "<!-- woon-wiki-timeline:start -->"
TIMELINE_END = "<!-- woon-wiki-timeline:end -->"

NODE_KINDS = {"root", "hub", "topic", "entity", "detail", "decision"}
VIEW_MODES = {"tree", "linear", "project", "topic-timeline", "article"}
TREE_VIEW_KINDS = {"root", "hub", "topic", "entity"}
FLATTEN_GROUP_MAX_CHILDREN = 20
LEGACY_TREE_FIELDS = {"parent_topics", "parent_moc", "map_role", "mindmap_role"}
_WIKILINK_RE = re.compile(r"^\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]$")
_BODY_WIKILINK_RE = re.compile(r"!?\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
WIKI_SOURCE_ARCHIVE_PARTS = ("private", "_sources")


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


def prepare_wiki_tree_refresh(vault: Path) -> WikiTreeReport:
    """Regenerate compact navigation and latest blocks from canonical metadata."""

    root = vault.expanduser().resolve()
    nodes, texts, issues = load_wiki_tree(root)
    if issues:
        return WikiTreeReport(len(nodes), 0, {}, issues)
    children = _children_by_parent(nodes)
    related = _related_neighbors(nodes, texts)
    pages: dict[Path, bytes] = {}
    changed = 0
    by_path = {node.relative_path: node for node in nodes}
    for node in nodes:
        refreshed = render_wiki_tree_view(
            texts[node.relative_path],
            node=node,
            nodes=by_path,
            children=children,
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
    except Exception:
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
        if canonical_id:
            key = canonical_id.casefold()
            if key in canonical:
                issues.append(
                    f"{relative}: duplicate canonical_id with {canonical[key]}: {canonical_id}"
                )
            canonical[key] = relative
        for identity in (title, *aliases, *keywords):
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
            )
        )
    by_path = {node.relative_path: node for node in nodes}
    for node in nodes:
        if node.parent_path is not None and node.parent_path not in by_path:
            issues.append(f"{node.relative_path}: parent is missing: {node.parent_path}")
    issues.extend(_cycle_and_reachability_issues(nodes))
    issues.extend(_domain_tree_issues(nodes, texts))
    return tuple(nodes), texts, tuple(dict.fromkeys(issues))


def render_wiki_tree_view(
    text: str,
    *,
    node: WikiTreeNode,
    nodes: dict[str, WikiTreeNode],
    children: dict[str, tuple[WikiTreeNode, ...]],
    related: dict[str, tuple[WikiTreeNode, ...]] | None = None,
) -> str:
    """Render all Core-owned navigation blocks for one already validated node."""

    descendants = _descendants(node.relative_path, children)
    direct = children.get(node.relative_path, ())
    parent = nodes.get(node.parent_path) if node.parent_path else None
    if node.node_kind in {"root", "hub", "entity"} or is_compact_link_page(node):
        # Navigation pages are intentionally link-only. Metadata and summaries
        # remain in frontmatter and the destination pages instead of being
        # repeated in the index that a person scans.
        updated = _normalize_h1_spacing(_strip_marker_block(text, OVERVIEW_START, OVERVIEW_END))
    else:
        overview_rows = [
            OVERVIEW_START,
            "> [!info] 한눈에 보기",
            f"> **한 줄 정리** · {node.summary}",
            f"> **종류** · {node.node_kind} · {node.view_mode}",
            f"> **대표 키워드** · {' · '.join(node.keywords)}",
            f"> **상태** · {node.knowledge_state}",
        ]
        if parent is not None:
            overview_rows.append(
                f"> **상위 키워드** · [[{_without_suffix(parent.relative_path)}|{parent.title}]]"
            )
        overview_rows.append(f"> **하위 문서** · 직접 {len(direct)}개")
        overview_rows.append(OVERVIEW_END)
        updated = _replace_or_insert_after_h1(
            text, OVERVIEW_START, OVERVIEW_END, "\n".join(overview_rows)
        )

    if node.entity_kind == "book":
        updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
        updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
        updated = _strip_section(updated, "최신 관련 문서", LATEST_START, LATEST_END)
        return updated.rstrip() + "\n"

    show_tree = node.node_kind in TREE_VIEW_KINDS and bool(descendants)
    if show_tree:
        child_rows = [
            CHILDREN_START,
            *_render_navigation_children(
                node,
                direct,
                children,
                include_sequence=node.view_mode == "linear",
            ),
            CHILDREN_END,
        ]
        updated = _replace_or_append_section(
            updated, "하위 키워드", CHILDREN_START, CHILDREN_END, "\n".join(child_rows)
        )
        if node.node_kind in {"root", "hub"}:
            updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
        else:
            direct_paths = {item.relative_path for item in direct}
            latest = sorted(
                (item for item in descendants if item.relative_path not in direct_paths),
                key=lambda item: (-item.updated.toordinal(), item.title),
            )[:10]
            if latest:
                latest_rows = [
                    LATEST_START,
                    *(_render_keyword_link(item, include_sequence=False) for item in latest),
                    LATEST_END,
                ]
                updated = _replace_or_append_section(
                    updated,
                    "최신 하위 문서",
                    LATEST_START,
                    LATEST_END,
                    "\n".join(latest_rows),
                )
                updated = _normalize_latest_heading(updated, "최신 하위 문서")
            else:
                updated = _strip_section(updated, "최신 하위 문서", LATEST_START, LATEST_END)
    elif node.node_kind == "entity" and related and related.get(node.relative_path):
        updated = _strip_section(updated, "하위 키워드", CHILDREN_START, CHILDREN_END)
        latest = sorted(
            related[node.relative_path],
            key=lambda item: (-item.updated.toordinal(), item.title.casefold()),
        )[:10]
        latest_rows = [
            LATEST_START,
            *(_render_keyword_link(item, include_sequence=False) for item in latest),
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
    return updated.rstrip() + "\n"


def strip_generated_wiki_views(text: str) -> str:
    """Remove derived view blocks before computing a compiler projection."""

    updated = _strip_section(text, "하위 키워드", CHILDREN_START, CHILDREN_END)
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
            updated = _replace_or_append_section(updated, heading, start, end, block)
    latest = _optional_marker_block(existing, LATEST_START, LATEST_END)
    if latest is not None:
        heading = "최신 관련 문서" if "## 최신 관련 문서" in existing else "최신 하위 문서"
        updated = _replace_or_append_section(updated, heading, LATEST_START, LATEST_END, latest)
        updated = _normalize_latest_heading(updated, heading)
    return updated.rstrip() + "\n"


def split_markdown(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?", text, flags=re.DOTALL)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")
    try:
        metadata = yaml.safe_load(match.group("yaml")) or {}
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
            if keyword.node_kind != "topic":
                issues.append(
                    f"{keyword.relative_path}: direct children of resources must be keyword topics"
                )
            if children.get(keyword.relative_path):
                issues.append(
                    f"{keyword.relative_path}: resource topics must link raw sources directly "
                    "and must not own Wiki child entities"
                )
            issues.extend(_resource_link_index_issues(keyword, texts[keyword.relative_path]))

    for node in nodes:
        if node.node_kind == "entity" and node.entity_kind not in {"book", "resource"}:
            issues.extend(_entity_link_index_issues(node, texts[node.relative_path]))
        if node.entity_kind == "book":
            if not _has_ancestor(node, books_path, by_path):
                issues.append(f"{node.relative_path}: book entity must stay below the books root")
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
    """Require book landing pages to be headings and links, not repeated prose."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    issues: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("##"):
            continue
        if re.fullmatch(r"-\s+\[\[[^\]]+\]\]", stripped):
            continue
        issues.append(
            f"{node.relative_path}: book page body must contain only headings and hyperlink rows"
        )
        break
    return issues


def _resource_link_index_issues(node: WikiTreeNode, text: str) -> list[str]:
    """Require resource keyword pages to contain hyperlinks and no explanation."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.fullmatch(r"-\s+\[\[[^\]]+\]\]", stripped):
            continue
        if re.fullmatch(r"-\s+\[[^\]]+\]\(https://[^\s)]+\)", stripped):
            continue
        return [f"{node.relative_path}: resource topic body must contain only hyperlink rows"]
    return []


def _entity_link_index_issues(node: WikiTreeNode, text: str) -> list[str]:
    """Keep every entity landing page as a compact keyword-link index."""

    _, body = split_markdown(strip_generated_wiki_views(text))
    body = re.sub(r"(?m)^# .+?\s*$", "", body, count=1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("##"):
            continue
        if re.fullmatch(r"-\s+\[\[[^\]]+\]\]", stripped):
            continue
        return [
            f"{node.relative_path}: entity landing page must contain only "
            "headings and hyperlink rows"
        ]
    return []


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


def _render_keyword_link(node: WikiTreeNode, *, include_sequence: bool = True) -> str:
    sequence = f"{node.sequence:g}. " if include_sequence and node.sequence is not None else ""
    label = _compact_keyword_label(node.keywords[0])
    return f"- {sequence}[[{_without_suffix(node.relative_path)}|{label}]]"


def _render_navigation_children(
    parent: WikiTreeNode,
    direct: tuple[WikiTreeNode, ...],
    children: dict[str, tuple[WikiTreeNode, ...]],
    *,
    include_sequence: bool,
) -> tuple[str, ...]:
    """Flatten small, link-only grouping hubs into one scannable index."""

    rows: list[str] = []
    for child in direct:
        grouped = children.get(child.relative_path, ())
        flatten = (
            parent.node_kind == "hub"
            and child.node_kind == "hub"
            and 0 < len(grouped) <= FLATTEN_GROUP_MAX_CHILDREN
        )
        if not flatten:
            rows.append(_render_keyword_link(child, include_sequence=include_sequence))
            continue
        rows.append(f"- {_compact_keyword_label(child.keywords[0])}")
        rows.extend(
            "  " + _render_keyword_link(item, include_sequence=include_sequence) for item in grouped
        )
    return tuple(rows)


def _compact_keyword_label(value: str) -> str:
    label = value.split(" — ", maxsplit=1)[0].strip()
    label = re.sub(r"\s+\([A-Za-z][A-Za-z0-9 .+/#-]*\)$", "", label).strip()
    if label.endswith(" 탐색"):
        label = label.removesuffix(" 탐색").rstrip()
    return label


def _normalize_h1_spacing(text: str) -> str:
    return re.sub(r"(?m)^(# .+?)\n{3,}", r"\1\n\n", text, count=1)


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
        return text
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
