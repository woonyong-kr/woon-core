"""Regression contract for same-page curated-to-verified book promotion."""

from __future__ import annotations

from hashlib import sha256
from urllib.parse import quote

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.compiled_wiki import (
    CompiledWiki,
    CompiledWikiSettings,
    VerifiedBookPage,
    _normalize,
    _validate_page,
)


def _compiler(tmp_path):
    vault = tmp_path
    return CompiledWiki(
        CompiledWikiSettings(
            vault=vault,
            output_root=vault / "wiki",
            sources_path=vault / "sources.yaml",
            claims_path=vault / "claims.yaml",
            pages_path=vault / "pages.yaml",
            curation_path=vault / "curation.yaml",
            relations_path=vault / "relations.yaml",
            receipts_path=vault / "receipts.yaml",
            review_queue_path=vault / "queue.yaml",
        )
    )


def _record(page_id: str) -> VerifiedBookPage:
    body = "## 검증된 본문\n\n현재 페이지의 완전한 교체 본문입니다.\n"
    return VerifiedBookPage(
        page_id=page_id,
        title="검증된 장",
        body=body,
        statement="검증된 장의 원문이다.",
        current_use="원문 순서로 읽는다.",
        source_locator="source://book/fixture#page=1",
        source_sha256="a" * 64,
        frontmatter={"type": "Wiki", "access": "local-only", "parent": "[[books/root|root]]"},
    )


def _inputs(page_id: str, other_page_id: str | None = None):
    owner = quote(page_id, safe="/")
    digest = sha256(_normalize("old\n").encode()).hexdigest()
    source_id = f"source://curated-wiki/{owner}/{digest[:24]}"
    claim_id = f"claim://curated-wiki/{owner}/{digest[:24]}"
    sources = {
        source_id: {
            "source_id": source_id,
            "kind": "curated-wiki",
            "locator": "fixture",
            "original_sha256": digest,
            "normalized_sha256": digest,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": "old",
            "purpose": "old",
            "body": "old\n",
        }
    }
    claims = {
        claim_id: {
            "claim_id": claim_id,
            "kind": "curated-document",
            "status": "accepted",
            "statement": "old",
            "source_ids": [source_id],
            "markdown": "old\n",
        }
    }
    pages = {
        page_id: {
            "page_id": page_id,
            "output_path": page_id + ".md",
            "title": "old",
            "frontmatter": {"title": "old", "parent": "books/root"},
            "source_ids": [source_id],
            "claim_ids": [claim_id],
            "render": {"kind": "source-body", "source_id": source_id},
        },
        "books/root": {
            "page_id": "books/root",
            "output_path": "books/root.md",
            "title": "root",
            "frontmatter": {"title": "root"},
            "source_ids": [],
            "claim_ids": [],
            "render": {"kind": "toc-only"},
        },
    }
    if other_page_id:
        pages[other_page_id] = {
            "page_id": other_page_id,
            "output_path": other_page_id + ".md",
            "title": "other",
            "frontmatter": {},
            "source_ids": [source_id],
            "claim_ids": [claim_id],
            "render": {"kind": "source-body", "source_id": source_id},
        }
    return sources, claims, pages, {}


def _old_ids(page_id: str) -> tuple[str, str]:
    digest = sha256(_normalize("old\n").encode()).hexdigest()[:24]
    owner = quote(page_id, safe="/")
    return (f"source://curated-wiki/{owner}/{digest}", f"claim://curated-wiki/{owner}/{digest}")


def test_same_page_curated_records_are_archived_and_superseded(tmp_path):
    page_id = "books/한글"  # Record owner is URL-encoded but decodes to this identity.
    sources, claims, pages, curations = _inputs(page_id)
    _compiler(tmp_path)._apply_verified_book_records(
        (_record(page_id),), sources, claims, pages, curations
    )
    new_source = pages[page_id]["source_ids"][0]
    new_claim = pages[page_id]["claim_ids"][0]
    old_source, old_claim = _old_ids(page_id)
    assert sources[old_source]["lifecycle"] == "archived"
    assert sources[old_source]["superseded_by"] == new_source
    assert claims[old_claim]["status"] == "superseded"
    assert claims[old_claim]["superseded_by"] == new_claim
    assert sources[old_source]["body"] == "old\n"
    assert claims[old_claim]["markdown"] == "old\n"
    assert pages[page_id]["source_ids"] == [new_source]
    assert pages[page_id]["claim_ids"] == [new_claim]
    _validate_page(
        pages[page_id],
        [sources[new_source]],
        [claims[new_claim]],
        curations[page_id],
    )


def test_shared_curated_records_remain_active_but_are_removed_from_target(tmp_path):
    page_id, other = "books/a", "books/b"
    sources, claims, pages, curations = _inputs(page_id, other)
    _compiler(tmp_path)._apply_verified_book_records(
        (_record(page_id),), sources, claims, pages, curations
    )
    old_source, old_claim = _old_ids(page_id)
    assert sources[old_source]["lifecycle"] == "compiled"
    assert claims[old_claim]["status"] == "accepted"
    assert old_source not in pages[page_id]["source_ids"]
    assert old_claim not in pages[page_id]["claim_ids"]


@pytest.mark.parametrize(
    "wrong_source,wrong_claim",
    [
        (
            f"source://curated-wiki/books/a-sibling/{'c' * 24}",
            f"claim://curated-wiki/books/a-sibling/{'d' * 24}",
        ),
        (f"source://unrelated/books/a/{'c' * 24}", f"claim://unrelated/books/a/{'d' * 24}"),
    ],
)
def test_unrelated_or_same_prefix_records_are_not_superseded(tmp_path, wrong_source, wrong_claim):
    sources, claims, pages, curations = _inputs("books/a")
    original_source, original_claim = _old_ids("books/a")
    sources[wrong_source] = dict(sources[original_source], source_id=wrong_source)
    claims[wrong_claim] = dict(
        claims[original_claim], claim_id=wrong_claim, source_ids=[wrong_source]
    )
    pages["books/a"]["source_ids"].append(wrong_source)
    pages["books/a"]["claim_ids"].append(wrong_claim)
    _compiler(tmp_path)._apply_verified_book_records(
        (_record("books/a"),), sources, claims, pages, curations
    )
    assert sources[wrong_source]["lifecycle"] == "compiled"
    assert claims[wrong_claim]["status"] == "accepted"


def test_detached_curated_source_is_archived_when_its_page_claim_is_replaced(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    old, _ = _old_ids("books/a")
    pages["books/a"]["source_ids"] = []
    _compiler(tmp_path)._apply_verified_book_records(
        (_record("books/a"),), sources, claims, pages, curations
    )
    assert sources[old]["lifecycle"] == "archived"


def test_missing_curated_source_record_fails_closed(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    sources.clear()
    with pytest.raises((WoonError, KeyError)):
        _compiler(tmp_path)._apply_verified_book_records(
            (_record("books/a"),), sources, claims, pages, curations
        )


def test_retained_claim_keeps_its_curated_source_active_and_in_target(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    old_source, old_claim = _old_ids("books/a")
    retained_id = "claim://external/independent-evidence"
    claims[retained_id] = dict(claims[old_claim], claim_id=retained_id, kind="observation")
    pages["books/a"]["claim_ids"].append(retained_id)
    compiler = _compiler(tmp_path)
    compiler._apply_verified_book_records((_record("books/a"),), sources, claims, pages, curations)
    assert old_source in pages["books/a"]["source_ids"]
    assert sources[old_source]["lifecycle"] == "compiled"
    assert claims[retained_id]["status"] == "accepted"
    page = pages["books/a"]
    _validate_page(
        page,
        compiler._page_sources(page, sources),
        compiler._page_claims(page, claims),
        compiler._page_curation(page, curations),
    )


def test_unattached_accepted_claim_keeps_shared_source_evidence_active(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    old_source, old_claim = _old_ids("books/a")
    retained_id = "claim://external/unattached-evidence"
    claims[retained_id] = dict(claims[old_claim], claim_id=retained_id, kind="observation")
    _compiler(tmp_path)._apply_verified_book_records(
        (_record("books/a"),), sources, claims, pages, curations
    )
    assert old_source not in pages["books/a"]["source_ids"]
    assert sources[old_source]["lifecycle"] == "compiled"
    assert claims[retained_id]["status"] == "accepted"


def test_detached_foreign_evidence_is_not_hidden_by_curated_transition(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    old_source, old_claim = _old_ids("books/a")
    foreign_source = "source://external/independent-document"
    sources[foreign_source] = dict(sources[old_source], source_id=foreign_source)
    claims[old_claim]["source_ids"].append(foreign_source)
    with pytest.raises(WoonError, match="detached foreign evidence"):
        _compiler(tmp_path)._apply_verified_book_records(
            (_record("books/a"),), sources, claims, pages, curations
        )


@pytest.mark.parametrize("suffix", ["old", "A" * 24, "a" * 23])
def test_malformed_curated_identity_is_preserved_not_superseded(tmp_path, suffix):
    sources, claims, pages, curations = _inputs("books/a")
    old_source, old_claim = _old_ids("books/a")
    malformed_source = "source://curated-wiki/books/a/" + suffix
    malformed_claim = "claim://curated-wiki/books/a/" + suffix
    sources[malformed_source] = dict(sources[old_source], source_id=malformed_source)
    claims[malformed_claim] = dict(
        claims[old_claim], claim_id=malformed_claim, source_ids=[malformed_source]
    )
    pages["books/a"]["source_ids"].append(malformed_source)
    pages["books/a"]["claim_ids"].append(malformed_claim)
    _compiler(tmp_path)._apply_verified_book_records(
        (_record("books/a"),), sources, claims, pages, curations
    )
    assert malformed_source in pages["books/a"]["source_ids"]
    assert malformed_claim in pages["books/a"]["claim_ids"]
    assert sources[malformed_source]["lifecycle"] == "compiled"
    assert claims[malformed_claim]["status"] == "accepted"


def test_encoded_path_separator_is_not_a_curated_owner_boundary(tmp_path):
    sources, claims, pages, curations = _inputs("books/a")
    old_source, old_claim = _old_ids("books/a")
    alias_source = old_source.replace("books/a/", "books%2Fa/")
    alias_claim = old_claim.replace("books/a/", "books%2Fa/")
    sources[alias_source] = dict(sources.pop(old_source), source_id=alias_source)
    claims[alias_claim] = dict(
        claims.pop(old_claim), claim_id=alias_claim, source_ids=[alias_source]
    )
    pages["books/a"]["source_ids"] = [alias_source]
    pages["books/a"]["claim_ids"] = [alias_claim]
    pages["books/a"]["render"]["source_id"] = alias_source
    _compiler(tmp_path)._apply_verified_book_records(
        (_record("books/a"),), sources, claims, pages, curations
    )
    assert sources[alias_source]["lifecycle"] == "compiled"
    assert claims[alias_claim]["status"] == "accepted"
