"""Verify LLM answer citations against the current Woon knowledge index.

This evaluator proves a mechanical boundary: every declared claim has a
resolvable, current excerpt and an exact quote.  It deliberately keeps semantic
entailment separate, because matching a quote does not by itself prove that an
LLM paraphrase is true.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.factory import build_knowledge_service

SEMANTIC_VERDICTS = {"supported", "unsupported", "uncertain"}


@dataclass(frozen=True, slots=True)
class AnswerCaseResult:
    """One answer's mechanical citation outcome."""

    identifier: str
    passed: bool
    claims: int
    citations: int
    errors: tuple[str, ...]


def evaluate_answer_citations(
    vault: Path, cases_path: Path, answers_path: Path
) -> dict[str, object]:
    """Evaluate one generator payload against the supplied retrieval cases.

    ``answers_path`` is intentionally provider-neutral.  A generator can be an
    LLM, a scripted baseline, or a human draft as long as it emits claim units
    and citation identifiers.  A semantic judgment is optional but is required
    before the top-level result can be considered fully passed.
    """

    cases = _load_cases(cases_path)
    payload = _load_object(answers_path, "answer payload")
    generator = _generator(payload.get("generator"))
    answers = _answers_by_case(payload.get("answers"), set(cases))
    _, service = build_knowledge_service(vault.expanduser().resolve())
    service.reindex()

    results: list[AnswerCaseResult] = []
    all_claim_keys: set[tuple[str, str]] = set()
    for identifier, case in cases.items():
        result, claim_keys = _evaluate_case(service, identifier, case, answers[identifier])
        results.append(result)
        all_claim_keys.update(claim_keys)

    semantic = _evaluate_semantics(payload.get("semantic_judgments"), all_claim_keys)
    mechanical_passed = all(result.passed for result in results)
    semantic_passed = semantic["status"] == "passed"
    return {
        "version": 1,
        "passed": mechanical_passed and semantic_passed,
        "generator": generator,
        "mechanical": {
            "passed": mechanical_passed,
            "cases": len(results),
            "claims": sum(result.claims for result in results),
            "citations": sum(result.citations for result in results),
        },
        "semantic": semantic,
        "cases": [asdict(result) for result in results],
    }


def _load_cases(path: Path) -> dict[str, dict[str, object]]:
    payload = _load_object(path, "evaluation cases")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise WoonError("evaluation cases must contain a non-empty cases list")
    cases: dict[str, dict[str, object]] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise WoonError("evaluation case must be an object")
        identifier = _text(raw_case.get("id"), "case.id")
        if identifier in cases:
            raise WoonError(f"evaluation cases contain duplicate id: {identifier}")
        relevant = raw_case.get("relevant", raw_case.get("expected_any", []))
        if not isinstance(relevant, list) or any(
            not isinstance(path, str) or not path.strip() for path in relevant
        ):
            raise WoonError(f"evaluation case {identifier!r} relevant must be a string list")
        expect_empty = bool(raw_case.get("expect_empty", False))
        if expect_empty == bool(relevant):
            raise WoonError(
                f"evaluation case {identifier!r} must define relevant or expect_empty"
            )
        cases[identifier] = {
            "relevant": {path.strip() for path in relevant},
            "expect_empty": expect_empty,
        }
    return cases


def _generator(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise WoonError("answer payload generator must be an object")
    return {
        "name": _text(raw.get("name"), "generator.name"),
        "version": _text(raw.get("version"), "generator.version"),
        "run_id": _text(raw.get("run_id"), "generator.run_id"),
    }


def _answers_by_case(raw: object, expected_cases: set[str]) -> dict[str, dict[str, object]]:
    if not isinstance(raw, list):
        raise WoonError("answer payload answers must be a list")
    answers: dict[str, dict[str, object]] = {}
    for answer in raw:
        if not isinstance(answer, dict):
            raise WoonError("answer payload answer must be an object")
        identifier = _text(answer.get("case_id"), "answer.case_id")
        if identifier in answers:
            raise WoonError(f"answer payload contains duplicate case_id: {identifier}")
        answers[identifier] = answer
    missing = sorted(expected_cases.difference(answers))
    unexpected = sorted(set(answers).difference(expected_cases))
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise WoonError(
            "answer payload case IDs do not match evaluation cases: " + " ".join(details)
        )
    return answers


def _evaluate_case(
    service: Any, identifier: str, case: dict[str, object], answer: dict[str, object]
) -> tuple[AnswerCaseResult, set[tuple[str, str]]]:
    text = answer.get("answer")
    claims = answer.get("claims")
    citations = answer.get("citations")
    errors: list[str] = []
    claim_keys: set[tuple[str, str]] = set()
    if not isinstance(text, str):
        errors.append("answer must be text")
        text = ""
    if not isinstance(claims, list):
        errors.append("claims must be a list")
        claims = []
    if not isinstance(citations, list):
        errors.append("citations must be a list")
        citations = []
    expect_empty = case["expect_empty"] is True
    if expect_empty:
        if text.strip() or claims or citations:
            errors.append("empty-result case must not contain an answer, claim, or citation")
        return (
            AnswerCaseResult(identifier, not errors, 0, 0, tuple(errors)),
            claim_keys,
        )
    if not text.strip():
        errors.append("positive case must contain an answer")
    citation_map = _citation_map(service, identifier, citations, case["relevant"], errors)
    seen_claims: set[str] = set()
    for raw_claim in claims:
        if not isinstance(raw_claim, dict):
            errors.append("claim must be an object")
            continue
        claim_id = _claim_id(raw_claim.get("id"), identifier, errors)
        claim_text = raw_claim.get("text")
        citation_ids = raw_claim.get("citation_ids")
        if claim_id is None:
            continue
        if claim_id in seen_claims:
            errors.append(f"duplicate claim id: {claim_id}")
            continue
        seen_claims.add(claim_id)
        claim_keys.add((identifier, claim_id))
        if not isinstance(claim_text, str) or not claim_text.strip():
            errors.append(f"claim {claim_id} must have text")
        elif not _normalized_contains(text, claim_text):
            errors.append(f"claim {claim_id} text must appear in answer")
        if not isinstance(citation_ids, list) or not citation_ids or any(
            not isinstance(item, str) or not item.strip() for item in citation_ids
        ):
            errors.append(f"claim {claim_id} must reference one or more citation IDs")
        else:
            missing = sorted({item.strip() for item in citation_ids}.difference(citation_map))
            if missing:
                errors.append(
                    f"claim {claim_id} references unknown citation IDs: {','.join(missing)}"
                )
    if not claims:
        errors.append("positive case must contain at least one claim")
    if not citations:
        errors.append("positive case must contain at least one citation")
    return (
        AnswerCaseResult(
            identifier=identifier,
            passed=not errors,
            claims=len(seen_claims),
            citations=len(citation_map),
            errors=tuple(errors),
        ),
        claim_keys,
    )


def _citation_map(
    service: Any,
    case_id: str,
    citations: list[object],
    relevant: object,
    errors: list[str],
) -> dict[str, object]:
    relevant_paths = relevant if isinstance(relevant, set) else set()
    resolved: dict[str, object] = {}
    for raw_citation in citations:
        if not isinstance(raw_citation, dict):
            errors.append("citation must be an object")
            continue
        citation_id = _citation_id(raw_citation.get("id"), errors)
        if citation_id is None:
            continue
        if citation_id in resolved:
            errors.append(f"duplicate citation id: {citation_id}")
            continue
        document_id = raw_citation.get("document_id")
        chunk_id = raw_citation.get("chunk_id")
        relative_path = raw_citation.get("relative_path")
        revision = raw_citation.get("revision")
        quote = raw_citation.get("quote")
        citation_fields = (document_id, chunk_id, relative_path, revision, quote)
        if not all(isinstance(value, str) and value.strip() for value in citation_fields):
            errors.append(
                f"citation {citation_id} requires document, chunk, path, revision, and quote"
            )
            continue
        assert isinstance(document_id, str)
        assert isinstance(chunk_id, str)
        assert isinstance(relative_path, str)
        assert isinstance(revision, str)
        assert isinstance(quote, str)
        if len(_normalize(quote)) < 5:
            errors.append(f"citation {citation_id} quote is too short")
            continue
        try:
            excerpt = service.read_excerpt(document_id.strip(), chunk_id.strip())
        except WoonError as error:
            errors.append(f"citation {citation_id} cannot resolve: {error}")
            continue
        if excerpt.relative_path != relative_path.strip():
            errors.append(f"citation {citation_id} relative_path is stale or incorrect")
        if excerpt.revision != revision.strip():
            errors.append(f"citation {citation_id} revision is stale or incorrect")
        if not _normalized_contains(excerpt.text, quote):
            errors.append(f"citation {citation_id} quote is not in the current excerpt")
        if excerpt.relative_path not in relevant_paths:
            errors.append(
                f"citation {citation_id} is outside this case's approved evidence path"
            )
        resolved[citation_id] = excerpt
    return resolved


def _evaluate_semantics(raw: object, claim_keys: set[tuple[str, str]]) -> dict[str, object]:
    if raw is None:
        return {
            "status": "not-evaluated",
            "reason": "mechanical citation checks do not prove semantic entailment",
        }
    if not isinstance(raw, dict):
        raise WoonError("semantic_judgments must be an object")
    evaluator = raw.get("evaluator")
    if not isinstance(evaluator, dict):
        raise WoonError("semantic_judgments.evaluator must be an object")
    evaluator_metadata = {
        "name": _text(evaluator.get("name"), "semantic evaluator.name"),
        "version": _text(evaluator.get("version"), "semantic evaluator.version"),
        "prompt_sha256": _sha256_text(
            _text(evaluator.get("prompt_sha256"), "semantic evaluator.prompt_sha256")
        ),
    }
    if evaluator_metadata["prompt_sha256"] != evaluator.get("prompt_sha256"):
        raise WoonError("semantic evaluator.prompt_sha256 must be a SHA-256 digest")
    judgments = raw.get("claims")
    if not isinstance(judgments, list):
        raise WoonError("semantic_judgments.claims must be a list")
    verdicts: dict[tuple[str, str], str] = {}
    for judgment in judgments:
        if not isinstance(judgment, dict):
            raise WoonError("semantic judgment must be an object")
        key = (
            _text(judgment.get("case_id"), "semantic judgment.case_id"),
            _text(judgment.get("claim_id"), "semantic judgment.claim_id"),
        )
        if key in verdicts:
            raise WoonError(f"duplicate semantic judgment: {key[0]}/{key[1]}")
        verdict = _text(judgment.get("verdict"), "semantic judgment.verdict")
        if verdict not in SEMANTIC_VERDICTS:
            raise WoonError(
                "semantic judgment.verdict must be supported, unsupported, or uncertain"
            )
        _text(judgment.get("rationale"), "semantic judgment.rationale")
        verdicts[key] = verdict
    if set(verdicts) != claim_keys:
        raise WoonError("semantic judgments must cover exactly the declared answer claims")
    supported = sum(verdict == "supported" for verdict in verdicts.values())
    return {
        "status": "passed" if supported == len(claim_keys) else "failed",
        "evaluator": evaluator_metadata,
        "claims": len(claim_keys),
        "supported_claims": supported,
    }


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        loaded = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if not isinstance(loaded, dict):
        raise WoonError(f"{label} must be an object")
    return loaded


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{field} must be non-empty text")
    return value.strip()


def _claim_id(value: object, case_id: str, errors: list[str]) -> str | None:
    try:
        return _text(value, "claim.id")
    except WoonError:
        errors.append(f"case {case_id} claim id must be non-empty text")
        return None


def _citation_id(value: object, errors: list[str]) -> str | None:
    try:
        return _text(value, "citation.id")
    except WoonError:
        errors.append("citation id must be non-empty text")
        return None


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _normalized_contains(container: str, value: str) -> bool:
    return _normalize(value) in _normalize(container)


def _sha256_text(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        return ""
    return value
