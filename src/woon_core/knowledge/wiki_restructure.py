"""Validate a complete, replay-safe Wiki restructure manifest.

The manifest is an instruction for a one-time physical migration; it is not a
second knowledge graph.  Keeping validation separate from mutation makes it
possible to reject an incomplete or stale migration before any canonical page,
catalog, or receipt is touched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import iter_wiki_pages, split_markdown

_DISPOSITIONS = {"keep", "merge", "move", "retire", "review"}


@dataclass(frozen=True, slots=True)
class WikiRestructurePreflight:
    """Read-only result for one complete Wiki restructure instruction."""

    document_count: int
    disposition_counts: dict[str, int]
    target_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WikiRestructureClassification:
    """A complete, non-mutating assignment to the approved target tree."""

    document_count: int
    disposition_counts: dict[str, int]
    scope_counts: dict[str, int]
    records: tuple[dict[str, str], ...]


def render_wiki_restructure_template(vault: Path) -> bytes:
    """Render a complete local baseline without assigning final destinations.

    Every record starts as ``review``.  This is intentionally not an apply
    manifest: a reviewer must assign a destination or a merge successor before
    the preflight can describe the transaction as ready.
    """

    root = vault.expanduser().resolve()
    compiler_owned = _compiler_owned_paths(root)
    records: list[dict[str, str]] = []
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        canonical_id = metadata.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise WoonError(f"Wiki template requires canonical_id: {relative}")
        records.append(
            {
                "current_path": relative,
                "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "canonical_id": canonical_id,
                "source_owner": "compiler" if relative in compiler_owned else "manual",
                "disposition": "review",
            }
        )
    payload = {"version": 1, "records": records}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_template(vault: Path, output_path: Path) -> Path:
    """Create one local baseline manifest without overwriting prior review work."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure template must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure template already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_template(root), mode=0o600)
    return output


def render_wiki_restructure_classification(vault: Path) -> bytes:
    """Classify every active page before any path or catalog mutation.

    A scope names the sole branch approved by the user.  It is intentionally
    not a target path: compiler-owned pages must still move through their
    source/claim/page-spec transaction, never through a Markdown rename.
    """

    root = vault.expanduser().resolve()
    records: list[dict[str, str]] = []
    dispositions: dict[str, int] = {}
    scopes: dict[str, int] = {}
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        scope, disposition, rationale = _approved_scope_for_legacy_path(relative)
        records.append(
            {
                "current_path": relative,
                "target_scope": scope,
                "disposition": disposition,
                "rationale": rationale,
            }
        )
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
        scopes[scope] = scopes.get(scope, 0) + 1
    payload = {
        "version": 1,
        "document_count": len(records),
        "disposition_counts": dispositions,
        "scope_counts": dict(sorted(scopes.items())),
        "records": records,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_classification(vault: Path, output_path: Path) -> Path:
    """Write the complete local-only classification without touching Wiki pages."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure classification must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure classification already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_classification(root), mode=0o600)
    return output


def prepare_wiki_restructure_preflight(
    vault: Path, manifest_path: Path
) -> WikiRestructurePreflight:
    """Validate that a manifest accounts for every active human Wiki page.

    A later writer may consume this result, but this function deliberately does
    not rename files or rewrite metadata.  In particular, raw evidence below
    ``wiki/private/_sources`` is excluded because its movement is owned by the
    source resolver rather than the human Wiki tree.
    """

    root = vault.expanduser().resolve()
    manifest_file = manifest_path.expanduser().resolve()
    if not manifest_file.is_file():
        raise WoonError(f"Wiki restructure manifest is missing: {manifest_file}")
    try:
        payload = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError("Wiki restructure manifest must use version: 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("Wiki restructure manifest requires a records list")

    active = {path.relative_to(root).as_posix(): path for path in iter_wiki_pages(root / "wiki")}
    compiler_owned = _compiler_owned_paths(root)
    issues: list[str] = []
    seen: set[str] = set()
    targets: dict[str, str] = {}
    final_paths: set[str] = set()
    move_parents: list[tuple[str, str]] = []
    counts = {disposition: 0 for disposition in sorted(_DISPOSITIONS)}

    for index, record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        current = _relative_path(record.get("current_path"), label, "current_path", issues)
        disposition = record.get("disposition")
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            issues.append(f"{label}: unsupported disposition {disposition!r}")
            continue
        counts[disposition] += 1
        if current is None:
            continue
        if current in seen:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active Wiki page: {current}")
            continue
        expected_hash = record.get("current_sha256")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            issues.append(f"{label}: current_sha256 does not match: {current}")
        try:
            metadata, _ = split_markdown(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, WoonError) as error:
            issues.append(f"{label}: cannot read frontmatter for {current}: {error}")
            continue
        if record.get("canonical_id") != metadata.get("canonical_id"):
            issues.append(f"{label}: canonical_id does not match: {current}")
        expected_owner = "compiler" if current in compiler_owned else "manual"
        if record.get("source_owner") != expected_owner:
            issues.append(f"{label}: source_owner must be {expected_owner!r} for {current}")

        target = record.get("target_path")
        if disposition == "move":
            target_path = _relative_path(target, label, "target_path", issues)
            if target_path is None:
                continue
            if not target_path.startswith("wiki/"):
                issues.append(f"{label}: target_path must stay below wiki/: {target_path}")
                continue
            previous = targets.setdefault(target_path, current)
            if previous != current:
                issues.append(f"{label}: target_path collision {target_path} with {previous}")
            final_paths.add(target_path)
            target_parent = _relative_path(
                record.get("target_parent"), label, "target_parent", issues
            )
            if target_parent is not None:
                move_parents.append((label, target_parent))
        elif disposition == "keep":
            final_paths.add(current)
        elif target not in {None, ""}:
            issues.append(f"{label}: only move records may define target_path")
        if disposition in {"merge", "retire"}:
            successor = record.get("link_successor")
            if not isinstance(successor, str) or not successor.strip():
                issues.append(f"{label}: {disposition} requires link_successor")

    missing = sorted(set(active) - seen)
    extra = sorted(seen - set(active))
    if missing:
        issues.append(f"manifest omits {len(missing)} active Wiki pages")
    if extra:
        issues.append(f"manifest names {len(extra)} non-active Wiki pages")
    for label, target_parent in move_parents:
        if target_parent not in final_paths:
            issues.append(f"{label}: target_parent is not a final Wiki page: {target_parent}")
    return WikiRestructurePreflight(
        document_count=len(active),
        disposition_counts={key: value for key, value in counts.items() if value},
        target_count=len(targets),
        issues=tuple(issues),
    )


def _relative_path(value: Any, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()


def _compiler_owned_paths(root: Path) -> frozenset[str]:
    """Read compiler page ownership without treating a missing catalog as an error."""

    catalog = root / "catalog/llm-wiki/pages.yaml"
    if not catalog.is_file():
        return frozenset()
    try:
        payload = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki page catalog is unreadable: {error}") from error
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        raise WoonError("Wiki page catalog requires a pages list")
    result: set[str] = set()
    for index, page in enumerate(pages, start=1):
        output = page.get("output_path") if isinstance(page, dict) else None
        if not isinstance(output, str) or not output.strip():
            raise WoonError(f"Wiki page catalog pages[{index}] has invalid output_path")
        result.add((Path("wiki") / output).as_posix())
    return frozenset(result)


def _approved_scope_for_legacy_path(relative: str) -> tuple[str, str, str]:
    """Assign legacy areas to one branch of the fixed, approved tree."""

    prefixes = (
        ("wiki/personal/kotlin-in-action", "Wiki > 책 > 프로그래밍 언어·설계", "book"),
        ("wiki/personal/컴퓨터-시스템-3판", "Wiki > 책 > 시스템·플랫폼", "book"),
        ("wiki/personal/밑바닥부터-시작하는-딥러닝-1", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/personal/밑바닥부터-만들면서-배우는-llm", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/private/novel", "창작 > 창작 프로젝트", "creative-project"),
        ("wiki/personal/projects", "창작 > 창작 프로젝트", "creative-project"),
        ("wiki/personal/career", "커리어", "career"),
        ("wiki/personal/interview", "커리어 > 지원 자료 > 면접 준비", "career"),
        ("wiki/ai", "Wiki > AI·머신러닝", "domain"),
        ("wiki/algorithm", "Wiki > 컴퓨터 과학 기초", "domain"),
        ("wiki/backend", "Wiki > 백엔드·서비스", "domain"),
        ("wiki/database", "Wiki > 데이터·저장소", "domain"),
        ("wiki/network", "Wiki > 컴퓨터 시스템·네트워크 > 네트워크", "domain"),
        ("wiki/os", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "domain"),
        ("wiki/pintos", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "learning-implementation"),
        ("wiki/security", "Wiki > 품질·보안·신뢰성", "domain"),
        ("wiki/books", "Wiki > 책", "book-map"),
        ("wiki/tools", "개인 운영 > 지식 운영", "operating"),
        ("wiki/knowledge", "개인 운영 > 지식 운영", "operating"),
        ("wiki/people", "인물", "people-map"),
    )
    for prefix, scope, rationale in prefixes:
        if relative == f"{prefix}.md" or relative.startswith(prefix + "/"):
            return scope, "move", rationale
    if relative == "wiki/README.md":
        return "Vault root", "keep", "vault-root"
    if relative.startswith("wiki/hubs/"):
        return "review", "review", "legacy-navigation-wrapper"
    if relative.startswith("wiki/resources/"):
        return "review", "review", "legacy-resource-wrapper"
    if relative.startswith(("wiki/common/", "wiki/concepts/", "wiki/nodes/")):
        return "review", "review", "mixed-legacy-topic"
    if relative.startswith("wiki/private/"):
        return "review", "review", "private-legacy-boundary"
    if relative.startswith("wiki/personal/"):
        return "review", "review", "personal-legacy-boundary"
    return "review", "review", "unclassified-legacy-path"
