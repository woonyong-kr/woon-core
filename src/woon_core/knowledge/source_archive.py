"""Atomically move an approved private corpus into the Wiki-owned source boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write

_EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_EXCLUDED_FILES = {".DS_Store", ".gitkeep"}
_SECRET_NAMES = {".env"}


@dataclass(frozen=True, slots=True)
class SourceArchiveResult:
    """Verified outcome of one external-to-Wiki source ownership transfer."""

    source_name: str
    destination: str
    files: int
    excluded: int
    bytes: int
    moved: bool
    receipt: str


def archive_private_source_corpus(
    source: Path,
    vault: Path,
    source_name: str,
    wiki_subject: str,
) -> SourceArchiveResult:
    """Move one exact corpus under ``wiki/private/_sources`` with rollback.

    The catalog and reconciliation ledger contain only safe relative paths and
    content hashes.  If any target byte or metadata write fails, the source
    directory and prior tracked catalogs are restored before the error escapes.
    """

    root = vault.expanduser().resolve()
    origin = source.expanduser().resolve()
    name = _safe_name(source_name)
    subject = _wiki_subject(root, wiki_subject)
    destination_relative = Path("wiki/private/_sources/knowledge/local-only") / name
    destination = root / destination_relative
    _require_disjoint(origin, root)

    catalog_path = root / "catalog/sources" / f"{name}.yaml"
    ledger_path = root / "catalog/reconciliation" / f"{name}.yaml"
    receipt_path = root / ".local/woon-knowledge/source-archive" / f"{name}.json"

    if not origin.exists() and destination.is_dir():
        records, excluded, byte_count = _inventory(destination, name, destination_relative)
        _verify_records(root, records)
        _write_metadata(
            catalog_path,
            ledger_path,
            receipt_path,
            name,
            subject,
            destination_relative,
            records,
            excluded,
            byte_count,
        )
        _verify_catalog(catalog_path, records, subject)
        return SourceArchiveResult(
            source_name=name,
            destination=destination_relative.as_posix(),
            files=len(records),
            excluded=len(excluded),
            bytes=byte_count,
            moved=False,
            receipt=_relative(root, receipt_path),
        )
    if not origin.is_dir():
        raise WoonError(f"private source corpus does not exist: {origin}")
    if destination.exists():
        raise WoonError(f"Wiki-owned source destination already exists: {destination_relative}")

    records, excluded, byte_count = _inventory(origin, name, destination_relative)
    if not records:
        raise WoonError("private source corpus has no active files")
    catalog_bytes = _catalog_bytes(name, subject, records, excluded)
    ledger_bytes = _ledger_bytes(name, subject, records)
    receipt_bytes = _receipt_bytes(
        name, subject, destination_relative, records, excluded, byte_count
    )
    previous = {
        catalog_path: catalog_path.read_bytes() if catalog_path.exists() else None,
        ledger_path: ledger_path.read_bytes() if ledger_path.exists() else None,
        receipt_path: receipt_path.read_bytes() if receipt_path.exists() else None,
    }
    moved = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        origin.rename(destination)
        moved = True
        _verify_records(root, records)
        if origin.exists():
            raise WoonError("external source directory still exists after archive move")
        atomic_write(catalog_path, catalog_bytes)
        atomic_write(ledger_path, ledger_bytes)
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path.parent.chmod(0o700)
        atomic_write(receipt_path, receipt_bytes, mode=0o600)
        _verify_catalog(catalog_path, records, subject)
    except BaseException:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content, mode=0o600 if path == receipt_path else 0o644)
        if moved and destination.exists() and not origin.exists():
            destination.rename(origin)
        raise
    return SourceArchiveResult(
        source_name=name,
        destination=destination_relative.as_posix(),
        files=len(records),
        excluded=len(excluded),
        bytes=byte_count,
        moved=True,
        receipt=_relative(root, receipt_path),
    )


def _inventory(
    source: Path,
    source_name: str,
    destination_root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, str]], int]:
    records: list[dict[str, object]] = []
    excluded: list[dict[str, str]] = []
    byte_count = 0
    for directory, names, filenames in os.walk(source):
        names[:] = sorted(name for name in names if name not in _EXCLUDED_DIRECTORIES)
        base = Path(directory)
        for filename in sorted(filenames):
            path = base / filename
            relative = path.relative_to(source)
            if path.is_symlink():
                raise WoonError(f"private source archive rejects symlink: {relative}")
            if filename in _SECRET_NAMES or filename.startswith(".env."):
                raise WoonError(f"private source archive rejects secret file: {relative}")
            if filename in _EXCLUDED_FILES or path.suffix.lower() == ".pyc":
                excluded.append({"locator": relative.as_posix(), "reason": "placeholder-or-cache"})
                continue
            content_hash = _sha256(path)
            size = path.stat().st_size
            byte_count += size
            target = (destination_root / relative).as_posix()
            records.append(
                {
                    "source_id": f"source://{source_name}/{quote(relative.as_posix(), safe='/')}",
                    "locator": relative.as_posix(),
                    "sha256": content_hash,
                    "size": size,
                    "role": _role(path),
                    "privacy": "private/local-only",
                    "state": "canonical",
                    "target": target,
                    "target_sha256": content_hash,
                }
            )
    return records, excluded, byte_count


def _catalog_bytes(
    source_name: str,
    wiki_subject: str,
    records: list[dict[str, object]],
    excluded: list[dict[str, str]],
) -> bytes:
    payload = {
        "version": 1,
        "source": source_name,
        "wiki_subject": wiki_subject,
        "summary": {"canonical": len(records)},
        "records": records,
        "excluded": excluded,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")


def _ledger_bytes(source_name: str, wiki_subject: str, records: list[dict[str, object]]) -> bytes:
    payload = {
        "version": 1,
        "source": source_name,
        "wiki_subject": wiki_subject,
        "records": [
            {
                "source_id": record["source_id"],
                "locator": record["locator"],
                "source_sha256": record["sha256"],
                "catalog_state": "canonical",
                "action": "move-to-wiki-source",
                "status": "verified",
                "target": record["target"],
                "target_before_sha256": None,
                "target_after_sha256": record["sha256"],
                "attempts": 0,
                "checks": [
                    "source-hash",
                    "target-hash",
                    "source-removed",
                    "wiki-source-boundary",
                ],
                "unresolved": [],
                "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
                "decision": "approved private source moved into the Wiki-owned archive",
            }
            for record in records
        ],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8")


def _receipt_bytes(
    source_name: str,
    wiki_subject: str,
    destination: Path,
    records: list[dict[str, object]],
    excluded: list[dict[str, str]],
    byte_count: int,
) -> bytes:
    payload = {
        "version": 1,
        "source": source_name,
        "wiki_subject": wiki_subject,
        "destination": destination.as_posix(),
        "created_at": datetime.now(UTC).isoformat(),
        "files": len(records),
        "excluded": len(excluded),
        "bytes": byte_count,
        "catalog_sha256": hashlib.sha256(
            _catalog_bytes(source_name, wiki_subject, records, excluded)
        ).hexdigest(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _verify_records(vault: Path, records: list[dict[str, object]]) -> None:
    for record in records:
        target = vault / str(record["target"])
        if not target.is_file() or _sha256(target) != record["sha256"]:
            raise WoonError(f"Wiki-owned source byte mismatch: {record['target']}")


def _verify_catalog(path: Path, expected: list[dict[str, object]], wiki_subject: str) -> None:
    if not path.is_file():
        raise WoonError("Wiki-owned source catalog is missing")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if payload.get("wiki_subject") != wiki_subject or payload.get("records") != expected:
        raise WoonError("Wiki-owned source catalog does not match archived bytes")


def _write_metadata(
    catalog_path: Path,
    ledger_path: Path,
    receipt_path: Path,
    source_name: str,
    wiki_subject: str,
    destination: Path,
    records: list[dict[str, object]],
    excluded: list[dict[str, str]],
    byte_count: int,
) -> None:
    """Idempotently repair metadata after archived bytes have been verified."""

    previous = {
        path: path.read_bytes() if path.is_file() else None
        for path in (catalog_path, ledger_path, receipt_path)
    }
    try:
        atomic_write(
            catalog_path,
            _catalog_bytes(source_name, wiki_subject, records, excluded),
        )
        atomic_write(ledger_path, _ledger_bytes(source_name, wiki_subject, records))
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        receipt_path.parent.chmod(0o700)
        receipt_payload = {}
        if receipt_path.is_file():
            try:
                receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                receipt_payload = {}
        if receipt_payload.get("wiki_subject") != wiki_subject:
            atomic_write(
                receipt_path,
                _receipt_bytes(
                    source_name,
                    wiki_subject,
                    destination,
                    records,
                    excluded,
                    byte_count,
                ),
                mode=0o600,
            )
        if not receipt_path.is_file():
            raise WoonError("Wiki-owned source archive receipt is missing")
        receipt_path.chmod(0o600)
    except BaseException:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content, mode=0o600 if path == receipt_path else 0o644)
        raise


def _require_disjoint(source: Path, vault: Path) -> None:
    try:
        source.relative_to(vault)
    except ValueError:
        try:
            vault.relative_to(source)
        except ValueError:
            return
    raise WoonError("private source archive input must be outside the Wiki vault")


def _wiki_subject(vault: Path, value: str) -> str:
    relative = Path(value.strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise WoonError("Wiki source subject must be a safe vault-relative path")
    if relative.suffix.lower() != ".md" or not relative.parts or relative.parts[0] != "wiki":
        raise WoonError("Wiki source subject must be a Markdown document under wiki/")
    if len(relative.parts) > 2 and relative.parts[1:3] == ("private", "_sources"):
        raise WoonError("Wiki source subject must be a human-readable canonical document")
    target = (vault / relative).resolve()
    if not target.is_file():
        raise WoonError(f"Wiki source subject does not exist: {relative.as_posix()}")
    return relative.as_posix()


def _safe_name(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized):
        raise WoonError("private source archive name must be a lowercase slug")
    return normalized


def _role(path: Path) -> str:
    if path.suffix.lower() in {
        ".adoc",
        ".asciidoc",
        ".csv",
        ".docx",
        ".htm",
        ".html",
        ".ipynb",
        ".md",
        ".pdf",
        ".pptx",
        ".rst",
        ".txt",
        ".xlsx",
    }:
        return "document"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
        return "asset"
    if path.suffix.lower() in {".yaml", ".yml", ".json"}:
        return "schema-or-view"
    return "other"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
