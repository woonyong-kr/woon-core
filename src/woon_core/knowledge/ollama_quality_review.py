"""Run immutable Korean Wiki review batches through a local Ollama model."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.content_quality_evaluation import (
    RUBRIC,
    VERDICTS,
    criterion_anchor_candidates,
    review_verdict_consistency_error,
    validate_criterion_evidence,
)

PLAN_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CONTEXT_TOKENS = 32_768
# Ollama defaults to a short response.  One review needs six anchor selections
# plus the structured response envelope, so bind a sufficient output budget.
DEFAULT_RESPONSE_TOKENS = 2_048
MIN_CONTEXT_TOKENS = 4_096
MAX_CONTEXT_TOKENS = 32_768
RUN_MANIFEST_FILE = "run-manifest.json"
INHERITED_RESULTS_FILE = ".inherited-results.json"
# The cutoff measures the fully rendered prompt, not only Markdown.  Each
# tier leaves room for the JSON response even when Korean text tokenizes near
# one token per character.
ADAPTIVE_CONTEXT_POLICY = (
    (12_000, 16_384),
    (20_000, 24_576),
    (None, 32_768),
)
CRITERIA = tuple(sorted(RUBRIC))
LOCAL_ANCHOR_CANDIDATE_LIMIT = 6


class ReviewTarget(TypedDict):
    output_sha256: str
    markdown: str


@dataclass(frozen=True, slots=True)
class OllamaQualityReviewReport:
    """Observable local review run with immutable per-batch outputs."""

    model: str
    context_tokens: int
    adaptive_context: bool
    context_token_counts: dict[str, int]
    reviewed_batches: tuple[str, ...]
    skipped_batches: tuple[str, ...]
    retried_batches: tuple[str, ...]
    failed_batches: tuple[dict[str, str], ...]
    reviewed_pages: int
    results: str


def run_ollama_quality_reviews(
    plan_path: Path,
    results_dir: Path,
    *,
    model: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    adaptive_context: bool = False,
    continue_on_error: bool = False,
    batch_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Review missing batches locally and reject invalid model output.

    The model receives only an immutable plan batch.  Existing result files are
    never overwritten; callers can resume safely after an interrupted run.
    """

    normalized_model = _text(model, "Ollama model")
    _validate_timeout(timeout_seconds)
    _validate_max_attempts(max_attempts)
    _validate_context_tokens(context_tokens)
    endpoint = _local_ollama_endpoint()
    plan = _load_plan(plan_path)
    batches = _selected_batches(plan, batch_ids)
    plan_root = plan_path.expanduser().resolve().parent
    destination = results_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    inherited_results_sha256 = _validate_inherited_results(destination)
    _prepare_run_manifest(
        destination,
        {
            "version": 1,
            "plan_sha256": hashlib.sha256(plan_path.expanduser().read_bytes()).hexdigest(),
            "model": normalized_model,
            "context_tokens": context_tokens,
            "adaptive_context": adaptive_context,
            "adaptive_context_policy": _adaptive_context_manifest()
            if adaptive_context
            else None,
            "response_tokens": DEFAULT_RESPONSE_TOKENS,
            "inherited_results_sha256": inherited_results_sha256,
            "timeout_seconds": timeout_seconds,
            "max_attempts": max_attempts,
        },
    )
    reviewed: list[str] = []
    skipped: list[str] = []
    retried: list[str] = []
    failed: list[dict[str, str]] = []
    pages = 0
    context_token_counts: dict[str, int] = {}
    for manifest_batch in batches:
        batch_id = _text(manifest_batch.get("batch_id"), "quality review batch_id")
        result_path = destination / _safe_relative(
            _text(manifest_batch.get("result_file"), "quality review result_file")
        )
        try:
            batch = _input_batch(plan, manifest_batch, plan_root)
            targets = _targets(batch.get("targets"), batch_id)
            if len(targets) != 1:
                raise WoonError(
                    "local Ollama quality review requires exactly one page per batch"
                )
            if result_path.exists():
                _validate_result(
                    _load_json(result_path, "quality review result"), batch_id, targets
                )
                skipped.append(batch_id)
                continue
            batch_context_tokens = _context_tokens_for_batch(
                batch, context_tokens, adaptive_context
            )
            result, was_retried = _review_batch_with_retries(
                batch,
                batch_id,
                targets,
                normalized_model,
                endpoint,
                timeout_seconds,
                max_attempts,
                batch_context_tokens,
            )
            atomic_write(result_path, encode_json(result))
            reviewed.append(batch_id)
            if was_retried:
                retried.append(batch_id)
            pages += len(targets)
            context_key = str(batch_context_tokens)
            context_token_counts[context_key] = context_token_counts.get(context_key, 0) + 1
        except WoonError as error:
            if not continue_on_error:
                raise
            failed.append({"batch_id": batch_id, "error": str(error)})
    return asdict(
        OllamaQualityReviewReport(
            model=normalized_model,
            context_tokens=context_tokens,
            adaptive_context=adaptive_context,
            context_token_counts=context_token_counts,
            reviewed_batches=tuple(reviewed),
            skipped_batches=tuple(skipped),
            retried_batches=tuple(retried),
            failed_batches=tuple(failed),
            reviewed_pages=pages,
            results=str(destination),
        )
    )


def _review_batch_with_retries(
    batch: dict[str, Any],
    batch_id: str,
    targets: dict[str, ReviewTarget],
    model: str,
    endpoint: str,
    timeout_seconds: int,
    max_attempts: int,
    context_tokens: int,
) -> tuple[dict[str, object], bool]:
    last_validation_error: WoonError | None = None
    for attempt in range(1, max_attempts + 1):
        model_result = _run_ollama(
            _prompt(batch, last_validation_error),
            _response_schema(batch_id, targets),
            model,
            endpoint,
            timeout_seconds,
            context_tokens,
        )
        result = _expand_local_model_result(model_result, batch_id, targets)
        _canonicalize_model_evidence_reasons(result)
        try:
            _validate_result(result, batch_id, targets)
        except WoonError as error:
            last_validation_error = error
            if attempt == max_attempts:
                raise WoonError(
                    f"Ollama quality review could not produce a valid result after "
                    f"{max_attempts} attempts: {batch_id}: {error}"
                ) from error
        else:
            return result, attempt > 1
    raise AssertionError("quality review retry loop must return or raise")


def _context_tokens_for_batch(
    batch: dict[str, Any], fixed_context_tokens: int, adaptive_context: bool
) -> int:
    """Choose a conservative context tier from the exact model prompt size."""

    if not adaptive_context:
        return fixed_context_tokens
    prompt_length = len(_prompt(batch))
    for maximum_chars, context_tokens in ADAPTIVE_CONTEXT_POLICY:
        if maximum_chars is None or prompt_length <= maximum_chars:
            return context_tokens
    raise AssertionError("adaptive context policy must include a final tier")


def _adaptive_context_manifest() -> dict[str, object]:
    """Expose the policy so a resumed run cannot silently change review scope."""

    return {
        "version": 1,
        "tiers": [
            {
                "maximum_prompt_characters": maximum_chars,
                "context_tokens": context_tokens,
            }
            for maximum_chars, context_tokens in ADAPTIVE_CONTEXT_POLICY
        ],
    }


def _local_ollama_endpoint() -> str:
    raw_host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
    parsed = urlparse(raw_host if "://" in raw_host else f"http://{raw_host}")
    if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise WoonError("quality review only permits a loopback Ollama host")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise WoonError("OLLAMA_HOST must not include a path, query, or fragment")
    if not parsed.netloc:
        raise WoonError("OLLAMA_HOST must include a host")
    return f"{parsed.scheme or 'http'}://{parsed.netloc}/api/generate"


def _run_ollama(
    prompt: str,
    response_schema: dict[str, object],
    model: str,
    endpoint: str,
    timeout_seconds: int,
    context_tokens: int,
) -> dict[str, object]:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "format": response_schema,
            # Ollama 0.32 can return only its first response fragment when
            # stream=false.  Consume the JSONL stream through its final done
            # record instead, then validate the assembled JSON below.
            "stream": True,
            "think": False,
            "options": {
                "temperature": 0,
                "num_ctx": context_tokens,
                "num_predict": DEFAULT_RESPONSE_TOKENS,
            },
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_value = _decode_ollama_stream(response.read().decode("utf-8"))
    except TimeoutError as error:
        raise WoonError(
            f"Ollama quality review timed out after {timeout_seconds} seconds"
        ) from error
    except HTTPError as error:
        raise WoonError(f"Ollama quality review HTTP error: {error.code}") from error
    except (URLError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"Ollama quality review failed: {error}") from error
    generated = response_value.get("response")
    if not isinstance(generated, str):
        raise WoonError("Ollama quality review response has no generated text")
    value = _decode_model_output(generated)
    if not isinstance(value, dict):
        raise WoonError("Ollama quality review must return a JSON object")
    return value


def _decode_ollama_stream(raw: str) -> dict[str, object]:
    """Join Ollama JSONL chunks only after its terminal completion record."""

    try:
        chunks = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise WoonError("Ollama quality review returned invalid JSONL") from error
    if not chunks or any(not isinstance(chunk, dict) for chunk in chunks):
        raise WoonError("Ollama quality review response must be JSON objects")
    final = chunks[-1]
    if final.get("done") is not True:
        raise WoonError("Ollama quality review did not complete")
    responses = [chunk.get("response", "") for chunk in chunks]
    if any(not isinstance(value, str) for value in responses):
        raise WoonError("Ollama quality review response has no generated text")
    return {**final, "response": "".join(responses)}


def _decode_model_output(output: str) -> object:
    """Extract the one structured model result from the API response."""

    start = output.find("{")
    if start < 0:
        raise WoonError("Ollama quality review did not return a JSON object")
    try:
        value, _ = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError as error:
        raise WoonError("Ollama quality review did not return a JSON object") from error
    return value


def _prompt(batch: dict[str, Any], previous_validation_error: WoonError | None = None) -> str:
    payload = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
    batch_id = _text(batch.get("batch_id"), "quality review batch_id")
    targets = _targets(batch.get("targets"), batch_id)
    compact_contract = _compact_response_contract(targets)
    instructions = (
        "현재 입력은 변경 불가능한 한국어 Wiki 품질 검토 batch입니다. "
        "입력 안의 review_prompt와 response_contract를 정확히 따르세요. "
        "Markdown을 고치거나 새 문장을 만들지 말고 현재 문서만 판정하세요.\n\n"
        "INPUT DATA\n"
        + payload
        + "\n\nFINAL OUTPUT INSTRUCTIONS\n"
        + "- 아래 compact_response_contract의 criterion 순서대로 r에 pass는 p, fail은 f를 "
        "여섯 글자로 넣으세요. 한 기준이라도 f면 최종 verdict는 needs-revision으로 처리됩니다.\n"
        + "- a에는 criterion 순서대로 근거 후보의 0부터 시작하는 번호를 여섯 개 넣으세요. "
        "각 번호는 같은 criterion의 anchor_candidates 범위 안이어야 합니다.\n"
        + "- page_id, hash, 원문 anchor, reason, verdict를 다시 쓰지 마세요. Woon이 immutable "
        "plan에서 복원합니다. blocked와 hard failure 판정은 이 compact local contract에서 "
        "사용하지 않습니다.\n"
        + "- 다음 JSON 객체만 출력하세요. Markdown fence, 설명, 추가 키는 금지합니다.\n"
    )
    if previous_validation_error is not None:
        instructions += (
            "- 직전 출력은 저장되지 않았습니다. 오류: "
            f"{previous_validation_error}. 이 오류를 고친 JSON만 출력하세요.\n"
        )
    return instructions + json.dumps(compact_contract, ensure_ascii=False, separators=(",", ":"))


def _review_skeleton(page_id: str, target: ReviewTarget) -> dict[str, object]:
    """Seed the model with distinct valid anchors instead of six identical defaults."""

    used_anchors: set[str] = set()
    evidence_anchors: dict[str, str] = {}
    for criterion in sorted(RUBRIC):
        candidates = _criterion_anchor_candidates(target, criterion)
        anchor = next((value for value in candidates if value not in used_anchors), candidates[0])
        used_anchors.add(anchor)
        evidence_anchors[criterion] = anchor
    return {
        "page_id": page_id,
        "output_sha256": target["output_sha256"],
        "verdict": "passed | needs-revision | blocked",
        "rubric": {criterion: "pass | fail" for criterion in sorted(RUBRIC)},
        "hard_failures": [],
        "evidence_anchors": evidence_anchors,
    }


def _canonicalize_model_evidence_reasons(result: dict[str, object]) -> None:
    """Make local-model evidence rationale reproducible after it selects an anchor.

    The local model makes the semantic choice: pass/fail and a page-local anchor.
    It is not reliable at repeating six long Korean rationale templates verbatim.
    The stored reason is therefore generated from that choice, while external
    review payloads still undergo their original free-form rationale checks.
    """

    reviews = result.get("reviews")
    if not isinstance(reviews, list):
        return
    for raw_review in reviews:
        if not isinstance(raw_review, dict):
            continue
        rubric = raw_review.get("rubric")
        if not isinstance(rubric, dict):
            continue
        anchors = raw_review.pop("evidence_anchors", None)
        if isinstance(anchors, dict):
            raw_review["criterion_evidence"] = {
                criterion: {
                    "anchor": anchor,
                    "reason": _canonical_reason(criterion, str(rubric[criterion]), anchor),
                }
                for criterion, anchor in anchors.items()
                if criterion in RUBRIC
                and isinstance(anchor, str)
                and rubric.get(criterion) in {"pass", "fail"}
            }
        evidence = raw_review.get("criterion_evidence")
        if not isinstance(evidence, dict):
            continue
        for criterion, raw_item in evidence.items():
            if (
                criterion not in RUBRIC
                or not isinstance(raw_item, dict)
                or not isinstance(raw_item.get("anchor"), str)
                or rubric.get(criterion) not in {"pass", "fail"}
            ):
                continue
            anchor = raw_item["anchor"].strip()
            if not anchor:
                continue
            raw_item["reason"] = _canonical_reason(
                criterion, str(rubric[criterion]), anchor
            )


def _canonical_reason(criterion: str, score: str, anchor: str) -> str:
    pass_reasons = {
        "reader_goal": "독자가 무엇을 이해할지 보여 주므로 독자 목표 기준을 통과한다.",
        "logical_flow": "설명의 순서와 흐름을 드러내므로 논리 흐름 기준을 통과한다.",
        "natural_korean": (
            "문장 연결과 호응이 자연스럽게 이어짐을 보여 주므로 문체 기준을 통과한다."
        ),
        "evidence_boundary": "사실과 근거의 경계를 드러내므로 근거 경계 기준을 통과한다.",
        "revisitability": "제목과 용어로 다시 찾을 실마리를 남기므로 재탐색 기준을 통과한다.",
        "current_use": "현재 재사용 purpose를 밝히므로 현재 사용 기준을 통과한다.",
    }
    fail_reasons = {
        "reader_goal": "를 확인했지만 독자 목표가 충분히 드러나지 않아 보완이 필요하다.",
        "logical_flow": "를 확인했지만 설명의 순서와 흐름이 충분히 드러나지 않아 보완이 필요하다.",
        "natural_korean": (
            "를 확인했지만 문장 연결과 호응이 충분히 자연스럽지 않아 보완이 필요하다."
        ),
        "evidence_boundary": (
            "를 확인했지만 사실과 근거의 경계가 충분히 드러나지 않아 보완이 필요하다."
        ),
        "revisitability": "를 확인했지만 제목과 용어로 다시 찾을 실마리가 부족해 보완이 필요하다.",
        "current_use": "를 확인했지만 현재 재사용 purpose가 충분히 드러나지 않아 보완이 필요하다.",
    }
    quoted = f"인용한 부분 “{anchor}”"
    if score == "pass":
        return quoted + "에서 " + pass_reasons[criterion]
    return quoted + fail_reasons[criterion]


def _response_schema(
    batch_id: str, targets: dict[str, ReviewTarget]
) -> dict[str, object]:
    """Constrain the compact local response without asking for immutable IDs."""

    if len(targets) != 1:
        raise WoonError("local Ollama quality review requires exactly one page per batch")
    candidate_count = max(len(values) for values in _compact_anchor_candidates(targets).values())
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["r", "a"],
        "properties": {
            "r": {"type": "string", "pattern": f"^[pf]{{{len(CRITERIA)}}}$"},
            "a": {
                "type": "array",
                "minItems": len(CRITERIA),
                "maxItems": len(CRITERIA),
                "items": {"type": "integer", "minimum": 0, "maximum": candidate_count - 1},
            },
        },
    }


def _compact_anchor_candidates(targets: dict[str, ReviewTarget]) -> dict[str, list[str]]:
    """Keep local-model choices short while retaining page-local evidence."""

    if len(targets) != 1:
        raise WoonError("local Ollama quality review requires exactly one page per batch")
    target = next(iter(targets.values()))
    return {
        criterion: _criterion_anchor_candidates(target, criterion)[:LOCAL_ANCHOR_CANDIDATE_LIMIT]
        for criterion in CRITERIA
    }


def _compact_response_contract(targets: dict[str, ReviewTarget]) -> dict[str, object]:
    return {
        "criterion_order": list(CRITERIA),
        "anchor_candidates": _compact_anchor_candidates(targets),
        "response": {"r": "pppppp", "a": [0] * len(CRITERIA)},
    }


def _expand_local_model_result(
    result: dict[str, object], batch_id: str, targets: dict[str, ReviewTarget]
) -> dict[str, object]:
    """Restore immutable identifiers and page-local anchors after compact inference."""

    # Keep test fixtures and manually captured legacy local responses readable;
    # production schema only permits the compact representation below.
    if {"version", "batch_id", "reviews"}.issubset(result):
        return result
    if set(result) != {"r", "a"}:
        raise WoonError("Ollama quality review compact response must contain only r and a")
    states = result["r"]
    indexes = result["a"]
    if not isinstance(states, str) or len(states) != len(CRITERIA) or set(states) - {"p", "f"}:
        raise WoonError("Ollama quality review compact rubric must be six p or f characters")
    if not isinstance(indexes, list) or len(indexes) != len(CRITERIA):
        raise WoonError("Ollama quality review compact anchors must contain six indexes")
    page_id, target = next(iter(sorted(targets.items())))
    candidates = _compact_anchor_candidates(targets)
    rubric = {
        criterion: "pass" if state == "p" else "fail"
        for criterion, state in zip(CRITERIA, states, strict=True)
    }
    anchors: dict[str, str] = {}
    for criterion, raw_index in zip(CRITERIA, indexes, strict=True):
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise WoonError("Ollama quality review compact anchor index must be an integer")
        options = candidates[criterion]
        if raw_index < 0 or raw_index >= len(options):
            raise WoonError(
                f"Ollama quality review compact anchor index is out of range: {criterion}"
            )
        anchors[criterion] = options[raw_index]
    verdict = "passed" if all(value == "pass" for value in rubric.values()) else "needs-revision"
    return {
        "version": PLAN_VERSION,
        "batch_id": batch_id,
        "reviews": [
            {
                "page_id": page_id,
                "output_sha256": target["output_sha256"],
                "verdict": verdict,
                "rubric": rubric,
                "hard_failures": [],
                "evidence_anchors": anchors,
            }
        ],
    }


def _load_plan(path: Path) -> dict[str, Any]:
    plan = _load_json(path, "quality review plan")
    if plan.get("version") != PLAN_VERSION or not isinstance(plan.get("batches"), list):
        raise WoonError("quality review plan has unsupported version or batches")
    return plan


def _input_batch(
    plan: dict[str, Any], manifest_batch: dict[str, Any], plan_root: Path
) -> dict[str, Any]:
    """Load the full immutable review input behind the compact manifest."""

    batch_id = _text(manifest_batch.get("batch_id"), "quality review batch_id")
    input_file = _safe_relative(
        _text(manifest_batch.get("input_file"), "quality review input_file")
    )
    batch = _load_json(plan_root / input_file, f"quality review input {batch_id}")
    if batch.get("version") != PLAN_VERSION or batch.get("batch_id") != batch_id:
        raise WoonError(f"quality review input does not match batch: {batch_id}")
    manifest_targets = _target_hashes(manifest_batch.get("targets"), batch_id)
    input_targets = _target_hashes(batch.get("targets"), batch_id)
    if input_targets != manifest_targets:
        raise WoonError(f"quality review input does not match manifest: {batch_id}")
    for reference_name in ("standard", "prompt"):
        if reference_name in plan and batch.get(reference_name) != plan[reference_name]:
            raise WoonError(
                f"quality review input has a different {reference_name}: {batch_id}"
            )
    return batch


def _selected_batches(plan: dict[str, Any], requested: tuple[str, ...]) -> list[dict[str, Any]]:
    batches = plan["batches"]
    assert isinstance(batches, list)
    indexed: dict[str, dict[str, Any]] = {}
    for raw_batch in batches:
        if not isinstance(raw_batch, dict):
            raise WoonError("quality review plan batch must be an object")
        batch_id = _text(raw_batch.get("batch_id"), "quality review batch_id")
        if batch_id in indexed:
            raise WoonError(f"duplicate quality review batch: {batch_id}")
        indexed[batch_id] = raw_batch
    if not requested:
        return [indexed[batch_id] for batch_id in sorted(indexed)]
    if len(set(requested)) != len(requested):
        raise WoonError("quality review batch may only be requested once")
    missing = sorted(set(requested).difference(indexed))
    if missing:
        raise WoonError("quality review plan has no batch: " + missing[0])
    return [indexed[batch_id] for batch_id in requested]


def _targets(value: object, batch_id: str) -> dict[str, ReviewTarget]:
    hashes = _target_hashes(value, batch_id)
    assert isinstance(value, list)
    targets: dict[str, ReviewTarget] = {}
    for raw_target in value:
        if not isinstance(raw_target, dict):
            raise WoonError(f"quality review batch {batch_id} target must be an object")
        page_id = _text(raw_target.get("page_id"), "quality review page_id")
        markdown = _text(raw_target.get("markdown"), "quality review markdown")
        targets[page_id] = {
            "output_sha256": hashes[page_id],
            "markdown": markdown,
        }
    return targets


def _criterion_anchor_candidates(target: ReviewTarget, criterion: str) -> list[str]:
    return criterion_anchor_candidates(target["markdown"], criterion)


def _target_hashes(value: object, batch_id: str) -> dict[str, str]:
    if not isinstance(value, list) or not value:
        raise WoonError(f"quality review batch {batch_id} has no targets")
    targets: dict[str, str] = {}
    for raw_target in value:
        if not isinstance(raw_target, dict):
            raise WoonError(f"quality review batch {batch_id} target must be an object")
        page_id = _text(raw_target.get("page_id"), "quality review page_id")
        if page_id in targets:
            raise WoonError(f"duplicate quality review page in batch {batch_id}: {page_id}")
        targets[page_id] = _digest(raw_target.get("output_sha256"), "quality review output_sha256")
    return targets


def _validate_result(
    result: dict[str, object], batch_id: str, targets: dict[str, ReviewTarget]
) -> None:
    if result.get("version") != PLAN_VERSION or result.get("batch_id") != batch_id:
        raise WoonError(f"Ollama quality review result does not match batch: {batch_id}")
    reviews = result.get("reviews")
    if not isinstance(reviews, list):
        raise WoonError(f"Ollama quality review result has no reviews: {batch_id}")
    received: dict[str, dict[str, object]] = {}
    for raw_review in reviews:
        if not isinstance(raw_review, dict):
            raise WoonError(f"Ollama quality review entry is not an object: {batch_id}")
        page_id = _text(raw_review.get("page_id"), "quality review page_id")
        if page_id in received:
            raise WoonError(f"duplicate Ollama quality review: {page_id}")
        if raw_review.get("verdict") not in VERDICTS:
            raise WoonError(f"Ollama quality review has invalid verdict: {page_id}")
        target = targets.get(page_id)
        if target is None or _digest(
            raw_review.get("output_sha256"), "quality review output_sha256"
        ) != target["output_sha256"]:
            raise WoonError(f"Ollama quality review is stale or unknown: {page_id}")
        rubric = raw_review.get("rubric")
        if not isinstance(rubric, dict) or set(rubric) != RUBRIC or any(
            score not in {"pass", "fail"} for score in rubric.values()
        ):
            raise WoonError(f"Ollama quality review has invalid rubric: {page_id}")
        hard_failures = raw_review.get("hard_failures")
        if not isinstance(hard_failures, list) or any(
            not isinstance(item, str) or not item.strip() for item in hard_failures
        ):
            raise WoonError(f"Ollama quality review has invalid hard_failures: {page_id}")
        consistency_error = review_verdict_consistency_error(
            str(raw_review["verdict"]),
            {criterion: str(rubric[criterion]) for criterion in RUBRIC},
            [str(item) for item in hard_failures],
            page_id,
        )
        if consistency_error is not None:
            raise WoonError(consistency_error)
        validate_criterion_evidence(
            raw_review.get("criterion_evidence"),
            {criterion: str(rubric[criterion]) for criterion in RUBRIC},
            page_id,
            target["markdown"],
        )
        received[page_id] = raw_review
    if set(received) != set(targets):
        raise WoonError(f"Ollama quality review result does not cover batch: {batch_id}")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be an object")
    return value


def _prepare_run_manifest(destination: Path, expected: dict[str, object]) -> None:
    """Bind resumable batch results to the model context that read them."""

    path = destination / RUN_MANIFEST_FILE
    if path.exists():
        actual = _load_json(path, "quality review run manifest")
        if actual != expected:
            raise WoonError("quality review results use a different execution manifest")
        return
    if any(destination.glob("*.result.json")) and expected.get("inherited_results_sha256") is None:
        raise WoonError("quality review results are missing their execution manifest")
    atomic_write(path, encode_json(expected))


def _validate_inherited_results(destination: Path) -> str | None:
    """Permit a rebase only when every copied result is provenance-bound."""

    marker_path = destination / INHERITED_RESULTS_FILE
    if (destination / RUN_MANIFEST_FILE).is_file():
        if not marker_path.is_file():
            return None
        return hashlib.sha256(marker_path.read_bytes()).hexdigest()
    result_paths = sorted(destination.glob("*.result.json"))
    if not result_paths:
        if marker_path.exists():
            raise WoonError("quality review inherited results marker has no result files")
        return None
    if not marker_path.is_file():
        return None
    marker = _load_json(marker_path, "quality review inherited results marker")
    if set(marker) != {
        "version",
        "prior_plan_sha256",
        "prior_run_manifest_sha256",
        "result_files",
    } or marker.get("version") != 1:
        raise WoonError("quality review inherited results marker has an invalid schema")
    _digest(marker.get("prior_plan_sha256"), "inherited quality review prior_plan_sha256")
    _digest(
        marker.get("prior_run_manifest_sha256"),
        "inherited quality review prior_run_manifest_sha256",
    )
    listed = marker.get("result_files")
    if not isinstance(listed, list) or not listed:
        raise WoonError("quality review inherited results marker has no result files")
    listed_paths: set[Path] = set()
    for raw_record in listed:
        if not isinstance(raw_record, dict) or set(raw_record) != {"path", "sha256"}:
            raise WoonError("quality review inherited results marker has an invalid result file")
        relative = _safe_relative(_text(raw_record.get("path"), "inherited result path"))
        expected_digest = _digest(raw_record.get("sha256"), "inherited result sha256")
        if relative in listed_paths:
            raise WoonError("quality review inherited results marker has duplicate result files")
        listed_paths.add(relative)
        actual_path = destination / relative
        actual_digest = (
            hashlib.sha256(actual_path.read_bytes()).hexdigest()
            if actual_path.is_file()
            else None
        )
        if actual_digest != expected_digest:
            raise WoonError("quality review inherited result does not match its provenance marker")
    actual_paths = {path.relative_to(destination) for path in result_paths}
    if actual_paths != listed_paths:
        raise WoonError("quality review inherited results marker does not cover existing results")
    return hashlib.sha256(marker_path.read_bytes()).hexdigest()


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise WoonError("quality review result_file must be a safe relative path")
    return path


def _validate_timeout(value: int) -> None:
    if not 30 <= value <= 3600:
        raise WoonError("Ollama quality review timeout must be between 30 and 3600 seconds")


def _validate_max_attempts(value: int) -> None:
    if not 1 <= value <= 5:
        raise WoonError("Ollama quality review max_attempts must be between 1 and 5")


def _validate_context_tokens(value: int) -> None:
    if not MIN_CONTEXT_TOKENS <= value <= MAX_CONTEXT_TOKENS:
        raise WoonError(
            "Ollama quality review context_tokens must be between "
            f"{MIN_CONTEXT_TOKENS} and {MAX_CONTEXT_TOKENS}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{field} must be non-empty text")
    return value.strip()


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise WoonError(f"{field} must be a SHA-256 digest")
    return value
