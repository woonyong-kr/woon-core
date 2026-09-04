"""Read-only inventory for moving raw source bytes out of the Wiki tree."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

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
    issues: tuple[str, ...]


def render_source_restructure_template(vault: Path) -> bytes:
    """Create one hash-complete source manifest without moving a byte.

    Only prefixes with an unambiguous source boundary receive a destination.
    Mixed imports and prior ``local-only`` corpora remain review records until
    their catalog-level storage scope has been reconciled.
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
        issues=tuple(issues),
    )


def _default_destination(relative: str) -> tuple[str, str | None, str]:
    prefixes = (
        ("knowledge/web/", "sources/knowledge/web/", "public-tracked"),
        ("novel/", "private/novel/", "private-tracked"),
        ("codex/", "private/codex/", "local-only"),
        ("legacy-wiki/", "private/legacy-wiki/", "private-tracked"),
    )
    for current, target, scope in prefixes:
        if relative.startswith(current):
            return "move", target + relative.removeprefix(current), scope
    return "review", None, "review"


def _relative(value: object, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()
