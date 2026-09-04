"""Move raw evidence under the one Wiki-owned private source boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.io import atomic_write

# ``SOURCE_ARCHIVE_RELATIVE`` is the legacy layout.  New code must ask the
# resolver below for a path rather than concatenate this constant directly.
SOURCE_ARCHIVE_RELATIVE = Path("wiki/private/_sources")
PUBLIC_SOURCE_RELATIVE = Path("sources")
PRIVATE_SOURCE_RELATIVE = Path("private")


@dataclass(frozen=True, slots=True)
class SourceBoundaryMigrationReport:
    source_count: int
    file_count: int
    byte_count: int
    sources: tuple[tuple[Path, Path], ...]
    manifest: bytes


def source_storage_layout(vault: Path) -> str:
    """Return the only raw-source layout that can currently be written.

    During this migration a Vault may still use the legacy archive.  A mixed
    tree is deliberately not writable: it requires the hash-complete
    restructure transaction rather than heuristic locator selection.
    """

    root = vault.expanduser().resolve()
    legacy = (root / SOURCE_ARCHIVE_RELATIVE).exists()
    target = (root / PUBLIC_SOURCE_RELATIVE).exists() or (root / PRIVATE_SOURCE_RELATIVE).exists()
    if legacy and target:
        return "mixed"
    if legacy:
        return "legacy"
    if target:
        return "target"
    return "empty"


def private_source_relative(vault: Path, *parts: str) -> Path:
    """Resolve a private source locator without silently mixing layouts."""

    layout = source_storage_layout(vault)
    if layout == "legacy":
        return SOURCE_ARCHIVE_RELATIVE.joinpath(*parts)
    if layout in {"target", "empty"}:
        return PRIVATE_SOURCE_RELATIVE.joinpath(*parts)
    raise WoonError("raw source layout is mixed; complete source-restructure before writing")


def is_private_source_relative(value: str | Path) -> bool:
    """Accept legacy and target private locators only during the transition."""

    candidate = Path(value)
    return candidate.is_relative_to(SOURCE_ARCHIVE_RELATIVE) or candidate.is_relative_to(
        PRIVATE_SOURCE_RELATIVE
    )


def is_raw_source_relative(value: str | Path) -> bool:
    """Recognize either approved raw-source root while migration is in progress."""

    candidate = Path(value)
    return (
        candidate.is_relative_to(SOURCE_ARCHIVE_RELATIVE)
        or candidate.is_relative_to(PUBLIC_SOURCE_RELATIVE)
        or candidate.is_relative_to(PRIVATE_SOURCE_RELATIVE)
    )


def prepare_source_boundary_migration(
    vault: Path, *, external_novel: Path
) -> SourceBoundaryMigrationReport:
    """Hash and validate the two legacy source roots without changing them."""

    root = vault.expanduser().resolve()
    sources = (
        (root / "sources", root / SOURCE_ARCHIVE_RELATIVE / "knowledge"),
        (
            external_novel.expanduser().resolve(),
            root / SOURCE_ARCHIVE_RELATIVE / "novel",
        ),
    )
    records: list[dict[str, object]] = []
    byte_count = 0
    for source, destination in sources:
        if not source.is_dir():
            raise WoonError(f"source boundary migration input is missing: {source}")
        if destination.exists():
            raise WoonError(f"source boundary migration destination exists: {destination}")
        if source == destination or destination.is_relative_to(source):
            raise WoonError("source boundary migration roots overlap")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise WoonError(f"source boundary migration rejects symlink: {path}")
            if not path.is_file():
                continue
            content = path.read_bytes()
            byte_count += len(content)
            records.append(
                {
                    "archive": destination.name,
                    "path": path.relative_to(source).as_posix(),
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
    payload = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "source_count": len(sources),
        "file_count": len(records),
        "byte_count": byte_count,
        "destinations": [destination.relative_to(root).as_posix() for _, destination in sources],
        "files": records,
    }
    manifest = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    return SourceBoundaryMigrationReport(
        source_count=len(sources),
        file_count=len(records),
        byte_count=byte_count,
        sources=sources,
        manifest=manifest,
    )


def apply_source_boundary_migration(vault: Path, report: SourceBoundaryMigrationReport) -> Path:
    """Rename both roots, verify every byte, and write a local receipt or roll back."""

    root = vault.expanduser().resolve()
    archive_root = root / SOURCE_ARCHIVE_RELATIVE
    receipt = root / ".local/woon-knowledge/source-boundary-migration/manifest.json"
    moved: list[tuple[Path, Path]] = []
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        for source, destination in report.sources:
            source.rename(destination)
            moved.append((source, destination))
        _verify_manifest(root, report.manifest)
        atomic_write(receipt, report.manifest, mode=0o600)
    except Exception:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                destination.rename(source)
        if archive_root.is_dir() and not any(archive_root.iterdir()):
            archive_root.rmdir()
        raise
    return receipt


def audit_source_boundary(vault: Path, *, legacy_novel: Path | None = None) -> tuple[str, ...]:
    """Validate placement and the recorded byte inventory without mutating files."""

    root = vault.expanduser().resolve()
    issues: list[str] = []
    if (root / "sources").exists():
        issues.append("legacy sources/ exists outside wiki/private/_sources")
    if legacy_novel is not None and legacy_novel.expanduser().resolve().exists():
        issues.append("legacy external Novel source root still exists")
    for name in ("knowledge", "novel"):
        if not (root / SOURCE_ARCHIVE_RELATIVE / name).is_dir():
            issues.append(f"missing Wiki-owned source archive: {name}")
    receipt = root / ".local/woon-knowledge/source-boundary-migration/manifest.json"
    if not receipt.is_file():
        issues.append("source boundary migration receipt is missing")
        return tuple(issues)
    try:
        payload = json.loads(receipt.read_bytes())
        destinations = payload.get("destinations")
        if destinations != [
            "wiki/private/_sources/knowledge",
            "wiki/private/_sources/novel",
        ]:
            issues.append("source boundary migration receipt has unexpected destinations")
        if not isinstance(payload.get("files"), list) or not payload["files"]:
            issues.append("source boundary migration receipt has no file inventory")
    except (OSError, ValueError, TypeError, WoonError) as error:
        issues.append(str(error))
    return tuple(issues)


def _verify_manifest(vault: Path, manifest: bytes) -> None:
    payload = json.loads(manifest)
    files = payload.get("files")
    if not isinstance(files, list):
        raise WoonError("source boundary migration receipt has no file inventory")
    expected: dict[tuple[str, str], tuple[int, str]] = {}
    for item in files:
        if not isinstance(item, dict):
            raise WoonError("source boundary migration receipt entry is invalid")
        key = (str(item["archive"]), str(item["path"]))
        expected[key] = (int(item["bytes"]), str(item["sha256"]))
    actual: dict[tuple[str, str], tuple[int, str]] = {}
    for archive in ("knowledge", "novel"):
        source = vault / SOURCE_ARCHIVE_RELATIVE / archive
        for path in sorted(source.rglob("*")) if source.is_dir() else ():
            if not path.is_file():
                continue
            content = path.read_bytes()
            actual[(archive, path.relative_to(source).as_posix())] = (
                len(content),
                hashlib.sha256(content).hexdigest(),
            )
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        added = sorted(set(actual) - set(expected))
        changed = sorted(key for key in set(expected) & set(actual) if expected[key] != actual[key])
        raise WoonError(
            "source boundary byte inventory mismatch: "
            f"missing={len(missing)} added={len(added)} changed={len(changed)}"
        )
