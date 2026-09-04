"""Audit complete routing of a source catalog into book and learning bundles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.book_rights import (
    PRIVATE_AUTHORIZATION_DECISION,
    PRIVATE_AUTHORIZATION_OWNERSHIP,
    PRIVATE_AUTHORIZATION_RESTRICTIONS,
    PRIVATE_AUTHORIZATION_SCOPE,
)
from woon_core.knowledge.source_boundary import (
    SOURCE_ARCHIVE_RELATIVE,
    private_source_relative,
    source_storage_layout,
)
from woon_core.knowledge.source_catalog import load_source_catalog

SCHEMA_VERSION = 1
KINDS = {"book", "extract", "course", "standard", "paper", "tutorial", "inventory"}
RIGHTS_STATES = {
    "official-public",
    "official-public-restricted-translation",
    "processing-prohibited",
    "user-authorized-private",
    "user-provided-private",
    "unverified-commercial",
}
PROCESSING_STATES = {
    "queued-structure",
    "structure-in-progress",
    "structure-verified",
    "content-in-progress",
    "blocked-rights",
    "routed-resource",
    "complete",
}


@dataclass(frozen=True, slots=True)
class BookIntakeAudit:
    source_count: int
    bundle_count: int
    assigned_count: int
    unassigned_count: int
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.errors


def audit_book_intake(vault: Path, manifest_name: str = "official-books") -> BookIntakeAudit:
    """Require every source catalog record to belong to exactly one declared bundle."""

    vault = vault.expanduser().resolve()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", manifest_name):
        return BookIntakeAudit(0, 0, 0, 0, ("manifest name is invalid",))
    manifest_path = vault / f"catalog/book-intake/{manifest_name}.json"
    if not manifest_path.is_file():
        return BookIntakeAudit(0, 0, 0, 0, (f"{manifest_path.relative_to(vault)} is missing",))
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return BookIntakeAudit(
            0,
            0,
            0,
            0,
            (f"{manifest_path.relative_to(vault)} is invalid: {error}",),
        )
    if not isinstance(payload, dict):
        return BookIntakeAudit(0, 0, 0, 0, ("book intake manifest must be an object",))

    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    source_catalog = payload.get("source_catalog")
    if not isinstance(source_catalog, str) or not source_catalog.strip():
        errors.append("source_catalog is required")
        return BookIntakeAudit(0, 0, 0, 0, tuple(errors))
    try:
        catalog_path = _safe_catalog_path(vault, source_catalog)
        catalog = load_source_catalog(catalog_path)
    except (OSError, WoonError, ValueError) as error:
        errors.append(f"source catalog is invalid: {error}")
        return BookIntakeAudit(0, 0, 0, 0, tuple(errors))

    raw_bundles = payload.get("bundles")
    if not isinstance(raw_bundles, list) or not raw_bundles:
        errors.append("bundles must contain at least one item")
        return BookIntakeAudit(len(catalog.records), 0, 0, len(catalog.records), tuple(errors))

    bundle_ids: set[str] = set()
    roots: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_bundles):
        label = f"bundles[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be an object")
            continue
        bundle_id = _required_text(raw, "id", label, errors)
        title = _required_text(raw, "title", label, errors)
        root = _required_text(raw, "source_root", label, errors)
        _required_text(raw, "language", label, errors)
        kind = _required_text(raw, "kind", label, errors)
        rights = _required_text(raw, "rights_status", label, errors)
        state = _required_text(raw, "processing_state", label, errors)
        _required_text(raw, "target", label, errors)
        archive_name = raw.get("archive_name")
        priority = raw.get("priority")
        if bundle_id:
            if bundle_id in bundle_ids:
                errors.append(f"{label}: duplicate id {bundle_id}")
            bundle_ids.add(bundle_id)
        if title and title.strip() != title:
            errors.append(f"{label}.title must not have surrounding whitespace")
        if root:
            try:
                _safe_source_root(root)
            except ValueError as error:
                errors.append(f"{label}.source_root is invalid: {error}")
            roots.append((bundle_id or label, root))
        if kind and kind not in KINDS:
            errors.append(f"{label}.kind is invalid: {kind}")
        if kind == "book":
            if not isinstance(archive_name, str) or not archive_name.strip():
                errors.append(f"{label}.archive_name is required for books")
            elif (
                archive_name != archive_name.strip()
                or archive_name in {".", ".."}
                or "/" in archive_name
                or "\\" in archive_name
            ):
                errors.append(
                    f"{label}.archive_name must be one safe actual book file or directory name"
                )
        if rights and rights not in RIGHTS_STATES:
            errors.append(f"{label}.rights_status is invalid: {rights}")
        if state and state not in PROCESSING_STATES:
            errors.append(f"{label}.processing_state is invalid: {state}")
        if rights == "processing-prohibited" and state != "blocked-rights":
            errors.append(f"{label}: processing-prohibited material must remain blocked-rights")
        if not isinstance(priority, int) or priority < 1:
            errors.append(f"{label}.priority must be a positive integer")
        if "private_processing_authorized" in raw:
            errors.append(
                f"{label}.private_processing_authorized is legacy; use "
                "rights_status=user-authorized-private with hash-pinned rights_evidence"
            )
        if rights == "unverified-commercial" and state != "blocked-rights":
            errors.append(f"{label}: unverified commercial material must be blocked-rights")
        if rights == PRIVATE_AUTHORIZATION_DECISION:
            _audit_private_authorization(vault, raw, label, catalog.records, root, errors)

    assigned_count = 0
    unassigned_count = 0
    for record in catalog.records:
        matches = [bundle_id for bundle_id, root in roots if _matches(record.locator, root)]
        if not matches:
            errors.append(f"unassigned source: {record.locator}")
            unassigned_count += 1
        elif len(matches) > 1:
            errors.append(f"source belongs to multiple bundles: {record.locator}: {matches}")
        else:
            assigned_count += 1

    for bundle_id, root in roots:
        if not any(_matches(record.locator, root) for record in catalog.records):
            errors.append(f"bundle has no source records: {bundle_id}: {root}")

    return BookIntakeAudit(
        source_count=len(catalog.records),
        bundle_count=len(raw_bundles),
        assigned_count=assigned_count,
        unassigned_count=unassigned_count,
        errors=tuple(errors),
    )


def validate_book_promotion_rights(
    vault: Path,
    book_id: str,
    source_sha256: set[str],
    *,
    allow_blocked_restore: bool = False,
) -> None:
    """Fail closed when an intake bundle does not authorize private reader delivery."""

    intake_root = vault.expanduser().resolve() / "catalog/book-intake"
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(intake_root.glob("*.json")) if intake_root.is_dir() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError(f"book promotion intake is invalid: {path.name}: {error}") from error
        bundles = payload.get("bundles") if isinstance(payload, dict) else None
        for bundle in bundles if isinstance(bundles, list) else ():
            if (
                isinstance(bundle, dict)
                and bundle.get("kind") == "book"
                and bundle.get("target") == book_id
            ):
                matches.append((path, bundle))
    if not matches:
        return
    if len(matches) != 1:
        raise WoonError(f"book promotion has ambiguous intake ownership: {book_id}")
    manifest_path, bundle = matches[0]
    rights = bundle.get("rights_status")
    state = bundle.get("processing_state")
    if rights in {"processing-prohibited", "unverified-commercial"}:
        if allow_blocked_restore and state == "blocked-rights":
            return
        raise WoonError(f"book promotion is rights-blocked; use book-rights-restore: {book_id}")
    if rights != PRIVATE_AUTHORIZATION_DECISION:
        return
    report = audit_book_intake(vault, manifest_path.stem)
    if not report.complete:
        raise WoonError(f"book promotion private authorization is invalid: {report.errors[0]}")
    evidence = bundle["rights_evidence"]
    if source_sha256 != {evidence["source_archive_sha256"]}:
        raise WoonError("book promotion source hash does not match its private authorization")
    archive = vault / Path(*PurePosixPath(evidence["source_archive_relative_path"]).parts)
    if archive.is_symlink() or not archive.is_file():
        raise WoonError("book promotion authorized source archive is missing")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != evidence["source_archive_sha256"]:
        raise WoonError("book promotion authorized source archive hash changed")


def _required_text(raw: dict[str, Any], key: str, label: str, errors: list[str]) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label}.{key} is required")
        return ""
    return value.strip()


def _safe_catalog_path(vault: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.suffix not in {".yaml", ".yml"}:
        raise ValueError("must be a safe relative YAML path")
    resolved = (vault / Path(*pure.parts)).resolve()
    resolved.relative_to(vault)
    return resolved


def _safe_source_root(root: str) -> None:
    pure = PurePosixPath(root)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts or root.startswith("./"):
        raise ValueError("must be a safe source-catalog locator or directory prefix")


def _matches(locator: str, root: str) -> bool:
    if root.endswith("/"):
        return locator.startswith(root)
    return locator == root


def _audit_private_authorization(
    vault: Path,
    raw: dict[str, Any],
    label: str,
    records: tuple[Any, ...],
    source_root: str,
    errors: list[str],
) -> None:
    """Require one exact source hash and a durable private-only approval receipt."""

    target = raw.get("target")
    if not isinstance(target, str) or not target.startswith("personal/"):
        errors.append(f"{label}: user-authorized private material must target personal/")
    if raw.get("processing_state") == "blocked-rights":
        errors.append(f"{label}: user-authorized private material must not remain blocked-rights")
    evidence = raw.get("rights_evidence")
    expected_fields = {
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
    if not isinstance(evidence, dict) or set(evidence) != expected_fields:
        errors.append(f"{label}.rights_evidence fields are invalid")
        return
    for key in (
        "source_archive_sha256",
        "notice_sha256",
        "authorization_receipt_sha256",
    ):
        if (
            not isinstance(evidence.get(key), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(evidence[key])) is None
        ):
            errors.append(f"{label}.rights_evidence.{key} must be a lowercase SHA-256")
    if evidence.get("decision") != PRIVATE_AUTHORIZATION_DECISION:
        errors.append(f"{label}.rights_evidence.decision must be user-authorized-private")
    if evidence.get("ownership_basis") != PRIVATE_AUTHORIZATION_OWNERSHIP:
        errors.append(
            f"{label}.rights_evidence.ownership_basis must be {PRIVATE_AUTHORIZATION_OWNERSHIP}"
        )
    if evidence.get("authorized_scope") != PRIVATE_AUTHORIZATION_SCOPE:
        errors.append(
            f"{label}.rights_evidence.authorized_scope must be {PRIVATE_AUTHORIZATION_SCOPE}"
        )
    if (
        not isinstance(evidence.get("authorized_on"), str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(evidence.get("authorized_on"))) is None
    ):
        errors.append(f"{label}.rights_evidence.authorized_on must be YYYY-MM-DD")
    if evidence.get("restrictions") != list(PRIVATE_AUTHORIZATION_RESTRICTIONS):
        errors.append(f"{label}.rights_evidence.restrictions must preserve private-only use")
    for key in ("notice_locator", "authorization_receipt_locator"):
        value = evidence.get(key)
        if not isinstance(value, str) or not value.strip() or value.startswith(("/", "~")):
            errors.append(f"{label}.rights_evidence.{key} must be a stable non-machine locator")
    archive = evidence.get("source_archive_relative_path")
    if not isinstance(archive, str):
        errors.append(f"{label}.rights_evidence.source_archive_relative_path is required")
    else:
        pure = PurePosixPath(archive)
        candidate = Path(*pure.parts)
        expected_root = private_source_relative(vault, "knowledge", "local-only")
        accepts_legacy_bootstrap = source_storage_layout(
            vault
        ) == "empty" and candidate.is_relative_to(
            SOURCE_ARCHIVE_RELATIVE / "knowledge" / "local-only"
        )
        if (
            pure.is_absolute()
            or pure.as_posix() != archive
            or ".." in pure.parts
            or candidate == expected_root
            or (not candidate.is_relative_to(expected_root) and not accepts_legacy_bootstrap)
        ):
            errors.append(
                f"{label}.rights_evidence.source_archive_relative_path must stay local-only"
            )
    matched = [record for record in records if _matches(record.locator, source_root)]
    if len(matched) != 1:
        errors.append(f"{label}: user-authorized private book must map to exactly one source file")
    elif evidence.get("source_archive_sha256") != matched[0].sha256:
        errors.append(f"{label}: authorized source hash does not match the source catalog")
