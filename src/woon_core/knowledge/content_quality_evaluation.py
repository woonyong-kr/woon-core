"""Fail closed when a Wiki quality review no longer matches compiled pages.

The compiler proves provenance and reproducibility.  This module deliberately
does not pretend that those structural checks prove Korean prose quality.  A
human or LLM reviewer supplies semantic judgments, while this evaluator proves
that the review covered every current compiled page and no judgment went stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError

RUBRIC = {
    "reader_goal",
    "logical_flow",
    "natural_korean",
    "evidence_boundary",
    "revisitability",
    "current_use",
}
VERDICTS = {"passed", "needs-revision", "blocked"}
RUBRIC_EVIDENCE_TERMS = {
    "reader_goal": ("독자", "질문", "목표", "이해", "판단", "실행"),
    "logical_flow": ("순서", "흐름", "먼저", "이후", "앞", "뒤", "이유", "원인", "결과", "이어"),
    "natural_korean": ("문장", "호응", "연결", "표현", "번역", "문체", "서술어", "접속"),
    "evidence_boundary": ("사실", "근거", "해석", "한계", "출처", "검증", "실행", "확인"),
    "revisitability": ("제목", "heading", "용어", "검색", "다시", "찾", "section", "목차"),
    "current_use": ("purpose", "목적", "현재", "재사용", "학습", "설명", "검색"),
}


def evaluate_content_quality(
    vault: Path, reviews_path: Path, standard_path: Path, prompt_path: Path
) -> dict[str, object]:
    """Validate semantic quality reviews against the exact compiled output.

    The review payload is provider-neutral so a human, local LLM, or hosted LLM
    can use the same acceptance boundary.  A structural match alone cannot
    pass: every current page needs a passed review with all rubric dimensions.
    """

    expected = _compiled_pages(vault)
    markdown_by_page = _compiled_markdown(vault, expected)
    payload = _load_object(reviews_path, "content quality review")
    if payload.get("version") != 1:
        raise WoonError("content quality review version must be 1")
    evaluator = _evaluator(payload.get("evaluator"))
    standard = _standard(payload.get("standard"))
    actual_standard_sha256 = _file_sha256(standard_path, "content quality standard")
    actual_prompt_sha256 = _file_sha256(prompt_path, "content quality review prompt")
    raw_reviews = payload.get("reviews")
    if not isinstance(raw_reviews, list):
        raise WoonError("content quality review reviews must be a list")

    errors: list[str] = []
    if standard["sha256"] != actual_standard_sha256:
        errors.append("content quality review used a stale or incorrect writing standard")
    if evaluator["prompt_sha256"] != actual_prompt_sha256:
        errors.append("content quality review used a stale or incorrect review prompt")
    reviewed: set[str] = set()
    stale = 0
    rejected = 0
    for raw_review in raw_reviews:
        if not isinstance(raw_review, dict):
            raise WoonError("content quality review entry must be an object")
        page_id = _text(raw_review.get("page_id"), "quality review page_id")
        if page_id in reviewed:
            raise WoonError(f"duplicate content quality review: {page_id}")
        reviewed.add(page_id)
        if page_id not in expected:
            errors.append(f"quality review references unknown page: {page_id}")
            continue
        output_sha256 = _digest(raw_review.get("output_sha256"), "quality review output_sha256")
        if output_sha256 != expected[page_id]:
            stale += 1
            errors.append(f"quality review is stale for current compiled page: {page_id}")
        verdict = _text(raw_review.get("verdict"), "quality review verdict")
        if verdict not in VERDICTS:
            raise WoonError("quality review verdict must be passed, needs-revision, or blocked")
        rubric = _rubric(raw_review.get("rubric"), page_id)
        hard_failures = _string_list(
            raw_review.get("hard_failures"), "quality review hard_failures"
        )
        validate_criterion_evidence(
            raw_review.get("criterion_evidence"), rubric, page_id, markdown_by_page[page_id]
        )
        consistency_error = review_verdict_consistency_error(
            verdict, rubric, hard_failures, page_id
        )
        if consistency_error is not None:
            errors.append(consistency_error)
        if verdict != "passed":
            rejected += 1
            errors.append(f"quality review is not accepted: {page_id} ({verdict})")

    missing = sorted(set(expected).difference(reviewed))
    if missing:
        errors.append(f"content quality review is missing {len(missing)} compiled pages")
    return {
        "version": 1,
        "passed": not errors,
        "evaluator": evaluator,
        "standard": standard,
        "prompt": {"sha256": actual_prompt_sha256},
        "coverage": {
            "compiled_pages": len(expected),
            "reviewed_pages": len(reviewed.intersection(expected)),
            "missing_pages": len(missing),
            "stale_reviews": stale,
            "rejected_reviews": rejected,
        },
        "errors": errors,
    }


def review_verdict_consistency_error(
    verdict: str, rubric: dict[str, str], hard_failures: list[str], page_id: str
) -> str | None:
    """Keep a review's overall verdict consistent with its own evidence fields."""

    all_passed = all(rubric[criterion] == "pass" for criterion in RUBRIC)
    if verdict == "passed" and (not all_passed or hard_failures):
        return f"passed quality review has unmet rubric or hard failure: {page_id}"
    if verdict == "needs-revision" and all_passed:
        return f"needs-revision quality review has no failed rubric: {page_id}"
    if verdict == "needs-revision" and hard_failures:
        return f"needs-revision quality review has a hard failure: {page_id}"
    if verdict == "blocked" and not hard_failures:
        return f"blocked quality review has no hard failure: {page_id}"
    return None


def _compiled_pages(vault: Path) -> dict[str, str]:
    pages = _load_yaml_list(vault / "catalog/llm-wiki/pages.yaml", "pages")
    receipts = _load_yaml_list(vault / "catalog/llm-wiki/receipts.yaml", "receipts")
    page_ids: set[str] = set()
    for page in pages:
        page_id = _text(page.get("page_id"), "page spec page_id")
        if page_id in page_ids:
            raise WoonError(f"duplicate compiled page spec: {page_id}")
        page_ids.add(page_id)
    output_hashes: dict[str, str] = {}
    for receipt in receipts:
        page_id = _text(receipt.get("page_id"), "receipt page_id")
        if page_id in output_hashes:
            raise WoonError(f"duplicate compiled receipt: {page_id}")
        output_hashes[page_id] = _digest(receipt.get("output_sha256"), "receipt output_sha256")
    missing = sorted(page_ids.difference(output_hashes))
    extra = sorted(set(output_hashes).difference(page_ids))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing receipts=" + ",".join(missing))
        if extra:
            details.append("orphan receipts=" + ",".join(extra))
        raise WoonError(
            "compiled quality evaluation requires current receipts: " + " ".join(details)
        )
    return {page_id: output_hashes[page_id] for page_id in page_ids}


def _compiled_markdown(vault: Path, expected: dict[str, str]) -> dict[str, str]:
    """Load current bytes so review evidence cannot be generic boilerplate."""

    pages = _load_yaml_list(vault / "catalog/llm-wiki/pages.yaml", "pages")
    markdown_by_page: dict[str, str] = {}
    for page in pages:
        page_id = _text(page.get("page_id"), "page spec page_id")
        output_path = _text(page.get("output_path"), "page spec output_path")
        relative = Path(output_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise WoonError("page spec output_path must be a safe relative path")
        try:
            markdown = (vault / "wiki" / relative).read_text(encoding="utf-8")
        except OSError as error:
            raise WoonError(
                f"cannot read compiled page for quality review: {page_id}: {error}"
            ) from error
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        if digest != expected[page_id]:
            raise WoonError(f"compiled page bytes do not match receipt: {page_id}")
        markdown_by_page[page_id] = markdown
    return markdown_by_page


def validate_criterion_evidence(
    value: object, rubric: dict[str, str], page_id: str, markdown: str
) -> dict[str, dict[str, str]]:
    """Require a page-local observation for every independent quality verdict."""

    if not isinstance(value, dict) or set(value) != RUBRIC:
        raise WoonError(
            f"quality review criterion_evidence must contain exactly {sorted(RUBRIC)}: {page_id}"
        )
    validated: dict[str, dict[str, str]] = {}
    anchors: list[str] = []
    reasons: list[str] = []
    for criterion in sorted(RUBRIC):
        raw_item = value[criterion]
        if not isinstance(raw_item, dict) or set(raw_item) != {"anchor", "reason"}:
            raise WoonError(
                "quality review criterion evidence requires anchor and reason: "
                f"{page_id}/{criterion}"
            )
        anchor = _text(raw_item.get("anchor"), "quality review criterion evidence anchor")
        reason = _text(raw_item.get("reason"), "quality review criterion evidence reason")
        if "__CRITERION_REASON__" in reason:
            raise WoonError(
                "quality review criterion evidence kept its placeholder: "
                f"{page_id}/{criterion}"
            )
        if len(reason) < 16:
            raise WoonError(
                f"quality review criterion evidence reason is too short: {page_id}/{criterion}"
            )
        if anchor not in markdown:
            raise WoonError(
                f"quality review criterion evidence anchor is absent from compiled page: "
                f"{page_id}/{criterion}"
            )
        if anchor not in criterion_anchor_candidates(markdown, criterion):
            raise WoonError(
                "quality review criterion evidence anchor does not match its allowed "
                f"document role: {page_id}/{criterion}"
            )
        if anchor not in reason:
            raise WoonError(
                "quality review criterion evidence reason must quote its anchor: "
                f"{page_id}/{criterion}"
            )
        if _reason_denies_anchor(reason, anchor):
            raise WoonError(
                "quality review criterion evidence reason denies its own anchor: "
                f"{page_id}/{criterion}"
            )
        if not any(term in reason for term in RUBRIC_EVIDENCE_TERMS[criterion]):
            raise WoonError(
                f"quality review criterion evidence is not specific to {criterion}: {page_id}"
            )
        validated[criterion] = {"anchor": anchor, "reason": reason}
        anchors.append(anchor)
        reasons.append(reason)
    if len(set(anchors)) < 4:
        raise WoonError(
            f"quality review requires four distinct criterion evidence anchors: {page_id}"
        )
    if len(set(reasons)) != len(reasons):
        raise WoonError(f"quality review criterion evidence reasons must be distinct: {page_id}")
    return validated


def _reason_denies_anchor(reason: str, anchor: str) -> bool:
    """Reject a model claim that its own page-local quotation is absent.

    The check deliberately covers only a deterministic contradiction: an LLM
    quotes an exact string from the current Markdown, then says the quoted
    evidence does not occur in that Markdown. Broader prose quality remains a
    semantic review judgment.
    """

    # The quote itself can naturally contain a negative fact such as
    # "해가 존재하지 않는다". Only a denial in the evaluator's own claim is
    # contradictory, so exclude the mandatory quote before checking it.
    normalized = re.sub(r"\s+", "", reason.replace(anchor, "", 1))
    denial_patterns = (
        r"존재하지않",
        r"포함(?:되어)?있지않",
        r"포함되지않",
        r"언급(?:되어)?있지않",
        r"언급되지않",
        r"나타나지않",
        r"찾을수없",
        r"전혀없",
    )
    return any(re.search(pattern, normalized) is not None for pattern in denial_patterns)


def criterion_anchor_candidates(markdown: str, criterion: str) -> list[str]:
    """Return exact evidence strings appropriate to one quality criterion.

    A breadcrumb, a code line, or a heading can prove that a page exists, but it
    cannot prove that its prose explains a fact boundary naturally.  Keeping this
    selector in the final evaluator makes an Ollama schema constraint enforceable
    for human and other-provider review payloads too.
    """

    if criterion not in RUBRIC:
        raise WoonError(f"unknown quality review criterion: {criterion}")
    frontmatter, body = _split_frontmatter(markdown)
    headings = _unique(
        line.lstrip("#").strip()
        for line in body
        if line.startswith("#") and 2 <= len(line.lstrip("#").strip()) <= 120
    )
    prose = _unique(_prose_candidates(body))[:12]
    purpose = _unique(
        line.strip()
        for line in frontmatter
        if line.startswith("purpose:") and 12 <= len(line.strip()) <= 240
    )
    all_candidates = _unique(headings + prose + purpose)[:12]
    if not all_candidates or not prose:
        raise WoonError("quality review markdown has no usable evidence anchor")
    if criterion in {"natural_korean", "evidence_boundary"}:
        return prose
    if criterion == "current_use":
        return purpose or headings[:1] or all_candidates[:1]
    if criterion == "revisitability":
        return headings or all_candidates
    return all_candidates


def _split_frontmatter(markdown: str) -> tuple[list[str], list[str]]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return [], lines
    try:
        end = lines.index("---", 1)
    except ValueError:
        return [], lines
    return lines[1:end], lines[end + 1 :]


def _prose_candidates(lines: list[str]) -> list[str]:
    """Keep reader-facing paragraphs while removing navigation and code syntax."""

    candidates: list[str] = []
    in_breadcrumb = False
    in_code_fence = False
    for raw_line in lines:
        line = raw_line.strip()
        if line == "<!-- breadcrumb:start -->":
            in_breadcrumb = True
            continue
        if line == "<!-- breadcrumb:end -->":
            in_breadcrumb = False
            continue
        if line.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        is_list_item = line.startswith(("- ", "* "))
        if is_list_item:
            line = line[2:].strip()
        if (
            not line
            or in_breadcrumb
            or in_code_fence
            or line.startswith(("#", "<!--", ">", "|", "[["))
            or line.startswith("    ")
            or not 12 <= len(line) <= 240
            or (is_list_item and not line.endswith((".", "?", "!")))
        ):
            continue
        candidates.append(line)
    return candidates


def _unique(candidates: Iterable[str]) -> list[str]:
    result: list[str] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def _load_yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"cannot read compiled {key} catalog: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError(f"compiled {key} catalog has unsupported version")
    records = payload.get(key)
    if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
        raise WoonError(f"compiled {key} catalog must contain a mapping list")
    return records


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be an object")
    return value


def _evaluator(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WoonError("content quality evaluator must be an object")
    return {
        "name": _text(value.get("name"), "quality evaluator.name"),
        "version": _text(value.get("version"), "quality evaluator.version"),
        "prompt_sha256": _digest(value.get("prompt_sha256"), "quality evaluator.prompt_sha256"),
    }


def _standard(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WoonError("content quality standard must be an object")
    uri = _text(value.get("uri"), "quality standard.uri")
    if not uri.startswith("repo://skills/"):
        raise WoonError("quality standard.uri must use repo://skills/")
    return {"uri": uri, "sha256": _digest(value.get("sha256"), "quality standard.sha256")}


def _rubric(value: object, page_id: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != RUBRIC:
        raise WoonError(f"quality review rubric must contain exactly {sorted(RUBRIC)}: {page_id}")
    if any(score not in {"pass", "fail"} for score in value.values()):
        raise WoonError(f"quality review rubric values must be pass or fail: {page_id}")
    return {criterion: str(value[criterion]) for criterion in RUBRIC}


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise WoonError(f"{field} must be a list of non-empty text")
    return [item.strip() for item in value]


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


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.expanduser().read_bytes()).hexdigest()
    except OSError as error:
        raise WoonError(f"cannot read {label}: {error}") from error
