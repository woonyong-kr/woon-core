"""Bind supplementary reader runs to accepted provenance and local execution.

These records never add source inventory elements or advance a book's phase.
Evidence paths are Vault-relative; reading or checking them never runs code.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaml import YAMLError

from woon_core.errors import WoonError
from woon_core.knowledge.book_reader_presentation import (
    FOOTNOTE_PREFIX,
    render_personal_footnotes,
)
from woon_core.knowledge.wiki_tree import strip_generated_wiki_views
from woon_core.knowledge.yaml_cache import load_yaml_file

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FENCE = re.compile(r"(?ms)^```(?P<language>[A-Za-z0-9_+-]+)[ \t]*\n(?P<body>.*?)^```[ \t]*$")
_FIELDS = {
    "owner_id",
    "source_id",
    "source_record_sha256",
    "claim_id",
    "claim_record_sha256",
    "run_language",
    "run_block_index",
    "code_sha256",
    "verification_evidence",
    "verification_sha256",
    "verification_case",
}


@dataclass(frozen=True, slots=True)
class SupplementalRunnable:
    owner_id: str
    source_id: str
    claim_id: str
    run_language: str
    run_block_index: int
    code_sha256: str
    evidence_files: tuple[tuple[str, str], ...]

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.owner_id, self.run_language, self.run_block_index


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _pinned_json(vault: Path, relative: str, expected: str) -> dict[str, Any]:
    candidate = Path(relative)
    if (
        not relative
        or candidate.is_absolute()
        or relative.startswith("~")
        or "\\" in relative
        or ".." in candidate.parts
        or candidate.as_posix() != relative
        or _SHA256.fullmatch(expected) is None
    ):
        raise WoonError("supplemental runnable evidence requires a Vault-relative path and SHA-256")
    path = vault / candidate
    if any(
        (vault / Path(*candidate.parts[:i])).is_symlink()
        for i in range(1, len(candidate.parts) + 1)
    ):
        raise WoonError("supplemental runnable evidence must not traverse symlinks")
    try:
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise WoonError(f"supplemental runnable evidence hash mismatch: {relative}")
        payload = json.loads(raw)
    except (OSError, ValueError) as error:
        raise WoonError(f"supplemental runnable evidence is unreadable: {relative}") from error
    if not isinstance(payload, dict):
        raise WoonError("supplemental runnable evidence must be a JSON object")
    return payload


def _local_evidence(payload: object) -> bool:
    if isinstance(payload, dict):
        if payload.get("external_transmission") is True:
            return False
        if "provider" in payload and payload["provider"] != "local":
            return False
        return all(_local_evidence(value) for value in payload.values())
    return not isinstance(payload, list) or all(_local_evidence(value) for value in payload)


def _catalog(vault: Path, name: str, identity: str) -> dict[str, dict[str, Any]]:
    try:
        payload = load_yaml_file(vault / f"catalog/llm-wiki/{name}.yaml")
        rows = payload.get(name) if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get(identity), str) for row in rows
        ):
            raise WoonError(f"supplemental runnable {name} catalog is invalid")
        indexed = {row[identity]: row for row in rows}
        if len(indexed) != len(rows):
            raise WoonError(f"supplemental runnable {name} catalog contains duplicate IDs")
        return indexed
    except (OSError, ValueError, YAMLError) as error:
        raise WoonError(f"supplemental runnable {name} catalog is unreadable") from error


def load_supplemental_runnables(
    vault: Path,
    manifest: dict[str, Any],
    *,
    sources: dict[str, dict[str, Any]] | None = None,
    claims: dict[str, dict[str, Any]] | None = None,
) -> tuple[SupplementalRunnable, ...]:
    """Read and validate pinned provenance/receipts, failing before any writer."""

    rows = manifest.get("supplemental_runnables", [])
    if not isinstance(rows, list):
        raise WoonError("supplemental_runnables must be a list")
    if not rows:
        return ()
    sources = _catalog(vault, "sources", "source_id") if sources is None else sources
    claims = _catalog(vault, "claims", "claim_id") if claims is None else claims
    nodes = manifest.get("nodes", [])
    if not isinstance(nodes, list):
        raise WoonError("supplemental runnable manifest nodes must be a list")
    owners = {
        row.get("canonical_id")
        for row in nodes
        if isinstance(row, dict) and row.get("leaf") is True
    }
    result: list[SupplementalRunnable] = []
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise WoonError("supplemental runnable fields are invalid")
        if any(
            not isinstance(row[key], str) or not row[key].strip()
            for key in _FIELDS - {"run_block_index"}
        ):
            raise WoonError("supplemental runnable string fields must be non-empty")
        owner, language, index = row["owner_id"], row["run_language"], row["run_block_index"]
        if owner not in owners or re.fullmatch(r"run-[A-Za-z0-9_-]+", language) is None:
            raise WoonError("supplemental runnable requires a leaf owner and run-* language")
        if type(index) is not int or index < 1:
            raise WoonError("supplemental runnable run_block_index must be a positive integer")
        if any(_SHA256.fullmatch(row[key]) is None for key in _FIELDS if key.endswith("sha256")):
            raise WoonError("supplemental runnable hashes must be lowercase SHA-256")
        identity = (owner, language, index)
        if identity in seen:
            raise WoonError("supplemental runnable reader block is assigned more than once")
        seen.add(identity)
        source, claim = sources.get(row["source_id"], {}), claims.get(row["claim_id"], {})
        if (
            source.get("source_id") != row["source_id"]
            or source.get("kind") != "curated-wiki"
            or source.get("lifecycle") != "compiled"
            or source.get("privacy") != "local-only"
            or claim.get("claim_id") != row["claim_id"]
            or claim.get("kind") != "curated-document"
            or claim.get("status") != "accepted"
            or claim.get("source_ids") != [row["source_id"]]
            or _digest(source) != row["source_record_sha256"]
            or _digest(claim) != row["claim_record_sha256"]
        ):
            raise WoonError("supplemental runnable requires exact accepted, local-only provenance")
        for body in (source.get("body"), claim.get("markdown")):
            if not isinstance(body, str) or not any(
                match["language"] in {language, language.removeprefix("run-")}
                and hashlib.sha256(match["body"].encode()).hexdigest() == row["code_sha256"]
                for match in _FENCE.finditer(body)
            ):
                raise WoonError("supplemental runnable code is absent from its accepted provenance")
        evidence = _pinned_json(vault, row["verification_evidence"], row["verification_sha256"])
        if (
            set(evidence) - {"verification"}
            != {
                "schema_version",
                "provider",
                "external_transmission",
                "execution_receipt_relative_path",
                "execution_receipt_sha256",
            }
            or type(evidence.get("schema_version")) is not int
            or evidence["schema_version"] != 1
            or evidence.get("provider") != "local"
            or evidence.get("external_transmission") is not False
            or not isinstance(evidence.get("execution_receipt_relative_path"), str)
            or not isinstance(evidence.get("execution_receipt_sha256"), str)
        ):
            raise WoonError(
                "supplemental runnable wrapper requires the local execution contract v1"
            )
        receipt_path = evidence["execution_receipt_relative_path"]
        receipt_hash = evidence["execution_receipt_sha256"]
        receipt = _pinned_json(vault, receipt_path, receipt_hash)
        if not _local_evidence(receipt):
            raise WoonError("supplemental runnable receipt records a non-local execution")
        cases = receipt.get("cases")
        if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
            raise WoonError("supplemental runnable execution cases are invalid")
        matches = [case for case in cases if case.get("key") == row["verification_case"]]
        if len(matches) != 1:
            raise WoonError("supplemental runnable execution case must match exactly once")
        case = matches[0]
        stdout = case.get("stdout")
        if (
            case.get("code_sha256") != row["code_sha256"]
            or type(case.get("compile_exit")) is not int
            or case["compile_exit"] != 0
            or type(case.get("run_exit")) is not int
            or case["run_exit"] != 0
            or case.get("expected_rejection") is True
            or not isinstance(stdout, str)
            or hashlib.sha256(stdout.encode()).hexdigest() != case.get("stdout_sha256")
        ):
            raise WoonError("supplemental runnable requires matching successful compile/run/stdout")
        result.append(
            SupplementalRunnable(
                owner,
                row["source_id"],
                row["claim_id"],
                language,
                index,
                row["code_sha256"],
                (
                    (row["verification_evidence"], row["verification_sha256"]),
                    (receipt_path, receipt_hash),
                ),
            )
        )
    return tuple(result)


def audit_supplemental_runnable_partition(
    vault: Path, manifest: dict[str, Any], reader_bodies: dict[str, str]
) -> dict[str, int]:
    """Require every reader run to have exactly one source or supplemental owner.

    Return original-run counts per reader, never an inflated total.
    """

    supplements = load_supplemental_runnables(vault, manifest)
    page_specs = _catalog(vault, "pages", "page_id") if supplements else {}
    original: set[tuple[str, str, int]] = set()
    counts: dict[str, int] = {}
    for assignment in manifest.get("source_element_assignments", []):
        if not isinstance(assignment, dict) or assignment.get("delivery") != "run-block":
            continue
        owner, language, index = (
            assignment.get("owner_id"),
            assignment.get("run_language"),
            assignment.get("run_block_index"),
        )
        if not isinstance(owner, str) or not isinstance(language, str) or type(index) is not int:
            raise WoonError("supplemental runnable partition has an invalid original assignment")
        identity = (owner, language, index)
        if identity in original:
            raise WoonError("supplemental runnable partition repeats an original reader run")
        original.add(identity)
        counts[owner] = counts.get(owner, 0) + 1
    additional = {record.identity: record for record in supplements}
    if original.intersection(additional):
        raise WoonError("supplemental runnable overlaps an original source run")
    actual: dict[tuple[str, str, int], str] = {}
    for owner, body in reader_bodies.items():
        indices: dict[str, int] = {}
        for match in _FENCE.finditer(body):
            language = match["language"]
            if not language.startswith("run-"):
                continue
            indices[language] = indices.get(language, 0) + 1
            actual[(owner, language, indices[language])] = match["body"]
        if sum(indices.values()) != len(re.findall(r"(?m)^```run-[A-Za-z0-9_-]+[ \t]*$", body)):
            raise WoonError("supplemental runnable reader has an unclosed run fence")
    if set(actual) != original | set(additional):
        raise WoonError("supplemental runnable partition has unassigned or missing reader runs")
    for identity, record in additional.items():
        if hashlib.sha256(actual[identity].encode()).hexdigest() != record.code_sha256:
            raise WoonError("supplemental runnable reader code hash mismatch")
        spec = page_specs.get(record.owner_id, {})
        render = spec.get("render", {})
        if (
            not isinstance(spec.get("source_ids"), list)
            or record.source_id not in spec["source_ids"]
            or not isinstance(spec.get("claim_ids"), list)
            or record.claim_id not in spec["claim_ids"]
            or not isinstance(render, dict)
            or not isinstance(render.get("supplemental_claim_ids"), list)
            or record.claim_id not in render["supplemental_claim_ids"]
        ):
            raise WoonError("supplemental runnable provenance is not attached to its reader")
    return counts


def personal_footnote_coverage_pages(vault: Path, reader_pages: dict[str, str]) -> dict[str, str]:
    """Recover coverage input only from an exactly reproduced presentation.

    Arbitrary footnotes are never stripped. Source text, note text and indented
    runnable code must all match the current source, claims and page spec.
    """
    if not (vault / "catalog/llm-wiki/pages.yaml").exists():
        if any(f"[^{FOOTNOTE_PREFIX}" in body for body in reader_pages.values()):
            raise WoonError("personal footnote presentation has no page catalog")
        return {}
    specs = _catalog(vault, "pages", "page_id")
    selected = {
        page_id: specs[page_id]
        for page_id in reader_pages
        if "personal_footnotes" in specs.get(page_id, {}).get("render", {})
    }
    for page_id, body in reader_pages.items():
        if f"[^{FOOTNOTE_PREFIX}" in body and page_id not in selected:
            raise WoonError("personal footnote presentation is not declared by its page")
    if not selected:
        return {}
    sources = _catalog(vault, "sources", "source_id")
    claims = _catalog(vault, "claims", "claim_id")
    result: dict[str, str] = {}
    for page_id, spec in selected.items():
        render = spec["render"]
        source_id = render.get("source_id")
        source_body = sources.get(source_id, {}).get("body")
        if source_id not in spec.get("source_ids", []) or not isinstance(source_body, str):
            raise WoonError("personal footnote presentation is missing its source")
        attached = [
            claims[claim_id] for claim_id in spec.get("claim_ids", []) if claim_id in claims
        ]
        expected = f"# {spec['title']}\n\n" + render_personal_footnotes(
            source_body,
            render,
            attached,
        )
        if strip_generated_wiki_views(reader_pages[page_id]).strip() != expected.strip():
            raise WoonError(
                f"personal footnote presentation differs from its exact inputs: {page_id}"
            )
        coverage_body = source_body
        for claim_id in render["supplemental_claim_ids"]:
            coverage_body = (
                coverage_body.rstrip() + "\n\n" + claims[claim_id]["markdown"].strip() + "\n"
            )
        result[page_id] = f"# {spec['title']}\n\n" + coverage_body
    return result
