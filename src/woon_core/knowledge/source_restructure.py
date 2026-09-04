"""Read-only inventory for moving raw source bytes out of the Wiki tree."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write

LEGACY_SOURCE_ROOT = Path("wiki/private/_sources")


@dataclass(frozen=True, slots=True)
class SourceRestructurePreflight:
    """Completeness and hash result for raw-source relocation instructions."""

    file_count: int
    byte_count: int
    disposition_counts: dict[str, int]
    catalog_pending_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceCatalogReferenceAudit:
    """Every catalog reference to one legacy raw-source byte path.

    This is deliberately read-only.  It is the evidence required before a
    relocation manifest can mark a source record as catalog-reconciled.
    """

    file_count: int
    catalog_record_count: int
    reference_count: int
    orphan_count: int
    duplicate_primary_count: int
    stale_reference_count: int
    issues: tuple[str, ...]
    records: tuple[dict[str, object], ...]


def render_source_restructure_template(vault: Path) -> bytes:
    """Create one hash-complete source manifest without moving a byte.

    Public web snapshots move below ``sources/``. Everything else stays private
    by default; the user-approved private Git boundary does not make it public.
    Every record still starts with catalog reconciliation pending, because a
    byte move without updating locator-bearing catalog and receipt records is
    unsafe.
    """

    root = vault.expanduser().resolve()
    source_root = root / LEGACY_SOURCE_ROOT
    if not source_root.is_dir():
        raise WoonError(f"legacy raw source root is missing: {source_root}")
    records: list[dict[str, object]] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise WoonError(f"raw source restructure rejects symlink: {path}")
        if not path.is_file():
            continue
        current = path.relative_to(root).as_posix()
        relative = path.relative_to(source_root).as_posix()
        disposition, target, storage_scope = _default_destination(relative)
        record: dict[str, object] = {
            "current_path": current,
            "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "storage_scope": storage_scope,
            "disposition": disposition,
            "catalog_reconciliation": "pending",
        }
        if target is not None:
            record["target_path"] = target
        records.append(record)
    return yaml.safe_dump(
        {"version": 1, "records": records}, allow_unicode=True, sort_keys=False, width=100
    ).encode("utf-8")


def write_source_restructure_template(vault: Path, output_path: Path) -> Path:
    """Write a local inventory and never overwrite an in-progress review."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/source-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "source restructure template must stay below .local/woon-knowledge/source-restructure"
        )
    if output.exists():
        raise WoonError(f"source restructure template already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_source_restructure_template(root), mode=0o600)
    return output


def audit_source_catalog_references(vault: Path) -> SourceCatalogReferenceAudit:
    """Inventory catalog locators before relocating raw source bytes.

    ``catalog/sources`` owns source records through ``records[*].target``.
    All YAML and JSON catalog documents are also scanned for legacy locators so
    claims, page specifications, and receipts cannot silently retain a path
    that will disappear.  A source can have many references but must have no
    more than one primary catalog owner.
    """

    root = vault.expanduser().resolve()
    source_root = root / LEGACY_SOURCE_ROOT
    catalog_root = root / "catalog"
    if not source_root.is_dir():
        raise WoonError(f"legacy raw source root is missing: {source_root}")
    if not catalog_root.is_dir():
        raise WoonError(f"catalog root is missing: {catalog_root}")

    paths = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in source_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
    )
    active = set(paths)
    primary_owners: dict[str, list[str]] = {path: [] for path in paths}
    references: dict[str, list[str]] = {path: [] for path in paths}
    issues: list[str] = []
    catalog_record_count = 0

    for document in sorted(catalog_root.rglob("*")):
        if not document.is_file() or document.is_symlink():
            continue
        suffix = document.suffix.lower()
        if suffix not in {".yaml", ".yml", ".json"}:
            continue
        relative_document = document.relative_to(root).as_posix()
        try:
            payload = _load_catalog_document(document)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
            issues.append(f"catalog document is unreadable: {relative_document}: {error}")
            continue
        for scalar_path, value in _scalar_strings(payload):
            for source_path, locator in _legacy_locators(value):
                label = f"{relative_document}:{scalar_path}"
                if source_path not in active:
                    issues.append(f"stale raw-source locator {locator} at {label}")
                    continue
                references[source_path].append(f"{label}={locator}")
        if document.parent == catalog_root / "sources":
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                issues.append(f"source catalog has no records list: {relative_document}")
                continue
            for index, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    issues.append(
                        f"invalid source catalog record: {relative_document}:records[{index}]"
                    )
                    continue
                catalog_record_count += 1
                target = record.get("target")
                if not isinstance(target, str):
                    continue
                for source_path, _locator in _legacy_locators(target):
                    if source_path in active:
                        source_id = record.get("source_id", "<missing-source-id>")
                        primary_owners[source_path].append(
                            f"{relative_document}:records[{index}]={source_id}"
                        )

    records: list[dict[str, object]] = []
    orphan_count = 0
    duplicate_primary_count = 0
    for path in paths:
        owners = sorted(primary_owners[path])
        refs = sorted(references[path])
        if not owners:
            orphan_count += 1
        if len(owners) > 1:
            duplicate_primary_count += 1
            issues.append(f"raw source has multiple primary catalog owners: {path}")
        records.append(
            {
                "current_path": path,
                "primary_catalog_owners": owners,
                "catalog_references": refs,
                "catalog_reconciliation": "reconciled" if len(owners) == 1 else "pending",
            }
        )
    stale_reference_count = sum(issue.startswith("stale raw-source locator ") for issue in issues)
    return SourceCatalogReferenceAudit(
        file_count=len(paths),
        catalog_record_count=catalog_record_count,
        reference_count=sum(len(items) for items in references.values()),
        orphan_count=orphan_count,
        duplicate_primary_count=duplicate_primary_count,
        stale_reference_count=stale_reference_count,
        issues=tuple(issues),
        records=tuple(records),
    )


def render_source_catalog_reference_audit(
    vault: Path, report: SourceCatalogReferenceAudit | None = None
) -> bytes:
    """Render a full, local-only locator inventory for human review."""

    report = report or audit_source_catalog_references(vault)
    return (
        json.dumps(
            {
                "version": 1,
                "file_count": report.file_count,
                "catalog_record_count": report.catalog_record_count,
                "reference_count": report.reference_count,
                "orphan_count": report.orphan_count,
                "duplicate_primary_count": report.duplicate_primary_count,
                "stale_reference_count": report.stale_reference_count,
                "issues": list(report.issues),
                "records": list(report.records),
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def write_source_catalog_reference_audit(
    vault: Path, output_path: Path, report: SourceCatalogReferenceAudit | None = None
) -> Path:
    """Write a locator inventory below the local-only restructure workspace."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/source-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "source catalog reference audit must stay below "
            ".local/woon-knowledge/source-restructure"
        )
    if output.exists():
        raise WoonError(f"source catalog reference audit already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_source_catalog_reference_audit(root, report), mode=0o600)
    return output


def prepare_source_restructure_preflight(
    vault: Path, manifest_path: Path
) -> SourceRestructurePreflight:
    """Verify one raw-source manifest before catalog or filesystem mutation."""

    root = vault.expanduser().resolve()
    source_root = root / LEGACY_SOURCE_ROOT
    manifest = manifest_path.expanduser().resolve()
    if not manifest.is_file():
        raise WoonError(f"source restructure manifest is missing: {manifest}")
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"source restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError("source restructure manifest must use version: 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("source restructure manifest requires a records list")
    paths = tuple(sorted(source_root.rglob("*")))
    active = {
        path.relative_to(root).as_posix(): path
        for path in paths
        if path.is_file() and not path.is_symlink()
    }
    counts: dict[str, int] = {}
    catalog_pending_count = 0
    issues = [
        f"raw source restructure rejects symlink: {path.relative_to(root).as_posix()}"
        for path in paths
        if path.is_symlink()
    ]
    seen: set[str] = set()
    targets: dict[str, str] = {}
    for index, record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        current = _relative(record.get("current_path"), label, "current_path", issues)
        disposition = record.get("disposition")
        if disposition not in {"move", "review"}:
            issues.append(f"{label}: disposition must be move or review")
            continue
        counts[disposition] = counts.get(disposition, 0) + 1
        if current is None:
            continue
        if current in seen:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active raw source: {current}")
            continue
        if record.get("current_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
            issues.append(f"{label}: current_sha256 does not match: {current}")
        if record.get("bytes") != source.stat().st_size:
            issues.append(f"{label}: byte count does not match: {current}")
        scope = record.get("storage_scope")
        if scope not in {"public-tracked", "private-tracked", "local-only", "review"}:
            issues.append(f"{label}: invalid storage_scope {scope!r}")
        catalog_reconciliation = record.get("catalog_reconciliation")
        if catalog_reconciliation not in {"pending", "reconciled", "not-required"}:
            issues.append(f"{label}: invalid catalog_reconciliation {catalog_reconciliation!r}")
        elif catalog_reconciliation == "pending":
            catalog_pending_count += 1
        target = record.get("target_path")
        if disposition == "move":
            target_path = _relative(target, label, "target_path", issues)
            if target_path is None:
                continue
            if not target_path.startswith(("sources/", "private/")):
                issues.append(f"{label}: target_path must be below sources/ or private/")
                continue
            previous = targets.setdefault(target_path, current)
            if previous != current:
                issues.append(f"{label}: target_path collision {target_path} with {previous}")
        elif target not in {None, ""}:
            issues.append(f"{label}: review record must not define target_path")
    missing = set(active) - seen
    if missing:
        issues.append(f"manifest omits {len(missing)} active raw source files")
    extra = seen - set(active)
    if extra:
        issues.append(f"manifest names {len(extra)} non-active raw source files")
    return SourceRestructurePreflight(
        file_count=len(active),
        byte_count=sum(path.stat().st_size for path in active.values()),
        disposition_counts=counts,
        catalog_pending_count=catalog_pending_count,
        issues=tuple(issues),
    )


def _default_destination(relative: str) -> tuple[str, str | None, str]:
    prefixes = (
        ("knowledge/web/", "sources/knowledge/web/", "public-tracked"),
        ("knowledge/", "private/knowledge/", "private-tracked"),
        ("novel/", "private/novel/", "private-tracked"),
        ("codex/", "private/codex/", "local-only"),
        ("legacy-wiki/", "private/legacy-wiki/", "private-tracked"),
    )
    for current, target, scope in prefixes:
        if relative.startswith(current):
            return "move", target + relative.removeprefix(current), scope
    return "review", None, "review"


def _load_catalog_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _scalar_strings(value: object, prefix: str = "$") -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((prefix, value),)
    if isinstance(value, list):
        result: list[tuple[str, str]] = []
        for index, child in enumerate(value):
            result.extend(_scalar_strings(child, f"{prefix}[{index}]"))
        return tuple(result)
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            result.extend(_scalar_strings(child, f"{prefix}.{key}"))
        return tuple(result)
    return ()


def _legacy_locators(value: str) -> tuple[tuple[str, str], ...]:
    """Extract a raw byte path and full locator (including an optional fragment)."""

    marker = LEGACY_SOURCE_ROOT.as_posix() + "/"
    locators: list[tuple[str, str]] = []
    start = 0
    while True:
        offset = value.find(marker, start)
        if offset < 0:
            break
        end = len(value)
        for delimiter in ("\n", "\r", "`", '"', "'", ")", "]", ">", "|"):
            candidate = value.find(delimiter, offset)
            if candidate >= 0:
                end = min(end, candidate)
        locator = value[offset:end].strip().rstrip(".,;:")
        source_path = locator.partition("#")[0]
        if source_path:
            locators.append((source_path, locator))
        start = offset + len(marker)
    return tuple(locators)


def _relative(value: object, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()
