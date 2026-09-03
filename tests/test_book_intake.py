from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.book_intake import (
    audit_book_intake,
    validate_book_promotion_rights,
)


def _source_catalog(vault: Path) -> None:
    target = vault / "catalog/sources/official-books.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "official-books",
                "summary": {"new": 2},
                "records": [
                    {
                        "source_id": "source://official-books/book.pdf",
                        "locator": "book.pdf",
                        "sha256": "a" * 64,
                        "size": 10,
                        "role": "document",
                        "privacy": "private/local-only",
                        "state": "new",
                        "target": None,
                        "target_sha256": None,
                    },
                    {
                        "source_id": "source://official-books/course/one.html",
                        "locator": "course/one.html",
                        "sha256": "b" * 64,
                        "size": 11,
                        "role": "document",
                        "privacy": "private/local-only",
                        "state": "new",
                        "target": None,
                        "target_sha256": None,
                    },
                ],
                "excluded": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _manifest(vault: Path, bundles: list[dict[str, object]]) -> None:
    target = vault / "catalog/book-intake/official-books.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_catalog": "catalog/sources/official-books.yaml",
                "bundles": bundles,
            }
        ),
        encoding="utf-8",
    )


def _bundle(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "book",
        "title": "Book",
        "source_root": "book.pdf",
        "archive_name": "Book.pdf",
        "kind": "book",
        "language": "ko",
        "rights_status": "user-provided-private",
        "processing_state": "content-in-progress",
        "priority": 1,
        "target": "personal/book",
    }
    value.update(overrides)
    return value


def _private_rights_evidence() -> dict[str, object]:
    return {
        "source_archive_relative_path": (
            "wiki/private/_sources/knowledge/local-only/book/Book.pdf"
        ),
        "source_archive_sha256": "a" * 64,
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
    }


def test_book_intake_assigns_every_source_once(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete
    assert report.source_count == 2
    assert report.assigned_count == 2


def test_book_intake_rejects_unassigned_and_overlapping_sources(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(source_root="course/", id="course"),
            _bundle(source_root="course/one.html", id="course-page"),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete is False
    assert any("unassigned source: book.pdf" in error for error in report.errors)
    assert any("multiple bundles" in error for error in report.errors)


def test_book_intake_requires_unverified_commercial_sources_to_stay_blocked(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(
                rights_status="unverified-commercial",
                processing_state="content-in-progress",
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete is False
    assert any("must be blocked-rights" in error for error in report.errors)


def test_book_intake_allows_hash_pinned_user_authorized_private_processing(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(
                rights_status="user-authorized-private",
                processing_state="content-in-progress",
                rights_evidence=_private_rights_evidence(),
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete


def test_book_intake_rejects_private_authorization_without_exact_date(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    evidence = _private_rights_evidence()
    evidence["authorized_on"] = "today"
    _manifest(
        vault,
        [
            _bundle(
                rights_status="user-authorized-private",
                processing_state="content-in-progress",
                rights_evidence=evidence,
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert any("authorized_on must be YYYY-MM-DD" in error for error in report.errors)


def test_book_intake_keeps_user_authorized_private_material_inside_personal_boundary(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(
                rights_status="user-authorized-private",
                processing_state="content-in-progress",
                rights_evidence=_private_rights_evidence(),
                target="books/refactoring",
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete is False
    assert any("must target personal/" in error for error in report.errors)


def test_book_intake_rejects_legacy_private_processing_boolean(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(private_processing_authorized=True),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete is False
    assert any("is legacy" in error for error in report.errors)


def test_book_promotion_rejects_blocked_rights_bundle(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    _manifest(
        vault,
        [
            _bundle(
                rights_status="processing-prohibited",
                processing_state="blocked-rights",
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    with pytest.raises(WoonError, match="use book-rights-restore"):
        validate_book_promotion_rights(vault, "personal/book", {"a" * 64})


def test_book_promotion_allows_only_the_authorized_private_archive_hash(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    source_bytes = b"purchased private book"
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    catalog_path = vault / "catalog/sources/official-books.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog["records"][0]["sha256"] = source_hash
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    evidence = _private_rights_evidence()
    evidence["source_archive_sha256"] = source_hash
    archive = vault / str(evidence["source_archive_relative_path"])
    archive.parent.mkdir(parents=True)
    archive.write_bytes(source_bytes)
    _manifest(
        vault,
        [
            _bundle(
                rights_status="user-authorized-private",
                processing_state="content-in-progress",
                rights_evidence=evidence,
            ),
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    validate_book_promotion_rights(vault, "personal/book", {source_hash})

    with pytest.raises(WoonError, match="does not match its private authorization"):
        validate_book_promotion_rights(vault, "personal/book", {"0" * 64})


def test_book_intake_requires_actual_book_archive_name(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _source_catalog(vault)
    book = _bundle()
    book.pop("archive_name")
    _manifest(
        vault,
        [
            book,
            _bundle(
                id="course",
                title="Course",
                source_root="course/",
                kind="course",
                rights_status="official-public",
                processing_state="routed-resource",
                target="resources/course",
            ),
        ],
    )

    report = audit_book_intake(vault)

    assert report.complete is False
    assert any("archive_name is required for books" in error for error in report.errors)


def test_book_intake_rejects_unsafe_manifest_name(tmp_path: Path) -> None:
    report = audit_book_intake(tmp_path, "../../outside")

    assert report.complete is False
    assert report.errors == ("manifest name is invalid",)
