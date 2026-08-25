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
        category_path = root / category_relative
        pages[category_path] = _render_page(category_path, metadata, f"# 소설 · {category}\n")
        for item_sequence, match in enumerate(
            _LINK.finditer(category_file.read_text(encoding="utf-8")), start=1
        ):
            label = match.group("label").strip()
            source = (category_file.parent / match.group("target")).resolve()
            if not source.is_relative_to(novel_root) or not source.is_file():
                raise WoonError(f"Novel navigation target is missing or escapes root: {source}")
            source_relative = source.relative_to(novel_root).as_posix()
            page_relative = (
                f"wiki/private/novel/{category_slug}/{item_sequence:02d}-{_slug(label)}.md"
            )
            title = label
            metadata = _metadata(
                title=title,
                canonical_id=(
                    f"private/novel/{category_slug}/source-{item_sequence:02d}-{_slug(label)}"
                ),
                parent=parent_link(category_relative, f"소설 · {category}"),
                keyword=title,
                summary=f"{label}의 local-only 원본 연결이다.",
                day=effective_day,
                node_kind="detail",
                view_mode="article",
                sequence=item_sequence,
            )
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            metadata.update(
                {
                    "source_workspace": "Wiki",
                    "source_path": source.relative_to(root).as_posix(),
                    "source_sha256": digest,
                    "source_authority": "local-only-original",
                }
            )
            page_path = root / page_relative
            link = _file_link(source, page_path, root)
            body = f"# {title}\n\n## 원본\n\n- [{label}]({link})\n"
            pages[page_path] = _render_page(page_path, metadata, body)
            source_receipts[source_relative] = {
                "wiki_path": page_relative,
                "sha256": digest,
            }
            source_count += 1

    event_count = _add_event_pages(
        root, novel_root, pages, category_paths, projection_day=effective_day
    )
    judgment_count = _add_judgment_pages(
        root,
        novel_root,
        pages,
        category_paths,
        project_subject=project_subject,
        projection_day=effective_day,
    )
    relation_count = _add_relation_pages(
        root,
        novel_root,
        pages,
        category_paths,
        source_receipts,
        project_subject=project_subject,
        projection_day=effective_day,
    )

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


def _add_event_pages(
    root: Path,
    novel: Path,
    pages: dict[Path, bytes],
    categories: dict[str, str],
    *,
    projection_day: date,
) -> int:
    ledger = novel / "work/analysis/event-evidence-ledger-2026-08-07.md"
    parent = categories.get("사건-히스토리")
    if parent is None or not ledger.is_file():
        return 0
    text = ledger.read_text(encoding="utf-8")
    matches = list(_NUMBERED_H2.finditer(text))
    for index, match in enumerate(matches):
        number = int(match.group("number"))
        title = match.group("title").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end() : end].strip().removesuffix("---").strip()
        page_title = f"소설 사건 {number:02d} · {title}"
        relative = f"wiki/private/novel/사건-히스토리/event-{number:02d}.md"
        metadata = _metadata(
            title=page_title,
            canonical_id=f"private/novel/events/{number:02d}",
            parent=parent_link(parent, "소설 · 사건·히스토리"),
            keyword=page_title,
            summary=f"사건 {number:02d}의 사실·해석·허구 경계를 관리한다.",
            day=projection_day,
            node_kind="detail",
            view_mode="topic-timeline",
            sequence=100 + number,
        )
        metadata["source_path"] = ledger.relative_to(root).as_posix()
        page_path = root / relative
        body = (
            f"# {page_title}\n\n{section}\n\n## 원본\n\n"
            f"- [사건 장부]({_file_link(ledger, page_path, root)})\n"
        )
        pages[page_path] = _render_page(page_path, metadata, body)
    return len(matches)


def _add_judgment_pages(
    root: Path,
    novel: Path,
    pages: dict[Path, bytes],
    categories: dict[str, str],
    *,
    project_subject: str,
    projection_day: date,
) -> int:
    source = novel / "work/planning/corpus-reading-2026-08-07.md"
    parent = categories.get("집필-계획")
    if parent is None or not source.is_file():
        return 0
    text = source.read_text(encoding="utf-8")
    matches = list(_H2.finditer(text))
    for index, match in enumerate(matches):
        heading = match.group("title").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.end() : end].strip()
        title = f"소설 판단 · {heading}"
        relative = f"wiki/private/novel/집필-계획/judgment-{index + 1:02d}-{_slug(heading)}.md"
        metadata = _metadata(
            title=title,
            canonical_id=f"private/novel/judgments/{index + 1:02d}-{_slug(heading)}",
            parent=parent_link(parent, "소설 · 집필 계획"),
            keyword=title,
            summary=f"{project_subject}의 {heading} 판단이다.",
            day=projection_day,
            node_kind="decision",
            view_mode="article",
            sequence=100 + index,
        )
        metadata["source_path"] = source.relative_to(root).as_posix()
        page_path = root / relative
        body = (
            f"# {title}\n\n{section}\n\n## 원본\n\n"
            f"- [전체 독해와 집필 판단]({_file_link(source, page_path, root)})\n"
        )
        pages[page_path] = _render_page(page_path, metadata, body)
    return len(matches)


def _add_relation_pages(
    root: Path,
    novel: Path,
    pages: dict[Path, bytes],
    categories: dict[str, str],
    source_receipts: dict[str, dict[str, str]],
    *,
    project_subject: str,
    projection_day: date,
) -> int:
    source = novel / "work/people/person-link-ledger.yaml"
    parent = categories.get("인물")
    if parent is None or not source.is_file():
        return 0
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    people = payload.get("people", [])
    if not isinstance(people, list):
        raise WoonError("Novel person ledger people must be a list")
    labels = {
        "choi-woonyoung": ("최우녕", "wiki/personal/최우녕"),
        "kim-heejun": ("김희준", "wiki/personal/김희준"),
        "lee-minjeong": ("이민정", "wiki/private/이민정"),
    }
    count = 0
    for sequence, item in enumerate(people, start=1):
        if not isinstance(item, dict):
            raise WoonError("Novel person ledger entry must be a mapping")
        person_id = str(item.get("person_id", "")).strip()
        if person_id not in labels:
            raise WoonError(f"Novel person ledger has an unresolved person: {person_id}")
        label, person_path = labels[person_id]
        title = f"소설 인물 · {label}"
        relative = f"wiki/private/novel/인물/{person_id}.md"
        metadata = _metadata(
            title=title,
            canonical_id=f"private/novel/people/{person_id}",
            parent=parent_link(parent, "소설 · 인물"),
            keyword=title,
            summary=f"{project_subject}과 {label}의 확인된 연결이다.",
            day=projection_day,
            node_kind="detail",
            view_mode="article",
            sequence=100 + sequence,
        )
        metadata.update(
            {
                "source_path": source.relative_to(root).as_posix(),
                "person_id": person_id,
            }
        )
        rows = [f"- [[{person_path}|{label}]]"]
        for link in item.get("links", []):
            if not isinstance(link, dict):
                continue
            path = str(link.get("path", "")).strip()
            receipt = source_receipts.get(path)
            if receipt is None:
                raise WoonError(f"Novel person link has no projected keyword page: {path}")
            target = Path(receipt["wiki_path"]).with_suffix("").as_posix()
            rows.append(f"- [[{target}|{Path(path).stem}]]")
        body = f"# {title}\n\n## 연결\n\n" + "\n".join(rows) + "\n"
        page_path = root / relative
        pages[page_path] = _render_page(page_path, metadata, body)
        count += 1
    return count


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
