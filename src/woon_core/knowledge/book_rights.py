"""Fail-closed request contract for demoting rights-blocked book content."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.book_contract import (
    PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256,
    PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
)

SCHEMA_VERSION = 2
RESTORE_SCHEMA_VERSION = 1
PRIVATE_AUTHORIZATION_DECISION = "user-authorized-private"
PRIVATE_AUTHORIZATION_OWNERSHIP = "user-purchased-copy"
PRIVATE_AUTHORIZATION_SCOPE = "source-landed-private-local-only"
PRIVATE_AUTHORIZATION_RESTRICTIONS = (
    "external-transmission-prohibited",
    "model-training-prohibited",
    "publication-prohibited",
    "redistribution-prohibited",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class BookRightsDemotion:
    """One hash-pinned, reversible book demotion request."""

    book_id: str
    rights_evidence: dict[str, str]
    survivor_page_ids: tuple[str, ...]
    retire_page_ids: tuple[str, ...]
    retire_replacements: dict[str, str]
    survivor_navigation_groups: dict[str, list[dict[str, Any]]]
    survivor_bodies: dict[str, str]
    survivor_body_sha256: dict[str, str]
    affected_source_ids: tuple[str, ...]
    affected_claim_ids: tuple[str, ...]
    expected_revisions: dict[str, str]
    expected_output_sha256: dict[str, str]
    expected_source_body_sha256: dict[str, str]
    expected_asset_sha256: dict[str, str]
    coverage: dict[str, Any]
    book_intake: dict[str, str]
    quarantine_relative_path: str

    @property
    def survivor_ids(self) -> tuple[str, ...]:
        return self.survivor_page_ids

    @property
    def target_ids(self) -> tuple[str, ...]:
        return (*self.survivor_ids, *self.retire_page_ids)


@dataclass(frozen=True, slots=True)
class BookRightsDemotionReport:
    """Observable result of a preflight or applied rights demotion."""

    ready: bool
    applied: bool
    survivor_count: int
    retired_page_count: int
    archived_source_count: int
    superseded_claim_count: int
    quarantined_output_count: int
    quarantined_asset_count: int
    quarantine_relative_path: str
    rights_source_count: int
    rights_claim_count: int


@dataclass(frozen=True, slots=True)
class BookRightsRestoration:
    """One explicit, private-only authorization to restore verified book pages."""

    book_id: str
    rights_evidence: dict[str, Any]
    book_intake: dict[str, str]
    quarantine_manifests: tuple[dict[str, str], ...]


@dataclass(frozen=True, slots=True)
class BookRightsRestorationReport:
    """Observable result of a private rights restoration preflight or write."""

    ready: bool
    applied: bool
    page_count: int
    coverage_mode: str
    coverage_path: str
    intake_relative_path: str
    quarantine_manifest_count: int
    staged_asset_count: int
    unchanged_asset_count: int
    rights_status: str = PRIVATE_AUTHORIZATION_DECISION


def load_book_rights_restoration(raw: object) -> BookRightsRestoration:
    """Load one exact user authorization embedded in a restore payload."""

    fields = {
        "schema_version",
        "rights_contract",
        "book_id",
        "rights_evidence",
        "book_intake",
        "quarantine_manifests",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise WoonError("book rights restore fields are invalid")
    if raw.get("schema_version") != RESTORE_SCHEMA_VERSION:
        raise WoonError(f"book rights restore schema_version must be {RESTORE_SCHEMA_VERSION}")
    contract = raw.get("rights_contract")
    if not isinstance(contract, dict) or set(contract) != {"version", "sha256"}:
        raise WoonError("book rights restore rights_contract fields are invalid")
    if contract.get("version") != PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION:
        raise WoonError("book rights restore rights_contract version is stale")
    if contract.get("sha256") != PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256:
        raise WoonError("book rights restore rights_contract hash is stale")
    book_id = _restore_text(raw.get("book_id"), "book_id")
    if not book_id.startswith("personal/"):
        raise WoonError("book rights restore book_id must stay under personal/")

    evidence = raw.get("rights_evidence")
    evidence_fields = {
        "source_archive_relative_path",
        "source_archive_sha256",
        "notice_locator",
        "notice_sha256",
        "authorization_receipt_locator",
        "authorization_receipt_sha256",
        "ownership_basis",
        "authorized_on",
        "authorized_scope",
        "decision",
        "restrictions",
    }
    if not isinstance(evidence, dict) or set(evidence) != evidence_fields:
        raise WoonError("book rights restore evidence fields are invalid")
    normalized_evidence = dict(evidence)
    for key in evidence_fields.difference({"restrictions"}):
        normalized_evidence[key] = _restore_text(evidence.get(key), f"rights_evidence.{key}")
    for key in (
        "source_archive_sha256",
        "notice_sha256",
        "authorization_receipt_sha256",
    ):
        _restore_digest(str(normalized_evidence[key]), f"rights_evidence.{key}")
    _restore_safe_relative(
        str(normalized_evidence["source_archive_relative_path"]),
        "source archive",
        prefix=("wiki", "private", "_sources", "knowledge", "local-only"),
    )
    for key in ("notice_locator", "authorization_receipt_locator"):
        locator = str(normalized_evidence[key])
        if locator.startswith(("/", "~")):
            raise WoonError(f"book rights restore {key} must not expose a machine path")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(normalized_evidence["authorized_on"])) is None:
        raise WoonError("book rights restore authorized_on must be YYYY-MM-DD")
    if normalized_evidence["decision"] != PRIVATE_AUTHORIZATION_DECISION:
        raise WoonError(f"book rights restore decision must be {PRIVATE_AUTHORIZATION_DECISION}")
    if normalized_evidence["ownership_basis"] != PRIVATE_AUTHORIZATION_OWNERSHIP:
        raise WoonError("book rights restore ownership_basis must identify a user-purchased copy")
    if normalized_evidence["authorized_scope"] != PRIVATE_AUTHORIZATION_SCOPE:
        raise WoonError(
            f"book rights restore authorized_scope must be {PRIVATE_AUTHORIZATION_SCOPE}"
        )
    restrictions = evidence.get("restrictions")
    if restrictions != list(PRIVATE_AUTHORIZATION_RESTRICTIONS):
        raise WoonError(
            "book rights restore restrictions must preserve the exact private-only boundary"
        )
    normalized_evidence["restrictions"] = list(PRIVATE_AUTHORIZATION_RESTRICTIONS)

    intake = raw.get("book_intake")
    if not isinstance(intake, dict) or set(intake) != {
        "relative_path",
        "expected_sha256",
        "bundle_id",
    }:
        raise WoonError("book rights restore book_intake fields are invalid")
    normalized_intake = {
        key: _restore_text(intake.get(key), f"book_intake.{key}") for key in intake
    }
    _restore_safe_relative(
        normalized_intake["relative_path"],
        "book intake manifest",
        prefix=("catalog", "book-intake"),
    )
    _restore_digest(normalized_intake["expected_sha256"], "book_intake.expected_sha256")

    raw_quarantines = raw.get("quarantine_manifests")
    if not isinstance(raw_quarantines, list):
        raise WoonError("book rights restore quarantine_manifests must be an array")
    quarantines: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw_quarantines):
        if not isinstance(item, dict) or set(item) != {"relative_path", "expected_sha256"}:
            raise WoonError(f"book rights restore quarantine_manifests[{index}] fields are invalid")
        relative = _restore_text(item.get("relative_path"), "quarantine relative_path")
        digest = _restore_text(item.get("expected_sha256"), "quarantine expected_sha256")
        _restore_safe_relative(
            relative,
            "quarantine manifest",
            prefix=("wiki", "private", "_sources", "knowledge", "local-only"),
        )
        if "/rights-quarantine/" not in relative or not relative.endswith("/manifest.json"):
            raise WoonError(
                "book rights restore quarantine manifest must be a rights-quarantine manifest"
            )
        _restore_digest(digest, "quarantine expected_sha256")
        if relative in seen_paths:
            raise WoonError("book rights restore quarantine manifests must be unique")
        seen_paths.add(relative)
        quarantines.append({"relative_path": relative, "expected_sha256": digest})

    return BookRightsRestoration(
        book_id=book_id,
        rights_evidence=normalized_evidence,
        book_intake=normalized_intake,
        quarantine_manifests=tuple(quarantines),
    )


def load_book_rights_demotion(path: Path) -> tuple[bool, BookRightsDemotion]:
    """Load one exact JSON request without accepting aliases or implicit defaults."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("book rights demotion input is invalid JSON") from error
    fields = {
        "apply",
        "schema_version",
        "book_id",
        "rights_evidence",
        "survivor_page_ids",
        "retire_page_ids",
        "retire_replacements",
        "survivor_navigation_groups",
        "survivor_bodies",
        "survivor_body_sha256",
        "affected_source_ids",
        "affected_claim_ids",
        "expected_revisions",
        "expected_output_sha256",
        "expected_source_body_sha256",
        "expected_asset_sha256",
        "coverage",
        "book_intake",
        "quarantine_relative_path",
    }
    if not isinstance(raw, dict) or set(raw) != fields:
        raise WoonError("book rights demotion input fields are invalid")
    apply = raw.get("apply")
    if not isinstance(apply, bool):
        raise WoonError("book rights demotion apply must be true or false")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise WoonError(f"book rights demotion schema_version must be {SCHEMA_VERSION}")

    book_id = _text(raw.get("book_id"), "book_id")
    rights = _string_map(raw.get("rights_evidence"), "rights_evidence")
    if set(rights) != {
        "source_archive_relative_path",
        "source_archive_sha256",
        "notice_locator",
        "notice_sha256",
        "notice_summary",
        "decision",
        "reviewed_on",
    }:
        raise WoonError("book rights evidence fields are invalid")
    if rights["decision"] != "blocked-rights":
        raise WoonError("book rights evidence decision must be blocked-rights")
    _digest(rights["source_archive_sha256"], "source_archive_sha256")
    _digest(rights["notice_sha256"], "notice_sha256")
    _safe_relative(
        rights["source_archive_relative_path"],
        "source archive",
        prefix=("wiki", "private", "_sources", "knowledge", "local-only"),
    )
    if rights["notice_locator"].startswith(("/", "~")):
        raise WoonError("book rights notice_locator must not expose a machine path")

    survivors = _string_tuple(raw.get("survivor_page_ids"), "survivor_page_ids")
    retirees = _string_tuple(raw.get("retire_page_ids"), "retire_page_ids")
    replacements = _string_map(
        raw.get("retire_replacements"),
        "retire_replacements",
        allow_empty=True,
    )
    navigation_groups = _navigation_group_map(raw.get("survivor_navigation_groups"))
    survivor_bodies = _body_map(raw.get("survivor_bodies"))
    survivor_body_sha256 = _digest_map(raw.get("survivor_body_sha256"), "survivor_body_sha256")
    sources = _string_tuple(raw.get("affected_source_ids"), "affected_source_ids")
    claims = _string_tuple(raw.get("affected_claim_ids"), "affected_claim_ids")
    if not survivors or not retirees or not sources or not claims:
        raise WoonError("book rights demotion target sets must be non-empty")
    all_ids = (*survivors, *retirees)
    if len(set(all_ids)) != len(all_ids):
        raise WoonError("book rights demotion page targets must be disjoint")
    if not all(page_id == book_id or page_id.startswith(book_id + "/") for page_id in all_ids):
        raise WoonError("book rights demotion page target is outside book_id")
    if not set(replacements).issubset(set(retirees)):
        raise WoonError("book rights replacement keys must be retired pages")
    if not set(replacements.values()).issubset(set(survivors)):
        raise WoonError("book rights replacements must target surviving pages")
    if not set(navigation_groups).issubset(set(survivors)):
        raise WoonError("book rights navigation replacements must target surviving pages")
    if set(survivor_bodies) != set(survivors):
        raise WoonError("book rights survivor_bodies must match surviving pages")
    if set(survivor_body_sha256) != set(survivors):
        raise WoonError("book rights survivor_body_sha256 must match surviving pages")
    for page_id, body in survivor_bodies.items():
        if survivor_body_sha256[page_id] != _sha256_text(body):
            raise WoonError(f"book rights survivor body hash does not match: {page_id}")

    revisions = _digest_map(raw.get("expected_revisions"), "expected_revisions")
    output_hashes = _digest_map(raw.get("expected_output_sha256"), "expected_output_sha256")
    source_hashes = _digest_map(
        raw.get("expected_source_body_sha256"), "expected_source_body_sha256"
    )
    asset_hashes = _digest_map(raw.get("expected_asset_sha256"), "expected_asset_sha256")
    if set(revisions) != set(all_ids) or set(output_hashes) != set(all_ids):
        raise WoonError("book rights demotion page revisions and output hashes must match targets")
    if set(source_hashes) != set(sources):
        raise WoonError("book rights demotion source body hashes must match affected sources")
    for relative_path in asset_hashes:
        _safe_relative(relative_path, "affected asset", prefix=("assets",))

    coverage = raw.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {
        "relative_path",
        "expected_sha256",
        "replacement",
    }:
        raise WoonError("book rights demotion coverage fields are invalid")
    _safe_relative(
        _text(coverage.get("relative_path"), "coverage.relative_path"),
        "coverage manifest",
        prefix=("catalog", "book-coverage"),
    )
    _digest(_text(coverage.get("expected_sha256"), "coverage.expected_sha256"), "coverage")
    if not isinstance(coverage.get("replacement"), dict):
        raise WoonError("book rights demotion coverage replacement must be an object")

    intake = _string_map(raw.get("book_intake"), "book_intake")
    if set(intake) != {"relative_path", "expected_sha256", "bundle_id"}:
        raise WoonError("book rights demotion book_intake fields are invalid")
    _safe_relative(
        intake["relative_path"],
        "book intake manifest",
        prefix=("catalog", "book-intake"),
    )
    _digest(intake["expected_sha256"], "book_intake.expected_sha256")

    quarantine = _text(raw.get("quarantine_relative_path"), "quarantine_relative_path")
    _safe_relative(
        quarantine,
        "rights quarantine",
        prefix=("wiki", "private", "_sources", "knowledge", "local-only"),
    )
    if "/rights-quarantine/" not in quarantine:
        raise WoonError("book rights quarantine path must contain rights-quarantine")

    return apply, BookRightsDemotion(
        book_id=book_id,
        rights_evidence=rights,
        survivor_page_ids=survivors,
        retire_page_ids=retirees,
        retire_replacements=replacements,
        survivor_navigation_groups=navigation_groups,
        survivor_bodies=survivor_bodies,
        survivor_body_sha256=survivor_body_sha256,
        affected_source_ids=sources,
        affected_claim_ids=claims,
        expected_revisions=revisions,
        expected_output_sha256=output_hashes,
        expected_source_body_sha256=source_hashes,
        expected_asset_sha256=asset_hashes,
        coverage=coverage,
        book_intake=intake,
        quarantine_relative_path=quarantine,
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"book rights demotion {label} must be a non-empty string")
    return value.strip()


def _string_tuple(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
    require_sorted: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise WoonError(f"book rights demotion {label} must be a non-empty string array")
    items = tuple(_text(item, label) for item in value)
    if len(set(items)) != len(items) or (require_sorted and list(items) != sorted(items)):
        suffix = "unique and sorted" if require_sorted else "unique"
        raise WoonError(f"book rights demotion {label} must be {suffix}")
    return items


def _string_map(value: object, label: str, *, allow_empty: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WoonError(f"book rights demotion {label} must be an object")
    if not value and not allow_empty:
        raise WoonError(f"book rights demotion {label} must not be empty")
    return {_text(key, label): _text(item, label) for key, item in value.items()}


def _navigation_group_map(value: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise WoonError("book rights demotion survivor_navigation_groups must be an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for page_id, raw_groups in value.items():
        normalized_id = _text(page_id, "survivor_navigation_groups")
        if not isinstance(raw_groups, list):
            raise WoonError("book rights navigation replacement must be an array")
        groups: list[dict[str, Any]] = []
        labels: set[str] = set()
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict) or set(raw_group) != {"label", "children"}:
                raise WoonError("book rights navigation group fields are invalid")
            label = _text(raw_group.get("label"), "navigation label")
            if label.casefold() in labels:
                raise WoonError("book rights navigation labels must be unique per page")
            labels.add(label.casefold())
            children = _string_tuple(
                raw_group.get("children"),
                "navigation children",
                allow_empty=False,
                require_sorted=False,
            )
            groups.append({"label": label, "children": list(children)})
        result[normalized_id] = groups
    return result


def _body_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WoonError("book rights demotion survivor_bodies must be an object")
    result: dict[str, str] = {}
    for page_id, body in value.items():
        normalized_id = _text(page_id, "survivor_bodies")
        if not isinstance(body, str):
            raise WoonError("book rights survivor body must be a string")
        result[normalized_id] = body
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_map(value: object, label: str) -> dict[str, str]:
    result = _string_map(value, label, allow_empty=True)
    for digest in result.values():
        _digest(digest, label)
    return result


def _digest(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise WoonError(f"book rights demotion {label} must be a lowercase SHA-256")


def _safe_relative(value: str, label: str, *, prefix: tuple[str, ...]) -> None:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != value
        or ".." in candidate.parts
        or candidate.parts[: len(prefix)] != prefix
    ):
        raise WoonError(f"book rights demotion {label} path is unsafe")


def _restore_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"book rights restore {label} must be a non-empty string")
    return value.strip()


def _restore_digest(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise WoonError(f"book rights restore {label} must be a lowercase SHA-256")


def _restore_safe_relative(value: str, label: str, *, prefix: tuple[str, ...]) -> None:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != value
        or ".." in candidate.parts
        or candidate.parts[: len(prefix)] != prefix
    ):
        raise WoonError(f"book rights restore {label} path is unsafe")
