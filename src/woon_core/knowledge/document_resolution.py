"""Terminal lifecycle for local document-intake candidates.

The semantic curator, normally the bounded Codex knowledge-curation task,
chooses one disposition after reading the verified source and searching the
canonical Wiki.  This module does not infer meaning and does not write Wiki
content.  It verifies the observed canonical/source state and records one
immutable terminal receipt so unresolved candidates cannot silently pile up.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

DOCUMENT_RESOLUTION_VERSION = 1
_CANDIDATE_ID = re.compile(r"docling-[0-9a-f]{64}")
_DISPOSITIONS = {"integrated", "duplicate", "discarded", "user-action-required"}
_REASONS = {
    "integrated": {"existing-subject-updated", "new-evidence-merged"},
    "duplicate": {"existing-evidence-duplicate", "same-content-hash"},
    "discarded": {
        "demo-only",
        "low-value",
        "out-of-scope",
        "transient-ui",
        "unusable-extraction",
    },
    "user-action-required": {"consequential-claim", "identity-conflict", "privacy-boundary"},
}


@dataclass(frozen=True, slots=True)
class DocumentResolutionResult:
    """One verified terminal result for a document candidate."""

    candidate_id: str
    disposition: str
    receipt: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class DocumentResolutionAudit:
    """Backlog and integrity summary for the hidden document runtime."""

    candidates: int
    resolved: int
    pending: tuple[str, ...]
    user_action_required: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.pending and not self.user_action_required and not self.errors


def resolve_document_candidate(
    vault: Path,
    decision_file: Path,
) -> DocumentResolutionResult:
    """Validate one semantic decision and persist an immutable resolution receipt."""

    root = vault.expanduser().resolve()
    decision_path = decision_file.expanduser().resolve()
    if not root.is_dir():
        raise WoonError(f"knowledge vault does not exist: {root}")
    if not decision_path.is_file() or decision_path.is_symlink():
        raise WoonError("document resolution decision file is missing or unsafe")
    decision_bytes = decision_path.read_bytes()
    try:
        decision = json.loads(decision_bytes)
    except json.JSONDecodeError as error:
        raise WoonError("document resolution decision must be valid JSON") from error
    if not isinstance(decision, dict):
        raise WoonError("document resolution decision must be a mapping")
    normalized = _validate_decision(root, decision)
    candidate_id = normalized["candidate_id"]
    runtime = root / ".local/woon-knowledge/document-intake"
    candidate_directory = runtime / "candidates" / candidate_id
    candidate_receipt_path = candidate_directory / "receipt.json"
    candidate_receipt = _verified_candidate_receipt(candidate_directory, candidate_id)
    legacy_candidate = candidate_receipt.get("promotion_state") == "review-required"
    if legacy_candidate and normalized["disposition"] != "discarded":
        raise WoonError("legacy document candidate can only be terminally discarded")
    if not legacy_candidate:
        quality = _load_json(candidate_directory / "quality.json", "document quality")
        if (
            normalized["disposition"] in {"integrated", "duplicate"}
            and quality.get("state") != "ready-for-semantic-curation"
        ):
            raise WoonError("low-quality document candidate cannot be integrated")
    document_hash = _sha256(candidate_directory / "document.md")
    observed = _observed_targets(root, normalized, candidate_receipt)
    stable_receipt = {
        "version": DOCUMENT_RESOLUTION_VERSION,
        "candidate_id": candidate_id,
        "candidate_receipt_sha256": _sha256(candidate_receipt_path),
        "document_sha256": document_hash,
        "source_sha256": candidate_receipt["source"]["sha256"],
        "disposition": normalized["disposition"],
        "reason_code": normalized["reason_code"],
        "rationale": normalized["rationale"],
        "canonical": observed["canonical"],
        "source_targets": observed["source_targets"],
        "question": normalized.get("question"),
        "decision_sha256": hashlib.sha256(decision_bytes).hexdigest(),
        "status": (
            "user-action-required"
            if normalized["disposition"] == "user-action-required"
            else "resolved"
        ),
    }
    receipt_path = runtime / "resolutions" / f"{candidate_id}.json"
    lock_path = runtime / "locks" / f"resolution-{candidate_id}.lock"
    with exclusive_file_lock(lock_path):
        replayed = receipt_path.is_file()
        if replayed:
            existing = _load_json(receipt_path, "document resolution receipt")
            comparable = dict(existing)
            comparable.pop("resolved_at", None)
            if comparable != stable_receipt:
                raise WoonError(
                    f"document candidate already has another resolution: {candidate_id}"
                )
        else:
            receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            receipt_path.parent.chmod(0o700)
            receipt = {
                **stable_receipt,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            atomic_write(receipt_path, encode_json(receipt), mode=0o600)
    return DocumentResolutionResult(
        candidate_id=candidate_id,
        disposition=normalized["disposition"],
        receipt=receipt_path.relative_to(root).as_posix(),
        replayed=replayed,
    )


def audit_document_resolutions(vault: Path) -> DocumentResolutionAudit:
    """Verify all candidates have a valid terminal receipt or report them as pending."""

    root = vault.expanduser().resolve()
    runtime = root / ".local/woon-knowledge/document-intake"
    candidate_root = runtime / "candidates"
    resolution_root = runtime / "resolutions"
    candidates = {
        path.name: path
        for path in sorted(candidate_root.glob("docling-*"))
        if path.is_dir() and _CANDIDATE_ID.fullmatch(path.name)
    }
    resolutions = {
        path.stem: path
        for path in sorted(resolution_root.glob("docling-*.json"))
        if path.is_file() and _CANDIDATE_ID.fullmatch(path.stem)
    }
    errors: list[str] = []
    user_action: list[str] = []
    resolved = 0
    for candidate_id, candidate_directory in candidates.items():
        try:
            candidate_receipt = _verified_candidate_receipt(candidate_directory, candidate_id)
        except WoonError as error:
            errors.append(str(error))
            continue
        resolution_path = resolutions.get(candidate_id)
        if resolution_path is None:
            continue
        try:
            receipt = _load_json(resolution_path, "document resolution receipt")
            _validate_resolution_receipt(root, candidate_directory, candidate_receipt, receipt)
        except WoonError as error:
            errors.append(str(error))
            continue
        if receipt.get("status") == "user-action-required":
            user_action.append(candidate_id)
        else:
            resolved += 1
    orphaned = sorted(set(resolutions).difference(candidates))
    errors.extend(f"orphan document resolution: {candidate_id}" for candidate_id in orphaned)
    pending = tuple(sorted(set(candidates).difference(resolutions)))
    return DocumentResolutionAudit(
        candidates=len(candidates),
        resolved=resolved,
        pending=pending,
        user_action_required=tuple(sorted(user_action)),
        errors=tuple(sorted(set(errors))),
    )


def _validate_decision(root: Path, decision: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "version",
        "candidate_id",
        "disposition",
        "reason_code",
        "rationale",
        "canonical_paths",
        "source_targets",
        "question",
    }
    required = {
        "version",
        "candidate_id",
        "disposition",
        "reason_code",
        "rationale",
        "canonical_paths",
        "source_targets",
    }
    if set(decision).difference(allowed) or not required.issubset(decision):
        raise WoonError("document resolution decision has unsupported fields")
    if decision.get("version") != DOCUMENT_RESOLUTION_VERSION:
        raise WoonError("unsupported document resolution decision version")
    candidate_id = decision.get("candidate_id")
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID.fullmatch(candidate_id):
        raise WoonError("document resolution candidate_id is invalid")
    disposition = decision.get("disposition")
    if disposition not in _DISPOSITIONS:
        raise WoonError("document resolution disposition is invalid")
    reason_code = decision.get("reason_code")
    if reason_code not in _REASONS[disposition]:
        raise WoonError("document resolution reason_code is invalid")
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 500:
        raise WoonError("document resolution rationale must be nonempty and bounded")
    canonical_paths = _safe_paths(decision.get("canonical_paths"), "canonical_paths")
    source_targets = _safe_paths(decision.get("source_targets"), "source_targets")
    question = decision.get("question")
    if disposition in {"integrated", "duplicate"}:
        if not canonical_paths or not source_targets or question is not None:
            raise WoonError("integrated document resolution requires canonical and source targets")
    elif disposition == "discarded":
        if canonical_paths or source_targets or question is not None:
            raise WoonError("discarded document resolution must not archive or link the candidate")
    else:
        if canonical_paths or source_targets:
            raise WoonError("user-action document resolution must not claim canonical writes")
        if not isinstance(question, str) or not question.strip() or len(question) > 300:
            raise WoonError("user-action document resolution requires one bounded question")
    return {
        "candidate_id": candidate_id,
        "disposition": disposition,
        "reason_code": reason_code,
        "rationale": " ".join(rationale.split()),
        "canonical_paths": canonical_paths,
        "source_targets": source_targets,
        "question": " ".join(question.split()) if isinstance(question, str) else None,
    }


def _safe_paths(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"document resolution {field} must be a string list")
    normalized: list[str] = []
    for item in value:
        path = Path(item.strip())
        if not item.strip() or path.is_absolute() or ".." in path.parts:
            raise WoonError(f"document resolution {field} contains an unsafe path")
        normalized.append(path.as_posix())
    if len(normalized) != len(set(normalized)):
        raise WoonError(f"document resolution {field} must be unique")
    return tuple(normalized)


def _verified_candidate_receipt(directory: Path, candidate_id: str) -> dict[str, Any]:
    receipt = _load_json(directory / "receipt.json", "document candidate receipt")
    promotion_state = receipt.get("promotion_state")
    if (
        receipt.get("candidate_id") != candidate_id
        or receipt.get("status") != "candidate"
        or promotion_state not in {"curation-required", "review-required"}
        or receipt.get("canonical_writes") is not False
    ):
        raise WoonError(f"document candidate receipt mismatch: {candidate_id}")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, dict):
        raise WoonError(f"document candidate outputs are missing: {candidate_id}")
    required_outputs = (
        (
            "chunks.jsonl",
            "document.json",
            "document.raw.md",
            "document.md",
            "promotion.json",
            "quality.json",
        )
        if promotion_state == "curation-required"
        else ("chunks.jsonl", "document.json", "document.md")
    )
    for name in required_outputs:
        record = outputs.get(name)
        path = directory / name
        if (
            not isinstance(record, dict)
            or not path.is_file()
            or record.get("sha256") != _sha256(path)
            or record.get("bytes") != path.stat().st_size
        ):
            raise WoonError(f"document candidate output mismatch: {candidate_id}/{name}")
    return receipt


def _observed_targets(
    root: Path,
    decision: dict[str, Any],
    candidate_receipt: dict[str, Any],
) -> dict[str, list[dict[str, str]]]:
    canonical: list[dict[str, str]] = []
    for relative in decision["canonical_paths"]:
        path = _inside(root, relative)
        parts = Path(relative).parts
        if (
            len(parts) < 2
            or parts[0] != "wiki"
            or path.suffix.lower() != ".md"
            or parts[1:3] == ("private", "_sources")
            or not path.is_file()
        ):
            raise WoonError("document resolution canonical target is not an active Wiki page")
        canonical.append({"path": relative, "sha256": _sha256(path)})

    source_targets: list[dict[str, str]] = []
    source_hash = candidate_receipt.get("source", {}).get("sha256")
    for relative in decision["source_targets"]:
        path = _inside(root, relative)
        if (
            Path(relative).parts[:4] != ("wiki", "private", "_sources", "knowledge")
            or not path.is_file()
        ):
            raise WoonError("document resolution source target must be Wiki-owned evidence")
        digest = _sha256(path)
        if digest != source_hash:
            raise WoonError("document resolution source target does not match input bytes")
        source_targets.append({"path": relative, "sha256": digest})
    return {"canonical": canonical, "source_targets": source_targets}


def _validate_resolution_receipt(
    root: Path,
    candidate_directory: Path,
    candidate_receipt: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    candidate_id = candidate_directory.name
    if (
        receipt.get("version") != DOCUMENT_RESOLUTION_VERSION
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("candidate_receipt_sha256") != _sha256(candidate_directory / "receipt.json")
        or receipt.get("document_sha256") != _sha256(candidate_directory / "document.md")
        or receipt.get("source_sha256") != candidate_receipt.get("source", {}).get("sha256")
        or receipt.get("disposition") not in _DISPOSITIONS
        or receipt.get("status") not in {"resolved", "user-action-required"}
    ):
        raise WoonError(f"document resolution receipt mismatch: {candidate_id}")
    if receipt.get("reason_code") not in _REASONS[receipt["disposition"]]:
        raise WoonError(f"document resolution reason mismatch: {candidate_id}")
    canonical = receipt.get("canonical")
    source_targets = receipt.get("source_targets")
    if not isinstance(canonical, list) or not isinstance(source_targets, list):
        raise WoonError(f"document resolution targets are invalid: {candidate_id}")
    disposition = receipt["disposition"]
    if disposition in {"integrated", "duplicate"} and (not canonical or not source_targets):
        raise WoonError(f"document resolution targets are missing: {candidate_id}")
    if disposition in {"discarded", "user-action-required"} and (canonical or source_targets):
        raise WoonError(f"document resolution targets are unexpected: {candidate_id}")

    # Canonical Wiki pages are expected to keep evolving.  The terminal receipt
    # preserves the page hash observed at resolution time, while later audits
    # require the page to remain active without treating an ordinary edit as
    # provenance drift.
    for record in canonical:
        if not isinstance(record, dict):
            raise WoonError(f"document resolution canonical record is invalid: {candidate_id}")
        relative = record.get("path")
        digest = record.get("sha256")
        if not isinstance(relative, str) or not _valid_sha256(digest):
            raise WoonError(f"document resolution canonical record is invalid: {candidate_id}")
        path = _inside(root, relative)
        parts = Path(relative).parts
        if (
            len(parts) < 2
            or parts[0] != "wiki"
            or path.suffix.lower() != ".md"
            or parts[1:3] == ("private", "_sources")
            or not path.is_file()
        ):
            raise WoonError(f"document resolution canonical target is missing: {candidate_id}")

    source_hash = candidate_receipt.get("source", {}).get("sha256")
    for record in source_targets:
        if not isinstance(record, dict):
            raise WoonError(f"document resolution source record is invalid: {candidate_id}")
        relative = record.get("path")
        digest = record.get("sha256")
        if not isinstance(relative, str) or digest != source_hash or not _valid_sha256(digest):
            raise WoonError(f"document resolution source record is invalid: {candidate_id}")
        path = _inside(root, relative)
        if (
            Path(relative).parts[:4] != ("wiki", "private", "_sources", "knowledge")
            or not path.is_file()
            or _sha256(path) != source_hash
        ):
            raise WoonError(f"document resolution source target drift: {candidate_id}")


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise WoonError("document resolution path escapes the knowledge vault")
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"{label} is unreadable") from error
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be a mapping")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
