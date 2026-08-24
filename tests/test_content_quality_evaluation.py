from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.content_quality_evaluation import (
    _reason_denies_anchor,
    evaluate_content_quality,
)

FIRST = """---
purpose: 현재 학습에 재사용할 목적을 기록한다.
---

# 첫 문서

## 질문

독자가 무엇을 이해해야 하는지 먼저 확인한다.

## 흐름

관찰한 순서와 이유를 이어 설명한다.

## 문장

문장은 연결 표현으로 앞뒤 관계를 드러낸다.

## 근거

사실과 해석의 근거를 구분한다.

## 찾기

다시 찾을 제목과 용어를 남긴다.

## 목적

현재 학습에 재사용할 목적을 기록한다.
"""
SECOND = FIRST.replace("첫 문서", "둘째 문서")


def _write_catalogs(vault: Path) -> None:
    catalog = vault / "catalog" / "llm-wiki"
    catalog.mkdir(parents=True)
    (vault / "wiki" / "os").mkdir(parents=True)
    (vault / "wiki" / "os" / "first.md").write_text(FIRST, encoding="utf-8")
    (vault / "wiki" / "os" / "second.md").write_text(SECOND, encoding="utf-8")
    (catalog / "pages.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "pages": [
                    {"page_id": "os/first", "output_path": "os/first.md"},
                    {"page_id": "os/second", "output_path": "os/second.md"},
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (catalog / "receipts.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "receipts": [
                    {
                        "page_id": "os/first",
                        "output_sha256": _digest(FIRST),
                    },
                    {
                        "page_id": "os/second",
                        "output_sha256": _digest(SECOND),
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _review(page_id: str, digest: str, *, verdict: str = "passed") -> dict[str, object]:
    return {
        "page_id": page_id,
        "output_sha256": digest,
        "verdict": verdict,
        "rubric": {
            "reader_goal": "pass",
            "logical_flow": "pass",
            "natural_korean": "pass",
            "evidence_boundary": "pass",
            "revisitability": "pass",
            "current_use": "pass",
        },
        "hard_failures": [],
        "criterion_evidence": _criterion_evidence(),
    }


def _criterion_evidence() -> dict[str, dict[str, str]]:
    return {
        "reader_goal": {
            "anchor": "질문",
            "reason": "질문은 독자가 무엇을 이해할지 밝혀 독자 목표를 보인다.",
        },
        "logical_flow": {
            "anchor": "흐름",
            "reason": "흐름은 관찰한 순서와 이유를 이어 논리 순서를 보인다.",
        },
        "natural_korean": {
            "anchor": "문장은 연결 표현으로 앞뒤 관계를 드러낸다.",
            "reason": ("“문장은 연결 표현으로 앞뒤 관계를 드러낸다.”는 자연스러운 문체를 보인다."),
        },
        "evidence_boundary": {
            "anchor": "사실과 해석의 근거를 구분한다.",
            "reason": ("“사실과 해석의 근거를 구분한다.”는 확인 가능한 근거 경계를 보인다."),
        },
        "revisitability": {
            "anchor": "찾기",
            "reason": "찾기는 다시 찾을 제목과 용어를 남겨 검색에 쓸 수 있다.",
        },
        "current_use": {
            "anchor": "purpose: 현재 학습에 재사용할 목적을 기록한다.",
            "reason": (
                "“purpose: 현재 학습에 재사용할 목적을 기록한다.”는 현재 재사용 목적을 분명히 한다."
            ),
        },
    }


def _digest(markdown: str) -> str:
    return hashlib.sha256(markdown.encode()).hexdigest()


def _write_reviews(
    path: Path, standard: Path, prompt: Path, reviews: list[dict[str, object]]
) -> None:
    standard.write_text("current writing standard\n", encoding="utf-8")
    prompt.write_text("current review prompt\n", encoding="utf-8")
    standard_sha256 = hashlib.sha256(standard.read_bytes()).hexdigest()
    prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "standard": {
                    "uri": "repo://skills/standards/learning-writing-harness.md",
                    "sha256": standard_sha256,
                },
                "evaluator": {
                    "name": "test-judge",
                    "version": "1",
                    "prompt_sha256": prompt_sha256,
                },
                "reviews": reviews,
            }
        ),
        encoding="utf-8",
    )


def test_requires_current_passed_reviews_for_every_compiled_page(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    _write_reviews(
        reviews,
        standard,
        prompt,
        [
            _review("os/first", _digest(FIRST)),
            _review("os/second", _digest(SECOND)),
        ],
    )

    result = evaluate_content_quality(tmp_path, reviews, standard, prompt)

    assert result["passed"] is True
    assert result["coverage"] == {
        "compiled_pages": 2,
        "reviewed_pages": 2,
        "missing_pages": 0,
        "stale_reviews": 0,
        "rejected_reviews": 0,
    }


def test_fails_for_missing_and_stale_reviews(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    _write_reviews(reviews, standard, prompt, [_review("os/first", "f" * 64)])

    result = evaluate_content_quality(tmp_path, reviews, standard, prompt)

    assert result["passed"] is False
    assert result["coverage"]["missing_pages"] == 1
    assert result["coverage"]["stale_reviews"] == 1
    assert any("stale" in error for error in result["errors"])
    assert any("missing" in error for error in result["errors"])


def test_fails_when_review_prompt_is_stale(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    _write_reviews(
        reviews,
        standard,
        prompt,
        [
            _review("os/first", _digest(FIRST)),
            _review("os/second", _digest(SECOND)),
        ],
    )
    prompt.write_text("changed review prompt\n", encoding="utf-8")

    result = evaluate_content_quality(tmp_path, reviews, standard, prompt)

    assert result["passed"] is False
    assert any("review prompt" in error for error in result["errors"])


def test_fails_when_needs_revision_verdict_has_no_failed_rubric(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    _write_reviews(
        reviews,
        standard,
        prompt,
        [
            _review("os/first", _digest(FIRST), verdict="needs-revision"),
            _review("os/second", _digest(SECOND)),
        ],
    )

    result = evaluate_content_quality(tmp_path, reviews, standard, prompt)

    assert result["passed"] is False
    assert any(
        "needs-revision quality review has no failed rubric" in error for error in result["errors"]
    )


def test_refuses_review_evidence_that_is_not_in_the_compiled_page(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    review = _review("os/first", _digest(FIRST))
    criterion_evidence = review["criterion_evidence"]
    assert isinstance(criterion_evidence, dict)
    criterion_evidence["evidence_boundary"]["anchor"] = "문서에 없는 근거"
    _write_reviews(reviews, standard, prompt, [review, _review("os/second", _digest(SECOND))])

    with pytest.raises(WoonError, match="criterion evidence anchor is absent"):
        evaluate_content_quality(tmp_path, reviews, standard, prompt)


def test_refuses_reason_that_denies_its_quoted_anchor(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    review = _review("os/first", _digest(FIRST))
    criterion_evidence = review["criterion_evidence"]
    assert isinstance(criterion_evidence, dict)
    criterion_evidence["reader_goal"]["reason"] = (
        "질문은 현재 문서에 존재하지 않아 독자 목표를 확인할 수 없다."
    )
    _write_reviews(reviews, standard, prompt, [review, _review("os/second", _digest(SECOND))])

    with pytest.raises(WoonError, match="reason denies its own anchor"):
        evaluate_content_quality(tmp_path, reviews, standard, prompt)


def test_allows_a_negative_statement_inside_the_required_quote() -> None:
    anchor = "손실 함수의 해는 대부분 존재하지 않는다."

    assert (
        _reason_denies_anchor(f"인용한 부분 “{anchor}”에서 근거의 한계를 드러낸다.", anchor)
        is False
    )
    assert (
        _reason_denies_anchor(
            f"인용한 부분 “{anchor}”은 현재 문서에 존재하지 않아 근거를 확인할 수 없다.", anchor
        )
        is True
    )


def test_fails_when_blocked_verdict_has_no_hard_failure(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    review = _review("os/first", _digest(FIRST), verdict="blocked")
    rubric = review["rubric"]
    assert isinstance(rubric, dict)
    rubric["reader_goal"] = "fail"
    _write_reviews(reviews, standard, prompt, [review, _review("os/second", _digest(SECOND))])

    result = evaluate_content_quality(tmp_path, reviews, standard, prompt)

    assert result["passed"] is False
    assert any("blocked quality review has no hard failure" in error for error in result["errors"])


def test_refuses_heading_as_evidence_boundary_anchor(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    review = _review("os/first", _digest(FIRST))
    criterion_evidence = review["criterion_evidence"]
    assert isinstance(criterion_evidence, dict)
    criterion_evidence["evidence_boundary"]["anchor"] = "근거"
    _write_reviews(reviews, standard, prompt, [review, _review("os/second", _digest(SECOND))])

    with pytest.raises(WoonError, match="does not match its allowed document role"):
        evaluate_content_quality(tmp_path, reviews, standard, prompt)


def test_refuses_a_generic_review_without_criterion_evidence(tmp_path: Path) -> None:
    _write_catalogs(tmp_path)
    reviews = tmp_path / "reviews.json"
    standard = tmp_path / "standard.md"
    prompt = tmp_path / "prompt.md"
    review = _review("os/first", _digest(FIRST))
    review.pop("criterion_evidence")
    review["evidence_anchors"] = ["질문", "흐름"]
    review["rationale"] = "질문과 흐름이 자연스럽게 이어져 독자의 이해를 돕는다."
    _write_reviews(reviews, standard, prompt, [review, _review("os/second", _digest(SECOND))])

    with pytest.raises(WoonError, match="criterion_evidence"):
        evaluate_content_quality(tmp_path, reviews, standard, prompt)
