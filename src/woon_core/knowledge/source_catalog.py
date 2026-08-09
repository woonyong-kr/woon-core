"""Deterministic, content-addressed planning for external source corpora."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write

DEFAULT_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".obsidian",
        ".local",
        ".legacy-backup",
        ".drawio-backup",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    }
)
PLACEHOLDERS = frozenset({".DS_Store", ".gitkeep"})
DOCUMENT_EXTENSIONS = frozenset({".md", ".txt", ".rst", ".adoc", ".html", ".htm", ".ipynb"})
ASSET_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".drawio"})
SECRET_NAMES = frozenset({".env"})


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """One source file and its deterministic reconciliation decision."""

    source_id: str
    locator: str
    sha256: str
    size: int
    role: str
    privacy: str
    state: str
    target: str | None
    target_sha256: str | None


@dataclass(frozen=True, slots=True)
class SourceCatalogPlan:
    """Complete inventory without machine-specific absolute paths."""

    source_name: str
    records: tuple[SourceRecord, ...]
    excluded: tuple[tuple[str, str], ...]

    @property
    def summary(self) -> dict[str, int]:
        counts = Counter(record.state for record in self.records)
        return {key: counts[key] for key in sorted(counts)}


def plan_source_catalog(
    source: Path,
    target: Path,
    source_name: str,
    *,
    protected_patterns: tuple[str, ...] = (),
    previous_records: tuple[SourceRecord, ...] = (),
) -> SourceCatalogPlan:
    """Compare every active source file with a target corpus by path and content."""

    source_root = source.expanduser().resolve()
    target_root = target.expanduser().resolve()
    if not source_root.is_dir():
        raise WoonError(f"source corpus does not exist: {source_root}")
    if not target_root.is_dir():
        raise WoonError(f"target knowledge repository does not exist: {target_root}")
    normalized_name = _source_name(source_name)
    previous_by_locator = {record.locator: record.source_id for record in previous_records}
    previous_by_hash: dict[str, list[str]] = defaultdict(list)
    for record in previous_records:
        previous_by_hash[record.sha256].append(record.source_id)
    target_hashes: dict[str, list[str]] = defaultdict(list)
    target_titles: dict[str, list[str]] = defaultdict(list)
    for path in _active_files(target_root):
        relative = path.relative_to(target_root).as_posix()
        target_hashes[_sha256(path)].append(relative)
        if path.suffix.lower() == ".md" and (title := _markdown_title(path)):
            target_titles[_title_fingerprint(title)].append(relative)

    records: list[SourceRecord] = []
    excluded: list[tuple[str, str]] = []
    for path in _all_files(source_root):
        relative = path.relative_to(source_root).as_posix()
        reason = _excluded_reason(path.relative_to(source_root))
        if reason is not None:
            excluded.append((relative, reason))
            continue
        digest = _sha256(path)
        protected = any(fnmatch.fnmatch(relative, pattern) for pattern in protected_patterns)
        role = _role(relative, path)
        same_path = target_root / relative
        target_path, target_digest, state = _compare(
            path,
            relative,
            digest,
            same_path,
            target_root,
            target_hashes,
            target_titles,
            protected,
            role,
        )
        records.append(
            SourceRecord(
                source_id=_source_id(
                    normalized_name,
                    relative,
                    digest,
                    previous_by_locator,
                    previous_by_hash,
                ),
                locator=relative,
                sha256=digest,
                size=path.stat().st_size,
                role=role,
                privacy="private/local-only",
                state=state,
                target=target_path,
                target_sha256=target_digest,
            )
        )
    plan = SourceCatalogPlan(
        source_name=normalized_name,
        records=tuple(sorted(records, key=lambda item: item.locator)),
        excluded=tuple(sorted(excluded)),
    )
    validate_source_catalog(plan)
    return plan


def load_source_catalog(path: Path) -> SourceCatalogPlan:
    """Load a generated catalog so stable source identities survive refreshes."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise WoonError(f"invalid source catalog: {path}")
    source = raw.get("source")
    records = raw.get("records")
    excluded = raw.get("excluded", [])
    if (
        not isinstance(source, str)
        or not isinstance(records, list)
        or not isinstance(excluded, list)
    ):
        raise WoonError(f"invalid source catalog fields: {path}")
    loaded_records: list[SourceRecord] = []
    for item in records:
        if not isinstance(item, dict):
            raise WoonError(f"invalid source catalog record: {path}")
        try:
            loaded_records.append(SourceRecord(**item))
        except TypeError as error:
            raise WoonError(f"invalid source catalog record fields: {path}") from error
    loaded_excluded: list[tuple[str, str]] = []
    for item in excluded:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("locator"), str)
            or not isinstance(item.get("reason"), str)
        ):
            raise WoonError(f"invalid source catalog exclusion: {path}")
        loaded_excluded.append((item["locator"], item["reason"]))
    plan = SourceCatalogPlan(source, tuple(loaded_records), tuple(loaded_excluded))
    validate_source_catalog(plan)
    return plan


def write_source_catalog(plan: SourceCatalogPlan, output: Path) -> None:
    """Atomically write a human-readable catalog after validating it."""

    validate_source_catalog(plan)
    raw = {
        "version": 1,
        "source": plan.source_name,
        "summary": plan.summary,
        "records": [asdict(record) for record in plan.records],
        "excluded": [{"locator": locator, "reason": reason} for locator, reason in plan.excluded],
    }
    data = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")
    atomic_write(output, data)


def validate_source_catalog(plan: SourceCatalogPlan) -> None:
    """Reject incomplete, duplicate, unsafe, or malformed catalog records."""

    source_ids: set[str] = set()
    locators: set[str] = set()
    for record in plan.records:
        if record.source_id in source_ids:
            raise WoonError(f"duplicate source_id in source catalog: {record.source_id}")
        if record.locator in locators:
            raise WoonError(f"duplicate locator in source catalog: {record.locator}")
        source_ids.add(record.source_id)
        locators.add(record.locator)
        _safe_relative(record.locator, "source locator")
        if record.target is not None:
            _safe_relative(record.target, "source target")
        if not re.fullmatch(r"[0-9a-f]{64}", record.sha256):
            raise WoonError(f"invalid sha256 for source catalog record: {record.locator}")
        if record.target_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", record.target_sha256
        ):
            raise WoonError(f"invalid target sha256 for source catalog record: {record.locator}")
        if record.size < 0:
            raise WoonError(f"negative source size: {record.locator}")
    for locator, _ in plan.excluded:
        _safe_relative(locator, "excluded source locator")
        if locator in locators:
            raise WoonError(f"source catalog both records and excludes {locator}")


def _compare(
    source: Path,
    relative: str,
    digest: str,
    same_path: Path,
    target_root: Path,
    target_hashes: dict[str, list[str]],
    target_titles: dict[str, list[str]],
    protected: bool,
    role: str,
) -> tuple[str | None, str | None, str]:
    if protected:
        if same_path.is_file():
            return relative, _sha256(same_path), "external-private-existing"
        return None, None, "external-private"
    if role == "repository-rule":
        if same_path.is_file():
            return relative, _sha256(same_path), "external-repository-rule"
        return None, None, "external-repository-rule"
    if same_path.is_file():
        target_digest = _sha256(same_path)
        if digest == target_digest:
            return relative, target_digest, "identical"
        if source.suffix.lower() == ".md" and _markdown_body(source) == _markdown_body(same_path):
            return relative, target_digest, "metadata-only"
        return relative, target_digest, "merge-required"
    matches = sorted(target_hashes.get(digest, ()))
    if matches:
        target_path = matches[0]
        return target_path, _sha256(target_root / target_path), "content-alias"
    if source.suffix.lower() == ".md":
        title = _markdown_title(source)
        title_matches = sorted(target_titles.get(_title_fingerprint(title), ())) if title else []
        if title_matches:
            target_path = title_matches[0]
            return target_path, _sha256(target_root / target_path), "semantic-match"
    return None, None, "new"


def _all_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(name for name in names if name not in DEFAULT_EXCLUDED_PARTS)
        base = Path(directory)
        files.extend(base / filename for filename in sorted(filenames))
    return sorted(files)


def _active_files(root: Path) -> list[Path]:
    return [path for path in _all_files(root) if _excluded_reason(path.relative_to(root)) is None]


def _excluded_reason(relative: Path) -> str | None:
    if any(part in DEFAULT_EXCLUDED_PARTS for part in relative.parts):
        return "generated-or-local"
    if relative.name in PLACEHOLDERS or relative.suffix.lower() == ".pyc":
        return "placeholder-or-cache"
    if relative.name in SECRET_NAMES or relative.name.startswith(".env."):
        return "secret-local"
    return None


def _role(relative: str, path: Path) -> str:
    parts = Path(relative).parts
    suffix = path.suffix.lower()
    if suffix in ASSET_EXTENSIONS:
        return "asset"
    if path.name in {"AGENTS.md", ".gitignore"}:
        return "repository-rule"
    if parts[0] in {"scripts"}:
        return "operation"
    if parts[0] in {"types", "views"} or suffix in {".yaml", ".yml", ".json"}:
        return "schema-or-view"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def _source_name(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", normalized):
        raise WoonError("source name must contain lowercase letters, digits, or hyphens")
    return normalized


def _source_id(
    source_name: str,
    locator: str,
    digest: str,
    previous_by_locator: dict[str, str],
    previous_by_hash: dict[str, list[str]],
) -> str:
    if previous := previous_by_locator.get(locator):
        return previous
    hash_matches = previous_by_hash.get(digest, [])
    if len(hash_matches) == 1:
        return hash_matches[0]
    return f"source://{source_name}/{quote(locator, safe='/._-')}"


def _safe_relative(value: str, field: str) -> None:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise WoonError(f"{field} must be a safe relative path: {value!r}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _markdown_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) == 3:
            text = parts[2]
    text = re.sub(
        r"<!-- breadcrumb:start -->.*?<!-- breadcrumb:end -->\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


def _markdown_title(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) == 3:
            try:
                raw = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                raw = {}
            if isinstance(raw, dict) and isinstance(raw.get("title"), str):
                return str(raw["title"]).strip()
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _title_fingerprint(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())
