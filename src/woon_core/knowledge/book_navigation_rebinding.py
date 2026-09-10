"""Pinned navigation ownership changes that preserve book evidence and progress."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.wiki_tree import split_markdown, strip_generated_wiki_views


@dataclass(frozen=True, slots=True)
class BookNavigationCoverageRebinding:
    relative_path: str
    expected_sha256: str
    replacement: dict[str, Any]
    scope_sha256: dict[str, str] = field(default_factory=dict)


def _pinned_file(vault: Path, relative: str, digest: str, prefix: str) -> Path:
    candidate = Path(relative)
    path = vault / candidate
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.as_posix() != relative
        or not relative.startswith(prefix)
        or candidate.suffix != ".json"
        or not path.is_file()
        or any(part.is_symlink() for part in (path, *path.parents))
        or hashlib.sha256(path.read_bytes()).hexdigest() != digest
    ):
        raise WoonError("book navigation coverage file changed or has an unsafe path: " + relative)
    return path


def prepare_book_navigation_rebindings(
    vault: Path,
    updates: tuple[BookNavigationCoverageRebinding, ...],
    pages: dict[str, dict[str, Any]],
    upserts: dict[str, dict[str, Any]],
    retiring: frozenset[str],
) -> dict[Path, bytes]:
    """Allow only empty-node retirement, display rebinding, and scope hash refresh.

    A private reader link is not proof of source prose or code completion. All
    edition, source inventories, meaning assignments, progress, and runnable
    records remain byte-equivalent structured values.
    """
    if not updates:
        return {}
    display_only = not retiring
    results: dict[Path, bytes] = {}
    covered: set[str] = set()
    for update in updates:
        if not isinstance(update, BookNavigationCoverageRebinding):
            raise WoonError("book navigation coverage requires a typed pinned update")
        path = _pinned_file(
            vault, update.relative_path, update.expected_sha256, "catalog/book-coverage/"
        )
        if len(Path(update.relative_path).parts) != 3 or path in results:
            raise WoonError("book navigation coverage must name each full manifest once")
        current = json.loads(path.read_bytes())
        after = update.replacement
        book = current.get("book_id")
        root = upserts.get(book, {})
        metadata = root.get("frontmatter", {})
        if (
            not isinstance(book, str)
            or after.get("book_id") != book
            or metadata.get("entity_kind") != "book"
            or metadata.get("access") != "local-only"
            or metadata.get("publish") is not False
            or metadata.get("reader_navigation") != "sidebar-only"
        ):
            raise WoonError("book navigation coverage requires an explicit private book root")
        mutable = {"nodes", "toc_node_count", "toc_leaf_count", "source_structure_assignments"}
        if display_only:
            mutable = {"source_structure_assignments"}
        if {k: v for k, v in current.items() if k not in mutable} != {
            k: v for k, v in after.items() if k not in mutable
        }:
            raise WoonError("book navigation coverage changed immutable evidence or progress")
        nodes = current.get("nodes")
        if not isinstance(nodes, list) or any(not isinstance(row, dict) for row in nodes):
            raise WoonError("book navigation coverage requires an existing node inventory")
        removed = {row.get("canonical_id") for row in nodes} & retiring
        if (not display_only and not removed) or covered.intersection(removed):
            raise WoonError("book navigation coverage needs distinct retired node ownership")
        covered.update(removed)
        expected_nodes = []
        for node in nodes:
            identifier = node.get("canonical_id")
            if identifier in removed:
                page = pages.get(identifier, {})
                output = vault / "wiki" / page.get("output_path", "")
                if not output.is_file() or node.get("has_direct_content") is not False:
                    raise WoonError("book navigation may retire only verified empty nodes")
                authored = split_markdown(strip_generated_wiki_views(output.read_text()))[1]
                if authored.split("\n", 1)[-1].strip():
                    raise WoonError("book navigation retirement would discard authored content")
                if any(
                    row.get("owner_id", row.get("canonical_id")) == identifier
                    for row in current.get("source_element_assignments", [])
                ):
                    raise WoonError("book navigation cannot retire a semantic source owner")
                continue
            preserved = copy.deepcopy(node)
            for key in ("parent", "parent_id"):
                if preserved.get(key) in removed:
                    preserved[key] = book
            expected_nodes.append(preserved)
        if (
            after.get("nodes") != expected_nodes
            or after.get("toc_node_count") != len(expected_nodes)
            or after.get("toc_leaf_count") != current.get("toc_leaf_count")
        ):
            raise WoonError("book navigation changed surviving node evidence or leaf counts")
        before_assignments = current.get("source_structure_assignments", [])
        assignments = after.get("source_structure_assignments", [])
        if len(assignments) != len(before_assignments):
            raise WoonError("book navigation must preserve every source structure assignment")
        structure_elements: dict[str, tuple[int | None, dict[str, Any]]] = {
            row["structure_id"]: (index, row)
            for index, row in enumerate(current.get("source_structure_elements", []), 1)
        }
        assignments_after = {row.get("structure_id"): row for row in assignments}
        changed_assignments = 0
        for before, replacement in zip(before_assignments, assignments, strict=True):
            if before.get("structure_id") != replacement.get("structure_id"):
                raise WoonError("book navigation changed source structure identity or order")
            if display_only:
                if replacement == before:
                    continue
                delivery_fields = {
                    "disposition",
                    "heading",
                    "reader_path",
                    "reader_anchor",
                    "reader_sha256",
                    "body_sha256",
                    "heading_review",
                }
                if before.get("disposition") == "navigation-group-heading":
                    structure_id = before["structure_id"]
                    source_order, element = structure_elements.get(structure_id, (None, {}))
                    if (
                        replacement.get("disposition") != "private-reader"
                        or element.get("kind") != "chapter"
                        or before.get("label") != element.get("title")
                        or type(replacement.get("source_order")) is not int
                        or replacement.get("source_order") != source_order
                    ):
                        raise WoonError(
                            "book group delivery must retain its source chapter position"
                        )
                    delivery_fields |= {"label", "source_order"}
                if (
                    before.get("disposition")
                    not in {
                        "book-root-heading",
                        "private-reader",
                        "navigation-group-heading",
                    }
                    or replacement.get("disposition") not in {"book-root-heading", "private-reader"}
                    or before.get("owner_id") != book
                    or replacement.get("owner_id") != book
                    or {k: v for k, v in before.items() if k not in delivery_fields}
                    != {k: v for k, v in replacement.items() if k not in delivery_fields}
                ):
                    raise WoonError(
                        "display-only book navigation may change only existing root delivery"
                    )
                changed_assignments += 1
                continue
            owner = before.get("canonical_id", before.get("owner_id"))
            if owner not in removed:
                if replacement != before:
                    raise WoonError("book navigation changed an unaffected source assignment")
            elif (
                replacement.get("disposition") not in {"private-reader", "book-root-heading"}
                or replacement.get("owner_id") != book
            ):
                raise WoonError("retired source structure needs an explicit reader or root heading")
        if display_only and not changed_assignments:
            raise WoonError("display-only book navigation requires a delivery change")
        content = (json.dumps(after, ensure_ascii=False, indent=2) + "\n").encode()
        results[path] = content
        new_hash = hashlib.sha256(content).hexdigest()
        scope_folder = vault / "catalog/book-coverage-scopes" / path.stem
        actual_scopes = {item.relative_to(vault).as_posix() for item in scope_folder.glob("*.json")}
        if actual_scopes != set(update.scope_sha256):
            raise WoonError("book navigation must pin all existing chapter scope manifests")
        for relative, digest in update.scope_sha256.items():
            scope_path = _pinned_file(vault, relative, digest, "catalog/book-coverage-scopes/")
            scope = json.loads(scope_path.read_bytes())
            boundary = scope.get("coverage_scope", {})
            if (
                scope.get("book_id") != book
                or boundary.get("base_relative_path") != update.relative_path
                or boundary.get("base_sha256") != update.expected_sha256
                or any(node.get("canonical_id") in removed for node in scope.get("nodes", []))
                or any(
                    row.get("canonical_id", row.get("owner_id")) in removed
                    for row in scope.get("source_structure_assignments", [])
                )
                or (
                    display_only
                    and any(
                        row != assignments_after.get(row.get("structure_id"))
                        for row in scope.get("source_structure_assignments", [])
                    )
                )
            ):
                raise WoonError("book navigation cannot alter an affected or stale chapter scope")
            boundary["base_sha256"] = new_hash
            results[scope_path] = (json.dumps(scope, ensure_ascii=False, indent=2) + "\n").encode()
    if covered != retiring:
        raise WoonError("book navigation coverage does not cover every retired page")
    return results


def validate_book_navigation_outputs(
    vault: Path,
    updates: tuple[BookNavigationCoverageRebinding, ...],
    pages: dict[str, dict[str, Any]],
) -> None:
    """Verify current files against the new structure without upgrading learning progress."""
    from woon_core.knowledge.book_coverage import _audit_source_structure_contract

    for update in updates:
        manifest = update.replacement
        book = manifest["book_id"]
        nodes = manifest["nodes"]
        node_order = [node["canonical_id"] for node in nodes]
        records = {}
        for identifier in [book, *node_order]:
            page = pages.get(identifier)
            if page is None:
                raise WoonError("book navigation survivor is missing: " + identifier)
            path = vault / "wiki" / page["output_path"]
            metadata, body = split_markdown(path.read_text(encoding="utf-8"))
            records[identifier] = (path, metadata, body)
        errors: list[str] = []
        _audit_source_structure_contract(
            update.relative_path,
            manifest,
            set(node_order),
            {node["canonical_id"] for node in nodes if node.get("leaf") is True},
            node_order,
            records,
            errors,
            vault=vault,
        )
        if errors:
            raise WoonError("book navigation source structure failed: " + "; ".join(errors))
