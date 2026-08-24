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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write

WIKI_ROOT = "wiki"
WIKI_PERSONAL_ROOT = "wiki/personal"
WIKI_PRIVATE_ROOT = "wiki/private"
WIKI_CURRENT_START = "<!-- woon-wiki-current:start -->"
WIKI_CURRENT_END = "<!-- woon-wiki-current:end -->"
WIKI_TIMELINE_START = "<!-- woon-wiki-timeline:start -->"
WIKI_TIMELINE_END = "<!-- woon-wiki-timeline:end -->"
WIKI_NAVIGATION_START = "<!-- woon-wiki-navigation:start -->"
WIKI_NAVIGATION_END = "<!-- woon-wiki-navigation:end -->"
INTERVIEW_CURRENT_START = "<!-- woon-interview-current:start -->"
INTERVIEW_CURRENT_END = "<!-- woon-interview-current:end -->"
INTERVIEW_HISTORY_START = "<!-- woon-interview-history:start -->"
INTERVIEW_HISTORY_END = "<!-- woon-interview-history:end -->"
INTERVIEW_ARCHIVE_START = "<!-- woon-interview-archive:start -->"
INTERVIEW_ARCHIVE_END = "<!-- woon-interview-archive:end -->"

ALLOWED_FACETS = {
    "개념",
    "프로젝트",
    "콘텐츠",
    "인물",
    "커리어",
    "학습",
    "생활",
}
FACET_ORDER = ("개념", "프로젝트", "콘텐츠", "인물", "커리어", "학습", "생활")
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

_FILE_STEM_RE = re.compile(r"[^0-9A-Za-z가-힣_-]+")
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
    parent_topics: tuple[str, ...] = ()
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


@dataclass(frozen=True, slots=True)
class LegacyWikiMergeReport:
    """One prepared removal of parallel subject roots into the Wiki."""

    subject_count: int
    index_count: int
    pages: dict[Path, bytes]
    deletions: tuple[Path, ...]
    path_mapping: dict[str, str]
    projections_to_refresh: tuple[Path, ...]


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

    return f"{WIKI_PERSONAL_ROOT}/{_slug(title)}.md"


def resolve_wiki_path(vault: Path, title: str) -> Path:
    """Resolve exactly one existing Wiki identity by title or choose a new path."""

    root = vault.expanduser().resolve()
    wiki_root = root / WIKI_ROOT
    matches: list[Path] = []
    if wiki_root.is_dir():
        wanted = title.strip().casefold()
        for path in wiki_root.rglob("*.md"):
            if _frontmatter_text(path.read_text(encoding="utf-8"), "title").casefold() == wanted:
                matches.append(path)
    if len(matches) > 1:
        raise WoonError(f"Wiki title resolves to multiple documents: {title.strip()}")
    if matches:
        return matches[0]
    return root / wiki_relative_path(title)


def prepare_wiki_pages(vault: Path, deltas: tuple[WikiDelta, ...]) -> dict[Path, bytes]:
    """Return all Wiki writes for a batch without mutating the Vault."""

    root = vault.expanduser().resolve()
    resolved = tuple((resolve_wiki_path(root, delta.title), delta) for delta in deltas)
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
    for path in sorted(wiki_root.rglob("*.md")):
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
            parent_topics=_parent_topics(root, relative),
        )
        encoded = normalized.encode("utf-8")
        pages[path] = encoded
        if encoded != path.read_bytes():
            changed_count += 1
    if document_count == 0:
        raise WoonError("Wiki migration found no documents")
    return WikiMigrationReport(document_count, changed_count, pages)


def prepare_legacy_wiki_merge(vault: Path, *, migration_day: date) -> LegacyWikiMergeReport:
    """Prepare all old growth/project/content/person roots for one-time removal."""

    root = vault.expanduser().resolve()
    index_paths = {
        "projects/README.md",
        "content/README.md",
        "users/README.md",
    }
    sources = tuple(
        sorted(
            path
            for relative in ("brain/wiki", "projects", "content", "users")
            for path in (root / relative).rglob("*.md")
            if path.is_file()
        )
    )
    mapping: dict[str, str] = {
        "projects/README.md": "wiki/README.md",
        "content/README.md": "wiki/README.md",
        "users/README.md": "wiki/README.md",
    }
    pages: dict[Path, bytes] = {}
    subject_count = 0
    for source in sources:
        old_relative = source.relative_to(root).as_posix()
        if old_relative in index_paths:
            continue
        text = source.read_text(encoding="utf-8")
        title = _frontmatter_text(text, "title")
        if not title:
            raise WoonError(f"legacy Wiki subject requires title: {old_relative}")
        target = _legacy_target_path(root, old_relative, text, title)
        if target.exists():
            raise WoonError(f"legacy Wiki subject needs explicit duplicate merge: {title}")
        new_relative = target.relative_to(root).as_posix()
        mapping[old_relative] = new_relative
        facets = _legacy_facets(old_relative, text)
        state = _legacy_state(old_relative, text)
        normalized = _normalize_existing_wiki(
            text,
            canonical_id=Path(new_relative).with_suffix("").relative_to(WIKI_ROOT).as_posix(),
            facets=facets,
            knowledge_state=state,
            migration_day=migration_day,
            parent_topics=_parent_topics(root, Path(new_relative)),
        )
        pages[target] = normalized.encode("utf-8")
        subject_count += 1

    rewritten: dict[Path, bytes] = {}
    projections_to_refresh: list[Path] = []
    for path in sorted(root.rglob("*.md")):
        if ".local" in path.relative_to(root).parts or path in sources:
            continue
        original = pages.get(path, path.read_bytes()).decode("utf-8")
        updated = _rewrite_legacy_paths(original, mapping)
        if updated != original:
            if _is_owned_readonly_projection(root, path):
                projections_to_refresh.append(path)
            else:
                rewritten[path] = updated.encode("utf-8")
    for path, content in tuple(pages.items()):
        pages[path] = _rewrite_legacy_paths(content.decode("utf-8"), mapping).encode("utf-8")
    pages.update(rewritten)
    return LegacyWikiMergeReport(
        subject_count=subject_count,
        index_count=len(index_paths.intersection(mapping)),
        pages=pages,
        deletions=sources,
        path_mapping=mapping,
        projections_to_refresh=tuple(projections_to_refresh),
    )


def apply_legacy_wiki_merge(vault: Path, report: LegacyWikiMergeReport) -> None:
    """Apply a prepared legacy merge with full byte rollback on any failure."""

    root = vault.expanduser().resolve()
    touched = set(report.pages).union(report.deletions)
    snapshots: dict[Path, tuple[bytes | None, int]] = {
        path: (
            path.read_bytes() if path.is_file() else None,
            (path.stat().st_mode & 0o777) if path.exists() else 0o644,
        )
        for path in touched
    }
    try:
        for path, content in sorted(report.pages.items(), key=lambda item: item[0].as_posix()):
            if not path.resolve().is_relative_to(root):
                raise WoonError("legacy Wiki merge write escapes Vault")
            atomic_write(path, content, mode=snapshots[path][1])
        for path in report.deletions:
            if not path.resolve().is_relative_to(root):
                raise WoonError("legacy Wiki merge deletion escapes Vault")
            path.unlink()
    except Exception as original_error:
        rollback_errors: list[str] = []
        for path, (rollback_content, mode) in snapshots.items():
            try:
                if rollback_content is None:
                    path.unlink(missing_ok=True)
                elif path.read_bytes() != rollback_content:
                    atomic_write(path, rollback_content, mode=mode)
            except Exception as rollback_error:  # pragma: no cover - emergency detail
                rollback_errors.append(f"{path}: {rollback_error}")
        if rollback_errors:
            raise WoonError(
                "legacy Wiki merge failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from original_error
        raise


def _legacy_target_path(root: Path, relative: str, text: str, title: str) -> Path:
    if (
        relative.startswith("users/")
        and _frontmatter_text(text, "person_scope") == "novel-local-only"
    ):
        return root / WIKI_PRIVATE_ROOT / f"{_slug(title)}.md"
    return resolve_wiki_path(root, title)


def _is_owned_readonly_projection(root: Path, path: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return relative.startswith(
        (
            "inbox/calendar/events/",
            "inbox/private-person-history/",
        )
    )


def _legacy_facets(relative: str, text: str) -> tuple[str, ...]:
    page_type = _frontmatter_text(text, "type").casefold()
    if relative.startswith("projects/") or page_type == "project":
        return ("프로젝트",)
    if relative.startswith("content/") or page_type == "content":
        content_kind = _frontmatter_text(text, "content_kind")
        return (
            ("콘텐츠", "학습")
            if content_kind in {"book", "lecture", "course", "article", "learning-material-bundle"}
            else ("콘텐츠",)
        )
    if relative.startswith("users/") and page_type != "project":
        return ("인물",)
    return _infer_facets(Path(relative), text)


def _legacy_state(relative: str, text: str) -> str:
    current = _frontmatter_text(text, "knowledge_state")
    if current in ALLOWED_KNOWLEDGE_STATES:
        return current
    if relative.startswith("brain/wiki/"):
        return "생각 중"
    return "확인 필요"


def _rewrite_legacy_paths(text: str, mapping: dict[str, str]) -> str:
    updated = text
    for old, new in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
        for old_value, new_value in (
            (old, new),
            (old.removesuffix(".md"), new.removesuffix(".md")),
        ):
            updated = updated.replace(old_value, new_value)
    return updated


def preserve_managed_context(existing: str, rendered: str) -> str:
    """Carry Woon context and its metadata across an evidence compiler render."""

    if existing and _frontmatter_text(existing, "knowledge_state") == "폐기됨":
        raise WoonError("A retired Wiki document cannot be compiled again")
    # A curated source can contain a previously rendered managed section.  The
    # compiler also carries the live section from ``existing`` below, so first
    # remove managed sections from the rendered body.  Otherwise every forced
    # compile duplicates the timeline/navigation blocks and the next run can no
    # longer parse them safely.
    merged = _strip_managed_section(
        rendered, "주제 연결", WIKI_NAVIGATION_START, WIKI_NAVIGATION_END
    )
    merged = _strip_managed_section(merged, "현재 이해", WIKI_CURRENT_START, WIKI_CURRENT_END)
    merged = _strip_managed_section(merged, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    merged = _upsert_frontmatter_value(merged, "type", "Wiki")
    existing_identity = _frontmatter_value(existing, "canonical_id") if existing else None
    if existing_identity is not None and not _frontmatter_raw(merged, "canonical_id"):
        merged = _set_frontmatter_object(merged, "canonical_id", existing_identity)
    existing_parent = _frontmatter_value(existing, "parent_topics") if existing else None
    if existing_parent is not None and _frontmatter_value(merged, "parent_topics") is None:
        merged = _set_frontmatter_object(merged, "parent_topics", existing_parent)
    for key in (
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
        value = _frontmatter_value(existing, key) if existing else None
        if value is not None and _frontmatter_value(merged, key) is None:
            merged = _set_frontmatter_object(merged, key, value)
    merged = _upsert_frontmatter_value(
        merged, "knowledge_state", json.dumps("근거 확인됨", ensure_ascii=False)
    )
    merged = _upsert_frontmatter_value(merged, "state_reason", "accepted-evidence-receipt")
    if not existing:
        return merged
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
        return merged
    blocks: list[str] = []
    if navigation is not None:
        blocks.extend(("## 주제 연결", "", navigation, ""))
    if current is not None:
        blocks.extend(("## 현재 이해", "", current))
    if timeline is not None:
        blocks.extend(("", "## 시간 이력", "", timeline))
    return merged.rstrip() + "\n\n" + "\n".join(blocks).rstrip() + "\n"


def _normalize_existing_wiki(
    text: str,
    *,
    canonical_id: str,
    facets: tuple[str, ...],
    knowledge_state: str,
    migration_day: date,
    parent_topics: tuple[str, ...] = (),
) -> str:
    updated = _upsert_frontmatter_value(text, "type", "Wiki")
    updated = _upsert_frontmatter_value(
        updated, "canonical_id", json.dumps(canonical_id, ensure_ascii=False)
    )
    updated = _upsert_frontmatter_value(updated, "facets", json.dumps(facets, ensure_ascii=False))
    updated = _upsert_frontmatter_value(
        updated, "knowledge_state", json.dumps(knowledge_state, ensure_ascii=False)
    )
    updated = _upsert_frontmatter_value(
        updated,
        "status",
        "Archived" if knowledge_state in {"오래됨", "폐기됨"} else "Active",
    )
    updated = _upsert_frontmatter_value(
        updated, "parent_topics", json.dumps(parent_topics, ensure_ascii=False)
    )
    if not _frontmatter_text(updated, "summary"):
        updated = _upsert_frontmatter_value(
            updated,
            "summary",
            json.dumps(_summary_from_document(updated), ensure_ascii=False),
        )
    updated = _upsert_frontmatter_value(
        updated,
        "state_reason",
        {
            "근거 확인됨": "accepted-evidence-receipt",
            "오래됨": "legacy-lifecycle",
        }.get(knowledge_state, "legacy-normalization"),
    )
    updated = _upsert_frontmatter_value(updated, "state_updated", migration_day.isoformat())
    if not _frontmatter_raw(updated, "record_owner"):
        updated = _insert_frontmatter_value(updated, "record_owner", "choi-woonyoung")

    if parent_topics:
        navigation = "\n".join(
            (WIKI_NAVIGATION_START, *(f"- {link}" for link in parent_topics), WIKI_NAVIGATION_END)
        )
        updated = _replace_or_append_managed_section(
            updated,
            "주제 연결",
            WIKI_NAVIGATION_START,
            WIKI_NAVIGATION_END,
            navigation,
        )

    timeline_body = _optional_marker_body(updated, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    rows = [
        line
        for line in (timeline_body or "").splitlines()
        if line.strip() and not _MIGRATION_TIMELINE_RE.fullmatch(line.strip())
    ]
    if not rows:
        if timeline_body is None:
            return updated
        return _strip_managed_section(updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    timeline = "\n".join((WIKI_TIMELINE_START, *rows, WIKI_TIMELINE_END))
    return _replace_or_append_managed_section(
        updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END, timeline
    )


def _parent_topics(root: Path, relative: Path) -> tuple[str, ...]:
    """Return one deterministic parent edge so every visible Wiki reaches the root."""

    if relative.as_posix() == "wiki/README.md":
        return ()
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
    return (f"[[{link}|{title or 'Wiki'}]]",)


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
    facets = [item for item in _frontmatter_list(text, "facets") if item in ALLOWED_FACETS]
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
    is_content = "콘텐츠" in facets or bool(content_kind)

    # The first unified migration temporarily gave every page the generic
    # concept/learning pair.  Strong entity properties are authoritative and
    # must remove that fallback so Facet filters retain their meaning.
    if is_person:
        facets = [item for item in facets if item not in {"개념", "학습"}]
    elif is_project or is_content:
        facets = [item for item in facets if item != "개념"]

    if any(tag == "domain:career" for tag in raw_tags):
        facets.append("커리어")
    if is_person:
        facets.append("인물")
    if is_project:
        facets.append("프로젝트")
    if is_content:
        facets.append("콘텐츠")
        if content_kind in {
            "book",
            "lecture",
            "course",
            "article",
            "learning-material-bundle",
        }:
            facets.append("학습")
    if any(
        tag.startswith("domain:") or tag.startswith("topic:") or tag.startswith("book:")
        for tag in raw_tags
    ):
        if not (is_person or is_project or is_content):
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
    explicit_parent_topics = _frontmatter_value(text, "parent_topics")
    if explicit_parent_topics is not None:
        if not isinstance(explicit_parent_topics, list) or not all(
            isinstance(item, str) and item.strip() for item in explicit_parent_topics
        ):
            raise WoonError("compiled Wiki parent_topics must be a non-empty string list")
        parent_topics = tuple(item.strip() for item in explicit_parent_topics)
    elif path_identity == "README":
        parent_topics = ()
    elif (
        len(relative.parts) > 1
        and relative.name != "README.md"
        and relative.parts[0] in COMPILED_SECTION_ROOTS
    ):
        parent_topics = (f"[[wiki/{relative.parts[0]}/README|{relative.parts[0]}]]",)
    else:
        parent_topics = ("[[wiki/README|Wiki]]",)
    return {
        "type": "Wiki",
        "canonical_id": canonical_id,
        "facets": list(_infer_facets(vault_relative, text)),
        "knowledge_state": "근거 확인됨",
        "state_reason": "accepted-evidence-receipt",
        "status": "Active",
        "record_owner": "choi-woonyoung",
        "parent_topics": list(parent_topics),
        "summary": _summary_from_document(text),
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

    facets = _frontmatter_list(text, "facets")
    merged_facets = tuple(dict.fromkeys((*facets, *delta.facets)))
    updated = _upsert_frontmatter_value(
        text, "facets", json.dumps(merged_facets, ensure_ascii=False)
    )
    if delta.parent_topics:
        updated = _upsert_frontmatter_value(
            updated,
            "parent_topics",
            json.dumps(delta.parent_topics, ensure_ascii=False),
        )
    elif not _frontmatter_raw(updated, "parent_topics"):
        updated = _insert_frontmatter_value(
            updated,
            "parent_topics",
            json.dumps(("[[wiki/README|Wiki]]",), ensure_ascii=False),
        )
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

    current = "\n".join((WIKI_CURRENT_START, delta.summary.strip(), WIKI_CURRENT_END))
    updated = _replace_or_append_managed_section(
        updated, "현재 이해", WIKI_CURRENT_START, WIKI_CURRENT_END, current
    )

    row = f"- {delta.day.isoformat()} · {delta.event_kind} — {delta.summary.strip()}"
    previous_timeline = _optional_marker_body(updated, WIKI_TIMELINE_START, WIKI_TIMELINE_END)
    rows = [line for line in (previous_timeline or "").splitlines() if line.strip()]
    if row not in rows:
        rows.append(row)
    timeline = "\n".join((WIKI_TIMELINE_START, *rows, WIKI_TIMELINE_END))
    updated = _replace_or_append_managed_section(
        updated, "시간 이력", WIKI_TIMELINE_START, WIKI_TIMELINE_END, timeline
    )
    if delta.intent:
        updated = _replace_or_append_h2(updated, "남긴 의도", f"추정 의도: {delta.intent.strip()}")
    if delta.next_question:
        updated = _replace_or_append_h2(updated, "다음 질문", delta.next_question.strip())
    if delta.related_documents:
        rows = [
            f"- [[{Path(path).with_suffix('').as_posix()}]]" for path in delta.related_documents
        ]
        updated = _merge_h2_rows(updated, "연결", rows)
    if delta.interview_answer is not None:
        updated = _merge_interview_answer(updated, delta)
    return updated


def _render_new(delta: WikiDelta) -> str:
    current = "\n".join((WIKI_CURRENT_START, delta.summary.strip(), WIKI_CURRENT_END))
    timeline = "\n".join(
        (
            WIKI_TIMELINE_START,
            f"- {delta.day.isoformat()} · {delta.event_kind} — {delta.summary.strip()}",
            WIKI_TIMELINE_END,
        )
    )
    lines = [
        "---",
        "type: Wiki",
        f"title: {json.dumps(delta.title.strip(), ensure_ascii=False)}",
        f"canonical_id: {json.dumps(f'personal/{_slug(delta.title)}', ensure_ascii=False)}",
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Active",
        f"facets: {json.dumps(delta.facets, ensure_ascii=False)}",
        (
            "parent_topics: "
            + json.dumps(
                delta.parent_topics or ("[[wiki/README|Wiki]]",),
                ensure_ascii=False,
            )
        ),
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
        "",
        "## 현재 이해",
        "",
        current,
        "",
        "## 시간 이력",
        "",
        timeline,
    ]
    lines = _insert_new_facet_properties(lines, delta)
    if delta.intent:
        lines.extend(("", "## 남긴 의도", "", f"추정 의도: {delta.intent.strip()}"))
    if delta.next_question:
        lines.extend(("", "## 다음 질문", "", delta.next_question.strip()))
    if delta.related_documents:
        lines.extend(("", "## 연결", ""))
        lines.extend(
            f"- [[{Path(path).with_suffix('').as_posix()}]]" for path in delta.related_documents
        )
    if delta.interview_answer is not None:
        lines.extend(_new_interview_sections(delta))
    return "\n".join((*lines, ""))


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
            or not relative.startswith(("wiki/", "maps/"))
            or (relative not in planned_paths and not (vault / candidate).is_file())
        ):
            raise WoonError("Wiki relation must point to an existing Wiki or Map")
    for parent in delta.parent_topics:
        target = _wikilink_target(parent)
        candidate = Path(f"{target}.md") if not target.endswith(".md") else Path(target)
        if (
            not target.startswith("wiki/")
            or candidate.is_absolute()
            or ".." in candidate.parts
            or (candidate.as_posix() not in planned_paths and not (vault / candidate).is_file())
        ):
            raise WoonError("Wiki parent topic must point to an existing Wiki")
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
        block = f"### {delta.day.isoformat()} · {prior_label}\n\n{previous.strip()}"
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
    answer = revision.answer.strip() if revision.answer else "아직 답변하지 않았다."
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
    if revision.answer is not None:
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


def _wikilink_target(value: str) -> str:
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

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---", text)
    if match is None:
        return None
    data = yaml.safe_load(match.group("yaml")) or {}
    if not isinstance(data, dict):
        return None
    return data.get(key)


def _set_frontmatter_object(text: str, key: str, value: object) -> str:
    """Set one YAML value while preserving the Markdown body verbatim."""

    match = re.match(r"\A---\n(?P<yaml>[\s\S]*?)\n---(?P<body>[\s\S]*)\Z", text)
    if match is None:
        raise WoonError("Wiki document requires YAML frontmatter")
    data = yaml.safe_load(match.group("yaml")) or {}
    if not isinstance(data, dict):
        raise WoonError("Wiki frontmatter is malformed")
    data[key] = value
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


def _upsert_frontmatter_value(text: str, key: str, value: str) -> str:
    if _frontmatter_raw(text, key):
        return re.sub(rf"(?m)^{re.escape(key)}:\s*.*$", f"{key}: {value}", text, count=1)
    return _insert_frontmatter_value(text, key, value)


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


def _replace_or_append_managed_section(
    text: str, heading: str, start: str, end: str, replacement: str
) -> str:
    body = _optional_marker_body(text, start, end)
    if body is not None:
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
        return pattern.sub(replacement, text, count=1)
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
    pattern = re.compile(
        rf"(?ms)^## {re.escape(heading)}\s*\n\s*"
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
