"""Project Wiki-owned Novel evidence into the private canonical keyword tree.

Raw files live only below ``wiki/private/_sources/novel``. They are physically
inside the one Woon Wiki vault but excluded from the human keyword tree. The
editable projection below ``wiki/private/novel`` is the sole navigation view.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import (
    SOURCE_INDEX_END,
    SOURCE_INDEX_START,
    parent_link,
    prepare_wiki_tree_refresh,
    preserve_generated_wiki_views,
    render_markdown,
    split_markdown,
)

_LINK = re.compile(r"(?m)^- \[(?P<label>[^]]+)]\((?P<target>[^)]+)\)\s*$")
_NUMBERED_H2 = re.compile(r"(?m)^## (?P<number>\d+)\. (?P<title>.+?)\s*$")
_H2 = re.compile(r"(?m)^## (?P<title>.+?)\s*$")
_SLUG = re.compile(r"[^0-9A-Za-z가-힣_-]+")


@dataclass(frozen=True, slots=True)
class NovelWikiProjectionReport:
    category_count: int
    source_count: int
    event_count: int
    judgment_count: int
    relation_count: int
    changed_count: int
    pages: dict[Path, bytes]
    stale_pages: tuple[Path, ...]
    manifest: bytes


def prepare_novel_wiki_projection(
    vault: Path, novel: Path, *, projection_day: date
) -> NovelWikiProjectionReport:
    """Create a complete private Wiki projection from the internal source archive."""

    root = vault.expanduser().resolve()
    novel_root = novel.expanduser().resolve()
    expected_source_root = root / "wiki/private/_sources/novel"
    if novel_root != expected_source_root:
        raise WoonError(
            "Novel source must live at wiki/private/_sources/novel inside the Wiki vault"
        )
    navigation = novel_root / "work/navigation"
    project_relative = "wiki/personal/projects/(미정)소설-집필.md"
    project_path = root / project_relative
    if not project_path.is_file() or not navigation.is_dir():
        raise WoonError("Novel Wiki projection requires the project entity and navigation tree")
    project_metadata, _ = split_markdown(project_path.read_text(encoding="utf-8"))
    project_title = str(project_metadata.get("title", "")).strip()
    if not project_title:
        raise WoonError("Novel project entity has no title")
    project_subject = project_title.removesuffix(" 집필").strip() or project_title

    category_files = tuple(
        path for path in sorted(navigation.glob("*.md")) if path.name != "README.md"
    )
    if not category_files:
        raise WoonError("Novel navigation has no keyword categories")

    input_sha256 = _projection_input_sha256(root, novel_root, project_title, category_files)
    effective_day = _effective_projection_day(root, input_sha256, projection_day)

    output_root = root / "wiki/private/novel"
    pages: dict[Path, bytes] = {}
    source_receipts: dict[str, dict[str, str]] = {}
    category_paths: dict[str, str] = {}
    category_metadata: dict[str, dict[str, object]] = {}
    category_source_groups: dict[str, list[tuple[str, list[tuple[str, Path]]]]] = {}
    source_count = 0
    for sequence, category_file in enumerate(category_files, start=1):
        category = _h1(category_file)
        category_slug = _slug(category)
        category_relative = f"wiki/private/novel/{category_slug}/README.md"
        category_paths[category_file.stem] = category_relative
        metadata = _metadata(
            title=f"소설 · {category}",
            canonical_id=f"private/novel/{category_slug}",
            parent=parent_link(project_relative, project_title),
            keyword=f"소설 · {category}",
            summary=f"{project_subject}의 {category} 키워드다.",
            day=effective_day,
            node_kind="hub",
            view_mode="tree",
            sequence=sequence,
        )
        category_metadata[category_file.stem] = metadata
        category_source_groups[category_file.stem] = []
        source_group_children: dict[str, list[tuple[str, Path]]] = {}
        category_path = root / category_relative
        pages[category_path] = _render_page(category_path, metadata, f"# 소설 · {category}\n")
        entries = _navigation_entries(category_file)
        for group_label, label, target in entries:
            source = (category_file.parent / target).resolve()
            if not source.is_relative_to(novel_root) or not source.is_file():
                raise WoonError(f"Novel navigation target is missing or escapes root: {source}")
            source_relative = source.relative_to(novel_root).as_posix()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if group_label not in source_group_children:
                source_group_children[group_label] = []
                category_source_groups[category_file.stem].append(
                    (group_label, source_group_children[group_label])
                )
            source_group_children[group_label].append((label, source))
            source_receipts[source_relative] = {
                "source_path": source_relative,
                "sha256": digest,
            }
            source_count += 1

    event_count = _count_event_sections(novel_root)
    judgment_count = _count_judgment_sections(novel_root)
    related_people = _relation_people(
        novel_root,
        source_receipts,
    )
    for category_name, metadata in category_metadata.items():
        category_path = root / category_paths[category_name]
        source_index = _source_index_body(
            category_path,
            str(metadata["title"]),
            category_source_groups[category_name],
            root,
            related_people=related_people if category_name == "인물" else (),
        )
        pages[category_path] = _render_page(
            category_path,
            metadata,
            source_index,
        )

    relation_count = len(related_people)

    expected = set(pages)
    stale = (
        tuple(path for path in sorted(output_root.rglob("*.md")) if path not in expected)
        if output_root.is_dir()
        else ()
    )
    changed = sum(
        1 for path, content in pages.items() if not path.is_file() or path.read_bytes() != content
    ) + len(stale)
    manifest = (
        json.dumps(
            {
                "version": 2,
                "projection_day": effective_day.isoformat(),
                "input_sha256": input_sha256,
                "category_count": len(category_files),
                "source_count": source_count,
                "event_count": event_count,
                "judgment_count": judgment_count,
                "relation_count": relation_count,
                "source_receipts": dict(sorted(source_receipts.items())),
                "stale_pages": [path.relative_to(root).as_posix() for path in stale],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return NovelWikiProjectionReport(
        len(category_files),
        source_count,
        event_count,
        judgment_count,
        relation_count,
        changed,
        pages,
        stale,
        manifest,
    )


def apply_novel_wiki_projection(vault: Path, report: NovelWikiProjectionReport) -> None:
    """Apply only the private Novel projection and validate the complete Wiki tree."""

    root = vault.expanduser().resolve()
    receipt = root / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    targets = (*report.pages.keys(), *report.stale_pages, receipt)
    snapshots = {path: path.read_bytes() if path.is_file() else None for path in targets}
    try:
        for path, content in report.pages.items():
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
            atomic_write(path, content, mode=mode)
        for path in report.stale_pages:
            path.unlink()
        atomic_write(receipt, report.manifest, mode=0o600)
        tree = prepare_wiki_tree_refresh(root)
        if tree.issues:
            raise WoonError(f"Novel Wiki projection failed validation: {tree.issues[0]}")
        for path, content in tree.pages.items():
            atomic_write(path, content, mode=path.stat().st_mode & 0o777)
    except Exception:
        for path, previous in reversed(tuple(snapshots.items())):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous, mode=0o600)
        raise


def _count_event_sections(novel: Path) -> int:
    ledger = novel / "work/analysis/event-evidence-ledger-2026-08-07.md"
    if not ledger.is_file():
        return 0
    return len(_NUMBERED_H2.findall(ledger.read_text(encoding="utf-8")))


def _count_judgment_sections(novel: Path) -> int:
    source = novel / "work/planning/corpus-reading-2026-08-07.md"
    if not source.is_file():
        return 0
    return len(_H2.findall(source.read_text(encoding="utf-8")))


def _relation_people(
    novel: Path,
    source_receipts: dict[str, dict[str, str]],
) -> tuple[tuple[str, str], ...]:
    source = novel / "work/people/person-link-ledger.yaml"
    if not source.is_file():
        return ()
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    people = payload.get("people", [])
    if not isinstance(people, list):
        raise WoonError("Novel person ledger people must be a list")
    people_targets = {
        "choi-woonyoung": ("최우녕", "wiki/personal/최우녕"),
        "kim-heejun": ("김희준", "wiki/personal/김희준"),
        "lee-minjeong": ("이민정", "wiki/private/이민정"),
    }
    resolved: list[tuple[str, str]] = []
    for item in people:
        if not isinstance(item, dict):
            raise WoonError("Novel person ledger entry must be a mapping")
        person_id = str(item.get("person_id", "")).strip()
        if person_id not in people_targets:
            raise WoonError(f"Novel person ledger has an unresolved person: {person_id}")
        for link in item.get("links", []):
            if not isinstance(link, dict):
                continue
            path = str(link.get("path", "")).strip()
            if path not in source_receipts:
                raise WoonError(f"Novel person link has no projected keyword page: {path}")
        resolved.append(people_targets[person_id])
    return tuple(resolved)


def _navigation_entries(path: Path) -> tuple[tuple[str, str, str], ...]:
    """Read link order and its human-authored H2 grouping axis."""

    current_group = ""
    entries: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current_group = line.removeprefix("## ").strip()
            continue
        match = _LINK.fullmatch(line)
        if match is None:
            continue
        entries.append((current_group, match.group("label").strip(), match.group("target")))
    if len(entries) > 1 and any(not group for group, _, _ in entries):
        raise WoonError(f"Novel navigation links require H2 groups: {path}")
    return tuple((group or "원자료", label, target) for group, label, target in entries)


def _source_index_body(
    page_path: Path,
    title: str,
    groups: list[tuple[str, list[tuple[str, Path]]]],
    vault: Path,
    *,
    related_people: tuple[tuple[str, str], ...] = (),
) -> str:
    rows = [
        f"# {title}",
        "",
        "## 원자료",
        "",
        SOURCE_INDEX_START,
    ]
    for group_label, links in groups:
        rows.append(f"- {group_label}")
        for label, source in links:
            rows.append(f"  - [{label}]({_file_link(source, page_path, vault)})")
    if related_people:
        rows.append("- 작품에 연결된 인물")
        for label, target in related_people:
            rows.append(f"  - [[{target}|{label}]]")
    rows.append(SOURCE_INDEX_END)
    return "\n".join(rows).rstrip() + "\n"


def _metadata(
    *,
    title: str,
    canonical_id: str,
    parent: str,
    keyword: str,
    summary: str,
    day: date,
    node_kind: str,
    view_mode: str,
    sequence: int,
) -> dict[str, object]:
    return {
        "type": "Wiki",
        "title": title,
        "canonical_id": canonical_id,
        "record_owner": "choi-woonyoung",
        "publish": False,
        "access": "local-only",
        "status": "Active",
        "facets": ["프로젝트"],
        "knowledge_state": "확인 필요",
        "state_reason": "novel-local-source-projection",
        "state_updated": day.isoformat(),
        "summary": summary,
        "aliases": [],
        "keywords": [keyword],
        "node_kind": node_kind,
        "view_mode": view_mode,
        "updated": day.isoformat(),
        "parent": parent,
        "sequence": sequence,
        "people": [],
        "related_to": [],
        "tags": [],
    }


def _h1(path: Path) -> str:
    match = re.search(r"(?m)^# (?P<title>.+?)\s*$", path.read_text(encoding="utf-8"))
    if match is None:
        raise WoonError(f"Novel navigation page has no H1: {path}")
    return match.group("title").strip()


def _render_page(path: Path, metadata: dict[str, object], body: str) -> bytes:
    rendered = render_markdown(metadata, body)
    if path.is_file():
        rendered = preserve_generated_wiki_views(path.read_text(encoding="utf-8"), rendered)
    return rendered.encode("utf-8")


def _slug(value: str) -> str:
    slug = _SLUG.sub("-", value.strip()).strip("-").casefold()
    if not slug:
        raise WoonError("Novel Wiki projection cannot create an empty slug")
    return slug


def _file_link(path: Path, page_path: Path, vault: Path) -> str:
    """Return an internal relative link without creating a second-vault dependency."""

    target = path.relative_to(vault)
    start = page_path.parent.relative_to(vault)
    relative = Path(os.path.relpath(target, start=start)).as_posix()
    return quote(relative, safe="/._-")


def _projection_input_sha256(
    vault: Path,
    novel: Path,
    project_title: str,
    categories: tuple[Path, ...],
) -> str:
    """Hash exactly the inputs that can change the editable Novel projection."""

    paths = {*categories}
    for category in categories:
        for match in _LINK.finditer(category.read_text(encoding="utf-8")):
            source = (category.parent / match.group("target")).resolve()
            if not source.is_relative_to(novel) or not source.is_file():
                raise WoonError(f"Novel navigation target is missing or escapes root: {source}")
            paths.add(source)
    paths.update(
        path
        for path in (
            novel / "work/analysis/event-evidence-ledger-2026-08-07.md",
            novel / "work/planning/corpus-reading-2026-08-07.md",
            novel / "work/people/person-link-ledger.yaml",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    digest.update(b"project-title\0")
    digest.update(project_title.encode("utf-8"))
    for path in sorted(paths):
        relative = path.relative_to(vault).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _effective_projection_day(vault: Path, input_sha256: str, requested: date) -> date:
    """Preserve metadata dates when a daily replay sees identical source inputs."""

    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    if not receipt.is_file():
        return requested
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"Novel Wiki projection receipt is unreadable: {error}") from error
    if payload.get("input_sha256") != input_sha256:
        return requested
    try:
        return date.fromisoformat(str(payload["projection_day"]))
    except (KeyError, TypeError, ValueError) as error:
        raise WoonError("Novel Wiki projection receipt has an invalid projection_day") from error
