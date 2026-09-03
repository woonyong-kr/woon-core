from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from woon_core.knowledge.book_contract import (
    BOOK_CONTRACT_SHA256,
    LEGACY_BOOK_CONTRACT_SHA256_V7,
    PRE_IN_PAGE_H2_BOOK_CONTRACT_SHA256_V7,
    PRE_ORDERED_READER_SECTIONS_BOOK_CONTRACT_SHA256_V7,
    book_promotion_contract_fields,
    require_current_book_contract,
)
from woon_core.knowledge.book_coverage import (
    _audit_ordered_book_reader_ui,
    _audit_source_structure_contract,
    _audit_static_harness_fidelity,
    _ordered_reader_owner_runs,
    audit_book_coverage,
    audit_book_coverage_scope,
)
from woon_core.knowledge.wiki_tree import CHILDREN_END, CHILDREN_START, split_markdown


def test_book_contract_v7_keeps_legacy_payload_read_compatibility() -> None:
    current = {"apply": False, **book_promotion_contract_fields()}
    current_contract = current["book_contract"]
    assert isinstance(current_contract, dict)
    assert current_contract["sha256"] == BOOK_CONTRACT_SHA256
    assert BOOK_CONTRACT_SHA256 != LEGACY_BOOK_CONTRACT_SHA256_V7

    legacy = json.loads(json.dumps(current))
    legacy["book_contract"]["sha256"] = LEGACY_BOOK_CONTRACT_SHA256_V7
    require_current_book_contract(legacy, "book-promote")

    pre_in_page_h2 = json.loads(json.dumps(current))
    pre_in_page_h2["book_contract"]["sha256"] = PRE_IN_PAGE_H2_BOOK_CONTRACT_SHA256_V7
    require_current_book_contract(pre_in_page_h2, "book-promote")

    pre_ordered_reader = json.loads(json.dumps(current))
    pre_ordered_reader["book_contract"]["sha256"] = (
        PRE_ORDERED_READER_SECTIONS_BOOK_CONTRACT_SHA256_V7
    )
    require_current_book_contract(pre_ordered_reader, "book-promote")


def _source_element(
    kind: str,
    semantic_unit: str,
    locator: str,
    source_sha256: str,
    *,
    runnable_support: str | None = None,
) -> dict[str, object]:
    identity = json.dumps(
        {
            "kind": kind,
            "semantic_unit": semantic_unit,
            "source_locator": locator,
            "source_sha256": source_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    element: dict[str, object] = {
        "element_id": f"{kind}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}",
        "kind": kind,
        "semantic_unit": semantic_unit,
        "source_locator": locator,
        "source_sha256": source_sha256,
    }
    if runnable_support is not None:
        element["runnable_support"] = runnable_support
    return element


def _inventory(
    elements: list[dict[str, object]], assignments: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "source_element_inventory_evidence": {
            "locator": "evidence/source-elements.json",
            "sha256": "c" * 64,
            "verified_on": "2026-09-01",
            "extraction_method": "manual-semantic-review",
            "semantic_unit_policy_sha256": "f" * 64,
        },
        "source_elements": elements,
        "source_element_assignments": assignments,
    }


def _verified_run_element_contract(owner_id: str) -> dict[str, object]:
    span = "자연스러운 한국어 학습 본문이다."
    elements = [
        _source_element("claim", "paragraph", "page 2 paragraph 1", "1" * 64),
        _source_element(
            "example",
            "worked-example",
            "page 2 example 1",
            "2" * 64,
            runnable_support="supported",
        ),
        _source_element(
            "code",
            "code-block",
            "page 2 listing 1",
            "3" * 64,
            runnable_support="supported",
        ),
    ]
    assignments: list[dict[str, object]] = [
        {
            "element_id": elements[0]["element_id"],
            "owner_id": owner_id,
            "delivery": "reader-span",
            "delivery_span": span,
            "delivery_span_sha256": hashlib.sha256(span.encode("utf-8")).hexdigest(),
        },
    ] + [
        {
            "element_id": element["element_id"],
            "owner_id": owner_id,
            "delivery": "run-block",
            "run_language": "run-kotlin",
            "run_block_index": index,
            "verification_evidence": "evidence/kotlin-example-1.json",
            "verification_sha256": "b" * 64,
        }
        for index, element in enumerate(elements[1:], start=1)
    ]
    return _inventory(elements, assignments)


def _claim_element_contract(owner_id: str) -> dict[str, object]:
    span = "자연스러운 한국어 학습 본문이다."
    element = _source_element("claim", "paragraph", "page 2 paragraph 1", "1" * 64)
    return _inventory(
        [element],
        [
            {
                "element_id": element["element_id"],
                "owner_id": owner_id,
                "delivery": "reader-span",
                "delivery_span": span,
                "delivery_span_sha256": hashlib.sha256(span.encode("utf-8")).hexdigest(),
            }
        ],
    )


def _structure_contract(
    entries: list[tuple[str, str, str, str]],
) -> dict[str, object]:
    elements: list[dict[str, object]] = []
    assignments: list[dict[str, object]] = []
    for canonical_id, kind, title, locator in entries:
        source_sha256 = hashlib.sha256(locator.encode("utf-8")).hexdigest()
        identity = json.dumps(
            {
                "kind": kind,
                "source_locator": locator,
                "source_sha256": source_sha256,
                "title": title,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        structure_id = f"structure:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
        elements.append(
            {
                "structure_id": structure_id,
                "kind": kind,
                "title": title,
                "source_locator": locator,
                "source_sha256": source_sha256,
            }
        )
        assignments.append(
            {
                "structure_id": structure_id,
                "disposition": "canonical-node",
                "canonical_id": canonical_id,
            }
        )
    return {
        "source_structure_inventory_evidence": {
            "locator": "evidence/source-structure.json",
            "sha256": "9" * 64,
            "verified_on": "2026-09-01",
        },
        "source_structure_elements": elements,
        "source_structure_assignments": assignments,
        "retired_source_section_wrappers": [],
    }


def _source_structure(kind: str, title: str, locator: str) -> dict[str, object]:
    source_sha256 = hashlib.sha256(locator.encode("utf-8")).hexdigest()
    identity = json.dumps(
        {
            "kind": kind,
            "source_locator": locator,
            "source_sha256": source_sha256,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "structure_id": f"structure:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}",
        "kind": kind,
        "title": title,
        "source_locator": locator,
        "source_sha256": source_sha256,
    }


def _page(path: Path, *, canonical_id: str, parent: str | None, book: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_row = f"parent: '[[wiki/{parent}|parent]]'\n" if parent else ""
    book_rows = "content_kind: book\nentity_kind: book\n" if book else ""
    path.write_text(
        "---\n"
        f"title: {canonical_id}\n"
        f"canonical_id: {canonical_id}\n"
        "node_kind: entity\n"
        f"{parent_row}{book_rows}"
        "---\n\n"
        f"# {canonical_id}\n\n"
        "## 설명\n\n자연스러운 한국어 학습 본문이다.\n",
        encoding="utf-8",
    )


def _verified_fixture(vault: Path) -> tuple[Path, dict[str, object]]:
    book_id = "books/kotlin"
    leaf_id = f"{book_id}/chapter-01"
    _page(vault / "wiki/books/kotlin.md", canonical_id=book_id, parent=None, book=True)
    leaf_path = vault / "wiki/books/kotlin/chapter-01.md"
    _page(leaf_path, canonical_id=leaf_id, parent=book_id)
    leaf_path.write_text(
        leaf_path.read_text(encoding="utf-8")
        + "\n```run-kotlin\nfun main() = println(1)\n```\n"
        + "\n```run-kotlin\nfun main() = println(2)\n```\n",
        encoding="utf-8",
    )
    manifest: dict[str, object] = {
        "schema_version": 2,
        "book_id": book_id,
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "publisher.example/toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
        **_structure_contract([(leaf_id, "chapter", leaf_id, "pages 1-10")]),
        **_verified_run_element_contract(leaf_id),
        "nodes": [
            {
                "canonical_id": leaf_id,
                "parent_id": book_id,
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "pages 1-10",
                "state": "code-verified",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 1, "covered": 1},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 1, "covered": 1},
                },
                "runnable": {"expected": 2, "verified": 2},
                "korean_prose_reviewed": True,
            }
        ],
    }
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")
    return target, manifest


def _write_verified_root_map(
    vault: Path,
    *,
    group_label: str = "1부 코틀린 소개",
    rendered_label: str | None = None,
    rendered_target: str = "books/kotlin/chapter-01",
    generic_wrapper: str = "",
) -> None:
    rendered_label = group_label if rendered_label is None else rendered_label
    (vault / "wiki/books/kotlin.md").write_text(
        "---\n"
        "title: 코틀린\n"
        "canonical_id: books/kotlin\n"
        "node_kind: entity\n"
        "content_kind: book\n"
        "entity_kind: book\n"
        "navigation_groups:\n"
        f"- label: {group_label}\n"
        "  children:\n"
        "  - books/kotlin/chapter-01\n"
        "---\n\n"
        "# 코틀린\n\n"
        f"{generic_wrapper}"
        f"{CHILDREN_START}\n"
        f"## {rendered_label}\n"
        f"- [[wiki/{rendered_target}|1장]]\n"
        f"{CHILDREN_END}\n",
        encoding="utf-8",
    )


def _upgrade_manifest_to_v7(
    vault: Path,
    manifest: dict[str, object],
    *,
    workflow_phase: str = "source-landed",
    translation_required: bool = True,
) -> None:
    source_bytes = b"verified book source"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    relative_source = (
        "wiki/private/_sources/knowledge/local-only/kotlin/Kotlin in Action.pdf"
    )
    source_path = vault / relative_source
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source_bytes)
    manifest["schema_version"] = 3
    manifest["workflow_phase"] = workflow_phase
    manifest["translation_required"] = translation_required
    manifest["edition"] = {"label": "2판", "source_sha256": source_sha256}
    manifest["source_archive"] = {
        "relative_path": relative_source,
        "actual_title": "Kotlin in Action",
        "sha256": source_sha256,
        "privacy": "local-only",
    }
    manifest["source_asset_inventory"] = []
    manifest["source_asset_inventory_evidence"] = {
        "locator": "catalog/book-assets/kotlin.json",
        "sha256": "d" * 64,
        "verified_on": "2026-09-02",
        "embedded_original_bytes": True,
        "scan_crop_provenance": False,
        "expected_asset_count": 0,
        "inventory_sha256": hashlib.sha256(b"[]").hexdigest(),
    }
    evidence: dict[str, object] = {
        "source-landed": {
            "locator": "catalog/book-phases/kotlin/source-landed.json",
            "sha256": "e" * 64,
        }
    }
    if workflow_phase in {"translated", "concept-linked", "understanding-enriched"}:
        evidence["translated"] = {
            "locator": "catalog/book-phases/kotlin/translated.json",
            "sha256": "f" * 64,
        }
    if workflow_phase in {"concept-linked", "understanding-enriched"}:
        pages: list[dict[str, str]] = []
        for node in manifest["nodes"]:
            assert isinstance(node, dict)
            canonical_id = str(node["canonical_id"])
            page_path = next(
                path
                for path in (vault / "wiki").rglob("*.md")
                if f"canonical_id: {canonical_id}\n" in path.read_text(encoding="utf-8")
            )
            _, page_body = split_markdown(page_path.read_text(encoding="utf-8"))
            pages.append(
                {
                    "canonical_id": canonical_id,
                    "reader_body": re.sub(r"(?m)^# .+?\s*$", "", page_body, count=1),
                }
            )
        evidence["concept-linked"] = {
            "locator": "catalog/book-phases/kotlin/concept-linked.json",
            "sha256": "1" * 64,
            "book_content_sha256": hashlib.sha256(
                json.dumps(
                    pages,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "relation_ids": [
                "books/kotlin/chapter-01|related_to|concepts/kotlin"
            ],
        }
    if workflow_phase == "understanding-enriched":
        source_coverage_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "source_elements": manifest.get("source_elements"),
                    "owner_bindings": sorted(
                        (
                            str(item.get("element_id", "")),
                            str(item.get("owner_id", "")),
                        )
                        for item in manifest.get("source_element_assignments", [])
                        if isinstance(item, dict)
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        translation_coverage_sha256 = hashlib.sha256(
            json.dumps(
                manifest.get("source_element_assignments"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        evidence["understanding-enriched"] = {
            "locator": "catalog/book-phases/kotlin/understanding-enriched.json",
            "sha256": "3" * 64,
            "source_coverage_sha256": source_coverage_sha256,
            "translation_coverage_sha256": translation_coverage_sha256,
            "source_session_ids": ["session://learning/kotlin"],
        }
    manifest["phase_evidence"] = evidence
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, dict)
        node["reader_language"] = (
            "en" if workflow_phase == "source-landed" and translation_required else "ko"
        )
        node["source_prose_verified"] = True
        if workflow_phase == "source-landed":
            node.pop("korean_prose_reviewed", None)
        else:
            node["korean_prose_reviewed"] = True


def test_book_coverage_v7_accepts_source_language_before_translation(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete
    assert report.covered_leaf_count == 1


def test_book_coverage_accepts_explicit_toc_only_non_leaf(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf_id = "books/kotlin/chapter-01"
    (vault / "wiki/books/kotlin/chapter-01.md").write_text(
        "---\n"
        f"title: {leaf_id}\n"
        f"canonical_id: {leaf_id}\n"
        "node_kind: entity\n"
        "parent: '[[wiki/books/kotlin|parent]]'\n"
        "content_state: toc-only\n"
        "---\n\n"
        f"# {leaf_id}\n",
        encoding="utf-8",
    )
    nodes = manifest["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    node = nodes[0]
    node["state"] = "toc-only"
    node["leaf"] = False
    node["has_direct_content"] = False
    node.pop("coverage")
    node.pop("runnable")
    manifest["toc_leaf_count"] = 0
    manifest["source_elements"] = []
    manifest["source_element_assignments"] = []
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete
    assert report.covered_leaf_count == 0


@pytest.mark.parametrize(
    ("node_change", "body", "message"),
    [
        ({"leaf": True}, "", "toc-only node must declare leaf=false"),
        (
            {"has_direct_content": True},
            "",
            "toc-only node must declare has_direct_content=false",
        ),
        ({}, "임의 설명이다.\n", "toc-only page contains authored prose"),
    ],
)
def test_book_coverage_rejects_invalid_toc_only_node(
    tmp_path: Path,
    node_change: dict[str, object],
    body: str,
    message: str,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf_id = "books/kotlin/chapter-01"
    (vault / "wiki/books/kotlin/chapter-01.md").write_text(
        "---\n"
        f"title: {leaf_id}\n"
        f"canonical_id: {leaf_id}\n"
        "node_kind: entity\n"
        "parent: '[[wiki/books/kotlin|parent]]'\n"
        "content_state: toc-only\n"
        "---\n\n"
        f"# {leaf_id}\n\n{body}",
        encoding="utf-8",
    )
    nodes = manifest["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    node = nodes[0]
    node.update(
        {
            "state": "toc-only",
            "leaf": False,
            "has_direct_content": False,
            **node_change,
        }
    )
    node.pop("coverage")
    node.pop("runnable")
    manifest["toc_leaf_count"] = 1 if node.get("leaf") is True else 0
    manifest["source_elements"] = []
    manifest["source_element_assignments"] = []
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert any(message in error for error in report.errors)


def test_book_coverage_v7_rejects_remote_runner_for_local_only_source(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    evidence = tmp_path / "run-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "external_transmission": True,
                "results": [{"provider": "Wandbox", "passed": True}],
            }
        ),
        encoding="utf-8",
    )
    assignments = manifest["source_element_assignments"]
    assert isinstance(assignments, list)
    for assignment in assignments:
        assert isinstance(assignment, dict)
        if assignment.get("delivery") == "run-block":
            assignment["verification_evidence"] = "run-evidence.json"
            assignment["verification_sha256"] = hashlib.sha256(
                evidence.read_bytes()
            ).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    remote = audit_book_coverage(vault)
    assert any("prohibited provider(s): wandbox" in error for error in remote.errors)
    assert any("records external transmission" in error for error in remote.errors)

    evidence.write_text(
        json.dumps(
            {
                "external_transmission": False,
                "results": [{"provider": "local", "passed": True}],
            }
        ),
        encoding="utf-8",
    )
    for assignment in assignments:
        assert isinstance(assignment, dict)
        if assignment.get("delivery") == "run-block":
            assignment["verification_sha256"] = hashlib.sha256(
                evidence.read_bytes()
            ).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    local = audit_book_coverage(vault)
    assert local.complete


def test_book_coverage_v7_requires_korean_review_at_translated_phase(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest, workflow_phase="translated")
    nodes = manifest["nodes"]
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    nodes[0]["reader_language"] = "en"
    nodes[0].pop("korean_prose_reviewed", None)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert not report.complete
    assert any("translated-or-later reader_language must be ko" in error for error in report.errors)
    assert any("translated-or-later korean_prose_reviewed" in error for error in report.errors)


def test_book_coverage_v7_korean_source_uses_no_op_translation_review(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(
        vault,
        manifest,
        workflow_phase="translated",
        translation_required=False,
    )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete


def test_book_coverage_v7_accepts_hash_pinned_concept_link_phase(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest, workflow_phase="concept-linked")
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete


def test_book_coverage_v7_rejects_enrichment_coverage_hash_drift(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest, workflow_phase="understanding-enriched")
    phase_evidence = manifest["phase_evidence"]
    assert isinstance(phase_evidence, dict)
    enrichment = phase_evidence["understanding-enriched"]
    assert isinstance(enrichment, dict)
    enrichment["translation_coverage_sha256"] = "0" * 64
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert not report.complete
    assert any(
        "understanding-enriched translation coverage hash does not match" in error
        for error in report.errors
    )


def test_book_coverage_v7_rejects_archive_name_or_hash_drift(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    source_archive = manifest["source_archive"]
    assert isinstance(source_archive, dict)
    source_archive["actual_title"] = "Wrong title"
    source_archive["sha256"] = "0" * 64
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert not report.complete
    assert any("filename must use actual_title" in error for error in report.errors)
    assert any("source archive hash must match" in error for error in report.errors)


def test_book_coverage_v7_rejects_embedded_image_byte_drift(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    asset_bytes = b"archived image bytes"
    asset_path = (
        vault
        / "wiki/private/_sources/knowledge/local-only/kotlin/assets/figure-01.png"
    )
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(asset_bytes)
    inventory = [
        {
            "asset_id": "figure-01",
            "source_locator": "epub://kotlin/ch01.xhtml#figure-01",
            "source_sha256": "0" * 64,
            "archive_relative_path": asset_path.relative_to(vault).as_posix(),
            "archive_sha256": hashlib.sha256(asset_bytes).hexdigest(),
            "extraction_kind": "embedded-original",
            "crop_provenance": None,
        }
    ]
    manifest["source_asset_inventory"] = inventory
    evidence = manifest["source_asset_inventory_evidence"]
    assert isinstance(evidence, dict)
    evidence["expected_asset_count"] = 1
    evidence["inventory_sha256"] = hashlib.sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert not report.complete
    assert any("embedded image bytes must be preserved exactly" in error for error in report.errors)


def test_book_coverage_v7_rejects_unpinned_scan_crop(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    asset_bytes = b"scan crop bytes"
    asset_path = vault / "wiki/private/_sources/knowledge/local-only/kotlin/assets/crop.png"
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(asset_bytes)
    inventory = [
        {
            "asset_id": "crop-01",
            "source_locator": "pdf://kotlin?page=12#figure-01",
            "source_sha256": hashlib.sha256(asset_bytes).hexdigest(),
            "archive_relative_path": asset_path.relative_to(vault).as_posix(),
            "archive_sha256": hashlib.sha256(asset_bytes).hexdigest(),
            "extraction_kind": "scan-crop",
            "crop_provenance": None,
        }
    ]
    manifest["source_asset_inventory"] = inventory
    evidence = manifest["source_asset_inventory_evidence"]
    assert isinstance(evidence, dict)
    evidence["expected_asset_count"] = 1
    evidence["inventory_sha256"] = hashlib.sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert not report.complete
    assert any("scan crop provenance fields are invalid" in error for error in report.errors)


def test_book_coverage_accepts_verified_leaf(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)
    _page(
        vault / "wiki/books/kotlin/chapter-01.md",
        canonical_id="books/kotlin/chapter-01",
        parent="books/kotlin",
    )
    chapter = vault / "wiki/books/kotlin/chapter-01.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8")
        + "\n```run-kotlin\nfun main() = println(1)\n```\n"
        + "\n```run-kotlin\nfun main() = println(2)\n```\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "book_id": "books/kotlin",
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "publisher.example/toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
        **_structure_contract(
            [
                (
                    "books/kotlin/chapter-01",
                    "chapter",
                    "books/kotlin/chapter-01",
                    "pages 1-10",
                )
            ]
        ),
        **_verified_run_element_contract("books/kotlin/chapter-01"),
        "nodes": [
            {
                "canonical_id": "books/kotlin/chapter-01",
                "parent_id": "books/kotlin",
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "pages 1-10",
                "state": "code-verified",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 1, "covered": 1},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 1, "covered": 1},
                },
                "runnable": {"expected": 2, "verified": 2},
                "korean_prose_reviewed": True,
            }
        ],
    }
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete
    assert report.covered_leaf_count == 1
    assert report.contract_version > 0
    assert len(report.contract_sha256) == 64
    assert all(
        lane.complete
        for lane in (
            report.structure,
            report.source,
            report.runnable,
            report.quality,
            report.ui,
        )
    )


def test_legacy_full_manifest_is_pending_while_verified_scope_is_complete(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    base_path, manifest = _verified_fixture(vault)
    legacy = dict(manifest)
    legacy["schema_version"] = 1
    base_bytes = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    base_path.write_bytes(base_bytes)
    fragment = dict(manifest)
    fragment["coverage_scope"] = {
        "root_id": "books/kotlin/chapter-01",
        "base_relative_path": "catalog/book-coverage/kotlin.json",
        "base_sha256": hashlib.sha256(base_bytes).hexdigest(),
    }
    relative_scope = "catalog/book-coverage-scopes/kotlin/chapter-01.json"
    fragment_path = vault / relative_scope
    fragment_path.parent.mkdir(parents=True)
    fragment_path.write_text(json.dumps(fragment), encoding="utf-8")

    scoped = audit_book_coverage_scope(vault, relative_scope)
    global_report = audit_book_coverage(vault)

    assert scoped.complete
    assert scoped.errors == ()
    assert scoped.covered_leaf_count == 1
    assert global_report.complete is False
    assert global_report.errors == ()
    assert global_report.pending_books == ("books/kotlin",)
    assert global_report.verified_scope_count == 1
    assert global_report.covered_leaf_count == 1


def test_book_coverage_accepts_current_managed_book_map_projection(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    _write_verified_root_map(vault)

    report = audit_book_coverage(vault)

    assert report.complete
    assert report.ui.complete


def test_book_coverage_rejects_missing_or_stale_managed_book_map_projection(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    _write_verified_root_map(
        vault,
        rendered_label="과거 분류",
        rendered_target="books/kotlin/stale-chapter",
    )

    report = audit_book_coverage(vault)

    assert not report.ui.complete
    assert any("managed group headings are stale" in error for error in report.ui.errors)
    assert any("managed direct links are stale" in error for error in report.ui.errors)


def test_book_coverage_rejects_generic_wrapper_around_managed_book_map(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    _write_verified_root_map(vault, generic_wrapper="## 하위 키워드\n\n")

    report = audit_book_coverage(vault)

    assert not report.ui.complete
    assert any("generic wrapper heading" in error for error in report.ui.errors)


def test_book_coverage_rejects_numbered_descendant_wrapper_child(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    _write_verified_root_map(vault)
    chapter_id = "books/kotlin/chapter-01"
    wrapper_id = f"{chapter_id}/1-1"
    terminal_id = f"{wrapper_id}/1-1-1"
    chapter = vault / "wiki/books/kotlin/chapter-01.md"
    chapter.write_text(
        "---\n"
        "title: books/kotlin/chapter-01\n"
        f"canonical_id: {chapter_id}\n"
        "node_kind: entity\n"
        "parent: '[[wiki/books/kotlin|Kotlin]]'\n"
        "navigation_groups:\n"
        "- label: 1.1 함수\n"
        f"  children: [{wrapper_id}]\n"
        "---\n\n"
        f"# {chapter_id}\n\n"
        f"{CHILDREN_START}\n"
        "## 1.1 함수\n"
        f"- [[wiki/{wrapper_id}|1.1 함수]]\n"
        f"{CHILDREN_END}\n",
        encoding="utf-8",
    )
    wrapper = vault / "wiki/books/kotlin/chapter-01/1-1.md"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        "---\n"
        "title: 1.1 함수\n"
        f"canonical_id: {wrapper_id}\n"
        "node_kind: entity\n"
        f"parent: '[[wiki/{chapter_id}|1장]]'\n"
        "navigation_groups:\n"
        "- label: 1.1\n"
        f"  children: [{terminal_id}]\n"
        "---\n\n"
        "# 1.1 함수\n\n"
        f"{CHILDREN_START}\n"
        "## 1.1\n"
        f"- [[wiki/{terminal_id}|1.1.1 선언]]\n"
        f"{CHILDREN_END}\n",
        encoding="utf-8",
    )
    _page(
        vault / "wiki/books/kotlin/chapter-01/1-1/1-1-1.md",
        canonical_id=terminal_id,
        parent=wrapper_id,
    )

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("duplicate-title wrapper child" in error for error in report.ui.errors)
    assert any("descendant-owning source section wrapper" in error for error in report.ui.errors)


def test_book_coverage_rejects_balanced_counts_without_source_inventory(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    manifest.pop("source_elements")
    manifest.pop("source_element_assignments")
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert report.source.complete is False
    assert report.structure.complete
    assert report.runnable.complete is False
    assert report.quality.complete
    assert report.ui.complete
    assert any("must inventory claim" in error for error in report.source.errors)
    assert any("runnable audit is incomplete" in error for error in report.runnable.errors)


def test_book_coverage_rejects_source_element_assigned_more_than_once(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    assignments = manifest["source_element_assignments"]
    assert isinstance(assignments, list)
    assignments.append(dict(assignments[0]))
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("assigned more than once" in error for error in report.source.errors)


def test_book_coverage_rejects_sparse_count_only_claim_coverage(tmp_path: Path) -> None:
    """Balanced claim counters cannot stand in for actual source-to-reader evidence."""

    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    assert isinstance(elements, list) and isinstance(assignments, list)
    elements.pop(0)
    assignments.pop(0)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "coverage.claims expected=1 covered=1" in error and "assignments=0" in error
        for error in report.source.errors
    )


def test_book_coverage_rejects_unassigned_front_or_back_matter(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    structures = manifest["source_structure_elements"]
    assert isinstance(structures, list)
    locator = "pdf pages 4-7 translator preface"
    digest = "8" * 64
    identity = json.dumps(
        {
            "kind": "front-matter",
            "source_locator": locator,
            "source_sha256": digest,
            "title": "3판 역자 서문",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    structures.insert(
        0,
        {
            "structure_id": f"structure:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}",
            "kind": "front-matter",
            "title": "3판 역자 서문",
            "source_locator": locator,
            "source_sha256": digest,
        },
    )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("source structure has no disposition" in error for error in report.source.errors)


def test_book_coverage_accepts_ordered_source_sections_as_in_page_h2(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf_id = "books/kotlin/chapter-01"
    leaf_path = vault / "wiki/books/kotlin/chapter-01.md"
    leaf_path.write_text(
        leaf_path.read_text(encoding="utf-8")
        + "\n## 1.1 첫 번째 절\n\n첫 번째 절의 원문 본문이다.\n"
        + "\n## 1.2 두 번째 절\n\n두 번째 절의 원문 본문이다.\n",
        encoding="utf-8",
    )
    structures = manifest["source_structure_elements"]
    assignments = manifest["source_structure_assignments"]
    assert isinstance(structures, list) and isinstance(assignments, list)
    for source_order, title in enumerate(("1.1 첫 번째 절", "1.2 두 번째 절"), start=1):
        structure = _source_structure(
            "section",
            title,
            f"pdf pages {source_order + 1}-{source_order + 2} section {source_order}",
        )
        structures.append(structure)
        assignments.append(
            {
                "structure_id": structure["structure_id"],
                "disposition": "in-page-h2",
                "owner_id": leaf_id,
                "heading": f"## {title}",
                "source_order": source_order,
            }
        )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete


def test_ordered_reader_owner_runs_allow_one_chapter_owner_to_resume() -> None:
    owner = "books/llm/chapter-03"
    metadata = {
        "navigation_groups": [
            {
                "label": "3.3 깊은 절",
                "children": [f"{owner}/3-3-1", f"{owner}/3-3-2"],
            }
        ],
        "ordered_reader_sections": [
            {"kind": "source-body", "label": "3.1 직접 절"},
            {"kind": "source-body", "label": "3.2 직접 절"},
            {"kind": "navigation-group", "label": "3.3 깊은 절"},
            {"kind": "source-body", "label": "3.7 요약"},
        ],
    }

    assert _ordered_reader_owner_runs(metadata, owner) == [
        owner,
        f"{owner}/3-3-1",
        f"{owner}/3-3-2",
        owner,
    ]

    invalid = json.loads(json.dumps(metadata))
    invalid["ordered_reader_sections"][2]["label"] = "3.4 다른 절"
    assert _ordered_reader_owner_runs(invalid, owner) is None


def test_source_structure_allows_exact_ordered_chapter_owner_resume(tmp_path: Path) -> None:
    owner = "books/llm/chapter-03"
    child = f"{owner}/3-3-1"
    structures = [
        _source_structure("chapter", "3장", "pages 1-20"),
        _source_structure("section", "3.1 직접 절", "pages 1-3"),
        _source_structure("subsection", "3.3.1 깊은 절", "pages 4-17"),
        _source_structure("section", "3.7 요약", "pages 18-20"),
    ]
    manifest = {
        "source_structure_inventory_evidence": {
            "locator": "evidence/chapter-03-structure.json",
            "sha256": "9" * 64,
            "verified_on": "2026-09-04",
        },
        "source_structure_elements": structures,
        "source_structure_assignments": [
            {
                "structure_id": structures[0]["structure_id"],
                "disposition": "canonical-node",
                "canonical_id": owner,
            },
            {
                "structure_id": structures[1]["structure_id"],
                "disposition": "in-page-h2",
                "owner_id": owner,
                "heading": "## 3.1 직접 절",
                "source_order": 1,
            },
            {
                "structure_id": structures[2]["structure_id"],
                "disposition": "canonical-node",
                "canonical_id": child,
            },
            {
                "structure_id": structures[3]["structure_id"],
                "disposition": "in-page-h2",
                "owner_id": owner,
                "heading": "## 3.7 요약",
                "source_order": 2,
            },
        ],
    }
    owner_metadata = {
        "title": "3장",
        "navigation_groups": [{"label": "3.3 깊은 절", "children": [child]}],
        "ordered_reader_sections": [
            {"kind": "source-body", "label": "3.1 직접 절"},
            {"kind": "navigation-group", "label": "3.3 깊은 절"},
            {"kind": "source-body", "label": "3.7 요약"},
        ],
    }
    pages = {
        owner: (
            tmp_path / "chapter-03.md",
            owner_metadata,
            "# 3장\n\n## 3.1 직접 절\n\n본문\n\n## 3.7 요약\n\n요약\n",
        ),
        child: (tmp_path / "3-3-1.md", {"title": "3.3.1 깊은 절"}, "# 깊은 절\n"),
    }
    errors: list[str] = []

    _audit_source_structure_contract(
        "book",
        manifest,
        {owner, child},
        {owner, child},
        [owner, child],
        pages,
        errors,
    )

    assert errors == []

    owner_metadata["ordered_reader_sections"] = [
        {"kind": "source-body", "label": "3.1 직접 절"},
        {"kind": "source-body", "label": "3.7 요약"},
        {"kind": "navigation-group", "label": "3.3 깊은 절"},
    ]
    stale_errors: list[str] = []
    _audit_source_structure_contract(
        "book",
        manifest,
        {owner, child},
        {owner, child},
        [owner, child],
        pages,
        stale_errors,
    )
    assert any("must be contiguous or match" in error for error in stale_errors)


def test_ordered_book_reader_ui_validates_interleaved_headings_and_links(
    tmp_path: Path,
) -> None:
    canonical_id = "books/llm/chapter-03"
    groups: list[object] = [
        {
            "label": "3.3 깊은 절",
            "children": [f"{canonical_id}/3-3-1", f"{canonical_id}/3-3-2"],
        }
    ]
    ordered: list[object] = [
        {"kind": "source-body", "label": "3.1 직접 절"},
        {"kind": "source-body", "label": "3.2 직접 절"},
        {"kind": "navigation-group", "label": "3.3 깊은 절"},
        {"kind": "source-body", "label": "3.7 요약"},
    ]
    body = (
        "# 3장\n\n"
        "## 3.1 직접 절\n\n첫째\n\n"
        "## 3.2 직접 절\n\n둘째\n\n"
        "<!-- woon-book-reader-navigation:start -->\n"
        "## 3.3 깊은 절\n"
        f"- [[wiki/{canonical_id}/3-3-1|3.3.1 첫째]]\n"
        f"- [[wiki/{canonical_id}/3-3-2|3.3.2 둘째]]\n"
        "<!-- woon-book-reader-navigation:end -->\n\n"
        "## 3.7 요약\n\n요약"
    )
    reader_body = "## 3.1 직접 절\n\n첫째\n\n## 3.2 직접 절\n\n둘째\n\n## 3.7 요약\n\n요약"
    pages = {
        f"{canonical_id}/3-3-1": (
            tmp_path / "3-3-1.md",
            {
                "title": "3.3.1 첫째",
                "parent": f"[[wiki/{canonical_id}|3장]]",
            },
            "",
        ),
        f"{canonical_id}/3-3-2": (
            tmp_path / "3-3-2.md",
            {
                "title": "3.3.2 둘째",
                "parent": f"[[wiki/{canonical_id}|3장]]",
            },
            "",
        ),
    }
    errors: list[str] = []

    _audit_ordered_book_reader_ui(
        canonical_id,
        tmp_path / "chapter-03.md",
        body,
        reader_body,
        groups,
        ordered,
        pages,
        errors,
    )

    assert errors == []

    stale_errors: list[str] = []
    _audit_ordered_book_reader_ui(
        canonical_id,
        tmp_path / "chapter-03.md",
        body.replace("## 3.3 깊은 절", "## 3.4 잘못된 절"),
        reader_body,
        groups,
        ordered,
        pages,
        stale_errors,
    )
    assert any("ordered reader H2 must occur exactly once" in error for error in stale_errors)
    assert any("navigation headings are stale" in error for error in stale_errors)


@pytest.mark.parametrize(
    ("headings", "expected_error"),
    [
        (
            "\n## 1.1 첫 번째 절\n\n본문\n\n## 1.1 첫 번째 절\n\n중복\n",
            "in-page H2 must occur exactly once",
        ),
        (
            "\n## 1.2 두 번째 절\n\n본문\n\n## 1.1 첫 번째 절\n\n본문\n",
            "in-page H2 order differs from source structure order",
        ),
    ],
)
def test_book_coverage_rejects_duplicate_or_reordered_in_page_h2(
    tmp_path: Path,
    headings: str,
    expected_error: str,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf_id = "books/kotlin/chapter-01"
    leaf_path = vault / "wiki/books/kotlin/chapter-01.md"
    leaf_path.write_text(leaf_path.read_text(encoding="utf-8") + headings, encoding="utf-8")
    structures = manifest["source_structure_elements"]
    assignments = manifest["source_structure_assignments"]
    assert isinstance(structures, list) and isinstance(assignments, list)
    for source_order, title in enumerate(("1.1 첫 번째 절", "1.2 두 번째 절"), start=1):
        structure = _source_structure(
            "section",
            title,
            f"pdf pages {source_order + 1}-{source_order + 2} section {source_order}",
        )
        structures.append(structure)
        assignments.append(
            {
                "structure_id": structure["structure_id"],
                "disposition": "in-page-h2",
                "owner_id": leaf_id,
                "heading": f"## {title}",
                "source_order": source_order,
            }
        )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(expected_error in error for error in report.source.errors)


def test_book_coverage_keeps_canonical_node_reuse_forbidden(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf_id = "books/kotlin/chapter-01"
    structure = _source_structure("section", "1.1 첫 번째 절", "pdf pages 2-3 section 1")
    structures = manifest["source_structure_elements"]
    assignments = manifest["source_structure_assignments"]
    assert isinstance(structures, list) and isinstance(assignments, list)
    structures.append(structure)
    assignments.append(
        {
            "structure_id": structure["structure_id"],
            "disposition": "canonical-node",
            "canonical_id": leaf_id,
        }
    )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "multiple structures reuse canonical node" in error
        for error in report.source.errors
    )


def test_book_coverage_rejects_node_order_that_differs_from_source_order(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    preface_id = "books/kotlin/preface"
    _page(
        vault / "wiki/books/kotlin/preface.md",
        canonical_id=preface_id,
        parent="books/kotlin",
    )
    structures = manifest["source_structure_elements"]
    structure_assignments = manifest["source_structure_assignments"]
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    nodes = manifest["nodes"]
    assert all(
        isinstance(value, list)
        for value in (structures, structure_assignments, elements, assignments, nodes)
    )
    structure = _structure_contract(
        [(preface_id, "front-matter", preface_id, "pdf pages 4-7 preface")]
    )
    structures.insert(0, structure["source_structure_elements"][0])
    structure_assignments.insert(0, structure["source_structure_assignments"][0])
    preface_claim = _source_element(
        "claim",
        "paragraph",
        "pdf page 4 preface paragraph 1",
        "6" * 64,
    )
    span = "자연스러운 한국어 학습 본문이다."
    elements.append(preface_claim)
    assignments.append(
        {
            "element_id": preface_claim["element_id"],
            "owner_id": preface_id,
            "delivery": "reader-span",
            "delivery_span": span,
            "delivery_span_sha256": hashlib.sha256(span.encode("utf-8")).hexdigest(),
        }
    )
    nodes.append(
        {
            "canonical_id": preface_id,
            "parent_id": "books/kotlin",
            "kind": "front-matter",
            "leaf": True,
            "has_direct_content": True,
            "source_locator": "pdf pages 4-7 preface",
            "state": "source-covered",
            "coverage": {
                "claims": {"expected": 1, "covered": 1},
                "examples": {"expected": 0, "covered": 0},
                "cautions": {"expected": 0, "covered": 0},
                "figures": {"expected": 0, "covered": 0},
                "code": {"expected": 0, "covered": 0},
            },
            "runnable": {"expected": 0, "verified": 0},
            "korean_prose_reviewed": True,
        }
    )
    manifest["toc_node_count"] = 2
    manifest["toc_leaf_count"] = 2
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "node order does not match source structure order" in error
        for error in report.source.errors
    )


def test_book_coverage_allows_only_exact_metadata_classification(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    structures = manifest["source_structure_elements"]
    assignments = manifest["source_structure_assignments"]
    assert isinstance(structures, list) and isinstance(assignments, list)
    locator = "pdf pages 1038-1055 index"
    digest = "7" * 64
    identity = json.dumps(
        {
            "kind": "index",
            "source_locator": locator,
            "source_sha256": digest,
            "title": "한글·영문 찾아보기",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    structure_id = f"structure:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
    structures.append(
        {
            "structure_id": structure_id,
            "kind": "index",
            "title": "한글·영문 찾아보기",
            "source_locator": locator,
            "source_sha256": digest,
        }
    )
    assignments.append(
        {
            "structure_id": structure_id,
            "disposition": "metadata-only",
            "metadata_field": "edition.index_locator",
            "reason": "검색용 색인이므로 원문 위치만 보존한다.",
        }
    )
    target.write_text(json.dumps(manifest), encoding="utf-8")

    accepted = audit_book_coverage(vault)
    assert accepted.complete

    structures[-1]["kind"] = "front-matter"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = audit_book_coverage(vault)
    assert rejected.complete is False
    assert any("meaningful front/back matter" in error for error in rejected.source.errors)


def test_book_coverage_rejects_missing_reader_delivery_span(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    assignments = manifest["source_element_assignments"]
    assert isinstance(assignments, list)
    missing = "원문 수치를 선언했지만 독자 본문에는 없는 합성 문장이다."
    assignments[0]["delivery_span"] = missing
    assignments[0]["delivery_span_sha256"] = hashlib.sha256(missing.encode("utf-8")).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("delivery_span must occur exactly once" in error for error in report.source.errors)


def test_book_coverage_rejects_reused_reader_delivery_span(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    nodes = manifest["nodes"]
    assert isinstance(elements, list) and isinstance(assignments, list) and isinstance(nodes, list)
    second = _source_element("claim", "table", "page 3 table 1", "4" * 64)
    elements.append(second)
    assignments.append(
        {
            "element_id": second["element_id"],
            "owner_id": "books/kotlin/chapter-01",
            "delivery": "reader-span",
            "delivery_span": assignments[0]["delivery_span"],
            "delivery_span_sha256": assignments[0]["delivery_span_sha256"],
        }
    )
    nodes[0]["coverage"]["claims"] = {"expected": 2, "covered": 2}
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("reuse the same reader delivery span" in error for error in report.source.errors)


def test_book_coverage_rejects_all_kind_count_mismatch(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    nodes = manifest["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["coverage"]["cautions"] = {"expected": 1, "covered": 1}
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "coverage.cautions expected=1 covered=1" in error and "assignments=0" in error
        for error in report.source.errors
    )


def test_book_coverage_verifies_figure_delivery_evidence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8") + "\n```mermaid\nflowchart LR\n  A --> B\n```\n",
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    nodes = manifest["nodes"]
    assert isinstance(elements, list) and isinstance(assignments, list) and isinstance(nodes, list)
    figure = _source_element("figure", "figure", "page 4 figure 1", "5" * 64)
    elements.append(figure)
    assignments.append(
        {
            "element_id": figure["element_id"],
            "owner_id": "books/kotlin/chapter-01",
            "delivery": "figure-mermaid",
            "mermaid_block_index": 1,
            "delivery_sha256": "0" * 64,
        }
    )
    nodes[0]["coverage"]["figures"] = {"expected": 1, "covered": 1}
    target.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = audit_book_coverage(vault)
    assert rejected.complete is False
    assert any("does not match the Mermaid block" in error for error in rejected.source.errors)

    assignments[-1]["delivery_sha256"] = hashlib.sha256(b"flowchart LR\n  A --> B\n").hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    accepted = audit_book_coverage(vault)
    assert accepted.complete


def test_book_coverage_rejects_label_only_figure_reader_span(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    label_only = "- 그림 3-3의 단어별 번역 실패."
    leaf.write_text(
        leaf.read_text(encoding="utf-8") + f"\n{label_only}\n",
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    nodes = manifest["nodes"]
    assert isinstance(elements, list) and isinstance(assignments, list) and isinstance(nodes, list)
    figure = _source_element("figure", "figure", "page 4 figure 1", "5" * 64)
    elements.append(figure)
    assignments.append(
        {
            "element_id": figure["element_id"],
            "owner_id": "books/kotlin/chapter-01",
            "delivery": "reader-span",
            "delivery_span": label_only,
            "delivery_span_sha256": hashlib.sha256(label_only.encode("utf-8")).hexdigest(),
        }
    )
    nodes[0]["coverage"]["figures"] = {"expected": 1, "covered": 1}
    target.write_text(json.dumps(manifest), encoding="utf-8")

    rejected = audit_book_coverage(vault)
    assert rejected.complete is False
    assert any(
        "short label-only sentence is not delivery evidence" in error
        for error in rejected.source.errors
    )

    explanation = (
        "그림 3-3은 독일어와 영어의 어순이 달라 단어를 같은 위치에서 일대일로 "
        "치환하면 문장의 문법과 의미가 무너지는 관계를 보여 준다."
    )
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace(label_only, explanation),
        encoding="utf-8",
    )
    assignments[-1]["delivery_span"] = explanation
    assignments[-1]["delivery_span_sha256"] = hashlib.sha256(
        explanation.encode("utf-8")
    ).hexdigest()
    target.write_text(json.dumps(manifest), encoding="utf-8")

    accepted = audit_book_coverage(vault)
    assert accepted.complete


def test_book_coverage_rejects_static_delivery_for_runnable_supported_source(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    assignments = manifest["source_element_assignments"]
    assert isinstance(assignments, list)
    assignments[1] = {
        "element_id": assignments[1]["element_id"],
        "owner_id": "books/kotlin/chapter-01",
        "delivery": "static-exception",
        "static_language": "kotlin",
        "static_block_index": 1,
        "exception_reason": "잘못된 예외 선언",
        "original_test_evidence": "evidence/original-test.json",
        "original_test_sha256": "d" * 64,
    }
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("requires delivery=run-block" in error for error in report.runnable.errors)


def test_book_coverage_accepts_pinned_static_exception_and_rejects_missing_evidence(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace(
            "fun main() = println(1)",
            "fun externalCall(value: Int) = value * 2\nfun main() = println(externalCall(42))",
        )
        + "\n```kotlin\nexternalCall(42)\n```\n"
        + "\n원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
        + "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다.\n",
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    assert isinstance(elements, list) and isinstance(assignments, list)
    elements[1]["runnable_support"] = "static-exception"
    assignments[1] = {
        "element_id": elements[1]["element_id"],
        "owner_id": "books/kotlin/chapter-01",
        "delivery": "static-exception",
        "static_language": "kotlin",
        "static_block_index": 1,
        "exception_reason": "외부 SDK와 실제 자격 증명이 필요한 원문 조각이다.",
        "original_test_evidence": "repo://learning/lrn-kotlin#external-fixture",
        "original_test_sha256": "d" * 64,
        "harness_run_language": "run-kotlin",
        "harness_block_index": 1,
        "harness_fidelity_span": (
            "원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
            "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다."
        ),
        "harness_fidelity_span_sha256": hashlib.sha256(
            (
                "원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
                "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다."
            ).encode()
        ).hexdigest(),
        "harness_verification_evidence": "evidence/kotlin-harness-1.json",
        "harness_verification_sha256": "e" * 64,
    }
    target.write_text(json.dumps(manifest), encoding="utf-8")

    accepted = audit_book_coverage(vault)
    assert accepted.complete

    assignments[1].pop("original_test_evidence")
    target.write_text(json.dumps(manifest), encoding="utf-8")
    rejected = audit_book_coverage(vault)

    assert rejected.complete is False
    assert any("original_test_evidence is required" in error for error in rejected.runnable.errors)


def test_book_coverage_accepts_source_pinned_static_code_without_synthetic_harness(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    static_body = "externalCall(42)\n"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace(
            "```run-kotlin\nfun main() = println(2)\n```",
            f"```kotlin\n{static_body}```",
        ),
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    nodes = manifest["nodes"]
    assert isinstance(elements, list) and isinstance(assignments, list)
    assert isinstance(nodes, list) and isinstance(nodes[0], dict)
    assert isinstance(elements[2], dict) and isinstance(assignments[2], dict)
    elements[2]["runnable_support"] = "static-exception"
    assignments[2] = {
        "element_id": elements[2]["element_id"],
        "owner_id": "books/kotlin/chapter-01",
        "delivery": "static-exception",
        "static_language": "kotlin",
        "static_block_index": 1,
        "static_body_sha256": hashlib.sha256(static_body.encode("utf-8")).hexdigest(),
        "exception_reason_code": "dependency",
        "runnable_required": False,
        "source_locator": elements[2]["source_locator"],
        "source_sha256": elements[2]["source_sha256"],
        "original_test_evidence": "evidence/source-code-inventory.json",
        "original_test_sha256": "d" * 64,
    }
    nodes[0]["runnable"] = {"expected": 1, "verified": 1}
    target.write_text(json.dumps(manifest), encoding="utf-8")

    accepted = audit_book_coverage(vault)
    assert accepted.complete
    assert "synthetic harness" not in leaf.read_text(encoding="utf-8")

    assignments[2]["exception_reason_code"] = "convenience"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    invalid_reason = audit_book_coverage(vault)
    assert any(
        "exception_reason_code must be one of" in error
        for error in invalid_reason.errors
    )

    assignments[2]["exception_reason_code"] = "dependency"
    assignments[2]["runnable_required"] = True
    target.write_text(json.dumps(manifest), encoding="utf-8")
    runnable_required = audit_book_coverage(vault)
    assert any(
        "runnable_required must be false" in error
        for error in runnable_required.errors
    )

    assignments[2]["runnable_required"] = False
    assignments[2]["static_body_sha256"] = "0" * 64
    target.write_text(json.dumps(manifest), encoding="utf-8")
    stale_body = audit_book_coverage(vault)
    assert any(
        "static_body_sha256 does not match" in error for error in stale_body.errors
    )


def test_source_landed_english_harness_fidelity_span_uses_source_language() -> None:
    span = (
        "The harness preserves the source input, performs the same token transformation, "
        "and prints the observable result for comparison."
    )
    errors: list[str] = []

    _audit_static_harness_fidelity(
        "source-landed",
        {
            "harness_fidelity_span": span,
            "harness_fidelity_span_sha256": hashlib.sha256(span.encode()).hexdigest(),
        },
        span,
        "transform(tokens)",
        "print(transform(tokens))",
        errors,
        manifest_schema=3,
        workflow_phase="source-landed",
        reader_language="en",
    )

    assert errors == []


def test_translated_harness_fidelity_span_requires_korean() -> None:
    english = (
        "The harness preserves the source input, performs the same token transformation, "
        "and prints the observable result for comparison."
    )
    korean = (
        "이 실행 코드는 원문의 토큰 입력을 보존하고 같은 변환을 수행한 뒤 관찰 가능한 "
        "결과를 출력하여 원문 동작과 직접 비교할 수 있게 한다."
    )
    english_errors: list[str] = []
    korean_errors: list[str] = []
    common = ("transform(tokens)", "print(transform(tokens))")

    _audit_static_harness_fidelity(
        "translated-English",
        {
            "harness_fidelity_span": english,
            "harness_fidelity_span_sha256": hashlib.sha256(english.encode()).hexdigest(),
        },
        english,
        *common,
        english_errors,
        manifest_schema=3,
        workflow_phase="translated",
        reader_language="ko",
    )
    _audit_static_harness_fidelity(
        "translated-Korean",
        {
            "harness_fidelity_span": korean,
            "harness_fidelity_span_sha256": hashlib.sha256(korean.encode()).hexdigest(),
        },
        korean,
        *common,
        korean_errors,
        manifest_schema=3,
        workflow_phase="translated",
        reader_language="ko",
    )

    assert any("must use substantive Korean prose" in error for error in english_errors)
    assert korean_errors == []


def test_legacy_schema_two_keeps_korean_harness_fidelity_gate() -> None:
    span = (
        "The harness preserves the source input, performs the same token transformation, "
        "and prints the observable result for comparison."
    )
    errors: list[str] = []

    _audit_static_harness_fidelity(
        "legacy",
        {
            "harness_fidelity_span": span,
            "harness_fidelity_span_sha256": hashlib.sha256(span.encode()).hexdigest(),
        },
        span,
        "transform(tokens)",
        "print(transform(tokens))",
        errors,
        manifest_schema=2,
        workflow_phase="",
        reader_language="",
    )

    assert any("must use substantive Korean prose" in error for error in errors)


def test_book_coverage_rejects_static_exception_without_same_leaf_harness(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8")
        + "\n```kotlin\nexternalCall(42)\n```\n"
        + "\n원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
        + "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다.\n",
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    assert isinstance(elements, list) and isinstance(assignments, list)
    elements[1]["runnable_support"] = "static-exception"
    assignments[1] = {
        "element_id": elements[1]["element_id"],
        "owner_id": "books/kotlin/chapter-01",
        "delivery": "static-exception",
        "static_language": "kotlin",
        "static_block_index": 1,
        "exception_reason": "외부 SDK와 실제 자격 증명이 필요한 원문 조각이다.",
        "original_test_evidence": "repo://learning/lrn-kotlin#external-fixture",
        "original_test_sha256": "d" * 64,
        "harness_run_language": "run-kotlin",
        "harness_block_index": 3,
        "harness_fidelity_span": (
            "원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
            "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다."
        ),
        "harness_fidelity_span_sha256": hashlib.sha256(
            (
                "원문 예제의 externalCall 입력 42와 호출 결과를 같은 절의 실행 harness에서 "
                "보존하여 외부 SDK 없이도 관찰 가능한 동작을 확인한다."
            ).encode()
        ).hexdigest(),
        "harness_verification_evidence": "evidence/kotlin-harness-3.json",
        "harness_verification_sha256": "e" * 64,
    }
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "same-leaf runnable harness does not exist" in error for error in report.runnable.errors
    )


def test_book_coverage_rejects_unrelated_toy_harness_for_static_source_code(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    fidelity = (
        "원문 PyTorch의 queries, keys, values 계산과 문맥 벡터 결과를 보존한다고 설명하지만 "
        "아래 작은 softmax 예제는 실제 원문 연산을 재현하지 않는다."
    )
    leaf.write_text(
        leaf.read_text(encoding="utf-8")
        + "\n```python\nqueries = inputs @ W_query\nkeys = inputs @ W_key\n"
        + "values = inputs @ W_value\ncontext = weights @ values\n```\n"
        + f"\n{fidelity}\n"
        + "\n```run-python\nimport math\nscores = [1.0, 2.0, 3.0]\n"
        + "print(sum(math.exp(x) for x in scores))\n```\n",
        encoding="utf-8",
    )
    elements = manifest["source_elements"]
    assignments = manifest["source_element_assignments"]
    assert isinstance(elements, list) and isinstance(assignments, list)
    elements[1]["runnable_support"] = "static-exception"
    assignments[1] = {
        "element_id": elements[1]["element_id"],
        "owner_id": "books/kotlin/chapter-01",
        "delivery": "static-exception",
        "static_language": "python",
        "static_block_index": 1,
        "exception_reason": "plugin runtime에는 PyTorch가 없다.",
        "original_test_evidence": "evidence/original-pytorch.json",
        "original_test_sha256": "d" * 64,
        "harness_run_language": "run-python",
        "harness_block_index": 1,
        "harness_fidelity_span": fidelity,
        "harness_fidelity_span_sha256": hashlib.sha256(fidelity.encode("utf-8")).hexdigest(),
        "harness_verification_evidence": "evidence/toy-softmax.json",
        "harness_verification_sha256": "e" * 64,
    }
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("harness is not source-faithful" in error for error in report.runnable.errors)


def test_book_coverage_rejects_two_source_elements_sharing_one_run_block(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    target, manifest = _verified_fixture(vault)
    assignments = manifest["source_element_assignments"]
    assert isinstance(assignments, list)
    assignments[2]["run_block_index"] = 1
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "multiple runnable source elements reuse the same reader delivery span" in error
        for error in report.runnable.errors
    )


def test_book_coverage_rejects_comment_only_run_block(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8").replace(
            "fun main() = println(1)",
            "// Code E-1: download, split, and save the dataset",
        ),
        encoding="utf-8",
    )

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("comment-only placeholder" in error for error in report.runnable.errors)


def test_book_coverage_rejects_untracked_runnable_block(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)
    _page(
        vault / "wiki/books/kotlin/chapter-01.md",
        canonical_id="books/kotlin/chapter-01",
        parent="books/kotlin",
    )
    chapter = vault / "wiki/books/kotlin/chapter-01.md"
    chapter.write_text(
        chapter.read_text(encoding="utf-8") + "\n```run-kotlin\nfun main() = println(1)\n```\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "book_id": "books/kotlin",
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "publisher.example/toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
        **_structure_contract(
            [
                (
                    "books/kotlin/chapter-01",
                    "chapter",
                    "books/kotlin/chapter-01",
                    "pages 1-10",
                )
            ]
        ),
        **_claim_element_contract("books/kotlin/chapter-01"),
        "nodes": [
            {
                "canonical_id": "books/kotlin/chapter-01",
                "parent_id": "books/kotlin",
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "pages 1-10",
                "state": "source-covered",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 0, "covered": 0},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 0, "covered": 0},
                },
                "runnable": {"expected": 0, "verified": 0},
                "korean_prose_reviewed": True,
            }
        ],
    }
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("reader run-* blocks=1" in error for error in report.errors)


def test_book_coverage_rejects_missing_manifest_and_shell(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert report.errors == ("books/kotlin: book coverage manifest is missing",)


def test_book_coverage_reports_rights_block_without_false_missing_manifest(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)
    intake = vault / "catalog/book-intake/official-books.json"
    intake.parent.mkdir(parents=True)
    intake.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "bundles": [
                    {
                        "id": "kotlin",
                        "kind": "book",
                        "target": "books/kotlin",
                        "rights_status": "unverified-commercial",
                        "processing_state": "blocked-rights",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = audit_book_coverage(vault)

    assert report.complete
    assert report.blocked_book_count == 1
    assert report.blocked_books == ("books/kotlin",)


def test_book_coverage_rejects_workflow_prose_in_reader_body(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    _page(leaf, canonical_id="books/kotlin/chapter-01", parent="books/kotlin")
    leaf.write_text(
        leaf.read_text(encoding="utf-8")
        + "\n## 근거와 학습 상태\n\n"
        + "- 학습 상태: 이 페이지가 존재한다는 사실은 숙달 증거가 아니다.\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "book_id": "books/kotlin",
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "publisher.example/toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 1,
        "toc_leaf_count": 1,
        **_structure_contract(
            [
                (
                    "books/kotlin/chapter-01",
                    "chapter",
                    "books/kotlin/chapter-01",
                    "pages 1-10",
                )
            ]
        ),
        **_claim_element_contract("books/kotlin/chapter-01"),
        "nodes": [
            {
                "canonical_id": "books/kotlin/chapter-01",
                "parent_id": "books/kotlin",
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "pages 1-10",
                "state": "source-covered",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 0, "covered": 0},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 0, "covered": 0},
                },
                "runnable": {"expected": 0, "verified": 0},
                "korean_prose_reviewed": True,
            }
        ],
    }
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any("workflow or completion metadata" in error for error in report.errors)


@pytest.mark.parametrize(
    "workflow_body",
    (
        "## 이전과 다음\n\n- 다음: [[wiki/books/kotlin/chapter-02]]\n",
        "## 자료를 닫고 답하기\n\n1. 핵심은 무엇인가?\n",
        "검증 상태: 실제 compile·run 결과\n```text\nok\n```\n",
        "This runnable harness preserves the source example's observable outcome.\n",
        "Chapter 18 source code 23 preserves virtual time without real waiting.\n",
        '```run-kotlin\nfun main() { println("compiled") }\n```\n',
        "```run-kotlin\nclass VirtualTestScope { var currentTime = 0L }\n```\n",
    ),
)
def test_book_coverage_rejects_generated_learning_workflow_sections(
    tmp_path: Path,
    workflow_body: str,
) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8") + "\n" + workflow_body,
        encoding="utf-8",
    )

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "reader body contains generated learning workflow prose" in error
        for error in report.errors
    )


def test_book_coverage_requires_coverage_for_non_leaf_direct_content(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _page(vault / "wiki/books/kotlin.md", canonical_id="books/kotlin", parent=None, book=True)
    _page(
        vault / "wiki/books/kotlin/part-01.md",
        canonical_id="books/kotlin/part-01",
        parent="books/kotlin",
    )
    _page(
        vault / "wiki/books/kotlin/chapter-01.md",
        canonical_id="books/kotlin/chapter-01",
        parent="books/kotlin/part-01",
    )
    manifest = {
        "schema_version": 2,
        "book_id": "books/kotlin",
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "publisher.example/toc", "verified_on": "2026-09-01"}],
        "toc_node_count": 2,
        "toc_leaf_count": 1,
        **_structure_contract(
            [
                (
                    "books/kotlin/part-01",
                    "part",
                    "books/kotlin/part-01",
                    "page 1",
                ),
                (
                    "books/kotlin/chapter-01",
                    "chapter",
                    "books/kotlin/chapter-01",
                    "pages 2-10",
                ),
            ]
        ),
        **_claim_element_contract("books/kotlin/chapter-01"),
        "nodes": [
            {
                "canonical_id": "books/kotlin/part-01",
                "parent_id": "books/kotlin",
                "kind": "part",
                "leaf": False,
                "has_direct_content": True,
                "source_locator": "page 1",
                "state": "drafted",
            },
            {
                "canonical_id": "books/kotlin/chapter-01",
                "parent_id": "books/kotlin/part-01",
                "kind": "chapter",
                "leaf": True,
                "has_direct_content": True,
                "source_locator": "pages 2-10",
                "state": "source-covered",
                "coverage": {
                    "claims": {"expected": 1, "covered": 1},
                    "examples": {"expected": 0, "covered": 0},
                    "cautions": {"expected": 0, "covered": 0},
                    "figures": {"expected": 0, "covered": 0},
                    "code": {"expected": 0, "covered": 0},
                },
                "runnable": {"expected": 0, "verified": 0},
                "korean_prose_reviewed": True,
            },
        ],
    }
    target = vault / "catalog/book-coverage/kotlin.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "nodes[0].coverage is required for direct source content" in error
        for error in report.errors
    )


def test_book_coverage_rejects_generated_semantic_ledger_and_broken_korean(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _verified_fixture(vault)
    leaf = vault / "wiki/books/kotlin/chapter-01.md"
    leaf.write_text(
        leaf.read_text(encoding="utf-8")
        + "\nclaim semantic unit 5의 claim 5은 앞의 개념을 연결한다.\n"
        + "이 예제는 입력 길이를 줄인다이다.\n",
        encoding="utf-8",
    )

    report = audit_book_coverage(vault)

    assert report.complete is False
    assert any(
        "reader body contains workflow or completion metadata" in error
        for error in report.quality.errors
    )
