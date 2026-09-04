"""Deterministic updates for the single human-readable Wiki.

The Wiki document is the only canonical knowledge page for one subject.  A
conversation, project, content item, person review, calendar event, and the
evidence compiler may contribute different *facets* or sections, but they must
not create a second page merely because the contribution has a different
verification state.

This module never accepts a transcript or a raw source body.  It receives a
small, already-sanitized delta and edits only explicit managed sections.  Free
form prose and compiler-owned prose outside those sections are preserved.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import (
    iter_wiki_pages,
    preserve_generated_wiki_views,
    split_markdown,
    strip_generated_wiki_views,
)
from woon_core.knowledge.yaml_cache import load_yaml_text

WIKI_ROOT = "wiki"
WIKI_PERSONAL_ROOT = "wiki/personal"
WIKI_NODES_ROOT = "wiki/nodes"
WIKI_PRIVATE_ROOT = "wiki/private"
WIKI_CURRENT_START = "<!-- woon-wiki-current:start -->"
WIKI_CURRENT_END = "<!-- woon-wiki-current:end -->"
WIKI_TIMELINE_START = "<!-- woon-wiki-timeline:start -->"
WIKI_TIMELINE_END = "<!-- woon-wiki-timeline:end -->"
WIKI_NAVIGATION_START = "<!-- woon-wiki-navigation:start -->"
WIKI_NAVIGATION_END = "<!-- woon-wiki-navigation:end -->"
WIKI_OVERVIEW_START = "<!-- woon-wiki-overview:start -->"
WIKI_OVERVIEW_END = "<!-- woon-wiki-overview:end -->"
WIKI_CURRENT_HEADING = "핵심 정리"
WIKI_INTENT_HEADING = "판단 기준"
WIKI_NEXT_HEADING = "다음 검증"
WIKI_RELATED_HEADING = "관련 문서"
INTERVIEW_CURRENT_START = "<!-- woon-interview-current:start -->"
INTERVIEW_CURRENT_END = "<!-- woon-interview-current:end -->"
INTERVIEW_HISTORY_START = "<!-- woon-interview-history:start -->"
INTERVIEW_HISTORY_END = "<!-- woon-interview-history:end -->"
INTERVIEW_ARCHIVE_START = "<!-- woon-interview-archive:start -->"
INTERVIEW_ARCHIVE_END = "<!-- woon-interview-archive:end -->"

ALLOWED_FACETS = {
    "개념",
    "프로젝트",
    "리소스",
    "인물",
    "커리어",
    "학습",
    "생활",
}
FACET_ORDER = ("개념", "프로젝트", "리소스", "인물", "커리어", "학습", "생활")
FACET_LABELS = frozenset(FACET_ORDER)
COMPILED_SECTION_ROOTS = {
    "ai",
    "algorithm",
    "backend",
    "common",
    "database",
    "network",
    "os",
    "security",
    "tools",
}
ALLOWED_KNOWLEDGE_STATES = {
    "생각 중",
    "확인 필요",
    "근거 확인됨",
    "오래됨",
    "폐기됨",
}
ALLOWED_STATE_AUTHORITIES = {"conversation", "evidence-compiler", "curation", "user"}
_UNRESOLVED_FRONTMATTER = object()

_FILE_STEM_RE = re.compile(r"[^0-9A-Za-z가-힣_-]+")
_WIKILINK_TARGET_RE = re.compile(r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]+)?(?:\|[^]]+)?]]")
_MIGRATION_TIMELINE_RE = re.compile(
    r"^- \d{4}-\d{2}-\d{2} · 변경 — 기존 문서를 단일 (?:Woon )?Wiki 정본 계약으로 전환$"
)


@dataclass(frozen=True, slots=True)
class InterviewAnswerRevision:
    """One minimized interview answer revision for a stable question identity."""

    question: str
    answer: str | None
    context: str | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    job_variants: tuple[str, ...] = ()
    change_reason: str = "답변을 새로 정리했다."
    quality_assessment: str | None = None
    source_label: str | None = None
    promote_current: bool = True


@dataclass(frozen=True, slots=True)
class WikiDelta:
    """One minimized update to a single subject document."""

    title: str
    summary: str
    facets: tuple[str, ...]
    knowledge_state: str
    day: date
    event_kind: str = "실행"
    intent: str | None = None
    next_question: str | None = None
    related_documents: tuple[str, ...] = ()
    content_kind: str | None = None
    creators: tuple[str, ...] = ()
    official_url: str | None = None
    project_id: str | None = None
    project_status: str | None = None
    objective: str | None = None
    materials: tuple[str, ...] = ()
    wiki_subject_path: str | None = None
    parent: str | None = None
    keywords: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    central_question: str | None = None
    node_kind: str = "topic"
    view_mode: str = "tree"
    entity_kind: str | None = None
    entity_section: str | None = None
    lifecycle_status: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    occurred_on: date | None = None
    sequence: float | None = None
    interview_tracks: tuple[str, ...] = ()
    question_topic: str | None = None
    interview_answer: InterviewAnswerRevision | None = None
    state_authority: str = "conversation"


@dataclass(frozen=True, slots=True)
class WikiMigrationReport:
    """Prepared corpus-wide normalization without performing writes."""

    document_count: int
    changed_count: int
    pages: dict[Path, bytes]


def apply_prepared_wiki_pages(vault: Path, pages: dict[Path, bytes]) -> tuple[Path, ...]:
    """Apply a fully prepared Wiki batch and roll every page back on failure."""

    root = vault.expanduser().resolve()
    wiki_root = (root / WIKI_ROOT).resolve()
    snapshots: list[tuple[Path, bytes | None, int]] = []
    normalized: list[tuple[Path, bytes, int]] = []
    for path, content in sorted(pages.items(), key=lambda item: item[0].as_posix()):
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(wiki_root) or resolved.suffix != ".md":
            raise WoonError("Wiki batch may write only wiki/**/*.md")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WoonError("Wiki batch must contain UTF-8 Markdown") from error
        previous = resolved.read_bytes() if resolved.is_file() else None
        mode = (resolved.stat().st_mode & 0o777) if resolved.exists() else 0o644
        snapshots.append((resolved, previous, mode))
        normalized.append((resolved, content, mode))

    try:
        for path, content, mode in normalized:
            atomic_write(path, content, mode=mode)
    except Exception:
        for path, previous, mode in reversed(snapshots):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous, mode=mode)
        raise
    return tuple(path for path, _, _ in normalized)


def wiki_relative_path(title: str) -> str:
    """Return the deterministic path used when no existing subject matches."""

    return f"{WIKI_NODES_ROOT}/{_slug(title)}.md"


def resolve_wiki_path(vault: Path, title: str) -> Path:
    """Resolve exactly one existing Wiki identity by title or choose a new path."""

    root = vault.expanduser().resolve()
    wiki_root = root / WIKI_ROOT
    matches: list[Path] = []
    if wiki_root.is_dir():
        wanted = title.strip().casefold()
        for path in iter_wiki_pages(wiki_root):
            if _frontmatter_text(path.read_text(encoding="utf-8"), "title").casefold() == wanted:
                matches.append(path)
    if len(matches) > 1:
        raise WoonError(f"Wiki title resolves to multiple documents: {title.strip()}")
    if matches:
        return matches[0]
    return root / wiki_relative_path(title)


def _delta_path(vault: Path, delta: WikiDelta) -> Path:
    if delta.wiki_subject_path is None:
        return resolve_wiki_path(vault, delta.title)
    candidate = Path(delta.wiki_subject_path)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.suffix != ".md"
        or not candidate.as_posix().startswith("wiki/")
        or candidate.parts[:3] == ("wiki", "private", "_sources")
    ):
        raise WoonError("Wiki delta subject path must target a canonical wiki/**/*.md page")
    return (vault / candidate).resolve()


def prepare_wiki_pages(vault: Path, deltas: tuple[WikiDelta, ...]) -> dict[Path, bytes]:
    """Return all Wiki writes for a batch without mutating the Vault."""

    root = vault.expanduser().resolve()
    resolved: list[tuple[Path, WikiDelta]] = []
    for delta in deltas:
        path = _delta_path(root, delta)
        if delta.node_kind == "entity" and delta.entity_kind != "book":
            resolved.extend(_expanded_entity_deltas(root, path, delta))
        else:
            resolved.append((path, delta))
    resolved = list(_assign_missing_sibling_sequences(root, tuple(resolved)))
    planned_paths = {path.relative_to(root).as_posix() for path, _ in resolved}
    grouped: dict[Path, list[WikiDelta]] = {}
    for path, delta in resolved:
        _validate_delta(root, delta, planned_paths=planned_paths)
        grouped.setdefault(path, []).append(delta)

    pages: dict[Path, bytes] = {}
    for path, subject_deltas in grouped.items():
        titles = {item.title.strip() for item in subject_deltas}
        if len(titles) != 1:
            raise WoonError("Wiki identity resolved conflicting titles in one run")
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        for delta in subject_deltas:
            text = _merge_delta(text, delta)
        pages[path] = text.encode("utf-8")
    return pages


def _assign_missing_sibling_sequences(
    root: Path, resolved: tuple[tuple[Path, WikiDelta], ...]
) -> tuple[tuple[Path, WikiDelta], ...]:
    """Give newly created siblings a stable order without guessing semantic stages.

    Explicit sequence values always win.  Dense parents with navigation groups
    still fail the tree audit when a caller does not place the child in a stage;
    this helper only prevents a new ungrouped sibling from becoming unordered.
    """

    next_by_parent: dict[str, int] = {}
    for page in iter_wiki_pages(root / "wiki"):
        metadata, _ = split_markdown(page.read_text(encoding="utf-8"))
        parent = str(metadata.get("parent", "")).strip()
        if not parent:
            continue
        target = _wikilink_page_path(parent)
        sequence = metadata.get("sequence")
        if target is None or not isinstance(sequence, (int, float)) or sequence >= 999:
            continue
        next_by_parent[target] = max(next_by_parent.get(target, 0), int(sequence))

    assigned: list[tuple[Path, WikiDelta]] = []
    for path, delta in resolved:
        if delta.sequence is not None or delta.parent is None:
            assigned.append((path, delta))
            continue
        target = _wikilink_page_path(delta.parent)
        if target is None:
            assigned.append((path, delta))
            continue
        next_sequence = next_by_parent.get(target, 0) + 1
        next_by_parent[target] = next_sequence
        assigned.append((path, replace(delta, sequence=float(next_sequence))))
    return tuple(assigned)


def _wikilink_page_path(value: str) -> str | None:
    match = re.fullmatch(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", value.strip())
    if match is None:
        return None
    return f"{Path(match.group(1)).with_suffix('').as_posix()}.md"


def _expanded_entity_deltas(
    root: Path, path: Path, landing: WikiDelta
) -> tuple[tuple[Path, WikiDelta], ...]:
    """Keep entity knowledge on its root and route only chronology to history."""

    relative = path.relative_to(root).as_posix()
    entity_path = Path(relative)
    stem = entity_path.stem
    title = landing.title.strip()
    parent = f"[[{entity_path.with_suffix('').as_posix()}|{title}]]"
    history_title = f"{title} 히스토리"
    history_path = entity_path.with_name(f"{stem}-히스토리.md")
    history = replace(
        landing,
        title=history_title,
        summary=f"{title}의 날짜별 변경과 판단을 기록한다.",
        wiki_subject_path=history_path.as_posix(),
        parent=parent,
        keywords=(history_title,),
        aliases=(),
        central_question=None,
        node_kind="detail",
        view_mode="topic-timeline",
        entity_kind=None,
        entity_section="history",
        lifecycle_status=None,
        started_on=None,
        ended_on=None,
        occurred_on=None,
        sequence=999,
        intent=None,
        next_question=None,
        related_documents=(),
        content_kind=None,
        creators=(),
        official_url=None,
        project_id=None,
        project_status=None,
        objective=None,
        materials=(),
    )
    index = replace(
        landing,
        wiki_subject_path=relative,
    )
    return (
        (path, index),
        (root / history_path, history),
    )


def prepare_wiki_corpus_migration(vault: Path, *, migration_day: date) -> WikiMigrationReport:
    """Normalize every visible ``wiki/**`` Markdown file to one Wiki contract.

    The function is deliberately read-only.  Callers must validate the complete
    output set before applying atomic writes.  Existing prose, source-backed
    claims, links, publication flags, and compiler metadata are preserved.
    """

    root = vault.expanduser().resolve()
    wiki_root = root / WIKI_ROOT
    if not wiki_root.is_dir():
        raise WoonError("Wiki root is missing")

    pages: dict[Path, bytes] = {}
    identities: dict[str, Path] = {}
    document_count = 0
    changed_count = 0
    for path in iter_wiki_pages(wiki_root):
        relative = path.relative_to(root)
        document_count += 1
        text = path.read_text(encoding="utf-8")
        title = _frontmatter_text(text, "title")
        if not title:
            raise WoonError(f"Wiki migration requires title: {relative.as_posix()}")
        identity = relative.with_suffix("").relative_to(WIKI_ROOT).as_posix()
        identity_key = identity.casefold()
        if identity_key in identities:
            raise WoonError(f"Wiki migration found duplicate canonical_id: {identity}")
        identities[identity_key] = path

        normalized = _normalize_existing_wiki(
            text,
            canonical_id=identity,
            facets=_infer_facets(relative, text),
            knowledge_state=_infer_knowledge_state(text),
            migration_day=migration_day,
            parent=_migration_parent(root, relative),
        )
        encoded = normalized.encode("utf-8")
        pages[path] = encoded
        if encoded != path.read_bytes():
            changed_count += 1
    if document_count == 0:
        raise WoonError("Wiki migration found no documents")
    return WikiMigrationReport(document_count, changed_count, pages)


def prepare_wiki_article_view_refresh(vault: Path) -> WikiMigrationReport:
    """Refresh only the human article view without touching canonical metadata.

    Compiler-owned pages already carry receipt-bound frontmatter.  A display
    refresh must therefore leave every YAML property and all free-form prose
    byte-for-byte intact except for the Core-owned overview and timeline title.
    """

    root = vault.expanduser().resolve()
    wiki_root = root / WIKI_ROOT
    if not wiki_root.is_dir():
        raise WoonError("Wiki root is missing")
    pages: dict[Path, bytes] = {}
    changed_count = 0
    for path in iter_wiki_pages(wiki_root):
        current = path.read_text(encoding="utf-8")
        refreshed = _normalize_article_view(current)
        encoded = refreshed.encode("utf-8")
        pages[path] = encoded
        if encoded != path.read_bytes():
            changed_count += 1
    if not pages:
        raise WoonError("Wiki article view refresh found no documents")
    return WikiMigrationReport(len(pages), changed_count, pages)


def preserve_managed_context(existing: str, rendered: str) -> str:
    """Carry Woon context and its metadata across an evidence compiler render."""

    if existing and _frontmatter_text(existing, "knowledge_state") == "폐기됨":
        raise WoonError("A retired Wiki document cannot be compiled again")
    existing_frontmatter = _frontmatter_mapping(existing) if existing else {}
    if existing_frontmatter is None:
        existing_frontmatter = {}
    # A curated source can contain a previously rendered managed section.  The
    # compiler also carries the live section from ``existing`` below, so first
    # remove managed sections from the rendered body.  Otherwise every forced
    # compile duplicates the timeline/navigation blocks and the next run can no
    # longer parse them safely.
    merged = _strip_managed_section(
        rendered, "주제 연결", WIKI_NAVIGATION_START, WIKI_NAVIGATION_END
    )
    merged = _strip_managed_section(
        merged, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END
    )
    merged = _strip_managed_section(merged, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    merged = strip_generated_wiki_views(merged)
    merged_frontmatter = _frontmatter_mapping(merged)
    if merged_frontmatter is None:
        if re.match(r"\A---\n[\s\S]*?\n---", merged) is not None:
            raise WoonError("Wiki frontmatter is malformed")
        raise WoonError("Wiki document requires YAML frontmatter")
    merged = _remove_frontmatter_keys(
        merged,
        ("parent_topics", "parent_moc", "map_role", "mindmap_role"),
        frontmatter=merged_frontmatter,
    )
    for key in ("parent_topics", "parent_moc", "map_role", "mindmap_role"):
        merged_frontmatter.pop(key, None)
    merged = _upsert_frontmatter_value(merged, "type", "Wiki")
    merged_frontmatter["type"] = "Wiki"
    existing_identity = existing_frontmatter.get("canonical_id")
    if existing_identity is not None and not _frontmatter_raw(merged, "canonical_id"):
        merged = _set_frontmatter_object(
            merged,
            "canonical_id",
            existing_identity,
            frontmatter=merged_frontmatter,
        )
        merged_frontmatter["canonical_id"] = existing_identity
    for key in (
        "node_kind",
        "parent",
        "keywords",
        "aliases",
        "view_mode",
        "updated",
        "entity_kind",
        "sequence",
        "central_question",
        "facets",
        "summary",
        "state_updated",
        "record_owner",
        "people",
        "person_roles",
        "entity_type",
        "person_id",
        "person_kind",
        "person_scope",
        "relationship_to_owner",
        "identifiers",
    ):
        value = existing_frontmatter.get(key)
        if value is not None and merged_frontmatter.get(key) is None:
            merged = _set_frontmatter_object(
                merged,
                key,
                value,
                frontmatter=merged_frontmatter,
            )
            merged_frontmatter[key] = value
    merged = _upsert_frontmatter_value(
        merged, "knowledge_state", json.dumps("근거 확인됨", ensure_ascii=False)
    )
    merged = _upsert_frontmatter_value(merged, "state_reason", "accepted-evidence-receipt")
    merged_parent = merged_frontmatter.get("parent")
    if not existing:
        return _normalize_article_view(merged, parent=merged_parent)
    current = _optional_marker_block(existing, WIKI_CURRENT_START, WIKI_CURRENT_END)
    timeline = _optional_marker_block(existing, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    if timeline is not None:
        timeline_rows = [
            line
            for line in timeline.splitlines()
            if line not in {WIKI_TIMELINE_START, WIKI_TIMELINE_END}
            and line.strip()
            and not _MIGRATION_TIMELINE_RE.fullmatch(line.strip())
        ]
        timeline = (
            "\n".join((WIKI_TIMELINE_START, *timeline_rows, WIKI_TIMELINE_END))
            if timeline_rows
            else None
        )
    navigation = _optional_marker_block(existing, WIKI_NAVIGATION_START, WIKI_NAVIGATION_END)
    if current is None and timeline is None and navigation is None:
        preserved = preserve_generated_wiki_views(
            existing, _normalize_article_view(merged, parent=merged_parent)
        )
        return _normalize_article_view(preserved, parent=merged_parent)
    blocks: list[str] = []
    if navigation is not None:
        blocks.extend(("## 주제 연결", "", navigation, ""))
    if current is not None:
        blocks.extend((f"## {WIKI_CURRENT_HEADING}", "", current))
    if timeline is not None:
        blocks.extend(("", "## 시간 이력", "", timeline))
    merged = merged.rstrip() + "\n\n" + "\n".join(blocks).rstrip() + "\n"
    preserved = preserve_generated_wiki_views(
        existing, _normalize_article_view(merged, parent=merged_parent)
    )
    return _normalize_article_view(preserved, parent=merged_parent)


def _normalize_existing_wiki(
    text: str,
    *,
    canonical_id: str,
    facets: tuple[str, ...],
    knowledge_state: str,
    migration_day: date,
    parent: str | None = None,
) -> str:
    match = re.match(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?", text, flags=re.DOTALL)
    if match is None:
        raise WoonError("Wiki migration requires YAML frontmatter")
    metadata = load_yaml_text(match.group("yaml")) or {}
    if not isinstance(metadata, dict):
        raise WoonError("Wiki migration frontmatter must be a mapping")
    existing_parent = metadata.get("parent")
    legacy_parents = metadata.get("parent_topics")
    legacy_parent = (
        legacy_parents[0]
        if isinstance(legacy_parents, list)
        and len(legacy_parents) == 1
        and isinstance(legacy_parents[0], str)
        else None
    )
    for legacy in ("parent_topics", "parent_moc", "map_role", "mindmap_role"):
        metadata.pop(legacy, None)
    title = str(metadata.get("title", "")).strip()
    entity_kind = str(metadata.get("entity_kind", "")).strip()
    if not entity_kind:
        if metadata.get("entity_type") == "person" or metadata.get("person_id"):
            entity_kind = "person"
        elif metadata.get("content_kind"):
            entity_kind = str(metadata["content_kind"])
        elif metadata.get("project_id"):
            entity_kind = "project"
    default_node_kind = (
        "root"
        if canonical_id == "README"
        else "hub"
        if canonical_id.endswith("/README")
        else "entity"
        if entity_kind
        else "topic"
    )
    node_kind = str(metadata.get("node_kind", "")).strip() or default_node_kind
    default_view_mode = {
        "person": "topic-timeline",
        "project": "project",
        "book": "linear",
    }.get(entity_kind, "tree")
    metadata.update(
        {
            "type": "Wiki",
            "canonical_id": canonical_id,
            "node_kind": node_kind,
            "keywords": metadata.get("keywords") or [title],
            "aliases": metadata.get("aliases") or [],
            "view_mode": metadata.get("view_mode") or default_view_mode,
            "updated": metadata.get("updated") or migration_day.isoformat(),
            "facets": list(facets),
            "knowledge_state": knowledge_state,
            "status": "Archived" if knowledge_state in {"오래됨", "폐기됨"} else "Active",
            "summary": metadata.get("summary") or _summary_from_document(text),
            "state_reason": {
                "근거 확인됨": "accepted-evidence-receipt",
                "오래됨": "legacy-lifecycle",
            }.get(knowledge_state, "legacy-normalization"),
            "state_updated": migration_day.isoformat(),
            "record_owner": metadata.get("record_owner") or "choi-woonyoung",
        }
    )
    if entity_kind:
        metadata["entity_kind"] = entity_kind
    effective_parent = (
        existing_parent if isinstance(existing_parent, str) and existing_parent else legacy_parent
    ) or parent
    if effective_parent is None:
        metadata.pop("parent", None)
    else:
        metadata["parent"] = effective_parent
    yaml_text = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
    updated = f"---\n{yaml_text}---\n\n{text[match.end() :].lstrip()}"

    timeline_body = _optional_marker_body(updated, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    rows = [
        line
        for line in (timeline_body or "").splitlines()
        if line.strip() and not _MIGRATION_TIMELINE_RE.fullmatch(line.strip())
    ]
    if not rows:
        if timeline_body is None:
            return _normalize_article_view(updated)
        updated = _strip_managed_section(
            updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
        )
        return _normalize_article_view(updated)
    timeline = "\n".join((WIKI_TIMELINE_START, *rows, WIKI_TIMELINE_END))
    updated = _replace_or_append_managed_section(
        updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END, timeline
    )
    return _normalize_article_view(updated)


def _migration_parent(root: Path, relative: Path) -> str | None:
    """Infer one provisional parent only for the retired corpus migration."""

    if relative.as_posix() == "wiki/README.md":
        return None
    parts = relative.parts
    parent = root / "wiki/README.md"
    if len(parts) >= 3 and relative.name != "README.md":
        candidate = root / parts[0] / parts[1] / "README.md"
        if candidate.is_file() and candidate != root / relative:
            parent = candidate
    title = (
        _frontmatter_text(parent.read_text(encoding="utf-8"), "title")
        if parent.is_file()
        else "Wiki"
    )
    link = parent.relative_to(root).with_suffix("").as_posix()
    return f"[[{link}|{title or 'Wiki'}]]"


def _summary_from_document(text: str) -> str:
    """Derive one stable, human-readable summary from the document body."""

    current = _optional_marker_body(text, WIKI_CURRENT_START, WIKI_CURRENT_END)
    candidates = [current] if current else []
    body = text.split("\n---", 1)[1] if text.startswith("---\n") and "\n---" in text else text
    body = re.sub(r"(?ms)```.*?```", "", body)
    body = re.sub(r"(?ms)<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->", "", body)
    body = re.sub(r"(?ms)<!--.*?-->", "", body)
    one_line = re.search(r"(?m)^>\s*(?:한 줄 요약|요약)\s*:\s*(?P<summary>.+?)\s*$", body)
    if one_line:
        candidates.append(one_line.group("summary"))
    candidates.extend(re.split(r"\n\s*\n", body))
    for candidate in candidates:
        if not candidate:
            continue
        lines = [line.strip() for line in candidate.splitlines() if line.strip()]
        if not lines or any(line.startswith(("#", "- ", "* ", ">", "|")) for line in lines):
            continue
        plain = " ".join(lines)
        plain = re.sub(r"!?(?:\[\[)(?:[^\]|]+\|)?([^\]]+)(?:\]\])", r"\1", plain)
        plain = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", plain)
        plain = re.sub(r"[`*_~]", "", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        if not plain:
            continue
        sentence = re.split(r"(?<=[.!?다요])\s+", plain, maxsplit=1)[0].strip()
        summary = sentence if len(sentence) >= 12 else plain
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "..."
        return summary
    title = _frontmatter_text(text, "title") or "주제"
    return f"{title}에 대해 현재 확인한 내용을 정리한다."


def _infer_facets(relative: Path, text: str) -> tuple[str, ...]:
    """Preserve explicit Facets and add only evidence-backed classifications.

    A corpus normalization may run repeatedly.  It must therefore never erase
    an explicit project/content/person identity merely because the document was
    moved below ``wiki/personal``.  Legacy entity properties are also strong
    enough to recover the corresponding Facet after an interrupted migration.
    """

    raw_tags = _yaml_block_list(text, "tags")
    raw_facets = _frontmatter_list(text, "facets")
    facets = [item for item in raw_facets if item in ALLOWED_FACETS]
    is_person = (
        "인물" in facets
        or _frontmatter_text(text, "entity_type").casefold() == "person"
        or bool(_frontmatter_text(text, "person_id"))
        or bool(_frontmatter_text(text, "person_kind"))
    )
    is_project = (
        "프로젝트" in facets
        or bool(_frontmatter_text(text, "project_id"))
        or bool(_frontmatter_text(text, "objective"))
        or bool(_frontmatter_text(text, "project_status"))
        or any(tag == "topic:project" or tag.startswith("topic:project-") for tag in raw_tags)
    )
    content_kind = _frontmatter_text(text, "content_kind")
    is_book = content_kind == "book"
    is_resource = (
        "리소스" in facets
        or (bool(content_kind) and not is_book)
        or ("콘텐츠" in raw_facets and not is_book)
    )

    # The first unified migration temporarily gave every page the generic
    # concept/learning pair.  Strong entity properties are authoritative and
    # must remove that fallback so Facet filters retain their meaning.
    if is_person:
        facets = [item for item in facets if item not in {"개념", "학습"}]
    elif is_project or is_resource or is_book:
        facets = [item for item in facets if item != "개념"]

    if any(tag == "domain:career" for tag in raw_tags):
        facets.append("커리어")
    if is_person:
        facets.append("인물")
    if is_project:
        facets.append("프로젝트")
    if is_resource:
        facets.append("리소스")
        if content_kind in {
            "lecture",
            "course",
            "article",
            "learning-material-bundle",
        }:
            facets.append("학습")
    if is_book:
        facets.append("학습")
    if any(
        tag.startswith("domain:") or tag.startswith("topic:") or tag.startswith("book:")
        for tag in raw_tags
    ):
        if not (is_person or is_project or is_resource or is_book):
            facets.append("개념")
        if not is_person:
            facets.append("학습")
    if not facets:
        facets.extend(("개념", "학습"))
    unique = set(facets)
    return tuple(item for item in FACET_ORDER if item in unique)


def compiled_wiki_contract(relative: Path, text: str) -> dict[str, object]:
    """Return deterministic Wiki metadata for one compiler-owned page.

    The evidence compiler and the conversation growth path must write the same
    document contract. Keeping this derivation here prevents a second
    post-compile migration from changing generated Markdown behind its receipt.
    ``relative`` is the page path below ``wiki/``.
    """

    vault_relative = Path(WIKI_ROOT) / relative
    path_identity = relative.with_suffix("").as_posix()
    canonical_id = _frontmatter_text(text, "canonical_id") or path_identity
    explicit_parent = _frontmatter_value(text, "parent")
    if explicit_parent is not None and not isinstance(explicit_parent, str):
        raise WoonError("compiled Wiki parent must be one wikilink")
    if path_identity == "README":
        parent = None
    elif isinstance(explicit_parent, str) and explicit_parent.strip():
        parent = explicit_parent.strip()
    elif (
        len(relative.parts) > 1
        and relative.name != "README.md"
        and relative.parts[0] in COMPILED_SECTION_ROOTS
    ):
        parent = f"[[wiki/{relative.parts[0]}/README|{relative.parts[0]}]]"
    else:
        parent = "[[wiki/README|Wiki]]"
    title = _frontmatter_text(text, "title") or relative.stem
    node_kind = _frontmatter_text(text, "node_kind") or (
        "root" if path_identity == "README" else "hub" if relative.name == "README.md" else "topic"
    )
    view_mode = _frontmatter_text(text, "view_mode") or (
        "tree" if node_kind in {"root", "hub", "topic", "entity"} else "article"
    )
    keywords = _frontmatter_list(text, "keywords") or (title,)
    aliases = _frontmatter_list(text, "aliases")
    updated = (
        _frontmatter_text(text, "updated")
        or _frontmatter_text(text, "state_updated")
        or "1970-01-01"
    )
    return {
        "type": "Wiki",
        "canonical_id": canonical_id,
        "node_kind": node_kind,
        **({"parent": parent} if parent is not None else {}),
        "keywords": list(keywords),
        "aliases": list(aliases),
        "view_mode": view_mode,
        "updated": updated,
        "facets": list(_infer_facets(vault_relative, text)),
        "knowledge_state": "근거 확인됨",
        "state_reason": "accepted-evidence-receipt",
        "status": "Active",
        "record_owner": "choi-woonyoung",
        "summary": _frontmatter_text(text, "summary") or _summary_from_document(text),
    }


def _infer_knowledge_state(text: str) -> str:
    current = _frontmatter_text(text, "knowledge_state")
    if current in ALLOWED_KNOWLEDGE_STATES:
        return current
    status = _frontmatter_text(text, "status").casefold()
    if status in {"historical", "retired", "deprecated", "archived"}:
        return "오래됨"
    if _frontmatter_raw(text, "llm_wiki") or "\nllm_wiki:\n" in text:
        return "근거 확인됨"
    return "확인 필요"


def _yaml_block_list(text: str, key: str) -> tuple[str, ...]:
    inline = _frontmatter_raw(text, key)
    if inline.startswith("["):
        try:
            value = json.loads(inline)
        except json.JSONDecodeError:
            value = []
        if isinstance(value, list):
            return tuple(str(item).strip() for item in value if str(item).strip())
    match = re.search(rf"(?ms)^{re.escape(key)}:\s*\n(?P<body>(?:\s*-\s*[^\n]+\n?)+)", text)
    if not match:
        return ()
    return tuple(
        line.split("-", 1)[1].strip().strip("'\"")
        for line in match.group("body").splitlines()
        if line.lstrip().startswith("-")
    )


def _merge_delta(text: str, delta: WikiDelta) -> str:
    if not text:
        return _render_new(delta)
    if _frontmatter_text(text, "title") != delta.title.strip():
        raise WoonError("Wiki update conflicts with the existing subject identity")
    if _frontmatter_text(text, "type") != "Wiki":
        raise WoonError("Wiki update requires type Wiki")
    current_state = _frontmatter_text(text, "knowledge_state")
    next_state = transition_knowledge_state(
        current_state=current_state,
        requested_state=delta.knowledge_state,
        authority=delta.state_authority,
    )

    previous_current = _optional_marker_body(text, WIKI_CURRENT_START, WIKI_CURRENT_END)
    if previous_current is None:
        previous_current = _optional_h2_body(text, WIKI_CURRENT_HEADING)
    if previous_current is None:
        previous_current = _optional_h2_body(text, "현재 이해")
    previous_updated = _frontmatter_text(text, "updated") or delta.day.isoformat()
    facets = _frontmatter_list(text, "facets")
    merged_facets = tuple(dict.fromkeys((*facets, *delta.facets)))
    updated = _upsert_frontmatter_value(
        text, "facets", json.dumps(merged_facets, ensure_ascii=False)
    )
    parent = delta.parent
    if parent is not None:
        updated = _upsert_frontmatter_value(
            updated, "parent", json.dumps(parent, ensure_ascii=False)
        )
    updated = _upsert_frontmatter_value(updated, "node_kind", delta.node_kind)
    updated = _upsert_frontmatter_value(updated, "view_mode", delta.view_mode)
    if delta.keywords:
        updated = _upsert_frontmatter_value(
            updated, "keywords", json.dumps(delta.keywords, ensure_ascii=False)
        )
    if delta.aliases:
        updated = _upsert_frontmatter_value(
            updated, "aliases", json.dumps(delta.aliases, ensure_ascii=False)
        )
    elif not _frontmatter_raw(updated, "aliases"):
        updated = _insert_frontmatter_value(updated, "aliases", "[]")
    if delta.central_question is not None:
        updated = _upsert_frontmatter_value(
            updated, "central_question", json.dumps(delta.central_question, ensure_ascii=False)
        )
    if delta.entity_kind is not None:
        updated = _upsert_frontmatter_value(updated, "entity_kind", delta.entity_kind)
    if delta.entity_section is not None:
        updated = _upsert_frontmatter_value(updated, "entity_section", delta.entity_section)
    for key, value in (
        ("lifecycle_status", delta.lifecycle_status),
        ("started_on", delta.started_on),
        ("ended_on", delta.ended_on),
        ("occurred_on", delta.occurred_on),
    ):
        if value is not None:
            rendered = value.isoformat() if isinstance(value, date) else value
            updated = _upsert_frontmatter_value(updated, key, rendered)
    if delta.sequence is not None:
        updated = _upsert_frontmatter_value(updated, "sequence", f"{delta.sequence:g}")
    if delta.interview_answer is not None:
        updated = _upsert_frontmatter_value(updated, "question_kind", "interview")
        updated = _upsert_frontmatter_value(
            updated,
            "interview_tracks",
            json.dumps(delta.interview_tracks, ensure_ascii=False),
        )
        updated = _upsert_frontmatter_value(
            updated,
            "question_topic",
            json.dumps(delta.question_topic, ensure_ascii=False),
        )
    updated = _upsert_frontmatter_value(
        updated, "knowledge_state", json.dumps(next_state, ensure_ascii=False)
    )
    updated = _upsert_frontmatter_value(updated, "state_reason", delta.state_authority)
    updated = _upsert_frontmatter_value(updated, "state_updated", delta.day.isoformat())
    updated = _upsert_frontmatter_value(updated, "updated", delta.day.isoformat())
    updated = _upsert_frontmatter_value(
        updated, "summary", json.dumps(delta.summary.strip(), ensure_ascii=False)
    )
    updated = _merge_facet_properties(updated, delta)

    if delta.node_kind == "entity":
        current = "\n".join((WIKI_CURRENT_START, delta.summary.strip(), WIKI_CURRENT_END))
        updated = _replace_or_append_managed_section(
            updated, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END, current
        )
        updated = _strip_managed_section(
            updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
        )
        if delta.intent:
            updated = _replace_or_append_h2(updated, WIKI_INTENT_HEADING, delta.intent.strip())
        if delta.next_question:
            updated = _replace_or_append_h2(updated, WIKI_NEXT_HEADING, delta.next_question.strip())
        if delta.related_documents:
            rows = [
                f"- [[{Path(path).with_suffix('').as_posix()}]]" for path in delta.related_documents
            ]
            updated = _merge_h2_rows(updated, "관련 키워드", rows)
        return _normalize_article_view(updated)

    if delta.interview_answer is not None:
        # The interview block already owns the current answer and its dated
        # revisions. A generic current/timeline pair would repeat one answer
        # three times on the same question page.
        updated = _strip_managed_section(
            updated, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END
        )
        updated = _strip_managed_section(
            updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
        )
    elif delta.entity_section == "history":
        updated = _strip_managed_section(
            updated, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END
        )
    else:
        current = "\n".join((WIKI_CURRENT_START, delta.summary.strip(), WIKI_CURRENT_END))
        updated = _replace_or_append_managed_section(
            updated, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END, current
        )

    row = f"- {delta.day.isoformat()} · {delta.event_kind} — {delta.summary.strip()}"
    if delta.interview_answer is not None:
        pass
    elif delta.entity_section == "information":
        updated = _strip_managed_section(
            updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
        )
    else:
        previous_timeline = _optional_marker_body(updated, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
        rows = [line for line in (previous_timeline or "").splitlines() if line.strip()]
        should_record_previous = delta.entity_section != "history" and (
            previous_current is not None and previous_current.strip() != delta.summary.strip()
        )
        previous_row = (
            f"- {previous_updated} · 이전 이해 — {previous_current.strip()}"
            if should_record_previous and previous_current is not None
            else None
        )
        if previous_row is not None and previous_row not in rows:
            rows.append(previous_row)
        if delta.entity_section == "history" and row not in rows:
            rows.append(row)
        if rows:
            timeline = "\n".join((WIKI_TIMELINE_START, *rows, WIKI_TIMELINE_END))
            updated = _replace_or_append_managed_section(
                updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END, timeline
            )
        else:
            updated = _strip_managed_section(
                updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
            )
    if delta.intent and delta.entity_section != "history":
        updated = _replace_or_append_h2(updated, WIKI_INTENT_HEADING, delta.intent.strip())
    if delta.next_question and delta.entity_section != "history":
        updated = _replace_or_append_h2(updated, WIKI_NEXT_HEADING, delta.next_question.strip())
    if delta.related_documents:
        rows = [
            f"- [[{Path(path).with_suffix('').as_posix()}]]" for path in delta.related_documents
        ]
        updated = _merge_h2_rows(updated, WIKI_RELATED_HEADING, rows)
    if delta.interview_answer is not None:
        updated = _merge_interview_answer(updated, delta)
    return _normalize_article_view(updated)


def _render_new(delta: WikiDelta) -> str:
    current = "\n".join((WIKI_CURRENT_START, delta.summary.strip(), WIKI_CURRENT_END))
    timeline = "\n".join(
        (
            WIKI_TIMELINE_START,
            f"- {delta.day.isoformat()} · {delta.event_kind} — {delta.summary.strip()}",
            WIKI_TIMELINE_END,
        )
    )
    canonical_path = Path(delta.wiki_subject_path or wiki_relative_path(delta.title))
    canonical_id = canonical_path.with_suffix("").relative_to("wiki").as_posix()
    lines = [
        "---",
        "type: Wiki",
        f"title: {json.dumps(delta.title.strip(), ensure_ascii=False)}",
        f"canonical_id: {json.dumps(canonical_id, ensure_ascii=False)}",
        f"node_kind: {delta.node_kind}",
        f"parent: {json.dumps(delta.parent, ensure_ascii=False)}",
        f"keywords: {json.dumps(delta.keywords, ensure_ascii=False)}",
        f"aliases: {json.dumps(delta.aliases, ensure_ascii=False)}",
        f"view_mode: {delta.view_mode}",
        *((f"entity_kind: {delta.entity_kind}",) if delta.entity_kind is not None else ()),
        *((f"entity_section: {delta.entity_section}",) if delta.entity_section is not None else ()),
        *((f"lifecycle_status: {delta.lifecycle_status}",) if delta.lifecycle_status else ()),
        *((f"started_on: {delta.started_on.isoformat()}",) if delta.started_on else ()),
        *((f"ended_on: {delta.ended_on.isoformat()}",) if delta.ended_on else ()),
        *((f"occurred_on: {delta.occurred_on.isoformat()}",) if delta.occurred_on else ()),
        *((f"sequence: {delta.sequence:g}",) if delta.sequence is not None else ()),
        *(
            (f"central_question: {json.dumps(delta.central_question, ensure_ascii=False)}",)
            if delta.central_question is not None
            else ()
        ),
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Active",
        f"facets: {json.dumps(delta.facets, ensure_ascii=False)}",
        *(
            (
                "question_kind: interview",
                f"interview_tracks: {json.dumps(delta.interview_tracks, ensure_ascii=False)}",
                f"question_topic: {json.dumps(delta.question_topic, ensure_ascii=False)}",
            )
            if delta.interview_answer is not None
            else ()
        ),
        f"knowledge_state: {json.dumps(delta.knowledge_state, ensure_ascii=False)}",
        f"state_reason: {delta.state_authority}",
        f"state_updated: {delta.day.isoformat()}",
        f"summary: {json.dumps(delta.summary.strip(), ensure_ascii=False)}",
        f"updated: {delta.day.isoformat()}",
        "---",
        "",
        f"# {delta.title.strip()}",
    ]
    if delta.node_kind == "entity":
        if delta.entity_kind == "book":
            lines.extend(("", "## 목차"))
        else:
            lines.extend(("", f"## {WIKI_CURRENT_HEADING}", "", current))
    elif delta.interview_answer is not None:
        # ``현재 최선 답변`` below is the sole current-state owner.
        pass
    elif delta.entity_section == "information":
        lines.extend(("", f"## {WIKI_CURRENT_HEADING}", "", current))
    elif delta.entity_section == "history":
        lines.extend(("", "## 시간 이력", "", timeline))
    else:
        lines.extend(("", f"## {WIKI_CURRENT_HEADING}", "", current))
    lines = _insert_new_facet_properties(lines, delta)
    if delta.intent and delta.entity_kind != "book" and delta.entity_section != "history":
        lines.extend(("", f"## {WIKI_INTENT_HEADING}", "", delta.intent.strip()))
    if delta.next_question and delta.entity_kind != "book" and delta.entity_section != "history":
        lines.extend(("", f"## {WIKI_NEXT_HEADING}", "", delta.next_question.strip()))
    if delta.related_documents:
        lines.extend(("", f"## {WIKI_RELATED_HEADING}", ""))
        lines.extend(
            f"- [[{Path(path).with_suffix('').as_posix()}]]" for path in delta.related_documents
        )
    if delta.interview_answer is not None:
        lines.extend(_new_interview_sections(delta))
    return _normalize_article_view("\n".join((*lines, "")))


def _validate_delta(vault: Path, delta: WikiDelta, *, planned_paths: set[str]) -> None:
    if not delta.title.strip() or not delta.summary.strip():
        raise WoonError("Wiki delta requires a title and summary")
    if len(delta.title.strip()) > 120 or len(delta.summary.strip()) > 600:
        raise WoonError("Wiki delta text is too long")
    if not delta.facets or set(delta.facets) - ALLOWED_FACETS:
        raise WoonError("Wiki delta contains an unsupported facet")
    if delta.knowledge_state not in ALLOWED_KNOWLEDGE_STATES:
        raise WoonError("Wiki delta contains an unsupported knowledge state")
    if delta.state_authority not in ALLOWED_STATE_AUTHORITIES:
        raise WoonError("Wiki delta contains an unsupported state authority")
    if delta.node_kind not in {"hub", "topic", "entity", "detail", "decision"}:
        raise WoonError("Wiki delta contains an unsupported node_kind")
    if delta.view_mode not in {"tree", "linear", "project", "topic-timeline", "article"}:
        raise WoonError("Wiki delta contains an unsupported view_mode")
    if delta.entity_section not in {None, "information", "history"}:
        raise WoonError("Wiki delta contains an unsupported entity_section")
    if delta.entity_section is not None and delta.node_kind != "detail":
        raise WoonError("Wiki entity_section requires node_kind detail")
    _validate_lifecycle_delta(delta)
    if not delta.keywords or len(set(item.casefold() for item in delta.keywords)) != len(
        delta.keywords
    ):
        raise WoonError("Wiki delta requires unique representative keywords")
    for keyword in delta.keywords:
        _bounded_line(keyword, "keyword", 120)
    if len(set(item.casefold() for item in delta.aliases)) != len(delta.aliases):
        raise WoonError("Wiki aliases must be unique")
    for alias in delta.aliases:
        _bounded_line(alias, "alias", 120)
    if delta.central_question is not None:
        _bounded_line(delta.central_question, "central_question", 240)
    transition_knowledge_state(
        current_state="",
        requested_state=delta.knowledge_state,
        authority=delta.state_authority,
    )
    if delta.event_kind not in {"예정", "실행", "변경", "산출물"}:
        raise WoonError("Wiki timeline event kind is invalid")
    if delta.content_kind is not None:
        _bounded_line(delta.content_kind, "content_kind", 48)
    if len(delta.creators) > 8 or len(set(delta.creators)) != len(delta.creators):
        raise WoonError("Wiki creators must be unique and bounded")
    for creator in delta.creators:
        _bounded_line(creator, "creator", 72)
    if delta.official_url is not None and (
        len(delta.official_url) > 240
        or not delta.official_url.startswith("https://")
        or any(char.isspace() for char in delta.official_url)
    ):
        raise WoonError("Wiki official_url must be a safe HTTPS URL")
    if delta.project_status is not None:
        _bounded_line(delta.project_status, "project_status", 32)
    project_id = _project_id(delta)
    objective = _project_objective(delta)
    if project_id is not None:
        _bounded_line(project_id, "project_id", 120)
    if objective is not None:
        _bounded_line(objective, "objective", 280)
    if len(delta.materials) > 12 or len(set(delta.materials)) != len(delta.materials):
        raise WoonError("Wiki materials must be unique and bounded")
    for material in delta.materials:
        _bounded_line(material, "material", 120)
    for relative in delta.related_documents:
        candidate = Path(relative)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or not relative.endswith(".md")
            or not relative.startswith("wiki/")
            or (relative not in planned_paths and not (vault / candidate).is_file())
        ):
            raise WoonError("Wiki relation must point to an existing Wiki")
    parent = delta.parent
    if parent is None:
        raise WoonError("Wiki delta requires one semantic parent")
    target = _required_wikilink_target(parent)
    candidate = Path(f"{target}.md") if not target.endswith(".md") else Path(target)
    if (
        not target.startswith("wiki/")
        or candidate.is_absolute()
        or ".." in candidate.parts
        or (candidate.as_posix() not in planned_paths and not (vault / candidate).is_file())
    ):
        raise WoonError("Wiki parent must point to an existing Wiki")
    if delta.interview_answer is not None:
        if not delta.interview_tracks or len(set(delta.interview_tracks)) != len(
            delta.interview_tracks
        ):
            raise WoonError("Wiki interview_tracks must be a unique non-empty list")
        for track in delta.interview_tracks:
            _bounded_line(track, "interview track", 120)
        if delta.question_topic is None:
            raise WoonError("Wiki interview question_topic is required")
        _bounded_line(delta.question_topic, "interview question_topic", 120)
        _validate_interview_answer(delta.interview_answer)


def _validate_lifecycle_delta(delta: WikiDelta) -> None:
    states = {"idea", "planned", "active", "paused", "completed", "cancelled", "archived"}
    state = delta.lifecycle_status
    dates = (delta.started_on, delta.ended_on, delta.occurred_on)
    if state is None:
        if any(value is not None for value in dates):
            raise WoonError("Wiki lifecycle dates require lifecycle_status")
        return
    if state not in states:
        raise WoonError("Wiki lifecycle_status is invalid")
    if delta.occurred_on is not None and any(
        value is not None for value in (delta.started_on, delta.ended_on)
    ):
        raise WoonError("Wiki occurred_on cannot be combined with a date range")
    if (
        delta.started_on is not None
        and delta.ended_on is not None
        and delta.ended_on < delta.started_on
    ):
        raise WoonError("Wiki ended_on cannot precede started_on")
    if state in {"completed", "cancelled", "archived"} and not (
        delta.ended_on or delta.occurred_on
    ):
        raise WoonError("Wiki closed lifecycle requires ended_on or occurred_on")
    if state in {"idea", "planned", "active", "paused"} and delta.ended_on is not None:
        raise WoonError("Wiki open lifecycle cannot have ended_on")


def _merge_interview_answer(text: str, delta: WikiDelta) -> str:
    revision = delta.interview_answer
    assert revision is not None
    current = _render_interview_current(revision)
    previous = _optional_marker_body(text, INTERVIEW_CURRENT_START, INTERVIEW_CURRENT_END)
    updated = text
    if not revision.promote_current:
        archived = _optional_marker_body(updated, INTERVIEW_ARCHIVE_START, INTERVIEW_ARCHIVE_END)
        archived_rows = [archived.strip()] if archived and archived.strip() else []
        attempt_label = revision.source_label or "연습 답변"
        block = f"### {delta.day.isoformat()} · {attempt_label}\n\n{_marker_inner(current).strip()}"
        if block not in archived_rows:
            archived_rows.append(block)
        archive = "\n\n".join((INTERVIEW_ARCHIVE_START, *archived_rows, INTERVIEW_ARCHIVE_END))
        updated = _replace_or_append_managed_section(
            updated,
            "과거 답변",
            INTERVIEW_ARCHIVE_START,
            INTERVIEW_ARCHIVE_END,
            archive,
        )
        return _append_interview_history(updated, delta, revision)
    if previous and previous.strip() != _marker_inner(current).strip():
        archived = _optional_marker_body(updated, INTERVIEW_ARCHIVE_START, INTERVIEW_ARCHIVE_END)
        archived_rows = [archived.strip()] if archived and archived.strip() else []
        prior_label = revision.source_label or "이전 답변"
        archived_previous = _compact_archived_interview_body(previous.strip())
        block = f"### {delta.day.isoformat()} · {prior_label}\n\n{archived_previous}"
        if block not in archived_rows:
            archived_rows.append(block)
        archive = "\n\n".join(
            (
                INTERVIEW_ARCHIVE_START,
                *archived_rows,
                INTERVIEW_ARCHIVE_END,
            )
        )
        updated = _replace_or_append_managed_section(
            updated,
            "과거 답변",
            INTERVIEW_ARCHIVE_START,
            INTERVIEW_ARCHIVE_END,
            archive,
        )
    updated = _replace_or_append_managed_section(
        updated,
        "현재 최선 답변",
        INTERVIEW_CURRENT_START,
        INTERVIEW_CURRENT_END,
        current,
    )
    return _append_interview_history(updated, delta, revision)


def _append_interview_history(
    text: str, delta: WikiDelta, revision: InterviewAnswerRevision
) -> str:
    history = _optional_marker_body(text, INTERVIEW_HISTORY_START, INTERVIEW_HISTORY_END)
    rows = [line for line in (history or "").splitlines() if line.strip()]
    row = f"- {delta.day.isoformat()} · {revision.change_reason.strip()}"
    if revision.quality_assessment:
        row += f" · 평가: {revision.quality_assessment.strip()}"
    if row not in rows:
        rows.append(row)
    body = "\n".join((INTERVIEW_HISTORY_START, *rows, INTERVIEW_HISTORY_END))
    return _replace_or_append_managed_section(
        text,
        "답변 성장 이력",
        INTERVIEW_HISTORY_START,
        INTERVIEW_HISTORY_END,
        body,
    )


def _new_interview_sections(delta: WikiDelta) -> list[str]:
    revision = delta.interview_answer
    assert revision is not None
    row = f"- {delta.day.isoformat()} · {revision.change_reason.strip()}"
    if revision.quality_assessment:
        row += f" · 평가: {revision.quality_assessment.strip()}"
    return [
        "",
        "## 현재 최선 답변",
        "",
        _render_interview_current(revision),
        "",
        "## 답변 성장 이력",
        "",
        INTERVIEW_HISTORY_START,
        row,
        INTERVIEW_HISTORY_END,
        "",
        "## 과거 답변",
        "",
        INTERVIEW_ARCHIVE_START,
        INTERVIEW_ARCHIVE_END,
    ]


def _render_interview_current(revision: InterviewAnswerRevision) -> str:
    lines = [INTERVIEW_CURRENT_START]
    if revision.context:
        lines.extend(("### 질문 맥락", "", revision.context.strip(), ""))
    lines.extend(("### 질문", "", revision.question.strip(), ""))
    answer = revision.answer.strip() if revision.answer else ""
    lines.extend(("### 답변", "", answer, ""))
    if revision.evidence:
        lines.extend(("### 확인된 근거", "", *(f"- {item}" for item in revision.evidence), ""))
    if revision.limitations:
        limitations = (f"- {item}" for item in revision.limitations)
        lines.extend(("### 아직 확인할 한계", "", *limitations, ""))
    if revision.job_variants:
        lines.extend(("### 지원별 변형", "", *(f"- {item}" for item in revision.job_variants), ""))
    while lines and not lines[-1]:
        lines.pop()
    lines.append(INTERVIEW_CURRENT_END)
    return "\n".join(lines)


def _marker_inner(block: str) -> str:
    lines = block.splitlines()
    if lines and lines[0] == INTERVIEW_CURRENT_START:
        lines = lines[1:]
    if lines and lines[-1] == INTERVIEW_CURRENT_END:
        lines = lines[:-1]
    return "\n".join(lines)


def _validate_interview_answer(revision: InterviewAnswerRevision) -> None:
    _bounded_multiline(revision.question, "interview question", 280)
    if revision.answer is None or not revision.answer.strip():
        raise WoonError("interview answer must contain a reusable answer")
    if revision.answer.strip() == "아직 답변하지 않았다.":
        raise WoonError("interview answer placeholder must not become a Wiki page")
    _bounded_multiline(revision.answer, "interview answer", 5000)
    if revision.context is not None:
        _bounded_multiline(revision.context, "interview context", 1200)
    _bounded_line(revision.change_reason, "interview change reason", 240)
    if revision.quality_assessment is not None:
        _bounded_line(revision.quality_assessment, "interview quality assessment", 360)
    if revision.source_label is not None:
        _bounded_line(revision.source_label, "interview source label", 120)
    for name, values, limit, item_limit in (
        ("evidence", revision.evidence, 12, 360),
        ("limitations", revision.limitations, 12, 360),
        ("job variants", revision.job_variants, 8, 600),
    ):
        if len(values) > limit or len(set(values)) != len(values):
            raise WoonError(f"Wiki interview {name} must be unique and bounded")
        for value in values:
            _bounded_multiline(value, f"interview {name}", item_limit)


def _bounded_multiline(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\x00" in value:
        raise WoonError(f"Wiki {field} must be bounded visible text")


def _required_wikilink_target(value: str) -> str:
    match = re.fullmatch(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", value.strip())
    if not match:
        raise WoonError("Wiki parent topic must be one wikilink")
    return match.group(1).strip()


def _merge_facet_properties(text: str, delta: WikiDelta) -> str:
    project_id = _project_id(delta)
    objective = _project_objective(delta)
    values: tuple[tuple[str, str | None], ...] = (
        ("content_kind", delta.content_kind),
        ("creators", json.dumps(delta.creators, ensure_ascii=False) if delta.creators else None),
        (
            "official_url",
            json.dumps(delta.official_url, ensure_ascii=False) if delta.official_url else None,
        ),
        ("project_id", project_id),
        ("project_status", delta.project_status or ("Active" if project_id else None)),
        ("objective", json.dumps(objective, ensure_ascii=False) if objective else None),
        ("materials", json.dumps(delta.materials, ensure_ascii=False) if delta.materials else None),
    )
    updated = text
    for key, value in values:
        if value is not None:
            updated = _upsert_frontmatter_value(updated, key, value)
    return updated


def _insert_new_facet_properties(lines: list[str], delta: WikiDelta) -> list[str]:
    insertion = len(lines)
    for index, line in enumerate(lines):
        if line == "---" and index > 0:
            insertion = index
            break
    properties: list[str] = []
    project_id = _project_id(delta)
    objective = _project_objective(delta)
    if delta.content_kind:
        properties.append(f"content_kind: {delta.content_kind}")
    if delta.creators:
        properties.append(f"creators: {json.dumps(delta.creators, ensure_ascii=False)}")
    if delta.official_url:
        properties.append(f"official_url: {json.dumps(delta.official_url, ensure_ascii=False)}")
    if project_id:
        properties.append(f"project_id: {project_id}")
        properties.append(f"project_status: {delta.project_status or 'Active'}")
    elif delta.project_status:
        properties.append(f"project_status: {delta.project_status}")
    if objective:
        properties.append(f"objective: {json.dumps(objective, ensure_ascii=False)}")
    if delta.materials:
        properties.append(f"materials: {json.dumps(delta.materials, ensure_ascii=False)}")
    return [*lines[:insertion], *properties, *lines[insertion:]]


def _project_id(delta: WikiDelta) -> str | None:
    if "프로젝트" not in delta.facets:
        return delta.project_id
    return delta.project_id or _slug(delta.title)


def _project_objective(delta: WikiDelta) -> str | None:
    if "프로젝트" not in delta.facets:
        return delta.objective
    return delta.objective or delta.summary.strip()


def _bounded_line(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value or "\r" in value:
        raise WoonError(f"Wiki {field} must be short visible text")


def transition_knowledge_state(*, current_state: str, requested_state: str, authority: str) -> str:
    """Apply the only allowed Wiki state transition contract."""

    if current_state and current_state not in ALLOWED_KNOWLEDGE_STATES:
        raise WoonError("Wiki current knowledge state is invalid")
    if requested_state not in ALLOWED_KNOWLEDGE_STATES:
        raise WoonError("Wiki requested knowledge state is invalid")
    if authority not in ALLOWED_STATE_AUTHORITIES:
        raise WoonError("Wiki state authority is invalid")
    if current_state == "폐기됨" and authority != "user":
        raise WoonError("A retired Wiki document requires explicit user reopening")
    permitted = {
        "conversation": {"생각 중", "확인 필요"},
        "evidence-compiler": {"근거 확인됨"},
        "curation": {"생각 중", "확인 필요", "오래됨", "폐기됨"},
        "user": ALLOWED_KNOWLEDGE_STATES,
    }[authority]
    if requested_state not in permitted:
        raise WoonError("Wiki state transition exceeds its authority")
    return requested_state


def _slug(title: str) -> str:
    stem = _FILE_STEM_RE.sub("-", title.strip()).strip("-_").lower()
    if not stem:
        raise WoonError("Wiki title cannot form a canonical filename")
    return stem


def _frontmatter_raw(text: str, key: str) -> str:
    # Horizontal whitespace only: ``\s`` crosses the newline and used to
    # misread the first item of a block YAML list as the scalar value.
    match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", text)
    return match.group(1).strip() if match else ""


def _frontmatter_value(text: str, key: str) -> object | None:
    """Read one frontmatter value without flattening YAML sequences."""

    data = _frontmatter_mapping(text)
    return data.get(key) if data is not None else None


def _frontmatter_mapping(text: str) -> dict[str, object] | None:
    """Parse a document frontmatter once for callers that need several keys."""

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---", text)
    if match is None:
        return None
    data = load_yaml_text(match.group("yaml")) or {}
    if not isinstance(data, dict):
        return None
    return data


def _set_frontmatter_object(
    text: str,
    key: str,
    value: object,
    *,
    frontmatter: dict[str, object] | None = None,
) -> str:
    """Set one YAML value while preserving the Markdown body verbatim."""

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---(?P<body>[\s\S]*)\Z", text)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")
    if frontmatter is None:
        data = load_yaml_text(match.group("yaml")) or {}
        if not isinstance(data, dict):
            raise WoonError("Wiki frontmatter is malformed")
    else:
        data = dict(frontmatter)
    data[key] = value
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{rendered}---{match.group('body')}"


def _remove_frontmatter_keys(
    text: str,
    keys: tuple[str, ...],
    *,
    frontmatter: dict[str, object] | None = None,
) -> str:
    """Remove retired root properties without touching the Markdown body."""

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---(?P<body>[\s\S]*)\Z", text)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")
    if frontmatter is None:
        data = load_yaml_text(match.group("yaml")) or {}
        if not isinstance(data, dict):
            raise WoonError("Wiki frontmatter is malformed")
    else:
        data = dict(frontmatter)
    changed = False
    for key in keys:
        if key in data:
            del data[key]
            changed = True
    if not changed:
        return text
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{rendered}---{match.group('body')}"


def _frontmatter_text(text: str, key: str) -> str:
    raw = _frontmatter_raw(text, key)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def _frontmatter_list(text: str, key: str) -> tuple[str, ...]:
    value = _frontmatter_value(text, key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"Wiki {key} must be a string list")
    return tuple(value)


def _normalize_article_view(text: str, *, parent: object = _UNRESOLVED_FRONTMATTER) -> str:
    """Keep structural metadata in Properties instead of a repeated body card."""

    start_count = text.count(WIKI_OVERVIEW_START)
    end_count = text.count(WIKI_OVERVIEW_END)
    if start_count != end_count or start_count > 1:
        raise WoonError("Wiki article overview markers are malformed")
    updated = text
    if start_count == 1:
        updated = re.sub(
            re.escape(WIKI_OVERVIEW_START) + r".*?" + re.escape(WIKI_OVERVIEW_END) + r"\n*",
            "",
            text,
            count=1,
            flags=re.DOTALL,
        )
    # Source bodies may contain several leading blank lines. The keyword-tree
    # renderer canonicalizes the space below H1, so the compiler must emit the
    # same bytes or every compile/refresh cycle invalidates its receipts.
    updated = re.sub(r"(?m)^(# .+?)\n{3,}", r"\1\n\n", updated, count=1)
    updated = _normalize_managed_prose(updated, parent=parent)
    return _normalize_timeline_heading(updated)


def _normalize_managed_prose(text: str, *, parent: object = _UNRESOLVED_FRONTMATTER) -> str:
    """Remove legacy archive scaffolding that repeats the current conclusion."""

    frontmatter_match = re.match(r"\A---\n.*?\n---", text, flags=re.DOTALL)
    prefix_end = frontmatter_match.end() if frontmatter_match is not None else 0
    prefix = text[:prefix_end]
    body = text[prefix_end:].replace("추정 의도: ", "")
    updated = _normalize_semantic_sections(prefix + body, parent=parent)
    updated = _compact_interview_archive(updated)

    if INTERVIEW_CURRENT_START in updated and WIKI_CURRENT_START in updated:
        updated = _strip_managed_section(
            updated, WIKI_CURRENT_HEADING, WIKI_CURRENT_START, WIKI_CURRENT_END
        )
        updated = _strip_managed_section(
            updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END
        )
        return updated

    current = _optional_marker_body(updated, WIKI_CURRENT_START, WIKI_CURRENT_END)
    timeline = _optional_marker_body(updated, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    if current is None or timeline is None:
        return updated
    normalized_current = re.sub(r"\s+", " ", current).strip()
    rows = [line for line in timeline.splitlines() if line.strip()]
    distinct_rows = [
        line for line in rows if not re.sub(r"\s+", " ", line).strip().endswith(normalized_current)
    ]
    if len(distinct_rows) == len(rows):
        return updated
    if not distinct_rows:
        return _strip_managed_section(updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    replacement = "\n".join((WIKI_TIMELINE_START, *distinct_rows, WIKI_TIMELINE_END))
    return _replace_or_append_managed_section(
        updated, "한 줄 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END, replacement
    )


def _normalize_semantic_sections(text: str, *, parent: object = _UNRESOLVED_FRONTMATTER) -> str:
    """Replace conversation scaffolding with headings that explain reader use."""

    updated = re.sub(r"(?m)^## 현재 이해[ \t]*$", f"## {WIKI_CURRENT_HEADING}", text)
    updated = re.sub(r"(?m)^## 남긴 의도[ \t]*$", f"## {WIKI_INTENT_HEADING}", updated)
    updated = re.sub(r"(?m)^## 다음 질문[ \t]*$", f"## {WIKI_NEXT_HEADING}", updated)
    updated = re.sub(r"(?m)^## 연결[ \t]*$", f"## {WIKI_RELATED_HEADING}", updated)
    semantic = "|".join(
        re.escape(value)
        for value in (
            WIKI_CURRENT_HEADING,
            WIKI_INTENT_HEADING,
            WIKI_NEXT_HEADING,
            WIKI_RELATED_HEADING,
        )
    )
    updated = re.sub(rf"(?m)^(## (?:{semantic}))[ \t]*\n(?!\n)", r"\1\n\n", updated)

    if parent is _UNRESOLVED_FRONTMATTER:
        parent = _frontmatter_value(updated, "parent")
    parent_target = _optional_wikilink_target(parent) if isinstance(parent, str) else None
    if parent_target is None:
        return updated
    pattern = re.compile(
        rf"(?ms)^## {re.escape(WIKI_RELATED_HEADING)}\s*\n(?P<body>.*?)(?=^## |\Z)"
    )
    match = pattern.search(updated)
    if match is None:
        return updated
    section = match.group("body")
    targets = tuple(_WIKILINK_TARGET_RE.findall(section))
    residue = _WIKILINK_TARGET_RE.sub("", section)
    residue = re.sub(r"[\s*+-]+", "", residue)
    if targets and set(targets) == {parent_target} and not residue:
        return pattern.sub("", updated, count=1).rstrip() + "\n"
    parent_row = re.compile(
        rf"^\s*[-*+]\s*\[\[{re.escape(parent_target)}(?:#[^\]|]+)?(?:\|[^]]+)?]]\s*$"
    )
    remaining = [line for line in section.splitlines() if not parent_row.fullmatch(line)]
    if len(remaining) != len(section.splitlines()):
        replacement = f"## {WIKI_RELATED_HEADING}\n\n" + "\n".join(remaining).strip() + "\n"
        return pattern.sub(replacement, updated, count=1).rstrip() + "\n"
    return updated


def _optional_wikilink_target(value: str) -> str | None:
    match = _WIKILINK_TARGET_RE.fullmatch(value.strip())
    return match.group("target") if match is not None else None


def _compact_interview_archive(text: str) -> str:
    archive = _optional_marker_body(text, INTERVIEW_ARCHIVE_START, INTERVIEW_ARCHIVE_END)
    if archive is None:
        return text
    compact = _compact_archived_interview_body(archive)
    if not compact:
        return _strip_managed_section(
            text, "과거 답변", INTERVIEW_ARCHIVE_START, INTERVIEW_ARCHIVE_END
        )
    replacement = "\n".join((INTERVIEW_ARCHIVE_START, compact, INTERVIEW_ARCHIVE_END))
    pattern = re.compile(
        re.escape(INTERVIEW_ARCHIVE_START) + r".*?" + re.escape(INTERVIEW_ARCHIVE_END),
        flags=re.DOTALL,
    )
    return pattern.sub(replacement, text, count=1)


def _compact_archived_interview_body(text: str) -> str:
    """Keep prior answers and evidence without repeating the stable question."""

    compact = re.sub(
        r"(?ms)^### 질문 맥락\s*\n.*?(?=^### |\Z)",
        "",
        text,
    )
    compact = re.sub(
        r"(?ms)^### 질문\s*\n.*?(?=^### |\Z)",
        "",
        compact,
    )
    compact = re.sub(
        r"(?ms)^### \d{4}-\d{2}-\d{2}[^\n]*\n\s*"
        r"### 답변\s*\n\s*아직 답변하지 않았다\.\s*"
        r"(?=^### \d{4}-\d{2}-\d{2}|\Z)",
        "",
        compact,
    )
    return re.sub(r"\n{3,}", "\n\n", compact).strip()


def _normalize_timeline_heading(text: str) -> str:
    return re.sub(
        rf"(?m)^## (?:시간 이력|한 줄 이력)\s*\n\s*(?={re.escape(WIKI_TIMELINE_START)})",
        "## 한 줄 이력\n\n",
        text,
    )


def _upsert_frontmatter_value(text: str, key: str, value: str) -> str:
    """Replace one root frontmatter property without leaving YAML block tails.

    Older Wiki pages commonly encode lists as a block::

        facets:
        - 개념
        - 학습

    Replacing only the ``facets:`` line turns the two list items into stray
    YAML and can create a second logical source of truth on the next run.  The
    replacement therefore owns the complete root-level YAML value, whether it
    was written inline or as a block.
    """

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---(?P<body>[\s\S]*)\Z", text)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")

    lines = match.group("yaml").splitlines()
    key_pattern = re.compile(rf"^{re.escape(key)}:[ \t]*(?:.*)$")
    root_key_pattern = re.compile(r"^[A-Za-z0-9_-]+:[ \t]*(?:.*)$")
    start = next((index for index, line in enumerate(lines) if key_pattern.match(line)), None)
    if start is None:
        return _insert_frontmatter_value(text, key, value)

    end = start + 1
    while end < len(lines) and not root_key_pattern.match(lines[end]):
        end += 1
    lines[start:end] = [f"{key}: {value}"]
    rendered = "\n".join(lines)
    return f"---\n{rendered}\n---{match.group('body')}"


def _insert_frontmatter_value(text: str, key: str, value: str) -> str:
    if not text.startswith("---\n"):
        raise WoonError("Wiki document requires YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise WoonError("Wiki frontmatter is malformed")
    return text[:end] + f"\n{key}: {value}" + text[end:]


def _optional_marker_body(text: str, start: str, end: str) -> str | None:
    if start not in text and end not in text:
        return None
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise WoonError("Wiki managed markers are malformed")
    return text.split(start, 1)[1].split(end, 1)[0].strip()


def _optional_marker_block(text: str, start: str, end: str) -> str | None:
    body = _optional_marker_body(text, start, end)
    if body is None:
        return None
    return "\n".join((start, body, end))


def _optional_h2_body(text: str, heading: str) -> str | None:
    """Read one legacy unmarked H2 body before converting it to managed form."""

    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n(?P<body>.*?)(?=^## |\Z)")
    matches = tuple(pattern.finditer(text))
    if not matches:
        return None
    if len(matches) > 1:
        raise WoonError(f"Wiki {heading} section is duplicated")
    body = re.sub(r"<!--.*?-->", "", matches[0].group("body"), flags=re.DOTALL).strip()
    return body or None


def _replace_or_append_managed_section(
    text: str, heading: str, start: str, end: str, replacement: str
) -> str:
    body = _optional_marker_body(text, start, end)
    if body is not None:
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        return pattern.sub(replacement, text, count=1)
    legacy_pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)")
    if legacy_pattern.search(text):
        refreshed = legacy_pattern.sub(f"## {heading}\n\n{replacement}\n\n", text, count=1)
        return refreshed.rstrip() + "\n"
    return text.rstrip() + f"\n\n## {heading}\n\n{replacement}\n"


def _strip_managed_section(text: str, heading: str, start: str, end: str) -> str:
    """Remove rendered managed sections while rejecting unbalanced markers.

    A historical curated source can contain the same complete block more than
    once.  All rendered copies are discarded because the live ``existing``
    document is the sole owner of managed context.  Unbalanced or detached
    markers still fail closed.
    """

    start_count = text.count(start)
    end_count = text.count(end)
    if start_count == 0 and end_count == 0:
        return text
    if start_count != end_count:
        raise WoonError("Wiki managed markers are malformed")
    if start == WIKI_TIMELINE_START:
        heading_pattern = r"(?:시간 이력|한 줄 이력)"
    elif start == WIKI_CURRENT_START:
        heading_pattern = rf"(?:현재 이해|{re.escape(WIKI_CURRENT_HEADING)})"
    else:
        heading_pattern = re.escape(heading)
    pattern = re.compile(
        rf"(?ms)^## {heading_pattern}\s*\n\s*"
        + re.escape(start)
        + r".*?"
        + re.escape(end)
        + r"\s*(?=^## |\Z)"
    )
    updated, count = pattern.subn("", text)
    if count != start_count or start in updated or end in updated:
        raise WoonError(f"Wiki {heading} managed section is malformed")
    return updated.rstrip() + "\n"


def _replace_or_append_h2(text: str, heading: str, body: str) -> str:
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n.*?(?=^## |\Z)")
    replacement = f"## {heading}\n\n{body}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return text.rstrip() + "\n\n" + replacement


def _merge_h2_rows(text: str, heading: str, rows: list[str]) -> str:
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)")
    match = pattern.search(text)
    if not match:
        return text.rstrip() + f"\n\n## {heading}\n\n" + "\n".join(rows) + "\n"
    existing = [line for line in match.group(1).splitlines() if line.strip()]
    merged = existing + [row for row in rows if row not in existing]
    return pattern.sub(f"## {heading}\n\n" + "\n".join(merged) + "\n\n", text, count=1)
