"""Canonical, hash-pinned contract for book coverage and promotion payloads."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from woon_core.errors import WoonError

BOOK_PROMOTION_PAYLOAD_SCHEMA_VERSION = 7
BOOK_CONTRACT_VERSION = 7
LEGACY_BOOK_CONTRACT_SHA256_V7 = (
    "2b0e8b4a115d1ce7b87a507920d3f44bf1312c91142c3dfc1dd7bfffe2841976"
)
PRE_IN_PAGE_H2_BOOK_CONTRACT_SHA256_V7 = (
    "de6157ed7d201def786ff722ea7bed0c39620c64ee0040b417eb4de31cce6656"
)
PRE_ORDERED_READER_SECTIONS_BOOK_CONTRACT_SHA256_V7 = (
    "719f313ea783890d257b91f6acb296acc729e4ddd1ec2c83241a784dff77616c"
)
BOOK_WORKFLOW_PHASES = (
    "source-landed",
    "translated",
    "concept-linked",
    "understanding-enriched",
)
BOOK_CONTRACT: dict[str, Any] = {
    "version": BOOK_CONTRACT_VERSION,
    "navigation": {
        "map": "H1-title/H2-source-topic/direct-child-wikilinks",
        "synthetic_wrappers": False,
        "authored_map_prose": False,
    },
    "source_coverage": {
        "structure_inventory": "source_structure_elements",
        "structure_assignment": "source_structure_assignments",
        "structure_scope": "front-matter/body/back-matter/appendix-or-copyright-metadata",
        "structure_dispositions": {
            "canonical-node": "one-source-structure-per-canonical-node",
            "in-page-h2": "ordered-source-sections-share-one-canonical-source-body-leaf",
            "metadata-only": "copyright-bibliography-index-only",
        },
        "in_page_heading": "exactly-one-H2-source-title-in-source-order",
        "mixed_depth_reader": (
            "explicit-ordered-source-body-and-navigation-group-sections-on-book-chapters"
        ),
        "inventory": "source_elements",
        "inventory_evidence": "source_element_inventory_evidence",
        "assignment": "source_element_assignments",
        "kinds": ["claim", "example", "caution", "figure", "code"],
        "semantic_unit": "stable-locator-and-source-content-hash",
        "owner_cardinality": "exactly-one-leaf",
        "reader_delivery": "unique-exact-span-or-pinned-figure-evidence",
        "node_counts": "derived-from-all-element-assignments",
    },
    "runnable": {
        "supported": "verified-run-block",
        "unsupported": "source-pinned-static-original-with-enumerated-non-runnable-reason",
        "unsupported_reasons": [
            "fragment",
            "dependency",
            "intentional-error",
            "placeholder",
        ],
        "synthetic_harness": False,
        "source_element_to_execution": "one-to-one-unique-delivery",
        "placeholder_code": "comment-only-fences-rejected",
        "narrative_example": "prose-evidence",
    },
    "promotion": {
        "coverage_manifest": "required-explicit-replace-or-merge-scope",
        "staged_scope": ("schema-v3-fragment-with-byte-preserved-base-and-optimistic-hashes"),
        "legacy_scope_audit": "stored-schema-v2-remains-readable",
    },
    "workflow": {
        "phases": list(BOOK_WORKFLOW_PHASES),
        "source_landed": (
            "actual-title-local-only-source-and-source-assets-archived-before-reader-delivery"
        ),
        "translated": (
            "same-canonical-leaf-natural-korean-with-immutable-source-provenance-and-coverage"
        ),
        "concept_linked": "hash-based-incremental-relations-without-reader-body-regeneration",
        "understanding_enriched": (
            "question-evidence-growth-without-source-or-translation-coverage-regression"
        ),
        "phase_progression": "monotonic",
        "understanding_enriched_completion": "non-blocking-continuous-state",
        "source_assets": (
            "all-images-inventoried; embedded-original-byte-exact; scan-crop-provenance"
        ),
    },
    "quality": {
        "source_landed_reader": "source-language-and-source-order-verified",
        "translated_reader": "korean-and-korean-prose-reviewed",
        "korean_source_translation": "translation-required-false-no-op-review",
        "workflow_prose_in_reader_body": False,
        "figure_reader_span": "substantive-relationship-not-label-only",
        "reader_language_artifacts": "no-semantic-ledger-or-broken-generated-korean",
        "textual_callouts": (
            "circled-digits-1-to-10; negative-dingbat-callouts-rejected"
        ),
    },
}
BOOK_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        BOOK_CONTRACT,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()

_BOOK_READER_WORKFLOW_PROSE = (
    (
        "learning workflow heading",
        re.compile(
            r"(?m)^#{2,6}\s+(?:직접 확인하기|직접 바꾸어 확인하기|"
            r"자료를 닫고 답하기|이전과 다음|이전·다음|완료 기준|검증 상태)\s*$"
        ),
    ),
    (
        "execution verification label",
        re.compile(
            r"(?m)^검증 상태:\s*(?:실제 compile·run 결과|미실행 예상 결과)(?:\s.*)?$"
        ),
    ),
    (
        "generated runnable harness prose",
        re.compile(
            r"(?im)^(?:This runnable harness|The runnable harness|"
            r"Listing\s+\d+\s+keeps\b|Chapter\s+\d+\s+source code\s+\d+\b|"
            r"Concrete\s+.{0,120}\s+definitions? replace\b).*$"
        ),
    ),
    (
        "synthetic runnable replacement",
        re.compile(
            r"(?ms)^```run-kotlin[ \t]*\n(?:(?!^```)[\s\S])*?"
            r"(?:println\(\"compiled\"\)|class\s+(?:VirtualTestScope|TestScheduler|"
            r"VirtualClock|FlowProbe|DeferredFailure)\b|"
            r"typealias\s+CoroutineExceptionHandler\s*=|"
            r"fun\s+(?:exceptionalFlow|myFlow)\(emit\s*:)"
            r"(?:(?!^```)[\s\S])*?^```[ \t]*$"
        ),
    ),
)


def book_reader_workflow_prose_violation(body: str) -> str | None:
    """Return the first generated learning-workflow marker in a book reader body."""

    for label, pattern in _BOOK_READER_WORKFLOW_PROSE:
        if pattern.search(body):
            return label
    return None

PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION = 1
PRIVATE_BOOK_RIGHTS_CONTRACT: dict[str, Any] = {
    "version": PRIVATE_BOOK_RIGHTS_CONTRACT_VERSION,
    "authorization": "explicit-user-approval-with-hash-pinned-receipt",
    "source": "user-purchased-byte-pinned-local-only-archive",
    "reader_output": "private-or-local-only-only",
    "allowed_workflow_entry": "source-landed",
    "prohibited_effects": [
        "external-transmission",
        "model-training",
        "publication",
        "redistribution",
    ],
    "restore": "atomic-intake-coverage-source-page-asset-index-update",
    "quarantine": "immutable-audit-evidence",
}
PRIVATE_BOOK_RIGHTS_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        PRIVATE_BOOK_RIGHTS_CONTRACT,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


def book_promotion_contract_fields(
    *,
    workflow_phase: str = "source-landed",
    translation_required: bool = False,
) -> dict[str, Any]:
    """Return the exact fields every newly generated promotion payload must carry."""

    return {
        "payload_schema_version": BOOK_PROMOTION_PAYLOAD_SCHEMA_VERSION,
        "book_contract": {
            "version": BOOK_CONTRACT_VERSION,
            "sha256": BOOK_CONTRACT_SHA256,
        },
        "workflow_phase": workflow_phase,
        "translation_required": translation_required,
    }


def require_current_book_contract(payload: object, operation: str) -> None:
    """Fail closed before applying a legacy or stale book promotion payload."""

    if not isinstance(payload, dict):
        raise WoonError(f"{operation} input must be an object")
    schema_version = payload.get("payload_schema_version")
    contract = payload.get("book_contract")
    if schema_version is None or contract is None:
        raise WoonError(
            f"{operation} input is legacy: payload_schema_version and book_contract are "
            "required; regenerate it with the current Woon Core"
        )
    if schema_version != BOOK_PROMOTION_PAYLOAD_SCHEMA_VERSION:
        raise WoonError(
            f"{operation} payload_schema_version mismatch: "
            f"expected={BOOK_PROMOTION_PAYLOAD_SCHEMA_VERSION} actual={schema_version}"
        )
    if not isinstance(contract, dict) or set(contract) != {"version", "sha256"}:
        raise WoonError(f"{operation} book_contract must contain version and sha256")
    if contract.get("version") != BOOK_CONTRACT_VERSION:
        raise WoonError(
            f"{operation} book contract version mismatch: "
            f"expected={BOOK_CONTRACT_VERSION} actual={contract.get('version')}"
        )
    compatible_hashes = {
        BOOK_CONTRACT_SHA256,
        LEGACY_BOOK_CONTRACT_SHA256_V7,
        PRE_IN_PAGE_H2_BOOK_CONTRACT_SHA256_V7,
        PRE_ORDERED_READER_SECTIONS_BOOK_CONTRACT_SHA256_V7,
    }
    if contract.get("sha256") not in compatible_hashes:
        raise WoonError(
            f"{operation} book contract hash mismatch: "
            f"expected one of={sorted(compatible_hashes)} actual={contract.get('sha256')}"
        )
    workflow_phase = payload.get("workflow_phase")
    if workflow_phase not in BOOK_WORKFLOW_PHASES:
        raise WoonError(
            f"{operation} workflow_phase must be one of: "
            f"{', '.join(BOOK_WORKFLOW_PHASES)}"
        )
    if not isinstance(payload.get("translation_required"), bool):
        raise WoonError(f"{operation} translation_required must be true or false")


def require_book_workflow_manifest(payload: dict[str, Any], operation: str) -> None:
    """Bind the payload workflow state to its schema-v3 coverage replacement."""

    coverage = payload.get("coverage_manifest")
    replacement = coverage.get("replacement") if isinstance(coverage, dict) else None
    if not isinstance(replacement, dict):
        raise WoonError(f"{operation} coverage_manifest.replacement must be an object")
    if replacement.get("schema_version") != 3:
        raise WoonError(f"{operation} v7 coverage replacement schema_version must be 3")
    if replacement.get("workflow_phase") != payload.get("workflow_phase"):
        raise WoonError(f"{operation} workflow_phase must match the coverage replacement")
    if replacement.get("translation_required") is not payload.get("translation_required"):
        raise WoonError(
            f"{operation} translation_required must match the coverage replacement"
        )


def book_workflow_phase_index(value: object) -> int:
    """Return the monotonic workflow rank or ``-1`` for an invalid phase."""

    try:
        return BOOK_WORKFLOW_PHASES.index(value)
    except ValueError:
        return -1
