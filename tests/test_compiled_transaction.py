from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import woon_core.cli as cli
from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    CompiledWiki,
    CompiledWikiSettings,
    CompiledWikiTransaction,
    CompiledWikiTransactionReport,
    LegacyPageAdoption,
    VerifiedBookPage,
    _materialize_book_coverage_scopes,
    _normalize,
    _validate_book_workflow_progression,
)
from woon_core.knowledge.service import KnowledgeService


def _settings(vault: Path) -> CompiledWikiSettings:
    catalog = vault / "catalog/llm-wiki"
    return CompiledWikiSettings(
        vault=vault,
        output_root=vault / "wiki",
        sources_path=catalog / "sources.yaml",
        claims_path=catalog / "claims.yaml",
        pages_path=catalog / "pages.yaml",
        curation_path=catalog / "curation.yaml",
        relations_path=catalog / "relations.yaml",
        receipts_path=catalog / "receipts.yaml",
        review_queue_path=catalog / "review-queue.yaml",
    )


def _write_seed(vault: Path) -> None:
    path = vault / "wiki/seed.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "type: Wiki\n"
        "canonical_id: seed\n"
        "title: 시드\n"
        "domain: seed\n"
        "summary: transaction test seed.\n"
        "status: Canonical\n"
        "publish: false\n"
        "access: local-only\n"
        "difficulty: foundation\n"
        "prerequisites: []\n"
        "next_concepts: []\n"
        "related: []\n"
        "source_ids: []\n"
        "---\n\n"
        "# 시드\n\n"
        "transaction catalog를 초기화한다.\n",
        encoding="utf-8",
    )


def _service(vault: Path) -> tuple[CompiledWiki, KnowledgeService]:
    _write_seed(vault)
    compiler = CompiledWiki(_settings(vault))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        SQLiteFtsSearchIndex(vault / ".local/search.sqlite3"),
        GitKnowledgeHistory(vault),
        compiled_wiki=compiler,
    )
    service.reindex()
    return compiler, service


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workflow_manifest(phase: str) -> dict[str, object]:
    phases = (
        "source-landed",
        "translated",
        "concept-linked",
        "understanding-enriched",
    )
    rank = phases.index(phase)
    return {
        "schema_version": 3,
        "workflow_phase": phase,
        "translation_required": True,
        "phase_evidence": {reached: {"proof": reached} for reached in phases[: rank + 1]},
        "source_structure_elements": [{"structure_id": "structure:one"}],
        "source_elements": [{"element_id": "claim:one"}],
        "source_element_assignments": [
            {
                "element_id": "claim:one",
                "owner_id": "books/kotlin/chapter-01",
                "delivery": "reader-span",
            }
        ],
    }


def _source_landed_extension_fixture() -> tuple[dict[str, object], dict[str, object]]:
    current = _workflow_manifest("source-landed")
    current.update(
        {
            "book_id": "books/example",
            "edition": {"label": "2nd", "source_sha256": "a" * 64},
            "source_archive": {
                "relative_path": "wiki/private/_sources/book.epub",
                "sha256": "a" * 64,
                "privacy": "local-only",
            },
            "nodes": [
                {"canonical_id": "books/example/chapter-01", "title": "Chapter 1"},
                {"canonical_id": "books/example/chapter-03", "title": "Chapter 3"},
            ],
            "source_structure_elements": [
                {"structure_id": "structure:one", "title": "Chapter 1"},
                {"structure_id": "structure:three", "title": "Chapter 3"},
            ],
            "source_structure_assignments": [
                {
                    "structure_id": "structure:one",
                    "canonical_id": "books/example/chapter-01",
                },
                {
                    "structure_id": "structure:three",
                    "canonical_id": "books/example/chapter-03",
                },
            ],
            "source_elements": [
                {"element_id": "claim:one", "source_sha256": "1" * 64},
                {"element_id": "claim:three", "source_sha256": "3" * 64},
            ],
            "source_element_assignments": [
                {
                    "element_id": "claim:one",
                    "owner_id": "books/example/chapter-01",
                    "delivery": "reader-span",
                },
                {
                    "element_id": "claim:three",
                    "owner_id": "books/example/chapter-03",
                    "delivery": "reader-span",
                },
            ],
            "source_asset_inventory": [
                {"asset_id": "asset:one", "source_sha256": "4" * 64},
                {"asset_id": "asset:three", "source_sha256": "6" * 64},
            ],
        }
    )
    inventory = current["source_asset_inventory"]
    assert isinstance(inventory, list)
    current["source_asset_inventory_evidence"] = {
        "expected_asset_count": len(inventory),
        "inventory_sha256": hashlib.sha256(
            json.dumps(
                inventory,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }

    replacement = copy.deepcopy(current)
    insertions = {
        "nodes": {"canonical_id": "books/example/chapter-02", "title": "Chapter 2"},
        "source_structure_elements": {
            "structure_id": "structure:two",
            "title": "Chapter 2",
        },
        "source_structure_assignments": {
            "structure_id": "structure:two",
            "canonical_id": "books/example/chapter-02",
        },
        "source_elements": {"element_id": "claim:two", "source_sha256": "2" * 64},
        "source_element_assignments": {
            "element_id": "claim:two",
            "owner_id": "books/example/chapter-02",
            "delivery": "reader-span",
        },
        "source_asset_inventory": {
            "asset_id": "asset:two",
            "source_sha256": "5" * 64,
        },
    }
    for field, insertion in insertions.items():
        values = replacement[field]
        assert isinstance(values, list)
        values.insert(1, insertion)
    replacement_inventory = replacement["source_asset_inventory"]
    assert isinstance(replacement_inventory, list)
    replacement["source_asset_inventory_evidence"] = {
        "expected_asset_count": len(replacement_inventory),
        "inventory_sha256": hashlib.sha256(
            json.dumps(
                replacement_inventory,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    }
    return current, replacement


def _transaction(count: int = 1) -> CompiledWikiTransaction:
    sources: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    pages: list[dict[str, object]] = []
    curations: list[dict[str, object]] = []
    expected: dict[str, str | None] = {}
    for index in range(count):
        page_id = f"concepts/reference-{index:02d}"
        body = f"공식 근거 {index}를 적용 판단과 함께 설명한다.\n"
        body_hash = _digest(body)
        source_id = f"source://transaction/reference-{index:02d}/{body_hash[:24]}"
        claim_id = f"claim://transaction/reference-{index:02d}/{body_hash[:24]}"
        sources.append(
            {
                "source_id": source_id,
                "kind": "official-reference",
                "locator": f"https://example.com/reference-{index:02d}",
                "original_sha256": body_hash,
                "normalized_sha256": _digest(_normalize(body)),
                "privacy": "public",
                "lifecycle": "compiled",
                "title": f"공식 근거 {index}",
                "purpose": "개발 판단의 공식 근거를 확인한다.",
                "body": body,
            }
        )
        claims.append(
            {
                "claim_id": claim_id,
                "kind": "reference-evidence",
                "status": "accepted",
                "statement": f"공식 근거 {index}를 적용한다.",
                "source_ids": [source_id],
                "markdown": "",
            }
        )
        pages.append(
            {
                "page_id": page_id,
                "output_path": f"{page_id}.md",
                "title": f"개발 참고 {index}",
                "frontmatter": {
                    "type": "Wiki",
                    "canonical_id": page_id,
                    "title": f"개발 참고 {index}",
                    "domain": "concepts",
                    "summary": "공식 개발 근거를 설명한다.",
                    "status": "Canonical",
                    "publish": False,
                    "access": "public",
                    "difficulty": "foundation",
                    "prerequisites": [],
                    "next_concepts": [],
                    "related": [],
                    "source_ids": [],
                },
                "source_ids": [source_id],
                "claim_ids": [claim_id],
                "render": {"kind": "source-body", "source_id": source_id},
            }
        )
        curations.append(
            {
                "page_id": page_id,
                "current_use": "개발 판단 전에 공식 근거를 다시 확인한다.",
                "basis": "curated-revision",
                "status": "confirmed",
            }
        )
        expected[page_id] = None
    return CompiledWikiTransaction(
        expected_revisions=expected,
        sources_upsert=tuple(sources),
        claims_upsert=tuple(claims),
        pages_upsert=tuple(pages),
        curations_upsert=tuple(curations),
    )


def _existing_seed_transaction(
    compiler: CompiledWiki, expected_revision: str
) -> CompiledWikiTransaction:
    _, _, pages, curations, _ = compiler._load_inputs()
    return CompiledWikiTransaction(
        expected_revisions={"seed": expected_revision},
        sources_upsert=(),
        claims_upsert=(),
        pages_upsert=(copy.deepcopy(pages["seed"]),),
        curations_upsert=(copy.deepcopy(curations["seed"]),),
    )


def test_legacy_page_adoption_preflight_preserves_existing_raw_page(tmp_path: Path) -> None:
    compiler, _ = _service(tmp_path)
    raw_path = tmp_path / "wiki/legacy.md"
    raw = (
        "---\n"
        "type: Wiki\ncanonical_id: legacy\ntitle: 레거시\ndomain: personal\n"
        "summary: 기존 정본이다.\nstatus: Canonical\npublish: false\naccess: local-only\n"
        "difficulty: foundation\nprerequisites: []\nnext_concepts: []\n"
        "related: []\nsource_ids: []\n"
        "---\n\n# 레거시\n\n원문 본문을 보존한다.\n"
    )
    raw_path.write_text(raw, encoding="utf-8")
    body = "원문 본문을 보존한다.\n"
    raw_sha = _digest(raw)
    source_id = "source://legacy-wiki/wiki/legacy.md"
    transaction = CompiledWikiTransaction(
        expected_revisions={"legacy": None},
        sources_upsert=(
            {
                "source_id": source_id,
                "kind": "legacy-wiki",
                "locator": "wiki/private/_sources/legacy/legacy.md",
                "original_sha256": raw_sha,
                "normalized_sha256": _digest(_normalize(body)),
                "privacy": "local-only",
                "lifecycle": "compiled",
                "title": "레거시",
                "body": body,
            },
        ),
        claims_upsert=(
            {
                "claim_id": "claim://legacy-wiki/legacy",
                "kind": "legacy-document",
                "status": "accepted",
                "statement": "레거시",
                "source_ids": [source_id],
                "markdown": "",
            },
        ),
        pages_upsert=(
            {
                "page_id": "legacy",
                "output_path": "legacy.md",
                "title": "레거시",
                "frontmatter": {
                    "type": "Wiki",
                    "canonical_id": "legacy",
                    "title": "레거시",
                    "domain": "personal",
                    "summary": "기존 정본이다.",
                    "status": "Canonical",
                    "publish": False,
                    "access": "local-only",
                    "difficulty": "foundation",
                    "prerequisites": [],
                    "next_concepts": [],
                    "related": [],
                    "source_ids": [],
                },
                "source_ids": [source_id],
                "claim_ids": ["claim://legacy-wiki/legacy"],
                "render": {"kind": "source-body", "source_id": source_id},
                "legacy_output_adoption": True,
            },
        ),
        curations_upsert=(
            {
                "page_id": "legacy",
                "current_use": "기존 정본을 다시 찾는다.",
                "basis": "legacy-page-metadata",
                "status": "provisional",
            },
        ),
    )
    assert compiler.preflight_legacy_page_adoptions(
        transaction,
        (
            LegacyPageAdoption(
                "legacy", "legacy.md", raw_sha, "wiki/private/_sources/legacy/legacy.md"
            ),
        ),
    ) == ("legacy",)
    assert raw_path.read_text(encoding="utf-8") == raw


def test_legacy_page_adoption_preflight_rejects_changed_raw_hash(tmp_path: Path) -> None:
    compiler, _ = _service(tmp_path)
    with pytest.raises(WoonError, match="opt in explicitly"):
        compiler.preflight_legacy_page_adoptions(
            _transaction(),
            (
                LegacyPageAdoption(
                    "concepts/reference-00",
                    "concepts/reference-00.md",
                    "0" * 64,
                    "wiki/private/_sources/legacy/missing.md",
                ),
            ),
        )


def _legacy_adoption_transaction(
    tmp_path: Path, *, invalid_curation: bool = False
) -> tuple[CompiledWikiTransaction, LegacyPageAdoption, str]:
    raw_path = tmp_path / "wiki/legacy.md"
    raw = (
        "---\n"
        "type: Wiki\ncanonical_id: legacy\ntitle: 레거시\ndomain: personal\n"
        "summary: 기존 정본이다.\nstatus: Canonical\npublish: false\n"
        "access: local-only\ndifficulty: foundation\nprerequisites: []\n"
        "next_concepts: []\nrelated: []\nsource_ids: []\nnode_kind: detail\n"
        "view_mode: article\nparent: '[[wiki/seed|시드]]'\nsequence: 1\n"
        "updated: '2026-09-04'\n"
        "---\n\n# 레거시\n\n원문 본문을 보존한다.\n"
    )
    raw_path.write_text(raw, encoding="utf-8")
    raw_sha, body = _digest(raw), "원문 본문을 보존한다.\n"
    source_id, claim_id = "source://legacy-wiki/wiki/legacy.md", "claim://legacy-wiki/legacy"
    archive = "wiki/private/_sources/legacy/legacy.md"
    frontmatter = yaml.safe_load(raw.split("---\n", 3)[1])
    transaction = CompiledWikiTransaction(
        expected_revisions={"legacy": None},
        sources_upsert=(
            {
                "source_id": source_id,
                "kind": "legacy-wiki",
                "locator": archive,
                "original_sha256": raw_sha,
                "normalized_sha256": _digest(_normalize(body)),
                "privacy": "local-only",
                "lifecycle": "compiled",
                "title": "레거시",
                "body": body,
            },
        ),
        claims_upsert=(
            {
                "claim_id": claim_id,
                "kind": "legacy-document",
                "status": "accepted",
                "statement": "레거시",
                "source_ids": [source_id],
                "markdown": "",
            },
        ),
        pages_upsert=(
            {
                "page_id": "legacy",
                "output_path": "legacy.md",
                "title": "레거시",
                "frontmatter": frontmatter,
                "source_ids": [source_id],
                "claim_ids": [claim_id],
                "render": {"kind": "source-body", "source_id": source_id},
                "legacy_output_adoption": True,
            },
        ),
        curations_upsert=(
            {
                "page_id": "legacy",
                "current_use": "" if invalid_curation else "기존 정본을 다시 찾는다.",
                "basis": "legacy-page-metadata",
                "status": "provisional",
            },
        ),
    )
    return transaction, LegacyPageAdoption("legacy", "legacy.md", raw_sha, archive), raw


def test_legacy_page_adoption_applies_and_archives_exact_raw_bytes(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    transaction, adoption, raw = _legacy_adoption_transaction(tmp_path)
    report = service.apply_legacy_page_adoptions(transaction, (adoption,))
    assert report.page_ids == ("legacy",)
    assert (tmp_path / adoption.archive_path).read_text(encoding="utf-8") == raw
    assert "llm_wiki:" in (tmp_path / "wiki/legacy.md").read_text(encoding="utf-8")
    assert compiler.audit().complete


def test_legacy_page_adoption_restores_raw_bytes_after_compile_failure(tmp_path: Path) -> None:
    _, service = _service(tmp_path)
    transaction, adoption, raw = _legacy_adoption_transaction(tmp_path, invalid_curation=True)
    with pytest.raises(WoonError):
        service.apply_legacy_page_adoptions(transaction, (adoption,))
    assert (tmp_path / "wiki/legacy.md").read_text(encoding="utf-8") == raw
    assert not (tmp_path / adoption.archive_path).exists()


def test_book_workflow_progression_starts_at_source_landed() -> None:
    with pytest.raises(WoonError, match="must begin at workflow_phase=source-landed"):
        _validate_book_workflow_progression(
            None,
            _workflow_manifest("translated"),
            "book coverage manifest",
        )


def test_book_workflow_progression_allows_independent_toc_index_before_source_landing() -> None:
    toc_indexed: dict[str, object] = {
        "schema_version": 3,
        "workflow_phase": "toc-indexed",
        "translation_required": True,
        "edition": {"label": "2판", "source_sha256": "a" * 64},
        "toc_evidence": [{"locator": "printed TOC", "verified_on": "2026-09-04"}],
        "phase_evidence": {"toc-indexed": {"locator": "evidence/toc.json", "sha256": "b" * 64}},
        "source_structure_inventory_evidence": {
            "locator": "evidence/structure.json",
            "sha256": "c" * 64,
            "verified_on": "2026-09-04",
        },
        "source_structure_elements": [{"structure_id": "structure:one"}],
    }
    source_landed = _workflow_manifest("source-landed")
    for field in (
        "edition",
        "toc_evidence",
        "source_structure_inventory_evidence",
        "source_structure_elements",
    ):
        source_landed[field] = copy.deepcopy(toc_indexed[field])

    _validate_book_workflow_progression(None, toc_indexed, "book coverage manifest")
    _validate_book_workflow_progression(toc_indexed, source_landed, "book coverage manifest")

    direct_translation = copy.deepcopy(source_landed)
    direct_translation["workflow_phase"] = "translated"
    with pytest.raises(WoonError, match="must advance through source-landed"):
        _validate_book_workflow_progression(
            toc_indexed, direct_translation, "book coverage manifest"
        )

    changed_toc = copy.deepcopy(source_landed)
    changed_toc["toc_evidence"] = [{"locator": "different TOC"}]
    with pytest.raises(WoonError, match="verified TOC toc_evidence cannot change"):
        _validate_book_workflow_progression(toc_indexed, changed_toc, "book coverage manifest")


def test_book_workflow_progression_preserves_source_and_translation() -> None:
    source = _workflow_manifest("source-landed")
    translated = _workflow_manifest("translated")
    _validate_book_workflow_progression(source, translated, "book coverage manifest")

    with pytest.raises(WoonError, match="cannot roll back"):
        _validate_book_workflow_progression(
            translated,
            source,
            "book coverage manifest",
        )

    changed_source = copy.deepcopy(translated)
    changed_source["source_elements"] = [{"element_id": "claim:replacement"}]
    with pytest.raises(WoonError, match="immutable source_elements"):
        _validate_book_workflow_progression(
            source,
            changed_source,
            "book coverage manifest",
        )

    concept = _workflow_manifest("concept-linked")
    changed_delivery = copy.deepcopy(concept)
    assignments = changed_delivery["source_element_assignments"]
    assert isinstance(assignments, list) and isinstance(assignments[0], dict)
    assignments[0]["delivery"] = "replacement-span"
    with pytest.raises(WoonError, match="translated reader delivery"):
        _validate_book_workflow_progression(
            translated,
            changed_delivery,
            "book coverage manifest",
        )


def test_source_landed_replace_accepts_ordered_supersequence(tmp_path: Path) -> None:
    current, replacement = _source_landed_extension_fixture()
    vault = tmp_path / "vault"
    path = vault / "catalog/book-coverage/example.json"
    path.parent.mkdir(parents=True)
    current_bytes = (json.dumps(current, sort_keys=True) + "\n").encode()
    path.write_bytes(current_bytes)
    compiler = CompiledWiki(_settings(vault))

    target, replacement_bytes = compiler._validated_coverage_manifest_update(
        BookCoverageManifestUpdate(
            relative_path="catalog/book-coverage/example.json",
            expected_sha256=hashlib.sha256(current_bytes).hexdigest(),
            replacement=replacement,
        )
    )

    assert target == path
    assert json.loads(replacement_bytes) == replacement


def test_source_landed_expansion_is_not_enabled_by_default() -> None:
    current, replacement = _source_landed_extension_fixture()

    with pytest.raises(WoonError, match="immutable source_asset_inventory"):
        _validate_book_workflow_progression(
            current,
            replacement,
            "scoped book coverage manifest",
        )


def test_materialize_book_coverage_scopes_replaces_only_pinned_subtrees() -> None:
    book = "books/example"
    chapter_one = f"{book}/chapter-01"
    old_leaf = f"{chapter_one}/1-1"
    chapter_two = f"{book}/chapter-02"
    base = {
        "schema_version": 3,
        "book_id": book,
        "edition": {"label": "1st", "source_sha256": "a" * 64},
        "source_archive": {"relative_path": "archive.pdf", "sha256": "a" * 64},
        "translation_required": False,
        "workflow_phase": "source-landed",
        "toc_node_count": 3,
        "toc_leaf_count": 1,
        "nodes": [
            {"canonical_id": chapter_one, "leaf": False, "state": "toc-only"},
            {"canonical_id": old_leaf, "leaf": True, "state": "source-covered"},
            {"canonical_id": chapter_two, "leaf": False, "state": "toc-only"},
        ],
        "source_structure_elements": [
            {"structure_id": "structure:chapter-one"},
            {"structure_id": "structure:section-one"},
            {"structure_id": "structure:chapter-two"},
        ],
        "source_structure_assignments": [
            {
                "structure_id": "structure:chapter-one",
                "disposition": "canonical-node",
                "canonical_id": chapter_one,
            },
            {
                "structure_id": "structure:section-one",
                "disposition": "canonical-node",
                "canonical_id": old_leaf,
            },
            {
                "structure_id": "structure:chapter-two",
                "disposition": "canonical-node",
                "canonical_id": chapter_two,
            },
        ],
        "source_elements": [{"element_id": "figure:old"}],
        "source_element_assignments": [
            {
                "element_id": "figure:old",
                "owner_id": old_leaf,
                "image_target": "archive/old.png",
            }
        ],
        "source_asset_inventory": [
            {
                "asset_id": "asset:old",
                "archive_relative_path": "archive/old.png",
            }
        ],
        "retired_source_section_wrappers": [],
    }
    scope = copy.deepcopy(base)
    scope["nodes"] = [{"canonical_id": chapter_one, "leaf": True, "state": "source-covered"}]
    scope["source_structure_elements"] = [
        {"structure_id": "structure:chapter-one"},
        {"structure_id": "structure:section-one"},
    ]
    scope["source_structure_assignments"] = [
        {
            "structure_id": "structure:chapter-one",
            "disposition": "canonical-node",
            "canonical_id": chapter_one,
        },
        {
            "structure_id": "structure:section-one",
            "disposition": "in-page-h2",
            "owner_id": chapter_one,
            "heading": "## 1.1 첫 절",
            "source_order": 1,
        },
    ]
    scope["source_elements"] = [{"element_id": "figure:new"}]
    scope["source_element_assignments"] = [
        {
            "element_id": "figure:new",
            "owner_id": chapter_one,
            "image_target": "archive/new.png",
        }
    ]
    scope["source_asset_inventory"] = [
        {
            "asset_id": "asset:new",
            "archive_relative_path": "archive/new.png",
        }
    ]

    merged = _materialize_book_coverage_scopes(base, (scope,), (chapter_one,))

    assert [node["canonical_id"] for node in merged["nodes"]] == [
        chapter_one,
        chapter_two,
    ]
    assert merged["toc_node_count"] == 2
    assert merged["toc_leaf_count"] == 1
    assert merged["source_structure_assignments"][1]["disposition"] == "in-page-h2"
    assert merged["source_element_assignments"][0]["owner_id"] == chapter_one
    assert merged["source_asset_inventory"] == scope["source_asset_inventory"]


def test_materialize_book_coverage_scopes_rejects_out_of_scope_owner() -> None:
    current, _ = _source_landed_extension_fixture()
    current_assignments = current["source_structure_assignments"]
    assert isinstance(current_assignments, list)
    for assignment in current_assignments:
        assert isinstance(assignment, dict)
        assignment["disposition"] = "canonical-node"
    scope = copy.deepcopy(current)
    for field in (
        "nodes",
        "source_structure_elements",
        "source_structure_assignments",
        "source_elements",
        "source_element_assignments",
        "source_asset_inventory",
    ):
        values = scope[field]
        assert isinstance(values, list)
        scope[field] = values[:1]
    assignments = scope["source_structure_assignments"]
    assert isinstance(assignments, list) and isinstance(assignments[0], dict)
    assignments[0]["canonical_id"] = "books/other/chapter-01"

    with pytest.raises(WoonError, match="owner is outside its root"):
        _materialize_book_coverage_scopes(
            current,
            (scope,),
            ("books/example/chapter-01",),
        )


@pytest.mark.parametrize(
    ("field", "case", "message"),
    [
        ("nodes", "delete", "cannot delete"),
        ("source_structure_elements", "change", "cannot change"),
        ("source_structure_assignments", "change", "cannot change"),
        ("source_elements", "reorder", "cannot reorder"),
        ("source_asset_inventory", "duplicate", "contains duplicate"),
        ("source_element_assignments", "reown", "cannot change"),
    ],
)
def test_source_landed_replace_rejects_non_monotonic_inventory_changes(
    field: str,
    case: str,
    message: str,
) -> None:
    current, replacement = _source_landed_extension_fixture()
    values = replacement[field]
    assert isinstance(values, list)
    if case == "delete":
        del values[0]
    elif case == "change":
        item = values[0]
        assert isinstance(item, dict)
        item["title"] = "Changed"
    elif case == "reorder":
        values[0], values[2] = values[2], values[0]
    elif case == "duplicate":
        values.append(copy.deepcopy(values[0]))
    else:
        item = values[0]
        assert isinstance(item, dict)
        item["owner_id"] = "books/example/chapter-02"

    with pytest.raises(WoonError, match=message):
        _validate_book_workflow_progression(
            current,
            replacement,
            "book coverage manifest",
            allow_source_landed_expansion=True,
        )


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("book_id", "immutable book_id"),
        ("edition", "immutable edition"),
        ("source_archive", "immutable source_archive"),
        ("phase_evidence", "phase evidence cannot change"),
        ("unexpected", "immutable unexpected"),
    ],
)
def test_source_landed_replace_preserves_pinned_source_fields(
    field: str,
    message: str,
) -> None:
    current, replacement = _source_landed_extension_fixture()
    replacement[field] = {"changed": True}

    with pytest.raises(WoonError, match=message):
        _validate_book_workflow_progression(
            current,
            replacement,
            "book coverage manifest",
            allow_source_landed_expansion=True,
        )


@pytest.mark.parametrize("field", ["expected_asset_count", "inventory_sha256"])
def test_source_landed_replace_rejects_stale_asset_evidence(field: str) -> None:
    current, replacement = _source_landed_extension_fixture()
    evidence = replacement["source_asset_inventory_evidence"]
    assert isinstance(evidence, dict)
    evidence[field] = 0 if field == "expected_asset_count" else "0" * 64

    with pytest.raises(WoonError, match=f"{field} is stale"):
        _validate_book_workflow_progression(
            current,
            replacement,
            "book coverage manifest",
            allow_source_landed_expansion=True,
        )


def test_concept_link_phase_rejects_reader_body_regeneration(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    compiler, _ = _service(vault)
    record = VerifiedBookPage(
        page_id="seed",
        title="시드",
        body="원문 본문이다.\n",
        statement="원문 본문을 보존한다.",
        current_use="원문을 확인할 때 사용한다.",
        source_locator="source://book/seed#page=1",
        source_sha256="a" * 64,
        frontmatter={"access": "local-only"},
    )
    compiler.promote_verified_book_pages((record,))

    compiler.validate_book_workflow_pages((record,), "concept-linked")
    with pytest.raises(WoonError, match="must not regenerate book reader body"):
        compiler.validate_book_workflow_pages(
            (replace(record, body="다르게 재생성한 본문이다.\n"),),
            "concept-linked",
        )


def test_compiled_transaction_rejects_stale_revision_before_mutation(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    inputs_before = compiler.snapshot_inputs()
    outputs_before = compiler.snapshot_outputs()

    with pytest.raises(WoonError, match="changed after it was read"):
        service.apply_compiled_wiki_transaction(_existing_seed_transaction(compiler, "0" * 64))

    assert compiler.snapshot_inputs() == inputs_before
    assert compiler.snapshot_outputs() == outputs_before


def test_compiled_transaction_rejects_duplicate_page_operation(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    revision = service.get("seed").revision
    transaction = _existing_seed_transaction(compiler, revision)
    duplicate = replace(
        transaction,
        pages_upsert=transaction.pages_upsert + transaction.pages_upsert,
        curations_upsert=transaction.curations_upsert + transaction.curations_upsert,
    )

    with pytest.raises(WoonError, match="duplicate page ID"):
        service.apply_compiled_wiki_transaction(duplicate)


def test_compiled_transaction_rejects_invalid_source_schema(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    transaction = _transaction()
    invalid_source = dict(transaction.sources_upsert[0])
    invalid_source["normalized_sha256"] = "f" * 64
    invalid = replace(transaction, sources_upsert=(invalid_source,))
    inputs_before = compiler.snapshot_inputs()
    outputs_before = compiler.snapshot_outputs()

    with pytest.raises(WoonError, match="normalized_sha256"):
        service.apply_compiled_wiki_transaction(invalid)

    assert compiler.snapshot_inputs() == inputs_before
    assert compiler.snapshot_outputs() == outputs_before


def test_compiled_transaction_rejects_non_exact_existing_source_id(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    sources, _, _, _, _ = compiler._load_inputs()
    source = copy.deepcopy(next(iter(sources.values())))
    body = "같은 source ID를 다른 내용에 재사용한다.\n"
    source["body"] = body
    source["original_sha256"] = _digest(body)
    source["normalized_sha256"] = _digest(_normalize(body))
    transaction = _existing_seed_transaction(compiler, service.get("seed").revision)

    with pytest.raises(WoonError, match="source ID collision is not an exact upsert"):
        service.apply_compiled_wiki_transaction(replace(transaction, sources_upsert=(source,)))


def test_compiled_transaction_rolls_back_compile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service = _service(tmp_path)
    inputs_before = compiler.snapshot_inputs()
    outputs_before = compiler.snapshot_outputs()
    original_compile = compiler.compile
    failed = False

    def fail_once(*args: object, **kwargs: object):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected transaction compile failure")
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(compiler, "compile", fail_once)
    with pytest.raises(RuntimeError, match="injected transaction compile failure"):
        service.apply_compiled_wiki_transaction(_transaction())

    assert compiler.snapshot_inputs() == inputs_before
    assert compiler.snapshot_outputs() == outputs_before
    assert compiler.audit().complete


def test_compiled_transaction_rolls_back_reindex_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service = _service(tmp_path)
    inputs_before = compiler.snapshot_inputs()
    outputs_before = compiler.snapshot_outputs()
    original_reindex = service._reindex_unlocked
    failed = False

    def fail_once() -> int:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected transaction reindex failure")
        return original_reindex()

    monkeypatch.setattr(service, "_reindex_unlocked", fail_once)
    with pytest.raises(RuntimeError, match="injected transaction reindex failure"):
        service.apply_compiled_wiki_transaction(_transaction())

    assert compiler.snapshot_inputs() == inputs_before
    assert compiler.snapshot_outputs() == outputs_before
    assert compiler.audit().complete


def test_compiled_transaction_applies_eleven_pages_and_reindexes(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    report = service.apply_compiled_wiki_transaction(_transaction(11))

    assert report.sources_upserted == 11
    assert report.claims_upserted == 11
    assert report.pages_upserted == 11
    assert report.curations_upserted == 11
    assert report.compiled == 11
    assert len(report.page_ids) == 11
    assert compiler.audit().complete
    assert service.search("공식 근거 10", 3)[0].canonical_id == "concepts/reference-10"


def test_compiled_transaction_refreshes_navigation_and_receipt(tmp_path: Path) -> None:
    compiler, service = _service(tmp_path)
    (tmp_path / "wiki/README.md").write_text(
        "---\n"
        "type: Wiki\n"
        "canonical_id: README\n"
        "title: Wiki\n"
        "node_kind: root\n"
        "view_mode: tree\n"
        "keywords:\n- Wiki\n"
        "aliases: []\n"
        "updated: 2026-09-03\n"
        "summary: 테스트 Wiki다.\n"
        "knowledge_state: 확인 필요\n"
        "---\n\n"
        "# Wiki\n",
        encoding="utf-8",
    )
    _, _, pages, curations, _ = compiler._load_inputs()
    child = _transaction()
    child_page = copy.deepcopy(child.pages_upsert[0])
    child_page["page_id"] = "seed/child"
    child_page["output_path"] = "seed/child.md"
    child_frontmatter = child_page["frontmatter"]
    assert isinstance(child_frontmatter, dict)
    child_frontmatter.update(
        {
            "canonical_id": "seed/child",
            "node_kind": "topic",
            "view_mode": "tree",
            "parent": "[[wiki/seed|시드]]",
            "sequence": 1,
        }
    )
    child_curation = copy.deepcopy(child.curations_upsert[0])
    child_curation["page_id"] = "seed/child"
    seed_page = copy.deepcopy(pages["seed"])
    seed_page["frontmatter"].update(
        {
            "node_kind": "topic",
            "view_mode": "tree",
            "parent": "[[wiki/README|Wiki]]",
            "sequence": 1,
            "navigation_groups": [
                {"label": "검증", "children": ["seed/child"]},
            ],
        }
    )

    report = service.apply_compiled_wiki_transaction(
        CompiledWikiTransaction(
            expected_revisions={"seed": service.get("seed").revision, "seed/child": None},
            sources_upsert=child.sources_upsert,
            claims_upsert=child.claims_upsert,
            pages_upsert=(seed_page, child_page),
            curations_upsert=(copy.deepcopy(curations["seed"]), child_curation),
        )
    )

    assert report.pages_upserted == 2
    assert "[[wiki/seed/child|개발 참고 0]]" in (tmp_path / "wiki/seed.md").read_text(
        encoding="utf-8"
    )
    assert compiler.audit().complete


def test_apply_compiled_transaction_cli_rejects_extra_schema_field(tmp_path: Path) -> None:
    payload = tmp_path / "transaction.json"
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                "expected_revisions": {},
                "sources_upsert": [],
                "claims_upsert": [],
                "pages_upsert": [],
                "curations_upsert": [],
                "metadata": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(WoonError, match="input fields are invalid"):
        run(["knowledge", "apply-compiled-transaction", "--input", str(payload)], StringIO())


def test_apply_compiled_transaction_cli_calls_service_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "transaction.json"
    transaction = _transaction()
    payload.write_text(
        json.dumps(
            {
                "apply": True,
                "expected_revisions": transaction.expected_revisions,
                "sources_upsert": transaction.sources_upsert,
                "claims_upsert": transaction.claims_upsert,
                "pages_upsert": transaction.pages_upsert,
                "curations_upsert": transaction.curations_upsert,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls: list[CompiledWikiTransaction] = []

    class FakeService:
        def apply_compiled_wiki_transaction(
            self, actual: CompiledWikiTransaction
        ) -> CompiledWikiTransactionReport:
            calls.append(actual)
            return CompiledWikiTransactionReport(1, 1, 1, 1, 1, 0, ("concepts/reference-00",))

    monkeypatch.setattr(
        cli,
        "build_knowledge_service",
        lambda vault: (SimpleNamespace(vault=vault), FakeService()),
    )
    run(
        [
            "knowledge",
            "apply-compiled-transaction",
            "--input",
            str(payload),
            "--vault",
            str(tmp_path),
        ],
        StringIO(),
    )
    assert len(calls) == 1
    assert calls[0].expected_revisions == {"concepts/reference-00": None}
