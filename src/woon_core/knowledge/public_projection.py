"""Build a privacy-safe, read-only public projection from compiled Wiki pages.

The Vault remains the only Markdown canonical.  This module consumes the
compiler's page specifications and receipts, then writes a deliberately small
Just the Docs input tree only when the caller explicitly applies a prepared
report.  It never deploys a site or mutates Vault knowledge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, exclusive_file_lock
from woon_core.knowledge.wiki_tree import CHILDREN_END, CHILDREN_START, split_markdown

_SCHEMA_VERSION = 1
_CONTENT_RELATIVE = Path("generated/public-content")
_RECEIPT_RELATIVE = Path(".local/woon-knowledge/public-projection/receipt.json")
_WIKI_ROOT = Path("wiki/Wiki")
_PUBLIC_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_LEGACY_PUBLIC_PATH = re.compile(
    r"/wiki/[a-z0-9]+(?:-[a-z0-9]+)*/[a-z0-9]+(?:-[a-z0-9]+)*(?:/|\.html)\Z"
)
_PRIVATE_ROUTE_SEGMENTS = frozenset({"private", "sources", "catalog", "personal", "local-only"})
_WIKILINK = re.compile(
    r"(?<!\!)\[\[(?P<target>[^\]|#]+)(?P<anchor>#[^\]|]+)?(?:\|(?P<label>[^\]]+))?\]\]"
)
_ANY_WIKILINK = re.compile(r"!?\[\[[^\]]+\]\]")
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\((?P<target>[^)]+)\)")
_CODE_FENCE = re.compile(r"(?m)^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>[^\n]*)(?:\n|$)")
_PRIVATE_CONTENT = (
    ("Obsidian wikilink", _ANY_WIKILINK),
    ("local file path", re.compile(r"(?:file://|/Users/|~/)")),
    (
        "private source link",
        re.compile(r"(?:\]\(|href=[\"'])(?:\.\./)*(?:wiki/private|sources|private)/"),
    ),
    ("source session ID", re.compile(r"source_session_ids?\s*[:=]", re.IGNORECASE)),
)
_RELATION_LIST_FIELDS = frozenset(
    {"prerequisites", "next_concepts", "related", "related_to", "source_roots"}
)


@dataclass(frozen=True, slots=True)
class PublicProjectionDocument:
    """One deterministic public Markdown document prepared from one page spec."""

    page_id: str
    canonical_id: str
    slug: str
    relative_path: Path
    content: bytes
    projection_sha256: str
    source_output_sha256: str


@dataclass(frozen=True, slots=True)
class PublicProjectionRedirect:
    """An explicit former public URL pointing directly to a canonical document."""

    slug: str | None
    target_slug: str
    relative_path: Path
    content: bytes
    public_path: str | None = None


@dataclass(frozen=True, slots=True)
class PublicProjectionReport:
    """Read-only public projection preflight result ready for explicit apply."""

    vault: Path
    site: Path
    content_root: Path
    documents: tuple[PublicProjectionDocument, ...]
    excluded_private_targets: tuple[str, ...]
    link_checks: tuple[str, ...]
    build_id: str
    input_sha256: str
    output_sha256: str
    receipt: bytes
    redirects: tuple[PublicProjectionRedirect, ...] = ()

    @property
    def artifacts(self) -> tuple[PublicProjectionDocument | PublicProjectionRedirect, ...]:
        """Generated files, keeping redirects outside canonical document counts."""
        return (*self.documents, *self.redirects)


@dataclass(frozen=True, slots=True)
class PublicProjectionApplyResult:
    """Observable result of atomically replacing the generated site input."""

    content_root: Path
    receipt_path: Path
    changed: bool


def prepare_public_projection(vault: Path, site: Path) -> PublicProjectionReport:
    """Preflight a public projection without changing the Vault or site.

    Only page specs explicitly marked ``publication_state: publish`` and
    ``access: public`` beneath ``wiki/Wiki`` are eligible.  Every selected
    page must have a matching compiler receipt, public provenance, and public
    outbound links before any Markdown is rendered.

    A survivor's ``public_redirect_from`` lists explicitly approved former
    public slugs. They become separate HTML artifacts targeting that verified
    document directly; private owners and inferred aliases never create them.
    ``public_redirect_from_paths`` separately names former category/document
    paths, ending in a slash or ``.html``; it does not relax canonical slugs.
    """

    root = vault.expanduser().resolve()
    site_root = site.expanduser().resolve()
    content_root = _validate_site_projection_contract(site_root)
    pages = _yaml_records(root / "catalog/llm-wiki/pages.yaml", "pages")
    sources = _records_by_id(root / "catalog/llm-wiki/sources.yaml", "sources", "source_id")
    receipts = _records_by_id(root / "catalog/llm-wiki/receipts.yaml", "receipts", "page_id")

    selected: list[dict[str, Any]] = []
    excluded: list[str] = []
    all_targets: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_id = _required_string(page, "page_id", "page spec")
        for alias in _page_aliases(root, page):
            existing = all_targets.get(alias)
            if existing is not None and existing is not page:
                raise WoonError(f"public projection page target is ambiguous: {alias}")
            all_targets[alias] = page
        frontmatter = _mapping(page.get("frontmatter"), f"page {page_id} frontmatter")
        state = frontmatter.get("publication_state")
        if state == "publish":
            selected.append(page)
        else:
            excluded.append(page_id)

    candidates: dict[str, dict[str, Any]] = {}
    for page in selected:
        page_id = _required_string(page, "page_id", "page spec")
        frontmatter = _mapping(page.get("frontmatter"), f"page {page_id} frontmatter")
        _validate_candidate_scope(root, page_id, page, frontmatter)
        for alias in _page_aliases(root, page):
            existing = candidates.get(alias)
            if existing is not None and existing is not page:
                raise WoonError(f"public projection published target is ambiguous: {alias}")
            candidates[alias] = page

    documents: list[PublicProjectionDocument] = []
    link_checks: list[str] = []
    seen_slugs: set[str] = set()
    rendered: dict[str, tuple[dict[str, Any], str, str]] = {}
    for page in sorted(selected, key=lambda item: _required_string(item, "page_id", "page spec")):
        page_id = _required_string(page, "page_id", "page spec")
        frontmatter = _mapping(page.get("frontmatter"), f"page {page_id} frontmatter")
        compiler_receipt = receipts.get(page_id)
        if compiler_receipt is None:
            raise WoonError(f"public projection requires compiler receipt: {page_id}")
        source_output_sha256, body = _verified_compiled_body(root, page, compiler_receipt)
        _validate_public_provenance(page_id, page, sources)
        slug = _public_slug(frontmatter, page_id)
        if slug in seen_slugs:
            raise WoonError(f"public projection public_slug is duplicated: {slug}")
        seen_slugs.add(slug)
        rendered[page_id] = (frontmatter, source_output_sha256, body)

    projection_targets = {
        _required_string(page, "page_id", "page spec"): _public_slug(
            _mapping(page.get("frontmatter"), "published page frontmatter"),
            _required_string(page, "page_id", "page spec"),
        )
        for page in selected
    }
    for page in sorted(selected, key=lambda item: _required_string(item, "page_id", "page spec")):
        page_id = _required_string(page, "page_id", "page spec")
        frontmatter, source_output_sha256, body = rendered[page_id]
        navigation_ancestry = _validate_frontmatter_relations(
            page_id,
            frontmatter,
            candidates,
            all_targets,
            projection_targets,
            link_checks,
        )
        projected_body = _project_body(
            page_id,
            body,
            candidates,
            all_targets,
            projection_targets,
            link_checks,
        )
        slug = _public_slug(frontmatter, page_id)
        content = _render_projected_markdown(
            page_id,
            frontmatter,
            slug,
            navigation_ancestry,
            projected_body,
            source_output_sha256,
        )
        _assert_safe_projected_content(page_id, content.decode())
        documents.append(
            PublicProjectionDocument(
                page_id=page_id,
                canonical_id=_required_string(frontmatter, "canonical_id", f"page {page_id}"),
                slug=slug,
                relative_path=Path(f"{slug}.md"),
                content=content,
                projection_sha256=_projection_payload_sha256(
                    page_id,
                    frontmatter,
                    slug,
                    navigation_ancestry,
                    projected_body,
                    source_output_sha256,
                ),
                source_output_sha256=source_output_sha256,
            )
        )

    redirects = _prepare_redirects(pages, rendered, seen_slugs)
    input_payload = {
        "version": _SCHEMA_VERSION,
        "documents": [
            {
                "page_id": item.page_id,
                "canonical_id": item.canonical_id,
                "slug": item.slug,
                "source_output_sha256": item.source_output_sha256,
                "projection_sha256": item.projection_sha256,
            }
            for item in documents
        ],
        "excluded_private_targets": sorted(excluded),
        "link_checks": sorted(set(link_checks)),
    }
    if redirects:
        input_payload["redirects"] = [
            {
                **({"path": item.public_path} if item.public_path else {"slug": item.slug}),
                "target_slug": item.target_slug,
            }
            for item in redirects
        ]
    input_sha256 = _sha256_json(input_payload)
    build_id = input_sha256[:24]
    document_hashes = {
        item.relative_path.as_posix(): hashlib.sha256(item.content).hexdigest()
        for item in documents
    }
    redirect_hashes = {
        item.relative_path.as_posix(): hashlib.sha256(item.content).hexdigest()
        for item in redirects
    }
    output_sha256 = _sha256_json(document_hashes | redirect_hashes)
    receipt_payload = {
        "version": _SCHEMA_VERSION,
        "build_id": build_id,
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "documents": document_hashes,
        "excluded_private_targets": sorted(excluded),
        "link_checks": sorted(set(link_checks)),
    }
    if redirect_hashes:
        receipt_payload["redirects"] = redirect_hashes
    receipt_bytes = _json_bytes(receipt_payload)
    return PublicProjectionReport(
        vault=root,
        site=site_root,
        content_root=content_root,
        documents=tuple(documents),
        excluded_private_targets=tuple(sorted(excluded)),
        link_checks=tuple(sorted(set(link_checks))),
        build_id=build_id,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        receipt=receipt_bytes,
        redirects=tuple(redirects),
    )


def _prepare_redirects(
    pages: list[dict[str, Any]],
    rendered: dict[str, tuple[dict[str, Any], str, str]],
    public_slugs: set[str],
) -> list[PublicProjectionRedirect]:
    # Reserve private metadata too: an alias must never expose or take over an
    # existing private page's URL. Only explicit fields on verified public
    # survivors can create output; aliases and historical source locators cannot.
    reserved = set(public_slugs)
    for page in pages:
        slug = page["frontmatter"].get("public_slug")
        if isinstance(slug, str):
            reserved.add(slug)
    reserved_urls = {f"/wiki/{slug}/" for slug in reserved}
    reserved_files = {_public_output_path(url) for url in reserved_urls}
    redirects = []
    for page_id, (frontmatter, _, _) in sorted(rendered.items()):
        former = frontmatter.get("public_redirect_from", [])
        if not isinstance(former, list) or any(
            not isinstance(slug, str) or not _PUBLIC_SLUG.fullmatch(slug) for slug in former
        ):
            raise WoonError(f"public projection redirects require safe public slugs: {page_id}")
        target = _public_slug(frontmatter, page_id)
        former_paths = frontmatter.get("public_redirect_from_paths", [])
        if not isinstance(former_paths, list) or any(
            not _safe_legacy_public_path(path) for path in former_paths
        ):
            raise WoonError(
                f"public projection redirects require safe legacy public paths: {page_id}"
            )
        for slug in sorted(former):
            url = f"/wiki/{slug}/"
            if url in reserved_urls or _public_output_path(url) in reserved_files:
                raise WoonError(f"public projection redirect slug conflicts: {slug}")
            reserved_urls.add(url)
            reserved_files.add(_public_output_path(url))
            redirects.append(
                PublicProjectionRedirect(
                    slug=slug,
                    target_slug=target,
                    relative_path=Path(f"{slug}.html"),
                    content=_render_redirect(url, target),
                )
            )
        for path in sorted(former_paths):
            output = _public_output_path(path)
            if path in reserved_urls or output in reserved_files:
                raise WoonError(f"public projection redirect path conflicts: {path}")
            reserved_urls.add(path)
            reserved_files.add(output)
            relative = Path("legacy-paths") / output.relative_to("wiki")
            redirects.append(
                PublicProjectionRedirect(
                    slug=None,
                    target_slug=target,
                    relative_path=relative,
                    content=_render_redirect(path, target),
                    public_path=path,
                )
            )
    return sorted(
        redirects, key=lambda item: (item.slug is None, item.slug or item.public_path or "")
    )


def _safe_legacy_public_path(value: object) -> bool:
    if not isinstance(value, str) or not _LEGACY_PUBLIC_PATH.fullmatch(value):
        return False
    parts = value.removeprefix("/wiki/").rstrip("/").removesuffix(".html").split("/")
    return not _PRIVATE_ROUTE_SEGMENTS.intersection(parts)


def _public_output_path(public_path: str) -> Path:
    return Path(public_path.lstrip("/") + ("index.html" if public_path.endswith("/") else ""))


def _render_redirect(public_path: str, target: str) -> bytes:
    frontmatter = {
        "layout": None,
        "permalink": public_path,
        "redirect_target": f"/wiki/{target}/",
        "nav_exclude": True,
        "search_exclude": True,
        "sitemap": False,
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False)
    body = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex">
<title>문서가 이동했습니다</title>
<link rel="canonical" href="{{ page.redirect_target | absolute_url | escape }}">
<meta http-equiv="refresh" content="0; url={{ page.redirect_target | relative_url | escape }}">
</head>
<body><p><a href="{{ page.redirect_target | relative_url | escape }}">문서 열기</a></p></body>
</html>
"""
    return f"---\n{header}---\n\n{body}".encode()


def apply_public_projection(report: PublicProjectionReport) -> PublicProjectionApplyResult:
    """Atomically replace only ``generated/public-content`` and persist a private receipt.

    The generated root is staged as a sibling before replacement.  A pre-existing
    ``README.md`` is retained verbatim because it is a human-maintained boundary
    note, not compiler-owned public content.
    """

    refreshed = prepare_public_projection(report.vault, report.site)
    if (
        refreshed.input_sha256 != report.input_sha256
        or refreshed.output_sha256 != report.output_sha256
    ):
        raise WoonError("public projection preflight is stale; prepare again before apply")
    content_root = refreshed.content_root
    expected_root = refreshed.site / _CONTENT_RELATIVE
    if content_root != expected_root:
        raise WoonError("public projection report has an invalid site content root")
    receipt_path = refreshed.vault / _RECEIPT_RELATIVE
    lock_path = refreshed.vault / ".local/woon-knowledge/public-projection/apply.lock"
    with exclusive_file_lock(lock_path):
        existing_snapshot = _tree_snapshot(content_root)
        desired_snapshot = _desired_snapshot(refreshed, content_root)
        changed = existing_snapshot != desired_snapshot
        if changed:
            _replace_content_root(content_root, refreshed)
        atomic_write(receipt_path, refreshed.receipt, mode=0o600)
    return PublicProjectionApplyResult(content_root, receipt_path, changed)


def _validate_site_projection_contract(site: Path) -> Path:
    if not site.is_dir():
        raise WoonError(f"public projection site is missing: {site}")
    config_path = site / "config/public-projection.yml"
    if not config_path.is_file():
        raise WoonError(f"public projection config is missing: {config_path}")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"public projection config is unreadable: {error}") from error
    if not isinstance(config, dict):
        raise WoonError("public projection config must be a mapping")
    expected = {
        "content_root": _CONTENT_RELATIVE.as_posix(),
        "input_owner": "Obsidian Vault",
        "write_policy": "compiler-only",
        "publish_policy": "approved-documents-only",
        "site_behavior": "read-only-build-input",
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise WoonError(f"public projection config {key} must be {value!r}")
    required = config.get("required_front_matter")
    required_fields = {
        "layout",
        "title",
        "nav_order",
        "permalink",
        "publication_state",
        "projection_id",
        "projection_sha256",
    }
    if not isinstance(required, list) or set(required) != required_fields:
        raise WoonError("public projection config required_front_matter is incomplete")
    prohibited = config.get("privacy_prohibited")
    expected_prohibited = {
        "obsidian-wikilink",
        "local-file-path",
        "private-source-link",
        "source-session-id",
    }
    if not isinstance(prohibited, list) or set(prohibited) != expected_prohibited:
        raise WoonError("public projection config privacy_prohibited is incomplete")
    return site / _CONTENT_RELATIVE


def _yaml_records(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise WoonError(f"public projection compiler input is missing: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(
            f"public projection compiler input is unreadable: {path}: {error}"
        ) from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError(f"public projection compiler input must use version: 1: {path}")
    records = payload.get(key)
    if not isinstance(records, list):
        raise WoonError(f"public projection compiler input requires {key}: {path}")
    parsed: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            raise WoonError(f"public projection {key} entry must be a mapping: {path}")
        parsed.append(record)
    return parsed


def _records_by_id(path: Path, key: str, identifier: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in _yaml_records(path, key):
        value = _required_string(record, identifier, f"public projection {key} entry")
        if value in indexed:
            raise WoonError(f"public projection {key} has duplicate {identifier}: {value}")
        indexed[value] = record
    return indexed


def _page_aliases(vault: Path, page: dict[str, Any]) -> tuple[str, ...]:
    page_id = _required_string(page, "page_id", "page spec")
    output_path = _safe_output_path(page, page_id)
    frontmatter = _mapping(page.get("frontmatter"), f"page {page_id} frontmatter")
    canonical_id = frontmatter.get("canonical_id")
    aliases = {
        page_id,
        output_path.as_posix(),
        output_path.with_suffix("").as_posix(),
        (Path("wiki") / output_path).as_posix(),
        (Path("wiki") / output_path).with_suffix("").as_posix(),
    }
    if isinstance(canonical_id, str) and canonical_id.strip():
        aliases.add(canonical_id.strip())
        aliases.add(canonical_id.strip().removeprefix("wiki/"))
    return tuple(sorted(alias for alias in aliases if alias))


def _safe_output_path(page: dict[str, Any], page_id: str) -> Path:
    raw = _required_string(page, "output_path", f"page {page_id}")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
        raise WoonError(f"public projection page has unsafe output_path: {page_id}")
    return relative


def _validate_candidate_scope(
    vault: Path, page_id: str, page: dict[str, Any], frontmatter: dict[str, Any]
) -> None:
    if frontmatter.get("access") != "public" or frontmatter.get("privacy", "public") != "public":
        raise WoonError(f"public projection page must be public-safe: {page_id}")
    canonical_id = _required_string(frontmatter, "canonical_id", f"page {page_id}")
    if canonical_id.startswith(("private/", "wiki/private/")):
        raise WoonError(f"public projection page has private canonical_id: {page_id}")
    _required_string(frontmatter, "title", f"page {page_id}")
    _public_slug(frontmatter, page_id)
    output = (vault / "wiki" / _safe_output_path(page, page_id)).resolve()
    allowed_root = (vault / _WIKI_ROOT).resolve()
    if output == allowed_root or not output.is_relative_to(allowed_root):
        raise WoonError(f"public projection page must be below wiki/Wiki: {page_id}")


def _public_slug(frontmatter: dict[str, Any], page_id: str) -> str:
    slug = frontmatter.get("public_slug")
    if not isinstance(slug, str) or not _PUBLIC_SLUG.fullmatch(slug):
        raise WoonError(f"public projection page has invalid public_slug: {page_id}")
    return slug


def _verified_compiled_body(
    vault: Path, page: dict[str, Any], receipt: dict[str, Any]
) -> tuple[str, str]:
    page_id = _required_string(page, "page_id", "page spec")
    expected_hash = _required_sha256(receipt.get("output_sha256"), f"compiler receipt {page_id}")
    output = vault / "wiki" / _safe_output_path(page, page_id)
    if not output.is_file():
        raise WoonError(f"public projection compiler output is missing: {page_id}")
    content = output.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        raise WoonError(f"public projection compiler output is stale: {page_id}")
    try:
        metadata, body = split_markdown(content.decode("utf-8"))
    except (UnicodeDecodeError, WoonError) as error:
        raise WoonError(
            f"public projection compiler output is unreadable: {page_id}: {error}"
        ) from error
    compiled = metadata.get("llm_wiki")
    if not isinstance(compiled, dict) or compiled.get("page_id") != page_id:
        raise WoonError(f"public projection requires compiler-owned Markdown: {page_id}")
    frontmatter = _mapping(page.get("frontmatter"), f"page {page_id} frontmatter")
    for field in (
        "title",
        "canonical_id",
        "publication_state",
        "access",
        "public_slug",
        "public_redirect_from",
        "public_redirect_from_paths",
        "content_status",
    ):
        if metadata.get(field) != frontmatter.get(field):
            raise WoonError(f"public projection compiler output metadata drift: {page_id}.{field}")
    header = f"# {_required_string(page, 'title', f'page {page_id}')}"
    if not body.startswith(header):
        raise WoonError(f"public projection compiler output has no matching H1: {page_id}")
    remaining = body[len(header) :].lstrip("\n")
    return expected_hash, remaining.rstrip() + "\n"


def _validate_public_provenance(
    page_id: str, page: dict[str, Any], sources: dict[str, dict[str, Any]]
) -> None:
    source_ids = page.get("source_ids")
    if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
        raise WoonError(f"public projection page source_ids are invalid: {page_id}")
    for source_id in source_ids:
        source = sources.get(source_id)
        if source is None:
            raise WoonError(f"public projection page references missing source: {page_id}")
        if source.get("privacy") != "public" or source.get("lifecycle") != "compiled":
            raise WoonError(f"public projection page has non-public provenance: {page_id}")
        locator = source.get("locator")
        if not isinstance(locator, str) or _looks_private_locator(locator):
            raise WoonError(f"public projection page has private provenance locator: {page_id}")


def _validate_frontmatter_relations(
    page_id: str,
    frontmatter: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    all_targets: dict[str, dict[str, Any]],
    projection_targets: dict[str, str],
    link_checks: list[str],
) -> tuple[str, ...]:
    parent_page: dict[str, Any] | None = None
    parent = frontmatter.get("parent")
    if parent is not None:
        target = _relation_target(parent, f"{page_id}.parent")
        parent_page = _require_public_relation(
            page_id, target, candidates, all_targets, projection_targets, link_checks
        )
    public_parent_id = frontmatter.get("public_parent_id")
    if public_parent_id is not None and (
        parent_page is None or public_parent_id != parent_page.get("page_id")
    ):
        raise WoonError(f"public projection public_parent_id does not match parent: {page_id}")
    public_nav_root = _public_nav_root(frontmatter, page_id)
    for field in _RELATION_LIST_FIELDS:
        value = frontmatter.get(field)
        if value is None:
            continue
        if not isinstance(value, list):
            raise WoonError(f"public projection relation must be a list: {page_id}.{field}")
        for item in value:
            target = _relation_target(item, f"{page_id}.{field}")
            _require_public_relation(
                page_id, target, candidates, all_targets, projection_targets, link_checks
            )
    navigation = frontmatter.get("navigation_groups")
    if navigation is not None:
        if not isinstance(navigation, list):
            raise WoonError(f"public projection navigation_groups must be a list: {page_id}")
        for group in navigation:
            mapping = _mapping(group, f"public projection navigation group {page_id}")
            children = mapping.get("children")
            if not isinstance(children, list):
                raise WoonError(f"public projection navigation children must be a list: {page_id}")
            for child in children:
                target = _relation_target(child, f"{page_id}.navigation_groups.children")
                _require_public_relation(
                    page_id, target, candidates, all_targets, projection_targets, link_checks
                )
    if public_nav_root or parent_page is None:
        return ()
    return _navigation_ancestry(
        page_id,
        parent_page,
        candidates,
        all_targets,
        projection_targets,
        link_checks,
    )


def _public_nav_root(frontmatter: dict[str, Any], page_id: str) -> bool:
    value = frontmatter.get("public_nav_root", False)
    if not isinstance(value, bool):
        raise WoonError(f"public projection public_nav_root must be boolean: {page_id}")
    return value


def _navigation_ancestry(
    page_id: str,
    parent_page: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    all_targets: dict[str, dict[str, Any]],
    projection_targets: dict[str, str],
    link_checks: list[str],
) -> tuple[str, ...]:
    """Return nearest-first public titles used by Just the Docs navigation."""

    titles: list[str] = []
    seen = {page_id}
    current = parent_page
    while True:
        current_id = _required_string(current, "page_id", "public navigation ancestor")
        if current_id in seen:
            raise WoonError(f"public projection navigation contains a cycle: {page_id}")
        seen.add(current_id)
        current_frontmatter = _mapping(current.get("frontmatter"), f"page {current_id} frontmatter")
        titles.append(_required_string(current_frontmatter, "title", "public navigation ancestor"))
        if _public_nav_root(current_frontmatter, current_id):
            break
        parent = current_frontmatter.get("parent")
        if parent is None:
            break
        target = _relation_target(parent, f"{current_id}.parent")
        resolved = _require_public_relation(
            current_id,
            target,
            candidates,
            all_targets,
            projection_targets,
            link_checks,
        )
        if resolved is None:
            break
        current = resolved
    return tuple(titles)


def _relation_target(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"public projection relation must be a non-empty string: {label}")
    cleaned = value.strip()
    match = _WIKILINK.fullmatch(cleaned)
    if match is not None:
        return match.group("target").strip()
    if cleaned.startswith(("https://", "http://")):
        return cleaned
    return cleaned


def _require_public_relation(
    page_id: str,
    target: str,
    candidates: dict[str, dict[str, Any]],
    all_targets: dict[str, dict[str, Any]],
    projection_targets: dict[str, str],
    link_checks: list[str],
) -> dict[str, Any] | None:
    if target.startswith(("https://", "http://")):
        link_checks.append(f"{page_id}:external:{target}")
        return None
    candidate = candidates.get(target)
    if candidate is not None:
        link_checks.append(
            f"{page_id}:public:{_required_string(candidate, 'page_id', 'public relation')}"
        )
        return candidate
    if _is_hidden_wiki_hub(target, all_targets):
        link_checks.append(f"{page_id}:hidden-hub:{target}")
        return None
    if target in all_targets:
        raise WoonError(
            f"public projection relation targets a non-published page: {page_id} -> {target}"
        )
    raise WoonError(f"public projection relation target is unresolved: {page_id} -> {target}")


def _is_hidden_wiki_hub(target: str, all_targets: dict[str, dict[str, Any]]) -> bool:
    page = all_targets.get(target)
    if page is None:
        return target.removesuffix(".md").rstrip("/") in {"Wiki", "wiki/Wiki"}
    return _safe_output_path(page, _required_string(page, "page_id", "page spec")).with_suffix(
        ""
    ) == Path("Wiki/README")


def _project_body(
    page_id: str,
    body: str,
    candidates: dict[str, dict[str, Any]],
    all_targets: dict[str, dict[str, Any]],
    projection_targets: dict[str, str],
    link_checks: list[str],
) -> str:
    body = _unwrap_compiler_navigation(body)
    for label, pattern in _PRIVATE_CONTENT:
        if pattern.search(body) and label != "Obsidian wikilink":
            raise WoonError(f"public projection body contains prohibited {label}: {page_id}")

    def replace(match: re.Match[str]) -> str:
        target = match.group("target").strip()
        resolved = _require_public_relation(
            page_id, target, candidates, all_targets, projection_targets, link_checks
        )
        if resolved is None:
            raise WoonError(f"public projection body links a hidden hub: {page_id} -> {target}")
        target_page_id = _required_string(resolved, "page_id", "public relation")
        label = (match.group("label") or target).strip()
        anchor = match.group("anchor") or ""
        url = f"/wiki/{projection_targets[target_page_id]}/{_jekyll_heading_fragment(anchor)}"
        return f"[{label}]({url})"

    projected = []
    for is_code, part in _fenced_markdown_parts(body):
        if is_code:
            projected.append(part)
            continue
        prose = _WIKILINK.sub(replace, part)
        if _ANY_WIKILINK.search(prose):
            raise WoonError(
                f"public projection body contains an unresolved Obsidian link: {page_id}"
            )
        for match in _MARKDOWN_LINK.finditer(prose):
            target = match.group("target").strip()
            normalized_target = target.removeprefix("/").removeprefix("./")
            if normalized_target.startswith(("wiki/private/", "sources/", "private/")):
                raise WoonError(f"public projection body has a private source link: {page_id}")
            if target.startswith(("https://", "http://", "/", "#", "mailto:")):
                continue
            raise WoonError(f"public projection body has a local Markdown link: {page_id}")
        projected.append(prose)
    return "".join(projected).rstrip() + "\n"


def _fenced_markdown_parts(body: str) -> Iterator[tuple[bool, str]]:
    """Separate fenced literals without changing their bytes or interpreting links."""
    offset = 0
    for opening in _CODE_FENCE.finditer(body):
        if opening.start() < offset:
            continue
        marker = opening.group("marker")
        if marker[0] == "`" and "`" in opening.group("info"):
            continue
        closing = re.compile(
            rf"(?m)^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*(?:\n|$)"
        ).search(body, opening.end())
        end = closing.end() if closing else len(body)
        yield False, body[offset : opening.start()]
        yield True, body[opening.start() : end]
        offset = end
    yield False, body[offset:]


def _jekyll_heading_fragment(anchor: str) -> str:
    """Match the site's kramdown-parser-gfm 1.1.0 automatic heading ID rule.

    Its generate_gfm_header_id keeps Unicode Word characters and hyphens,
    then translates each space/tab to a hyphen without collapsing runs.
    This is a heading fragment, not a filename slug or a rewrite of URI links.
    """
    if not anchor:
        return ""
    text = "".join(character.lower() for character in anchor.removeprefix("#").strip())
    kept = (
        character
        for character in text
        if character in "- \t"
        or unicodedata.category(character)[0] in {"L", "M"}
        or unicodedata.category(character) in {"Nl", "Nd", "Pc"}
    )
    return "#" + "".join("-" if character in " \t" else character for character in kept)


def _unwrap_compiler_navigation(body: str) -> str:
    """Keep the direct-child map while removing compiler-only boundary comments."""

    return body.replace(CHILDREN_START, "").replace(CHILDREN_END, "").strip()


def _render_projected_markdown(
    page_id: str,
    frontmatter: dict[str, Any],
    slug: str,
    navigation_ancestry: tuple[str, ...],
    body: str,
    source_output_sha256: str,
) -> bytes:
    title = _required_string(frontmatter, "title", f"page {page_id}")
    payload_hash = _projection_payload_sha256(
        page_id, frontmatter, slug, navigation_ancestry, body, source_output_sha256
    )
    output: dict[str, Any] = {
        "layout": "default",
        "title": title,
        "nav_order": _nav_order(frontmatter, page_id),
        "permalink": f"/wiki/{slug}/",
        "publication_state": "publish",
        "has_toc": False,
        "projection_id": _required_string(frontmatter, "canonical_id", f"page {page_id}"),
        "projection_sha256": payload_hash,
    }
    if navigation_ancestry:
        output["parent"] = navigation_ancestry[0]
    # The preview flag is a content state, never a privacy override. Eligibility
    # and public provenance have already been checked above.
    content_status = frontmatter.get("content_status")
    if content_status is not None:
        if content_status not in {"planned", "overview", "ready"}:
            raise WoonError(f"public projection content_status is invalid: {page_id}")
        output["content_status"] = content_status
        # Authored maps already guide readers through their grouped children.
        # Keep the fallback list for planned pages and ungrouped hubs.
        authored_child_guide = (
            content_status in {"overview", "ready"}
            and frontmatter.get("reader_navigation") == "sidebar-only"
            and any(group["children"] for group in (frontmatter.get("navigation_groups") or []))
        )
        output["has_toc"] = not authored_child_guide
    if frontmatter.get("public_parent_id"):
        output["public_parent_id"] = frontmatter["public_parent_id"]
    search_terms = frontmatter.get("public_search_terms")
    if search_terms is not None:
        if not isinstance(search_terms, list) or any(
            not isinstance(term, str) or not term.strip() for term in search_terms
        ):
            raise WoonError(f"public projection search terms are invalid: {page_id}")
        if search_terms:
            output["search_terms"] = search_terms
    if len(navigation_ancestry) >= 2:
        output["grand_parent"] = navigation_ancestry[1]
    if len(navigation_ancestry) >= 3:
        output["ancestor"] = navigation_ancestry[-1]
    yaml_text = yaml.safe_dump(
        output, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    if content_status == "planned":
        body = '<p class="wn-content-status">작성 예정</p>\n\n' + body
    reader_body = body if content_status else _insert_reader_toc(body)
    return f"---\n{yaml_text}---\n\n# {title}\n{{: .no_toc }}\n\n{reader_body}".encode()


def _insert_reader_toc(body: str) -> str:
    """Insert the Just the Docs page outline before the first authored H2."""

    match = re.search(r"(?m)^##\s+", body)
    if match is None:
        return body
    introduction = body[: match.start()].rstrip()
    sections = body[match.start() :].lstrip()
    toc = "## 목차\n{: .no_toc .text-delta }\n\n1. TOC\n{:toc}"
    return f"{introduction}\n\n{toc}\n\n{sections}" if introduction else f"{toc}\n\n{sections}"


def _assert_safe_projected_content(page_id: str, content: str) -> None:
    prose = "".join(part for is_code, part in _fenced_markdown_parts(content) if not is_code)
    for label, pattern in _PRIVATE_CONTENT:
        if pattern.search(prose if label == "Obsidian wikilink" else content):
            raise WoonError(f"public projection output contains prohibited {label}: {page_id}")


def _projection_payload_sha256(
    page_id: str,
    frontmatter: dict[str, Any],
    slug: str,
    navigation_ancestry: tuple[str, ...],
    body: str,
    source_output_sha256: str,
) -> str:
    payload = {
        "page_id": page_id,
        "canonical_id": _required_string(frontmatter, "canonical_id", f"page {page_id}"),
        "title": _required_string(frontmatter, "title", f"page {page_id}"),
        "slug": slug,
        "navigation_ancestry": navigation_ancestry,
        "body": body,
        "source_output_sha256": source_output_sha256,
    }
    return _sha256_json(payload)


def _nav_order(frontmatter: dict[str, Any], page_id: str) -> int:
    value = frontmatter.get("sequence", 1000)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WoonError(
            f"public projection page sequence must be a non-negative integer: {page_id}"
        )
    return value


def _looks_private_locator(locator: str) -> bool:
    candidate = locator.replace("\\", "/").lstrip("./")
    return candidate.startswith(("private/", "wiki/private/", "wiki/private/_sources/"))


def _tree_snapshot(root: Path) -> dict[str, str] | None:
    if not root.exists():
        return None
    if root.is_symlink() or not root.is_dir():
        raise WoonError(f"public projection content root is not a directory: {root}")
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise WoonError(f"public projection content root rejects symlink: {path}")
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return snapshot


def _desired_snapshot(report: PublicProjectionReport, current_root: Path) -> dict[str, str]:
    desired = {
        document.relative_path.as_posix(): hashlib.sha256(document.content).hexdigest()
        for document in report.artifacts
    }
    readme = current_root / "README.md"
    if readme.is_file():
        desired["README.md"] = hashlib.sha256(readme.read_bytes()).hexdigest()
    return desired


def _replace_content_root(content_root: Path, report: PublicProjectionReport) -> None:
    parent = content_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage_path = Path(tempfile.mkdtemp(prefix=".public-content-stage-", dir=parent))
    stage: Path | None = stage_path
    backup: Path | None = None
    try:
        existing_readme = content_root / "README.md"
        if existing_readme.is_file():
            atomic_write(stage_path / "README.md", existing_readme.read_bytes(), mode=0o644)
        for document in report.artifacts:
            destination = stage_path / document.relative_path
            atomic_write(destination, document.content, mode=0o644)
        if content_root.exists():
            backup = Path(tempfile.mkdtemp(prefix=".public-content-backup-", dir=parent))
            backup.rmdir()
            os.replace(content_root, backup)
        os.replace(stage_path, content_root)
        stage = None
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if content_root.exists() and backup is not None:
            shutil.rmtree(content_root)
        if backup is not None and backup.exists() and not content_root.exists():
            os.replace(backup, content_root)
        raise
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"public projection {label} must be a mapping")
    return value


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"public projection {label} requires {key}")
    return value.strip()


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise WoonError(f"public projection {label} requires a sha256")
    return value


def _sha256_json(payload: object) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
