"""Plan a review-only intake from Zotero metadata and NotebookLM Markdown.

The planner deliberately does not call either external application.  It proves
which local export was inspected and keeps generated NotebookLM text outside
the canonical source/claim/page pipeline until a human reviews its evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_ARXIV = re.compile(r"(?:arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
_VIDEO_URL = re.compile(r"(?:youtube\.com|youtu\.be)", re.IGNORECASE)


def create_research_intake_plan(
    *,
    purpose: str,
    zotero_export: Path | None = None,
    notebooklm_manifest: Path | None = None,
) -> dict[str, object]:
    """Build an immutable intake plan without importing external content.

    At least one input must be present.  Zotero contributes bibliographic
    metadata only.  NotebookLM artifacts are always marked review-required,
    even when their export hash and cited identifiers are valid.
    """

    normalized_purpose = _purpose(purpose)
    if zotero_export is None and notebooklm_manifest is None:
        raise WoonError("research intake requires --zotero or --notebooklm-manifest")

    records: list[dict[str, object]] = []
    inputs: list[dict[str, object]] = []
    retained_refs: set[str] | None = None
    if zotero_export is not None:
        source_path = _file(zotero_export, "Zotero export")
        zotero_records = _zotero_records(source_path)
        retained_refs = _retained_refs(zotero_records)
        records.extend(zotero_records)
        inputs.append(
            {
                "kind": "zotero-export",
                "sha256": _sha256(source_path),
                "records": len(zotero_records),
            }
        )
    if notebooklm_manifest is not None:
        manifest_path = _file(notebooklm_manifest, "NotebookLM manifest")
        notebooklm_records = _notebooklm_records(manifest_path, retained_refs)
        records.extend(notebooklm_records)
        inputs.append(
            {
                "kind": "notebooklm-manifest",
                "sha256": _sha256(manifest_path),
                "records": len(notebooklm_records),
            }
        )

    identities: dict[str, list[str]] = {}
    for record in records:
        identity = str(record["identity"])
        identities.setdefault(identity, []).append(str(record["source_id"]))
    duplicates = [
        {"identity": identity, "source_ids": source_ids}
        for identity, source_ids in sorted(identities.items())
        if len(source_ids) > 1
    ]
    states = Counter(str(record["state"]) for record in records)
    plan = {
        "version": 1,
        "purpose": normalized_purpose,
        "inputs": inputs,
        "summary": {
            "records": len(records),
            "metadata_ready": states["metadata-ready"],
            "review_required": states["derived-review-required"],
            "duplicate_identities": len(duplicates),
        },
        "records": sorted(records, key=lambda item: str(item["source_id"])),
        "duplicates": duplicates,
        "next_action": (
            "review records, then archive evidence-backed claims through the canonical compiler"
        ),
    }
    return plan


def write_research_intake_plan(plan: dict[str, object], output: Path) -> None:
    """Write a plan atomically so a failed invocation never leaves partial JSON."""

    atomic_write(output.expanduser().resolve(), encode_json(plan) + b"\n")


def export_notebooklm_artifact(
    *,
    artifact_id: str,
    kind: str,
    source_refs: tuple[str, ...],
    tool_revision: str,
    output_markdown: Path,
    manifest_output: Path,
    nlm_binary: str = "nlm",
) -> dict[str, object]:
    """Export one approved NotebookLM artifact and bind it to an intake manifest.

    The command deliberately has a narrow side-effect boundary: `nlm` may
    download exactly one selected artifact, then this function records its
    immutable bytes. It never uploads a source, alters a notebook, or writes
    to the Woon Vault.
    """

    normalized_artifact_id = _text(artifact_id, "NotebookLM artifact_id")
    normalized_kind = _text(kind, "NotebookLM artifact kind")
    normalized_binary = _text(nlm_binary, "nlm binary")
    normalized_revision = _text(tool_revision, "NotebookLM tool revision")
    if not re.fullmatch(r"[0-9a-f]{40}", normalized_revision):
        raise WoonError("NotebookLM tool revision must be a 40-character Git commit")
    normalized_refs = _source_refs(list(source_refs), normalized_artifact_id)

    markdown_path = output_markdown.expanduser().resolve()
    manifest_path = manifest_output.expanduser().resolve()
    if markdown_path.suffix.lower() != ".md":
        raise WoonError("NotebookLM export output must end in .md")
    if markdown_path.exists():
        raise WoonError(f"NotebookLM export output already exists: {markdown_path}")
    if manifest_path.exists():
        raise WoonError(f"NotebookLM manifest output already exists: {manifest_path}")
    try:
        relative_markdown = markdown_path.relative_to(manifest_path.parent)
    except ValueError as error:
        raise WoonError(
            "NotebookLM export Markdown must be inside the manifest directory"
        ) from error
    if not relative_markdown.parts:
        raise WoonError("NotebookLM export Markdown path cannot be empty")

    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                normalized_binary,
                "artifact",
                "export",
                normalized_artifact_id,
                "--format",
                "md",
                "--output",
                str(markdown_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise WoonError(
            f"could not run NotebookLM exporter {normalized_binary!r}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().replace("\n", " ")
        if len(detail) > 400:
            detail = detail[:400] + "..."
        suffix = f": {detail}" if detail else ""
        raise WoonError(
            f"NotebookLM artifact export failed with exit {completed.returncode}{suffix}"
        )
    if not markdown_path.is_file() or not markdown_path.read_text(encoding="utf-8").strip():
        raise WoonError("NotebookLM exporter did not create a non-empty Markdown file")
    markdown = markdown_path.read_text(encoding="utf-8")
    if _VIDEO_URL.search(markdown):
        raise WoonError("NotebookLM artifact contains a prohibited video URL")

    manifest = {
        "version": 1,
        "tool": {"name": "nlm", "revision": normalized_revision},
        "artifacts": [
            {
                "artifact_id": normalized_artifact_id,
                "kind": normalized_kind,
                "path": relative_markdown.as_posix(),
                "sha256": _sha256(markdown_path),
                "source_refs": normalized_refs,
            }
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(manifest_path, encode_json(manifest) + b"\n")
    return {
        "artifact_id": normalized_artifact_id,
        "markdown": str(markdown_path),
        "manifest": str(manifest_path),
        "source_refs": normalized_refs,
        "state": "derived-review-required",
    }


def _zotero_records(path: Path) -> list[dict[str, object]]:
    raw = _json(path, "Zotero export")
    items = _zotero_items(raw)
    records: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        key = _zotero_key(item, index)
        source_id = f"research://zotero/{quote(key, safe='._-')}"
        if source_id in seen_ids:
            raise WoonError(f"duplicate Zotero citation key: {key}")
        seen_ids.add(source_id)
        title = _text(item.get("title"), f"Zotero item {key} title")
        doi = _doi(_first_text(item, "DOI", "doi"))
        arxiv = _arxiv(_first_text(item, "arXiv", "arxiv", "archive", "archiveLocation"))
        url = _first_text(item, "url", "URL")
        identity = f"doi:{doi}" if doi else f"arxiv:{arxiv}" if arxiv else f"zotero:{key}"
        records.append(
            {
                "source_id": source_id,
                "kind": "scholarly-metadata",
                "identity": identity,
                "citation_key": key,
                "title": title,
                "doi": doi,
                "arxiv": arxiv,
                "url": url,
                "year": _year(item),
                "state": "metadata-ready",
                "canonical": False,
                "requires": [
                    "licensed or accessible source text",
                    "purpose-bound source record",
                    "claim review before canonical compilation",
                ],
            }
        )
    return records


def _notebooklm_records(
    manifest_path: Path,
    retained_refs: set[str] | None,
) -> list[dict[str, object]]:
    raw = _json(manifest_path, "NotebookLM manifest")
    if not isinstance(raw, dict):
        raise WoonError("NotebookLM manifest must be an object")
    if set(raw).difference({"version", "tool", "artifacts"}):
        raise WoonError("NotebookLM manifest contains unsupported fields")
    if raw.get("version") != 1:
        raise WoonError("NotebookLM manifest version must be 1")
    tool = raw.get("tool")
    if not isinstance(tool, dict) or set(tool) != {"name", "revision"}:
        raise WoonError("NotebookLM manifest tool requires name and revision")
    revision = tool.get("revision")
    if (
        tool.get("name") != "nlm"
        or not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        raise WoonError("NotebookLM manifest currently supports a pinned nlm export only")
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise WoonError("NotebookLM manifest artifacts must be a non-empty list")

    records: list[dict[str, object]] = []
    seen_artifacts: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {
            "artifact_id",
            "kind",
            "path",
            "sha256",
            "source_refs",
        }:
            raise WoonError(
                "NotebookLM artifact must define id, kind, path, sha256, and source_refs"
            )
        artifact_id = _text(item.get("artifact_id"), "NotebookLM artifact_id")
        if artifact_id in seen_artifacts:
            raise WoonError(f"duplicate NotebookLM artifact_id: {artifact_id}")
        seen_artifacts.add(artifact_id)
        relative = _relative(_text(item.get("path"), f"NotebookLM artifact {artifact_id} path"))
        artifact_path = (manifest_path.parent / relative).resolve()
        try:
            artifact_path.relative_to(manifest_path.parent.resolve())
        except ValueError as error:
            raise WoonError(
                f"NotebookLM artifact resolves outside the export directory: {relative}"
            ) from error
        if not artifact_path.is_file() or artifact_path.suffix.lower() != ".md":
            raise WoonError(f"NotebookLM artifact must be an existing Markdown file: {relative}")
        expected = _text(item.get("sha256"), f"NotebookLM artifact {artifact_id} sha256")
        if not _SHA256.fullmatch(expected):
            raise WoonError(f"invalid NotebookLM artifact sha256: {relative}")
        actual = _sha256(artifact_path)
        if actual != expected:
            raise WoonError(f"NotebookLM artifact hash mismatch: {relative}")
        markdown = artifact_path.read_text(encoding="utf-8")
        if _VIDEO_URL.search(markdown):
            raise WoonError(f"NotebookLM artifact contains a prohibited video URL: {relative}")
        refs = _source_refs(item.get("source_refs"), artifact_id)
        if retained_refs is not None:
            unmatched = sorted(set(refs).difference(retained_refs))
            if unmatched:
                raise WoonError(
                    "NotebookLM artifact source_refs do not match the Zotero export: "
                    + ", ".join(unmatched)
                )
        records.append(
            {
                "source_id": f"research://notebooklm/{quote(artifact_id, safe='._-')}",
                "kind": "notebooklm-derived",
                "identity": f"notebooklm:{artifact_id}",
                "artifact_kind": _text(item.get("kind"), f"NotebookLM artifact {artifact_id} kind"),
                "artifact_sha256": actual,
                "source_refs": refs,
                "source_refs_verified_by": "zotero-export" if retained_refs is not None else None,
                "state": "derived-review-required",
                "canonical": False,
                "requires": [
                    "source_refs must resolve to retained evidence",
                    "claim-by-claim evidence review",
                    "archive or compile creates canonical records",
                ],
            }
        )
    return records


def _retained_refs(records: list[dict[str, object]]) -> set[str]:
    """Return every stable scholarly identifier present in the selected collection."""

    refs: set[str] = set()
    for record in records:
        doi = record.get("doi")
        arxiv = record.get("arxiv")
        if isinstance(doi, str):
            refs.add(f"doi:{doi}")
        if isinstance(arxiv, str):
            refs.add(f"arxiv:{arxiv}")
    return refs


def _zotero_items(raw: object) -> list[dict[str, object]]:
    items: object
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("items", raw.get("references"))
    else:
        items = None
    if not isinstance(items, list) or not items:
        raise WoonError("Zotero export must contain a non-empty items or references list")
    if not all(isinstance(item, dict) for item in items):
        raise WoonError("Zotero export items must be objects")
    return [dict(item) for item in items]


def _zotero_key(item: dict[str, object], index: int) -> str:
    for key in ("citationKey", "id", "key"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"item-{index}"


def _source_refs(value: object, artifact_id: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise WoonError(f"NotebookLM artifact {artifact_id} requires non-empty source_refs")
    refs: list[str] = []
    for item in value:
        raw = _text(item, f"NotebookLM artifact {artifact_id} source_ref")
        if raw.lower().startswith("doi:"):
            identifier = _doi(raw[4:])
            if identifier is not None:
                refs.append(f"doi:{identifier}")
                continue
        if raw.lower().startswith("arxiv:"):
            identifier = _arxiv(raw[6:])
            if identifier is not None:
                refs.append(f"arxiv:{identifier}")
                continue
        raise WoonError(
            f"NotebookLM artifact {artifact_id} source_refs must contain valid doi: or arxiv: IDs"
        )
    return sorted(set(refs))


def _purpose(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise WoonError("research intake purpose must be non-empty")
    return normalized


def _file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise WoonError(f"{label} does not exist: {resolved}")
    return resolved


def _json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise WoonError(f"invalid {label} JSON: {path}") from error


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{label} must be non-empty text")
    return value.strip()


def _first_text(item: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _doi(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
    match = _DOI.search(candidate)
    return match.group(0).lower() if match else None


def _arxiv(value: str | None) -> str | None:
    if value is None:
        return None
    match = _ARXIV.search(value)
    return match.group(1).lower() if match else None


def _year(item: dict[str, object]) -> int | None:
    for key in ("date", "issued", "year"):
        value = item.get(key)
        if isinstance(value, int) and 1000 <= value <= 9999:
            return value
        if isinstance(value, str):
            match = re.search(r"\b(\d{4})\b", value)
            if match:
                return int(match.group(1))
        if isinstance(value, dict):
            parts = value.get("date-parts")
            if (
                isinstance(parts, list)
                and parts
                and isinstance(parts[0], list)
                and parts[0]
                and isinstance(parts[0][0], int)
            ):
                return parts[0][0]
    return None


def _relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WoonError(f"NotebookLM artifact path must be a safe relative path: {value!r}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
