"""One-time, replay-safe migration from Markdown maps to the canonical Wiki tree."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import (
    LEGACY_TREE_FIELDS,
    iter_wiki_pages,
    parent_link,
    prepare_wiki_tree_refresh,
    render_markdown,
    split_markdown,
    strip_generated_wiki_views,
)

_MAP_TARGETS = {
    "maps/wiki-keyword-tree.md": "wiki/README.md",
    "maps/concepts-index.md": "wiki/concepts/README.md",
    "maps/content-index.md": "wiki/resources/README.md",
    "maps/books-index.md": "wiki/books/README.md",
    "maps/learning-index.md": "wiki/learning/README.md",
    "maps/life-index.md": "wiki/life/README.md",
    "maps/people-index.md": "wiki/people/README.md",
    "maps/book-build-llm-from-scratch-map.md": "wiki/personal/밑바닥부터-만들면서-배우는-llm.md",
    "maps/book-deep-learning-from-scratch-1-map.md": (
        "wiki/personal/밑바닥부터-시작하는-딥러닝-1.md"
    ),
}

_MAP_PARENT_NAMES = {
    "concepts-index": "wiki/README.md",
    "content-index": "wiki/README.md",
    "books-index": "wiki/README.md",
    "learning-index": "wiki/README.md",
    "life-index": "wiki/README.md",
    "people-index": "wiki/README.md",
    "ai-concept-to-code-map": "wiki/ai/README.md",
    "ai-neural-network-moc": "wiki/ai/README.md",
    "ai-llm-moc": "wiki/ai/README.md",
    "math-statistics-foundations-map": "maps/ai-neural-network-moc.md",
    "neural-network-fundamentals-map": "maps/ai-neural-network-moc.md",
    "cnn-vision-map": "maps/ai-neural-network-moc.md",
    "sequence-model-rnn-map": "maps/ai-neural-network-moc.md",
    "transformer-attention-map": "maps/ai-llm-moc.md",
    "llm-pretraining-scaling-map": "maps/ai-llm-moc.md",
    "llm-alignment-finetuning-map": "maps/ai-llm-moc.md",
    "llm-inference-serving-map": "maps/ai-llm-moc.md",
    "rag-agent-application-map": "maps/ai-llm-moc.md",
    "algorithm-data-structure-map": "wiki/algorithm/README.md",
    "backend-runtime-map": "wiki/backend/README.md",
    "web-server-proxy-map": "maps/backend-runtime-map.md",
    "database-storage-map": "wiki/database/README.md",
    "network-protocol-map": "wiki/network/README.md",
    "security-web-map": "wiki/security/README.md",
    "tools-obsidian-pkm-map": "wiki/tools/README.md",
    "vault-taxonomy-map": "wiki/tools/README.md",
    "knowledge-operations-map": "wiki/tools/README.md",
    "context-graph/README": "wiki/tools/README.md",
    "concept-to-code-map": "wiki/os/README.md",
    "os-moc": "wiki/os/README.md",
    "os-responsibility-boundary-map": "maps/os-moc.md",
    "cpu-execution-program-loading-map": "maps/os-moc.md",
    "threads-execution-model-map": "maps/os-moc.md",
    "process-lifecycle-map": "maps/os-moc.md",
    "user-program-execution-boundary-map": "maps/os-moc.md",
    "virtual-memory-translation-map": "maps/os-moc.md",
    "file-system-storage-map": "maps/os-moc.md",
    "qemu-hardware-byte-debugging-map": "maps/os-moc.md",
    "pintos-moc": "wiki/os/README.md",
    "pintos-alarm-clock-question-navigation": "maps/pintos-moc.md",
    "pintos-process-visual-map": "maps/pintos-moc.md",
    "pintos-user-program-execution-visual-map": "maps/pintos-moc.md",
    "pintos-vm-implementation-readiness-map": "maps/pintos-moc.md",
    "pintos-vm-visual-map": "maps/pintos-moc.md",
    "aws-immersion-day-map": "wiki/personal/projects/README.md",
    "portfolio-project-moc": "wiki/personal/projects/README.md",
    "creative-index": "wiki/README.md",
    "cs-basics-map": "wiki/common/README.md",
    "local-private-index": "wiki/README.md",
    "resource-index": "wiki/README.md",
}

_TOP_HUB_PATHS = {
    "wiki/concepts/README.md",
    "wiki/resources/README.md",
    "wiki/books/README.md",
    "wiki/learning/README.md",
    "wiki/life/README.md",
    "wiki/people/README.md",
}

_DUPLICATE_WIKI_MERGES = {
    "wiki/ai/maximum-a-posteriori.md": "wiki/ai/map-estimation.md",
}


@dataclass(frozen=True, slots=True)
class WikiTreeMigrationReport:
    wiki_document_count: int
    map_document_count: int
    changed_count: int
    pages: dict[Path, bytes]
    catalog_writes: dict[Path, bytes]
    deletions: tuple[Path, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WikiTreeRepairReport:
    pages: dict[Path, bytes]
    catalog_pages: bytes
    changed_count: int


_REPARENT = {
    "wiki/ai/README.md": "wiki/concepts/README.md",
    "wiki/algorithm/README.md": "wiki/concepts/README.md",
    "wiki/backend/README.md": "wiki/concepts/README.md",
    "wiki/common/README.md": "wiki/concepts/README.md",
    "wiki/database/README.md": "wiki/concepts/README.md",
    "wiki/network/README.md": "wiki/concepts/README.md",
    "wiki/os/README.md": "wiki/concepts/README.md",
    "wiki/security/README.md": "wiki/concepts/README.md",
    "wiki/tools/README.md": "wiki/concepts/README.md",
    "wiki/books/README.md": "wiki/README.md",
    "wiki/hubs/cs-basics.md": "wiki/common/README.md",
    "wiki/hubs/creative-index.md": "wiki/personal/projects/README.md",
    "wiki/hubs/local-private-index.md": "wiki/tools/README.md",
    "wiki/hubs/resource-index.md": "wiki/resources/README.md",
    "wiki/knowledge/brain-trinity-evaluation-2026-08-14.md": "wiki/hubs/knowledge-operations.md",
    "wiki/personal/aws-워크숍과-강의-자료-2026.md": "wiki/resources/aws.md",
    "wiki/personal/cedis-알고리즘-참고-글-묶음.md": "wiki/resources/algorithm.md",
    "wiki/personal/codex-대화는-완료된-경계부터-누적-정리한다.md": (
        "wiki/hubs/knowledge-operations.md"
    ),
    "wiki/personal/interview/README.md": "wiki/personal/career/README.md",
    "wiki/personal/interview/topics/README.md": "wiki/personal/interview/README.md",
    "wiki/personal/transformer-explainer.md": "wiki/resources/ai.md",
    "wiki/personal/구요한-교수-obsidian-운영-사례.md": "wiki/resources/obsidian.md",
    "wiki/personal/이력서-복원은-검증-문장-우선.md": "wiki/personal/career/README.md",
    "wiki/personal/자동화는-실제-산출물로-검증한다.md": "wiki/hubs/knowledge-operations.md",
    "wiki/personal/테스트-실패-원인을-조건별로-분리한다.md": ("wiki/hubs/knowledge-operations.md"),
    "wiki/personal/플러그인-가치는-연결된-원문-탐색-경험에-둔다.md": ("wiki/tools/README.md"),
    "wiki/private/이민정.md": "wiki/private/README.md",
}


def prepare_wiki_tree_repair(vault: Path) -> WikiTreeRepairReport:
    """Repair semantic parents and retired Map references after the one-time move."""

    root = vault.expanduser().resolve()
    manifest_path = root / ".local/woon-knowledge/wiki-tree-migration/manifest.json"
    if not manifest_path.is_file():
        raise WoonError("Wiki tree repair requires the migration manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping = manifest.get("map_to_wiki")
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
    ):
        raise WoonError("Wiki tree migration manifest has invalid Map mapping")
    map_stems = _map_stem_index(mapping)
    documents: dict[str, tuple[dict[str, Any], str]] = {}
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        metadata, body = split_markdown(path.read_text(encoding="utf-8"))
        metadata = _rewrite_object_wikilinks(metadata, mapping, map_stems)
        body = _rewrite_duplicate_wikilinks(_rewrite_wikilinks(body, mapping, map_stems))
        body = _repair_retired_map_prose(body)
        documents[relative] = (metadata, body)

    titles = {
        relative: str(metadata.get("title", "")).strip()
        for relative, (metadata, _) in documents.items()
    }
    project = "wiki/personal/리모트ai-가칭.md"
    for relative, (metadata, body) in documents.items():
        if relative == "wiki/README.md":
            metadata.pop("parent", None)
            metadata.pop("sequence", None)
        else:
            parent = _REPARENT.get(relative)
            title = str(metadata.get("title", ""))
            entity = _entity_kind(metadata)
            if title.startswith("리모트AI —"):
                parent = project
            elif entity == "book":
                parent = "wiki/books/README.md"
            elif entity == "person" and metadata.get("person_scope") == "novel-local-only":
                parent = "wiki/private/README.md"
            elif entity == "person":
                parent = "wiki/people/README.md"
            if parent is not None:
                metadata["parent"] = parent_link(parent, titles[parent])
                metadata.pop("sequence", None)
        documents[relative] = (metadata, body)

    rendered = {
        root / relative: render_markdown(metadata, body).encode("utf-8")
        for relative, (metadata, body) in documents.items()
    }
    catalog_path = root / "catalog/llm-wiki/pages.yaml"
    catalog = _catalog_with_tree_metadata(catalog_path, documents)
    changed = sum(1 for path, content in rendered.items() if path.read_bytes() != content) + int(
        catalog_path.read_bytes() != catalog
    )
    return WikiTreeRepairReport(rendered, catalog, changed)


def apply_wiki_tree_repair(vault: Path, report: WikiTreeRepairReport) -> None:
    root = vault.expanduser().resolve()
    catalog_path = root / "catalog/llm-wiki/pages.yaml"
    snapshots = {path: path.read_bytes() for path in {*report.pages.keys(), catalog_path}}
    try:
        for path, content in report.pages.items():
            atomic_write(path, content, mode=path.stat().st_mode & 0o777)
        atomic_write(catalog_path, report.catalog_pages, mode=catalog_path.stat().st_mode & 0o777)
        tree = prepare_wiki_tree_refresh(root)
        if tree.issues:
            raise WoonError(f"Wiki tree repair failed validation: {tree.issues[0]}")
        for path, content in tree.pages.items():
            atomic_write(path, content, mode=path.stat().st_mode & 0o777)
    except Exception:
        for path, content in snapshots.items():
            atomic_write(path, content, mode=path.stat().st_mode & 0o777)
        raise


def prepare_wiki_tree_migration(vault: Path, *, migration_day: date) -> WikiTreeMigrationReport:
    root = vault.expanduser().resolve()
    wiki_root = root / "wiki"
    maps_root = root / "maps"
    catalog_path = root / "catalog/llm-wiki/pages.yaml"
    if not wiki_root.is_dir() or not catalog_path.is_file():
        raise WoonError("Wiki tree migration requires wiki/ and compiler page catalog")

    existing: dict[str, tuple[dict[str, Any], str]] = {}
    for path in iter_wiki_pages(wiki_root):
        relative = path.relative_to(root).as_posix()
        existing[relative] = split_markdown(path.read_text(encoding="utf-8"))
    for retired, survivor in _DUPLICATE_WIKI_MERGES.items():
        if retired not in existing:
            continue
        if survivor not in existing:
            raise WoonError(f"duplicate Wiki survivor is missing: {survivor}")
        retired_metadata, _ = existing.pop(retired)
        survivor_metadata, survivor_body = existing[survivor]
        aliases = _unique_strings(
            [
                *_strings(survivor_metadata.get("aliases", [])),
                str(retired_metadata.get("title", "")),
                *_strings(retired_metadata.get("aliases", [])),
            ]
        )
        survivor_metadata = dict(survivor_metadata)
        survivor_metadata["aliases"] = aliases
        existing[survivor] = (survivor_metadata, survivor_body)
    map_docs: dict[str, tuple[dict[str, Any], str]] = {}
    if maps_root.is_dir():
        for path in sorted(maps_root.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            map_docs[relative] = split_markdown(path.read_text(encoding="utf-8"))

    mapping = _map_targets(map_docs)
    map_stems = _map_stem_index(mapping)
    pages = dict(existing)
    merged_maps: dict[str, list[str]] = {}
    for map_relative, (metadata, body) in map_docs.items():
        target = mapping[map_relative]
        merged_maps.setdefault(target, []).append(map_relative)
        if target == "wiki/README.md":
            continue
        rewritten_body = _rewrite_wikilinks(body, mapping, map_stems)
        if target in existing:
            if map_relative.startswith("maps/book-"):
                target_metadata, target_body = pages[target]
                marker = f"<!-- woon-map-merge:{Path(map_relative).stem} -->"
                if marker not in target_body:
                    cleaned = _without_h1(rewritten_body).strip()
                    target_body = (
                        target_body.rstrip() + f"\n\n## 책의 선형 키워드\n\n{marker}\n\n{cleaned}\n"
                    )
                    pages[target] = (target_metadata, target_body)
            continue
        title = str(metadata.get("title", "")).strip()
        summary = str(metadata.get("summary", "")).strip()
        if not title or not summary:
            raise WoonError(f"Map migration requires title and summary: {map_relative}")
        pages[target] = (
            {
                "type": "Wiki",
                "title": title,
                "publish": bool(metadata.get("publish", False)),
                "access": metadata.get("access", "local-only"),
                "status": "Active",
                "record_owner": metadata.get("record_owner", "choi-woonyoung"),
                "people": metadata.get("people", []),
                "related_to": [],
                "summary": summary,
                "knowledge_state": metadata.get("knowledge_state", "확인 필요"),
                "state_reason": "map-to-wiki-tree-migration",
                "state_updated": migration_day.isoformat(),
            },
            _clean_map_body(rewritten_body, title=title, summary=summary),
        )

    title_by_path = {
        relative: str(metadata.get("title", "")).strip()
        for relative, (metadata, _) in pages.items()
    }
    linked_from = _map_memberships(map_docs, mapping, map_stems, set(pages))
    final: dict[str, tuple[dict[str, Any], str]] = {}
    for relative, (raw_metadata, raw_body) in sorted(pages.items()):
        metadata = dict(raw_metadata)
        body = _rewrite_duplicate_wikilinks(_rewrite_wikilinks(raw_body, mapping, map_stems))
        body = _strip_legacy_navigation(strip_generated_wiki_views(body))
        for field in LEGACY_TREE_FIELDS:
            metadata.pop(field, None)
        title = str(metadata.get("title", "")).strip()
        if not title:
            raise WoonError(f"Wiki migration requires title: {relative}")
        summary = str(metadata.get("summary", "")).strip() or _summary_from_body(title, body)
        metadata["type"] = "Wiki"
        metadata["summary"] = summary
        metadata["canonical_id"] = str(
            metadata.get("canonical_id")
            or (
                "root"
                if relative == "wiki/README.md"
                else Path(relative).with_suffix("").relative_to("wiki").as_posix()
            )
        )
        aliases = _unique_strings(metadata.get("aliases", []))
        metadata["aliases"] = aliases
        metadata["keywords"] = _unique_strings([title, *aliases])
        node_kind = _node_kind(relative, metadata, merged_maps.get(relative, []))
        metadata["node_kind"] = node_kind
        metadata["view_mode"] = _view_mode(node_kind, metadata)
        entity_kind = _entity_kind(metadata)
        if entity_kind is not None:
            metadata["entity_kind"] = entity_kind
        metadata["updated"] = _updated(metadata, migration_day)
        if relative == "wiki/README.md":
            metadata.pop("parent", None)
        else:
            parent = _semantic_parent(
                relative,
                raw_metadata,
                mapping=mapping,
                map_stems=map_stems,
                linked_from=linked_from,
                metadata=metadata,
                known_paths=set(pages),
            )
            if parent == relative or parent not in pages:
                raise WoonError(
                    f"Wiki migration cannot resolve semantic parent: {relative} -> {parent}"
                )
            metadata["parent"] = parent_link(parent, title_by_path[parent])
        tags = _strings(metadata.get("tags", []))
        if node_kind in {"root", "hub", "entity"}:
            tags = list(dict.fromkeys((*tags, "graph/overview")))
        else:
            tags = [item for item in tags if item != "graph/overview"]
        metadata["tags"] = tags
        if relative in linked_from and linked_from[relative]:
            primary_map, sequence = linked_from[relative][0]
            if (
                _map_target_depth(mapping[primary_map], mapping) > 0
                and metadata.get("sequence") is None
            ):
                metadata["sequence"] = sequence
        final[relative] = (metadata, body)

    rendered_pages = {
        root / relative: render_markdown(metadata, body).encode("utf-8")
        for relative, (metadata, body) in final.items()
    }
    catalog_writes = _updated_catalogs(root, final)
    manifest = {
        "version": 1,
        "migration_day": migration_day.isoformat(),
        "map_to_wiki": dict(sorted(mapping.items())),
        "merged_maps": {key: sorted(value) for key, value in sorted(merged_maps.items())},
        "documents_before": len(existing),
        "maps_before": len(map_docs),
        "documents_after": len(final),
        "input_sha256": {
            relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in sorted((*existing.keys(), *map_docs.keys()))
        },
    }
    changed = sum(
        1
        for path, content in rendered_pages.items()
        if not path.is_file() or path.read_bytes() != content
    )
    return WikiTreeMigrationReport(
        wiki_document_count=len(final),
        map_document_count=len(map_docs),
        changed_count=changed,
        pages=rendered_pages,
        catalog_writes=catalog_writes,
        deletions=tuple(
            root / relative
            for relative in sorted((*map_docs, *_DUPLICATE_WIKI_MERGES.keys()))
            if (root / relative).is_file()
        ),
        manifest=manifest,
    )


def retired_map_mapping() -> dict[str, str]:
    """Return the complete deterministic link rewrite for retired Markdown Maps."""

    relatives = set(_MAP_TARGETS)
    for stem in _MAP_PARENT_NAMES:
        relative = f"maps/{stem}.md"
        if stem == "context-graph/README":
            relative = "maps/context-graph/README.md"
        relatives.add(relative)
    return _map_targets({relative: ({}, "") for relative in sorted(relatives)})


def rewrite_retired_map_links(text: str) -> str:
    mapping = retired_map_mapping()
    return _rewrite_ambiguous_legacy_wikilinks(
        _rewrite_wikilinks(text, mapping, _map_stem_index(mapping))
    )


def apply_wiki_tree_migration(vault: Path, report: WikiTreeMigrationReport) -> None:
    """Apply all prepared pages, catalog metadata, and Map deletions with rollback."""

    root = vault.expanduser().resolve()
    manifest_path = root / ".local/woon-knowledge/wiki-tree-migration/manifest.json"
    touched = set(report.pages).union(
        report.deletions, {*report.catalog_writes.keys(), manifest_path}
    )
    snapshots = {
        path: (
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mode & 0o777 if path.exists() else 0o644,
        )
        for path in touched
    }
    try:
        for path, content in sorted(report.pages.items(), key=lambda item: item[0].as_posix()):
            if not path.resolve().is_relative_to(root / "wiki"):
                raise WoonError("Wiki migration page escapes wiki/")
            atomic_write(path, content, mode=snapshots[path][1])
        for path, content in report.catalog_writes.items():
            atomic_write(path, content, mode=snapshots[path][1])
        atomic_write(
            manifest_path,
            (
                json.dumps(report.manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode(),
            mode=0o600,
        )
        # Retired duplicate Wiki pages must leave the candidate graph before
        # validation; their bytes remain in ``snapshots`` for full rollback.
        for path in report.deletions:
            if path.resolve().is_relative_to(root / "wiki"):
                path.unlink(missing_ok=True)
        tree = prepare_wiki_tree_refresh(root)
        if tree.issues:
            raise WoonError(f"Wiki tree migration failed validation: {tree.issues[0]}")
        for path, content in tree.pages.items():
            atomic_write(path, content, mode=path.stat().st_mode & 0o777)
        for path in report.deletions:
            path.unlink(missing_ok=True)
    except Exception:
        for path, (restore_content, mode) in snapshots.items():
            if restore_content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, restore_content, mode=mode)
        raise


def _map_targets(map_docs: dict[str, tuple[dict[str, Any], str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in map_docs:
        if relative in _MAP_TARGETS:
            result[relative] = _MAP_TARGETS[relative]
            continue
        stem = Path(relative).with_suffix("").relative_to("maps").as_posix()
        slug = re.sub(r"-(?:map|moc)$", "", stem.replace("/README", "-context-graph"))
        result[relative] = f"wiki/hubs/{slug}.md"
    return result


def _map_stem_index(mapping: dict[str, str]) -> dict[str, str]:
    index: dict[str, str] = {}
    for relative in mapping:
        keys = {relative.removesuffix(".md")}
        if Path(relative).stem != "README":
            keys.add(Path(relative).stem)
        if relative == "maps/context-graph/README.md":
            keys.add("maps/context-graph/README")
        for key in keys:
            if key in index and index[key] != relative:
                raise WoonError(f"ambiguous Map stem: {key}")
            index[key] = relative
    return index


def _map_parent(relative: str, mapping: dict[str, str]) -> str:
    stem = Path(relative).with_suffix("").relative_to("maps").as_posix()
    configured = _MAP_PARENT_NAMES.get(stem) or _MAP_PARENT_NAMES.get(Path(relative).stem)
    if configured is None:
        return "wiki/README.md"
    return mapping.get(configured, configured)


def _semantic_parent(
    relative: str,
    old: dict[str, Any],
    *,
    mapping: dict[str, str],
    map_stems: dict[str, str],
    linked_from: dict[str, list[tuple[str, int]]],
    metadata: dict[str, Any],
    known_paths: set[str],
) -> str:
    source_maps = [key for key, target in mapping.items() if target == relative]
    if source_maps:
        parents = {_map_parent(item, mapping) for item in source_maps}
        parents.discard(relative)
        if parents:
            return sorted(parents)[0]
    entity = _entity_kind(metadata)
    if (
        entity == "person"
        and metadata.get("person_scope") == "novel-local-only"
        and "wiki/private/README.md" in known_paths
    ):
        return "wiki/private/README.md"
    if entity == "book" and "wiki/books/README.md" in known_paths:
        return "wiki/books/README.md"
    if entity == "project" and "wiki/personal/projects/README.md" in known_paths:
        return "wiki/personal/projects/README.md"
    if entity == "person" and "wiki/people/README.md" in known_paths:
        return "wiki/people/README.md"
    if relative.endswith("/README.md"):
        return "wiki/README.md"
    for key in ("parent", "parent_moc"):
        target = _link_target(old.get(key))
        resolved = _resolve_old_target(target, mapping, map_stems, known_paths)
        if resolved and resolved != relative:
            return resolved
    old_parents = old.get("parent_topics")
    if isinstance(old_parents, list) and old_parents:
        resolved = _resolve_old_target(
            _link_target(old_parents[0]), mapping, map_stems, known_paths
        )
        if resolved and resolved != relative:
            return resolved
    candidates = linked_from.get(relative, [])
    if candidates:
        ranked = sorted(
            candidates,
            key=lambda item: (-_map_target_depth(mapping[item[0]], mapping), item[1], item[0]),
        )
        for map_relative, _ in ranked:
            target = mapping[map_relative]
            if target != relative:
                return target
    domain = Path(relative).parts[1] if len(Path(relative).parts) > 2 else ""
    domain_readme = f"wiki/{domain}/README.md"
    if domain_readme in known_paths and domain_readme != relative:
        return domain_readme
    facets = set(_strings(metadata.get("facets", [])))
    if "커리어" in facets and "wiki/personal/career/README.md" in known_paths:
        return "wiki/personal/career/README.md"
    if {"콘텐츠", "리소스"}.intersection(facets) and "wiki/resources/README.md" in known_paths:
        return "wiki/resources/README.md"
    if "학습" in facets and "wiki/learning/README.md" in known_paths:
        return "wiki/learning/README.md"
    if "생활" in facets and "wiki/life/README.md" in known_paths:
        return "wiki/life/README.md"
    if "개념" in facets and "wiki/concepts/README.md" in known_paths:
        return "wiki/concepts/README.md"
    return "wiki/README.md"


def _map_memberships(
    map_docs: dict[str, tuple[dict[str, Any], str]],
    mapping: dict[str, str],
    map_stems: dict[str, str],
    known_paths: set[str],
) -> dict[str, list[tuple[str, int]]]:
    by_filename: dict[str, list[str]] = {}
    for path in known_paths:
        by_filename.setdefault(Path(path).stem, []).append(path)
    result: dict[str, list[tuple[str, int]]] = {}
    for map_relative, (_, body) in map_docs.items():
        if map_relative.startswith("maps/book-"):
            continue
        sequence = 0
        for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", body):
            sequence += 1
            target = match.group(1).strip()
            map_key = _resolve_map_key(target, map_stems)
            resolved: str | None
            if map_key is not None:
                resolved = mapping[map_key]
            else:
                resolved = _resolve_wiki_target(target, known_paths, by_filename)
            if resolved is None or resolved == mapping[map_relative]:
                continue
            result.setdefault(resolved, []).append((map_relative, sequence))
    return result


def _resolve_wiki_target(
    target: str, known_paths: set[str], by_filename: dict[str, list[str]]
) -> str | None:
    normalized = target.lstrip("./")
    if normalized.startswith("wiki/"):
        candidate = normalized if normalized.endswith(".md") else normalized + ".md"
        return candidate if candidate in known_paths else None
    matches = by_filename.get(Path(normalized).stem, [])
    return matches[0] if len(matches) == 1 else None


def _resolve_old_target(
    target: str | None,
    mapping: dict[str, str],
    map_stems: dict[str, str],
    known_paths: set[str],
) -> str | None:
    if not target:
        return None
    map_key = _resolve_map_key(target, map_stems)
    if map_key is not None:
        return mapping[map_key]
    normalized = target.lstrip("./")
    candidate = normalized if normalized.endswith(".md") else normalized + ".md"
    return candidate if candidate in known_paths else None


def _resolve_map_key(target: str, map_stems: dict[str, str]) -> str | None:
    normalized = target.replace("\\", "/").removesuffix(".md")
    while normalized.startswith("../"):
        normalized = normalized[3:]
    normalized = normalized.removeprefix("./")
    if normalized.startswith("wiki/"):
        return None
    if normalized in map_stems:
        return map_stems[normalized]
    if normalized.startswith("maps/") and normalized in map_stems:
        return map_stems[normalized]
    return map_stems.get(Path(normalized).stem)


def _rewrite_wikilinks(text: str, mapping: dict[str, str], map_stems: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        target, fragment, label = match.group(1), match.group(2) or "", match.group(3) or ""
        key = _resolve_map_key(target, map_stems)
        if key is None:
            return match.group(0)
        replacement = Path(mapping[key]).with_suffix("").as_posix()
        return f"[[{replacement}{fragment}{label}]]"

    return re.sub(r"\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]+)?\]\]", replace, text)


def _rewrite_duplicate_wikilinks(text: str) -> str:
    replacements = {
        Path(retired).with_suffix("").as_posix(): Path(survivor).with_suffix("").as_posix()
        for retired, survivor in _DUPLICATE_WIKI_MERGES.items()
    }
    stem_replacements = {
        Path(retired).stem: Path(survivor).stem
        for retired, survivor in _DUPLICATE_WIKI_MERGES.items()
    }

    def replace(match: re.Match[str]) -> str:
        target, fragment, label = match.group(1), match.group(2) or "", match.group(3) or ""
        normalized = target.removesuffix(".md")
        replacement = replacements.get(normalized)
        if replacement is None:
            replacement = stem_replacements.get(Path(normalized).stem)
        if replacement is None:
            return match.group(0)
        return f"[[{replacement}{fragment}{label}]]"

    return re.sub(r"\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]+)?\]\]", replace, text)


def _rewrite_ambiguous_legacy_wikilinks(text: str) -> str:
    """Expand a once-unique bare Wiki stem that became ambiguous after hub migration."""

    return re.sub(
        r"\[\[web-server-proxy(?=(?:#|\||\]\]))",
        "[[wiki/backend/web-server-proxy",
        text,
    )


def _rewrite_object_wikilinks(
    value: Any, mapping: dict[str, str], map_stems: dict[str, str]
) -> Any:
    if isinstance(value, str):
        return _rewrite_ambiguous_legacy_wikilinks(
            _rewrite_duplicate_wikilinks(_rewrite_wikilinks(value, mapping, map_stems))
        )
    if isinstance(value, list):
        return [_rewrite_object_wikilinks(item, mapping, map_stems) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_object_wikilinks(item, mapping, map_stems) for key, item in value.items()
        }
    return value


def _repair_retired_map_prose(body: str) -> str:
    replacements = {
        "`maps/<book-name>-map.md`": "책 entity의 `linear` 하위 키워드",
        "위치: `maps/`": "위치: 같은 `wiki/` 문서의 `parent`·하위 키워드 block",
        "긴 설명은 Wiki 문서로 분리하고, `maps/`에는 필요한 링크만 남긴다.": (
            "독립된 중심 질문이 있는 설명만 Wiki child로 분리하고, "
            "상위 문서에는 Core가 전체 하위 키워드를 생성한다."
        ),
        "`wiki/common/`과 `maps/`의 경로": "`wiki/`의 parent tree와 관련 문서 경로",
        "흐름: inbox → sources → wiki → maps.": (
            "흐름: inbox → sources → 하나의 Wiki → 같은 페이지의 파생 tree view."
        ),
    }
    updated = body
    for old, new in replacements.items():
        updated = updated.replace(old, new)
    return updated


def _clean_map_body(body: str, *, title: str, summary: str) -> str:
    body = _without_h1(body)
    body = re.sub(r"(?ms)<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->\s*", "", body)
    rows: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if re.match(r"^(?:[-*]|\d+\.)\s+.*\[\[[^\]]+\]\].*$", stripped):
            continue
        if stripped.startswith("상위 링크:"):
            continue
        rows.append(line)
    cleaned = "\n".join(rows)
    cleaned = re.sub(r"(?ms)^## ([^\n]+)\s*\n(?=\s*(?:## |\Z))", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    intro = f"# {title}\n\n{summary}"
    return intro + (f"\n\n{cleaned}" if cleaned and cleaned != summary else "") + "\n"


def _without_h1(body: str) -> str:
    return re.sub(r"(?m)^# [^\n]+\n?", "", body, count=1).lstrip()


def _strip_legacy_navigation(body: str) -> str:
    updated = re.sub(r"(?ms)<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->\s*", "", body)
    updated = re.sub(
        r"(?ms)^## 주제 연결\s*\n+"
        r"<!-- woon-wiki-navigation:start -->.*?<!-- woon-wiki-navigation:end -->\s*",
        "",
        updated,
    )
    return re.sub(r"\n{3,}", "\n\n", updated).rstrip() + "\n"


def _node_kind(relative: str, metadata: dict[str, Any], map_sources: list[str]) -> str:
    if relative == "wiki/README.md":
        return "root"
    entity = _entity_kind(metadata)
    if entity is not None:
        return "entity"
    if relative in _TOP_HUB_PATHS or relative.endswith("/README.md"):
        return "hub"
    if any(
        Path(source).name
        in {"ai-llm-moc.md", "ai-neural-network-moc.md", "os-moc.md", "pintos-moc.md"}
        for source in map_sources
    ):
        return "hub"
    if metadata.get("question_kind"):
        return "detail"
    return "topic"


def _entity_kind(metadata: dict[str, Any]) -> str | None:
    explicit = metadata.get("entity_kind")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if metadata.get("content_kind") == "book":
        return "book"
    if metadata.get("person_id") or metadata.get("entity_type") == "person":
        return "person"
    if metadata.get("project_id") or metadata.get("objective") or metadata.get("project_status"):
        return "project"
    return None


def _view_mode(node_kind: str, metadata: dict[str, Any]) -> str:
    entity = _entity_kind(metadata)
    if entity == "book":
        return "linear"
    if entity == "project":
        return "project"
    if entity == "person":
        return "topic-timeline"
    return "tree" if node_kind in {"root", "hub", "topic", "entity"} else "article"


def _updated(metadata: dict[str, Any], fallback: date) -> str:
    for key in ("updated", "state_updated"):
        value = metadata.get(key)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                continue
    return fallback.isoformat()


def _link_target(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^\[\[([^\]|#]+)", value.strip())
    return match.group(1).strip() if match else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _unique_strings(value: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in _strings(value):
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _summary_from_body(title: str, body: str) -> str:
    for paragraph in re.split(r"\n\s*\n", _without_h1(body)):
        plain = " ".join(line.strip() for line in paragraph.splitlines() if line.strip())
        if plain and not plain.startswith(("#", "-", ">", "<!--", "```")):
            return plain[:240]
    return f"{title}의 현재 이해와 하위 키워드를 정리한다."


def _map_target_depth(target: str, mapping: dict[str, str]) -> int:
    reverse = {value: key for key, value in mapping.items()}
    depth = 0
    seen: set[str] = set()
    current = target
    while current in reverse and current not in seen:
        seen.add(current)
        parent = _map_parent(reverse[current], mapping)
        if parent == current:
            break
        current = parent
        depth += 1
    return depth


def _catalog_with_tree_metadata(path: Path, pages: dict[str, tuple[dict[str, Any], str]]) -> bytes:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = raw.get("pages")
    if not isinstance(records, list):
        raise WoonError("compiled page catalog must contain pages list")
    for record in records:
        if not isinstance(record, dict):
            raise WoonError("compiled page catalog entry must be a mapping")
        output = record.get("output_path")
        relative = f"wiki/{output}" if isinstance(output, str) else ""
        if relative not in pages:
            raise WoonError(f"compiled page output missing during tree repair: {output}")
        metadata = pages[relative][0]
        frontmatter = record.get("frontmatter")
        if not isinstance(frontmatter, dict):
            raise WoonError("compiled page frontmatter must be a mapping")
        mapping = retired_map_mapping()
        rewritten_frontmatter = _rewrite_object_wikilinks(
            frontmatter, mapping, _map_stem_index(mapping)
        )
        frontmatter.clear()
        frontmatter.update(rewritten_frontmatter)
        for field in LEGACY_TREE_FIELDS:
            frontmatter.pop(field, None)
        for key in (
            "canonical_id",
            "node_kind",
            "parent",
            "keywords",
            "aliases",
            "view_mode",
            "updated",
            "entity_kind",
            "sequence",
            "central_question",
        ):
            if key in metadata:
                frontmatter[key] = metadata[key]
            else:
                frontmatter.pop(key, None)
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")


def _updated_catalogs(
    root: Path, pages: dict[str, tuple[dict[str, Any], str]]
) -> dict[Path, bytes]:
    catalog_root = root / "catalog/llm-wiki"
    page_path = catalog_root / "pages.yaml"
    raw = yaml.safe_load(page_path.read_text(encoding="utf-8")) or {}
    records = raw.get("pages")
    if not isinstance(records, list):
        raise WoonError("compiled page catalog must contain pages list")
    retired_ids = {
        Path(relative).with_suffix("").relative_to("wiki").as_posix()
        for relative in _DUPLICATE_WIKI_MERGES
    }
    raw["pages"] = [
        record
        for record in records
        if isinstance(record, dict) and record.get("page_id") not in retired_ids
    ]
    for record in raw["pages"]:
        if not isinstance(record, dict):
            raise WoonError("compiled page catalog entry must be a mapping")
        output = record.get("output_path")
        relative = f"wiki/{output}" if isinstance(output, str) else ""
        if relative not in pages:
            raise WoonError(f"compiled page output missing during tree migration: {output}")
        metadata = pages[relative][0]
        frontmatter = record.get("frontmatter")
        if not isinstance(frontmatter, dict):
            raise WoonError("compiled page frontmatter must be a mapping")
        for field in LEGACY_TREE_FIELDS:
            frontmatter.pop(field, None)
        for key in (
            "canonical_id",
            "node_kind",
            "parent",
            "keywords",
            "aliases",
            "view_mode",
            "updated",
            "entity_kind",
            "sequence",
            "central_question",
        ):
            if key in metadata:
                frontmatter[key] = metadata[key]
            else:
                frontmatter.pop(key, None)
    writes = {page_path: yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")}

    winner = next(item for item in raw["pages"] if item.get("page_id") == "ai/map-estimation")
    render = winner.get("render")
    if not isinstance(render, dict) or not isinstance(render.get("source_id"), str):
        raise WoonError("duplicate Wiki survivor requires source-body render")
    winner_source = render["source_id"]
    winner_claim = next(
        (
            value
            for value in winner.get("claim_ids", [])
            if isinstance(value, str) and value.startswith("claim://curated-wiki/")
        ),
        None,
    )
    if winner_claim is None:
        raise WoonError("duplicate Wiki survivor requires a curated claim")
    for filename, key, identifier, state_key, inactive_state, successor in (
        (
            "sources.yaml",
            "sources",
            "source_id",
            "lifecycle",
            "archived",
            winner_source,
        ),
        (
            "claims.yaml",
            "claims",
            "claim_id",
            "status",
            "superseded",
            winner_claim,
        ),
    ):
        path = catalog_root / filename
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        values = document.get(key)
        if not isinstance(values, list):
            raise WoonError(f"compiled {filename} must contain {key} list")
        for value in values:
            if not isinstance(value, dict):
                continue
            record_id = value.get(identifier)
            if isinstance(record_id, str) and "maximum-a-posteriori" in record_id:
                value[state_key] = inactive_state
                value["superseded_by"] = successor
        writes[path] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")

    for filename, key in (
        ("curation.yaml", "curations"),
        ("receipts.yaml", "receipts"),
    ):
        path = catalog_root / filename
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        values = document.get(key)
        if not isinstance(values, list):
            raise WoonError(f"compiled {filename} must contain {key} list")
        document[key] = [
            value
            for value in values
            if not isinstance(value, dict) or value.get("page_id") not in retired_ids
        ]
        writes[path] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")

    relations_path = catalog_root / "relations.yaml"
    relations = yaml.safe_load(relations_path.read_text(encoding="utf-8")) or {}
    values = relations.get("relations")
    if not isinstance(values, list):
        raise WoonError("compiled relations.yaml must contain relations list")
    relations["relations"] = [
        value
        for value in values
        if not isinstance(value, dict) or value.get("from_page_id") not in retired_ids
    ]
    writes[relations_path] = yaml.safe_dump(relations, allow_unicode=True, sort_keys=False).encode(
        "utf-8"
    )
    return writes
