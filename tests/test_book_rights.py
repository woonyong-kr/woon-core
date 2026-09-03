from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import MarkdownDocumentRepository, SQLiteFtsSearchIndex
from woon_core.knowledge.book_contract import (
    PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256,
    PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
)
from woon_core.knowledge.book_rights import (
    load_book_rights_demotion,
    load_book_rights_restoration,
)
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    CompiledWiki,
    StagedBookAsset,
    VerifiedBookPage,
    _flatten_navigation_groups,
    _validate_rights_toc_body,
)
from woon_core.knowledge.service import KnowledgeService

DIGEST = "a" * 64
BOOK_ID = "personal/example-book"


def payload() -> dict[str, object]:
    return {
        "apply": False,
        "schema_version": 2,
        "book_id": BOOK_ID,
        "rights_evidence": {
            "source_archive_relative_path": (
                "wiki/private/_sources/knowledge/local-only/example/book.pdf"
            ),
            "source_archive_sha256": DIGEST,
            "notice_locator": "copyright page, PDF 3",
            "notice_sha256": DIGEST,
            "notice_summary": "Processing is prohibited.",
            "decision": "blocked-rights",
            "reviewed_on": "2026-09-02",
        },
        "survivor_page_ids": [
            f"{BOOK_ID}/chapter-01",
            f"{BOOK_ID}/chapter-01/1-1",
        ],
        "retire_page_ids": [f"{BOOK_ID}/chapter-01/section-1"],
        "retire_replacements": {
            f"{BOOK_ID}/chapter-01/section-1": f"{BOOK_ID}/chapter-01/1-1"
        },
        "survivor_navigation_groups": {},
        "survivor_bodies": {
            f"{BOOK_ID}/chapter-01": "",
            f"{BOOK_ID}/chapter-01/1-1": "",
        },
        "survivor_body_sha256": {
            f"{BOOK_ID}/chapter-01": hashlib.sha256(b"").hexdigest(),
            f"{BOOK_ID}/chapter-01/1-1": hashlib.sha256(b"").hexdigest(),
        },
        "affected_source_ids": ["source://book/example"],
        "affected_claim_ids": ["claim://book/example"],
        "expected_revisions": {
            f"{BOOK_ID}/chapter-01": DIGEST,
            f"{BOOK_ID}/chapter-01/1-1": DIGEST,
            f"{BOOK_ID}/chapter-01/section-1": DIGEST,
        },
        "expected_output_sha256": {
            f"{BOOK_ID}/chapter-01": DIGEST,
            f"{BOOK_ID}/chapter-01/1-1": DIGEST,
            f"{BOOK_ID}/chapter-01/section-1": DIGEST,
        },
        "expected_source_body_sha256": {"source://book/example": DIGEST},
        "expected_asset_sha256": {},
        "coverage": {
            "relative_path": "catalog/book-coverage/example.json",
            "expected_sha256": DIGEST,
            "replacement": {},
        },
        "book_intake": {
            "relative_path": "catalog/book-intake/official-books.json",
            "expected_sha256": DIGEST,
            "bundle_id": "example",
        },
        "quarantine_relative_path": (
            "wiki/private/_sources/knowledge/local-only/example/"
            "rights-quarantine/aaaaaaaaaaaaaaaaaaaaaaaa"
        ),
    }


def write_payload(tmp_path: Path, value: dict[str, object]) -> Path:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def restore_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "rights_contract": {
            "version": PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
            "sha256": PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256,
        },
        "book_id": BOOK_ID,
        "rights_evidence": {
            "source_archive_relative_path": (
                "wiki/private/_sources/knowledge/local-only/example/book.pdf"
            ),
            "source_archive_sha256": DIGEST,
            "notice_locator": "판권면, PDF 3쪽",
            "notice_sha256": "b" * 64,
            "authorization_receipt_locator": "conversation://task/turn-1",
            "authorization_receipt_sha256": "c" * 64,
            "ownership_basis": "user-purchased-copy",
            "authorized_on": "2026-09-03",
            "authorized_scope": "source-landed-private-local-only",
            "decision": "user-authorized-private",
            "restrictions": [
                "external-transmission-prohibited",
                "model-training-prohibited",
                "publication-prohibited",
                "redistribution-prohibited",
            ],
        },
        "book_intake": {
            "relative_path": "catalog/book-intake/official-books.json",
            "expected_sha256": DIGEST,
            "bundle_id": "example",
        },
        "quarantine_manifests": [
            {
                "relative_path": (
                    "wiki/private/_sources/knowledge/local-only/example/"
                    "rights-quarantine/aaaaaaaaaaaaaaaaaaaaaaaa/manifest.json"
                ),
                "expected_sha256": "d" * 64,
            }
        ],
    }


def test_load_book_rights_demotion_requires_explicit_safe_exact_contract(
    tmp_path: Path,
) -> None:
    apply, request = load_book_rights_demotion(write_payload(tmp_path, payload()))

    assert apply is False
    assert request.book_id == BOOK_ID
    assert request.survivor_ids == (
        f"{BOOK_ID}/chapter-01",
        f"{BOOK_ID}/chapter-01/1-1",
    )


def test_load_book_rights_restoration_requires_hash_pinned_private_contract() -> None:
    request = load_book_rights_restoration(restore_payload())

    assert request.book_id == BOOK_ID
    assert request.rights_evidence["decision"] == "user-authorized-private"
    assert len(request.quarantine_manifests) == 1


def test_load_book_rights_restoration_rejects_missing_distribution_boundary() -> None:
    value = restore_payload()
    evidence = dict(value["rights_evidence"])  # type: ignore[arg-type]
    evidence["restrictions"] = ["publication-prohibited"]
    value["rights_evidence"] = evidence

    with pytest.raises(WoonError, match="exact private-only boundary"):
        load_book_rights_restoration(value)


def test_load_book_rights_restoration_rejects_missing_purchase_basis() -> None:
    value = restore_payload()
    evidence = dict(value["rights_evidence"])  # type: ignore[arg-type]
    evidence["ownership_basis"] = "user-provided-copy"
    value["rights_evidence"] = evidence

    with pytest.raises(WoonError, match="user-purchased copy"):
        load_book_rights_restoration(value)


def test_load_book_rights_restoration_rejects_stale_contract() -> None:
    value = restore_payload()
    contract = dict(value["rights_contract"])  # type: ignore[arg-type]
    contract["sha256"] = "0" * 64
    value["rights_contract"] = contract

    with pytest.raises(WoonError, match="contract hash is stale"):
        load_book_rights_restoration(value)


def test_book_rights_restore_supersedes_blocked_decision_records() -> None:
    page_id = f"{BOOK_ID}/chapter-01"
    toc_page_id = f"{BOOK_ID}/appendix-a"
    rights_source = f"source://book-rights/{BOOK_ID}/notice/page"
    rights_claim = f"claim://book-rights/{BOOK_ID}/notice/page"
    current_source = "source://book/example/current"
    current_claim = "claim://book/example/current"
    sources = {
        rights_source: {"source_id": rights_source, "lifecycle": "compiled"},
        current_source: {"source_id": current_source, "lifecycle": "compiled"},
    }
    claims = {
        rights_claim: {
            "claim_id": rights_claim,
            "source_ids": [rights_source],
            "status": "accepted",
        },
        current_claim: {
            "claim_id": current_claim,
            "source_ids": [current_source],
            "status": "accepted",
        },
    }
    pages = {
        page_id: {
            "source_ids": [rights_source, current_source],
            "claim_ids": [rights_claim, current_claim],
            "render": {"kind": "source-body", "source_id": current_source},
        },
        toc_page_id: {
            "source_ids": [],
            "claim_ids": [],
            "render": {"kind": "toc-only"},
        },
    }
    record = VerifiedBookPage(
        page_id=page_id,
        title="1장",
        body="source body",
        statement="source statement",
        current_use="source use",
        source_locator="source://book/example#page=1",
        source_sha256=DIGEST,
        frontmatter={},
        expected_revision=None,
    )

    CompiledWiki._retire_book_rights_decisions(
        BOOK_ID, (record,), sources, claims, pages
    )

    assert pages[page_id]["source_ids"] == [current_source]
    assert pages[page_id]["claim_ids"] == [current_claim]
    assert sources[rights_source] == {
        "source_id": rights_source,
        "lifecycle": "archived",
        "superseded_by": current_source,
    }
    assert claims[rights_claim] == {
        "claim_id": rights_claim,
        "source_ids": [rights_source],
        "status": "superseded",
        "superseded_by": current_claim,
    }


def test_book_rights_restore_rejects_source_free_non_toc_page() -> None:
    page_id = f"{BOOK_ID}/chapter-01"
    pages = {
        page_id: {
            "source_ids": [],
            "claim_ids": [],
            "render": {"kind": "source-body", "source_id": "source://missing"},
        }
    }

    with pytest.raises(WoonError, match="page source_ids must be a non-empty string list"):
        CompiledWiki._retire_book_rights_decisions(
            BOOK_ID,
            (),
            {},
            {},
            pages,
        )


def test_load_book_rights_demotion_rejects_archive_escape(tmp_path: Path) -> None:
    value = payload()
    rights = dict(value["rights_evidence"])  # type: ignore[arg-type]
    rights["source_archive_relative_path"] = "../../book.pdf"
    value["rights_evidence"] = rights

    with pytest.raises(WoonError, match="source archive path is unsafe"):
        load_book_rights_demotion(write_payload(tmp_path, value))


def test_load_book_rights_demotion_allows_wrapper_free_terminal_retirement(
    tmp_path: Path,
) -> None:
    value = payload()
    leaf_id = f"{BOOK_ID}/chapter-01/1-1"
    value["survivor_page_ids"] = [f"{BOOK_ID}/chapter-01"]
    value["retire_page_ids"] = [leaf_id]
    value["retire_replacements"] = {}
    value["survivor_navigation_groups"] = {f"{BOOK_ID}/chapter-01": []}
    toc_body = "## 1.1 시작\n\n- 1.1 첫 절\n"
    value["survivor_bodies"] = {f"{BOOK_ID}/chapter-01": toc_body}
    value["survivor_body_sha256"] = {
        f"{BOOK_ID}/chapter-01": hashlib.sha256(toc_body.encode()).hexdigest()
    }
    value["expected_revisions"] = {
        f"{BOOK_ID}/chapter-01": DIGEST,
        leaf_id: DIGEST,
    }
    value["expected_output_sha256"] = {
        f"{BOOK_ID}/chapter-01": DIGEST,
        leaf_id: DIGEST,
    }

    _, request = load_book_rights_demotion(write_payload(tmp_path, value))

    assert request.retire_replacements == {}
    assert request.retire_page_ids == (leaf_id,)


def test_load_book_rights_demotion_rejects_replacement_outside_survivors(
    tmp_path: Path,
) -> None:
    value = payload()
    value["retire_replacements"] = {
        f"{BOOK_ID}/chapter-01/section-1": f"{BOOK_ID}/chapter-02"
    }

    with pytest.raises(WoonError, match="surviving pages"):
        load_book_rights_demotion(write_payload(tmp_path, value))


@pytest.mark.parametrize(("survivor_count", "leaf_count"), [(10, 160), (13, 413)])
def test_rights_demotion_can_reduce_large_wrapper_free_books_to_maps(
    survivor_count: int,
    leaf_count: int,
) -> None:
    pages: dict[str, dict[str, object]] = {}
    leaf_ids: list[str] = []
    for index in range(leaf_count):
        chapter = index % survivor_count + 1
        leaf_id = f"{BOOK_ID}/chapter-{chapter:02d}/leaf-{index:03d}"
        leaf_ids.append(leaf_id)
        pages[leaf_id] = {"title": f"{chapter}.{index + 1} 원문 절", "frontmatter": {}}
    for chapter in range(1, survivor_count + 1):
        root_id = f"{BOOK_ID}/chapter-{chapter:02d}"
        children = [leaf_id for leaf_id in leaf_ids if f"chapter-{chapter:02d}/" in leaf_id]
        pages[root_id] = {
            "title": f"{chapter}장",
            "frontmatter": {
                "navigation_groups": [{"label": f"{chapter}장", "children": children}]
            },
        }
        assert _flatten_navigation_groups(
            pages[root_id]["frontmatter"]["navigation_groups"],
            pages,
            set(leaf_ids),
        ) == []
        body = f"## {chapter}장\n\n" + "\n".join(
            f"- {pages[child]['title']}" for child in children
        )
        _validate_rights_toc_body(body, root_id, set(leaf_ids))


def test_rights_demotion_expands_retired_wrapper_to_surviving_leaves() -> None:
    root_id = f"{BOOK_ID}/chapter-03"
    wrapper_id = f"{root_id}/section-3-3"
    leaf_ids = [f"{root_id}/3-3-1", f"{root_id}/3-3-2"]
    pages = {
        root_id: {
            "title": "3장",
            "frontmatter": {
                "navigation_groups": [{"label": "3장", "children": [wrapper_id]}]
            },
        },
        wrapper_id: {
            "title": "3.3 셀프 어텐션",
            "frontmatter": {
                "navigation_groups": [{"label": "3.3", "children": leaf_ids}]
            },
        },
    }

    assert _flatten_navigation_groups(
        pages[root_id]["frontmatter"]["navigation_groups"],
        pages,
        {wrapper_id},
    ) == [{"label": "3.3 셀프 어텐션", "children": leaf_ids}]


def test_book_rights_restore_rolls_back_intake_and_coverage_on_writer_failure(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    archive_bytes = b"purchased private source"
    source_hash = hashlib.sha256(archive_bytes).hexdigest()
    archive_relative = (
        "wiki/private/_sources/knowledge/local-only/example/book.pdf"
    )
    archive_path = vault / archive_relative
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(archive_bytes)
    quarantine_relative = (
        "wiki/private/_sources/knowledge/local-only/example/rights-quarantine/"
        "aaaaaaaaaaaaaaaaaaaaaaaa/manifest.json"
    )
    quarantine_path = vault / quarantine_relative
    quarantine_path.parent.mkdir(parents=True)
    quarantine = {
        "schema_version": 1,
        "book_id": BOOK_ID,
        "rights_evidence": {"source_archive_sha256": source_hash},
        "entries": [],
        "entry_count": 0,
    }
    quarantine_bytes = (json.dumps(quarantine) + "\n").encode()
    quarantine_path.write_bytes(quarantine_bytes)
    intake_path = vault / "catalog/book-intake/official-books.json"
    intake_path.parent.mkdir(parents=True)
    intake = {
        "schema_version": 1,
        "source_catalog": "catalog/sources/official-books.yaml",
        "bundles": [
            {
                "id": "example",
                "title": "Example",
                "source_root": "book.pdf",
                "archive_name": "Example.pdf",
                "kind": "book",
                "language": "ko",
                "rights_status": "processing-prohibited",
                "processing_state": "blocked-rights",
                "priority": 1,
                "target": BOOK_ID,
            }
        ],
    }
    intake_bytes = (json.dumps(intake) + "\n").encode()
    intake_path.write_bytes(intake_bytes)
    coverage_path = vault / "catalog/book-coverage/example.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_bytes = b'{"blocked": true}\n'
    coverage_path.write_bytes(coverage_bytes)

    raw_restore = restore_payload()
    evidence = dict(raw_restore["rights_evidence"])  # type: ignore[arg-type]
    evidence["source_archive_sha256"] = source_hash
    raw_restore["rights_evidence"] = evidence
    raw_restore["book_intake"] = {
        "relative_path": "catalog/book-intake/official-books.json",
        "expected_sha256": hashlib.sha256(intake_bytes).hexdigest(),
        "bundle_id": "example",
    }
    raw_restore["quarantine_manifests"] = [
        {
            "relative_path": quarantine_relative,
            "expected_sha256": hashlib.sha256(quarantine_bytes).hexdigest(),
        }
    ]
    request = load_book_rights_restoration(raw_restore)
    coverage = BookCoverageManifestUpdate(
        mode="replace",
        relative_path="catalog/book-coverage/example.json",
        expected_sha256=hashlib.sha256(coverage_bytes).hexdigest(),
        replacement={
            "schema_version": 3,
            "book_id": BOOK_ID,
            "workflow_phase": "source-landed",
            "translation_required": False,
            "edition": {"source_sha256": source_hash},
        },
    )
    page = VerifiedBookPage(
        page_id=BOOK_ID,
        title="Example",
        body="원문 본문이다.\n",
        statement="원문을 보존한다.",
        current_use="책을 읽을 때 사용한다.",
        source_locator="source://example#page=1",
        source_sha256=source_hash,
        frontmatter={"access": "local-only"},
        expected_revision=None,
    )

    class FailingCompiler:
        def __init__(self) -> None:
            self.vault = vault
            self.fail_writer = True

        def validate_verified_book_retirement_content(
            self, *args: object, **kwargs: object
        ) -> None:
            del args, kwargs
            return None

        def validate_book_workflow_pages(
            self, *args: object, **kwargs: object
        ) -> None:
            del args, kwargs
            return None

        def validate_book_coverage_manifest_update(
            self, update: BookCoverageManifestUpdate
        ) -> Path:
            return vault / update.relative_path

        def validate_staged_book_assets(
            self,
            assets: tuple[StagedBookAsset, ...],
            update: BookCoverageManifestUpdate,
        ) -> tuple[int, int]:
            del assets, update
            return 0, 0

        def snapshot_inputs(
            self, *, extra_paths: tuple[Path, ...] = ()
        ) -> dict[Path, bytes | None]:
            return {path: path.read_bytes() if path.is_file() else None for path in extra_paths}

        def snapshot_outputs(self, **kwargs: object) -> dict[Path, bytes | None]:
            del kwargs
            return {}

        def snapshot_staged_book_assets(
            self, assets: tuple[StagedBookAsset, ...]
        ) -> dict[Path, bytes | None]:
            del assets
            return {}

        def install_staged_book_assets(self, assets: tuple[StagedBookAsset, ...]) -> None:
            del assets

        def apply_verified_book_update(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            coverage_path.write_bytes(b"partial coverage")
            if self.fail_writer:
                raise RuntimeError("injected restore writer failure")

        def dry_run_verified_book_update(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            if self.fail_writer:
                raise RuntimeError("injected restore writer failure")

        def restore_inputs(self, snapshot: dict[Path, bytes | None]) -> None:
            for path, content in snapshot.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)

        def restore_outputs(self, snapshot: dict[Path, bytes | None]) -> None:
            del snapshot

        def restore_staged_book_assets(self, snapshot: dict[Path, bytes | None]) -> None:
            del snapshot

    compiler = cast(CompiledWiki, FailingCompiler())
    service = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        SQLiteFtsSearchIndex(vault / ".local/search.sqlite3"),
        cast(object, SimpleNamespace()),  # history is not used by this transaction
        compiled_wiki=compiler,
    )

    with pytest.raises(RuntimeError, match="injected restore writer failure"):
        service.preflight_book_rights_restoration(request, (page,), coverage)

    assert intake_path.read_bytes() == intake_bytes
    assert coverage_path.read_bytes() == coverage_bytes
    assert quarantine_path.read_bytes() == quarantine_bytes
    assert archive_path.read_bytes() == archive_bytes

    cast(FailingCompiler, compiler).fail_writer = False
    report = service.preflight_book_rights_restoration(request, (page,), coverage)
    assert report.ready is True
    assert report.applied is False
    assert intake_path.read_bytes() == intake_bytes
    assert coverage_path.read_bytes() == coverage_bytes
    assert quarantine_path.read_bytes() == quarantine_bytes
    assert archive_path.read_bytes() == archive_bytes

    cast(FailingCompiler, compiler).fail_writer = True
    with pytest.raises(RuntimeError, match="injected restore writer failure"):
        service.apply_book_rights_restoration(request, (page,), coverage)

    assert intake_path.read_bytes() == intake_bytes
    assert coverage_path.read_bytes() == coverage_bytes
    assert quarantine_path.read_bytes() == quarantine_bytes
    assert archive_path.read_bytes() == archive_bytes
