from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import ollama_quality_review

MARKDOWN = """---
purpose: 현재 학습에 다시 쓰기 위한 검토 문서다.
---

# 첫 문서

<!-- breadcrumb:start -->
상위 링크: [[Wiki]] / [[첫 문서]]
<!-- breadcrumb:end -->

## 질문

독자가 무엇을 이해해야 하는지 먼저 확인한다.

## 흐름

관찰한 순서와 이유를 이어 설명한다.

## 문장

문장은 연결 표현으로 앞뒤 관계를 드러낸다.

## 근거

사실과 해석의 근거를 구분한다.

- 목록에 있어도 완전한 문장이라면 독자가 읽는 본문 근거로 쓸 수 있다.

## 찾기

다시 찾을 제목과 용어를 남긴다.

## 목적

현재 학습에 재사용할 목적을 기록한다.
"""


class _Response:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._value).encode("utf-8")


def _plan(path: Path) -> None:
    target = {
        "page_id": "os/first",
        "output_sha256": "a" * 64,
        "markdown": MARKDOWN,
    }
    path.with_name("quality-001.input.json").write_text(
        json.dumps(
            {
                "version": 1,
                "batch_id": "quality-001",
                "targets": [target],
            }
        ),
        encoding="utf-8",
    )
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "batches": [
                    {
                        "batch_id": "quality-001",
                        "input_file": "quality-001.input.json",
                        "result_file": "quality-001.result.json",
                        "targets": [{key: target[key] for key in ("page_id", "output_sha256")}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _result(digest: str = "a" * 64) -> dict[str, object]:
    return {
        "version": 1,
        "batch_id": "quality-001",
        "reviews": [
            {
                "page_id": "os/first",
                "output_sha256": digest,
                "verdict": "passed",
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
        ],
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
            "reason": (
                "“문장은 연결 표현으로 앞뒤 관계를 드러낸다.”는 자연스러운 문체를 보인다."
            ),
        },
        "evidence_boundary": {
            "anchor": "사실과 해석의 근거를 구분한다.",
            "reason": (
                "“사실과 해석의 근거를 구분한다.”는 확인 가능한 근거 경계를 보인다."
            ),
        },
        "revisitability": {
            "anchor": "찾기",
            "reason": "찾기는 다시 찾을 제목과 용어를 남겨 검색에 쓸 수 있다.",
        },
        "current_use": {
            "anchor": "purpose: 현재 학습에 다시 쓰기 위한 검토 문서다.",
            "reason": (
                "“purpose: 현재 학습에 다시 쓰기 위한 검토 문서다.”는 "
                "현재 사용 purpose를 분명히 한다."
            ),
        },
    }


def test_runs_local_ollama_and_persists_only_valid_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    observed: dict[str, object] = {}
    target = ollama_quality_review._targets(  # noqa: SLF001
        [{"page_id": "os/first", "output_sha256": "a" * 64, "markdown": MARKDOWN}],
        "quality-001",
    )["os/first"]
    candidates = ollama_quality_review._compact_anchor_candidates(  # noqa: SLF001
        {"os/first": target}
    )
    used: set[str] = set()
    anchor_indexes: list[int] = []
    for criterion in ollama_quality_review.CRITERIA:
        options = candidates[criterion]
        index = next((i for i, value in enumerate(options) if value not in used), 0)
        used.add(options[index])
        anchor_indexes.append(index)
    model_result = {"r": "p" * len(ollama_quality_review.CRITERIA), "a": anchor_indexes}

    def fake_urlopen(request: object, *, timeout: int) -> _Response:
        observed["request"] = request
        observed["timeout"] = timeout
        return _Response({"done": True, "response": json.dumps(model_result)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", batch_ids=("quality-001",)
    )

    assert report["reviewed_pages"] == 1
    request = observed["request"]
    assert hasattr(request, "full_url")
    assert request.full_url == "http://127.0.0.1:11434/api/generate"  # type: ignore[attr-defined]
    assert observed["timeout"] == 600
    assert hasattr(request, "data")
    payload = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
    assert payload["model"] == "qwen3:4b-instruct"
    assert payload["stream"] is True
    assert payload["options"] == {
        "temperature": 0,
        "num_ctx": 32_768,
        "num_predict": ollama_quality_review.DEFAULT_RESPONSE_TOKENS,
    }
    assert '"batch_id":"quality-001"' in payload["prompt"]
    assert payload["format"]["required"] == ["r", "a"]
    assert payload["format"]["properties"]["r"] == {"type": "string", "pattern": "^[pf]{6}$"}
    assert '"criterion_order"' in payload["prompt"]
    assert '"anchor_candidates"' in payload["prompt"]
    saved = json.loads((results / "quality-001.result.json").read_text(encoding="utf-8"))
    reason = saved["reviews"][0]["criterion_evidence"]["evidence_boundary"]["reason"]
    expected_anchor = candidates["evidence_boundary"][
        anchor_indexes[ollama_quality_review.CRITERIA.index("evidence_boundary")]
    ]
    assert reason == (
        f"인용한 부분 “{expected_anchor}”에서 사실과 근거의 경계를 드러내므로 "
        "근거 경계 기준을 통과한다."
    )
    run_manifest = json.loads((results / "run-manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["model"] == "qwen3:4b-instruct"
    assert run_manifest["context_tokens"] == 32_768
    assert run_manifest["response_tokens"] == ollama_quality_review.DEFAULT_RESPONSE_TOKENS


def test_compiles_local_model_anchor_selection_into_final_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    model_result = _result()
    review = model_result["reviews"][0]
    raw_evidence = review.pop("criterion_evidence")  # type: ignore[union-attr]
    assert isinstance(raw_evidence, dict)
    review["evidence_anchors"] = {  # type: ignore[index]
        criterion: item["anchor"] for criterion, item in raw_evidence.items()
    }

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(model_result)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)
    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct"
    )

    assert report["reviewed_pages"] == 1
    saved = json.loads((results / "quality-001.result.json").read_text(encoding="utf-8"))
    evidence = saved["reviews"][0]["criterion_evidence"]
    assert evidence["reader_goal"] == {
        "anchor": "질문",
        "reason": (
            "인용한 부분 “질문”에서 독자가 무엇을 이해할지 보여 주므로 "
            "독자 목표 기준을 통과한다."
        ),
    }
    assert "evidence_anchors" not in saved["reviews"][0]


def test_joins_streamed_ollama_fragments_only_after_terminal_record() -> None:
    response = ollama_quality_review._decode_ollama_stream(  # noqa: SLF001
        '\n'.join(
            [
                '{"response":"{\\\"version\\\":1,","done":false}',
                '{"response":"\\\"reviews\\\":[]}","done":true}',
            ]
        )
    )

    assert response == {
        "response": '{"version":1,"reviews":[]}',
        "done": True,
    }


def test_rejects_stream_without_terminal_record() -> None:
    with pytest.raises(WoonError, match="did not complete"):
        ollama_quality_review._decode_ollama_stream(  # noqa: SLF001
            '{"response":"partial","done":false}'
        )


def test_rejects_invalid_model_result_without_writing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(_result("b" * 64))})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    with pytest.raises(WoonError, match="stale or unknown"):
        ollama_quality_review.run_ollama_quality_reviews(plan, results, model="qwen3:4b-instruct")

    assert not (results / "quality-001.result.json").exists()


def test_rejects_model_evidence_that_is_not_in_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    invalid = _result()
    invalid["reviews"][0]["criterion_evidence"]["evidence_boundary"]["anchor"] = (  # type: ignore[index]
        "문서에 없는 근거"
    )

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(invalid)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    with pytest.raises(WoonError, match="criterion evidence anchor is absent"):
        ollama_quality_review.run_ollama_quality_reviews(plan, results, model="qwen3:4b-instruct")

    assert not (results / "quality-001.result.json").exists()


def test_rejects_needs_revision_verdict_without_a_failed_rubric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    invalid = _result()
    invalid["reviews"][0]["verdict"] = "needs-revision"  # type: ignore[index]

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(invalid)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    with pytest.raises(WoonError, match="needs-revision quality review has no failed rubric"):
        ollama_quality_review.run_ollama_quality_reviews(
            plan, results, model="qwen3:4b-instruct", max_attempts=1
        )

    assert not (results / "quality-001.result.json").exists()


def test_normalizes_local_model_reason_that_denies_its_quoted_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    invalid = _result()
    invalid["reviews"][0]["criterion_evidence"]["reader_goal"]["reason"] = (  # type: ignore[index]
        "질문은 현재 문서에 존재하지 않아 독자 목표를 확인할 수 없다."
    )

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(invalid)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", max_attempts=1
    )

    assert report["reviewed_pages"] == 1
    saved = json.loads((results / "quality-001.result.json").read_text(encoding="utf-8"))
    reason = saved["reviews"][0]["criterion_evidence"]["reader_goal"]["reason"]
    assert reason == (
        "인용한 부분 “질문”에서 독자가 무엇을 이해할지 보여 주므로 "
        "독자 목표 기준을 통과한다."
    )


def test_rejects_blocked_verdict_without_a_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    invalid = _result()
    invalid["reviews"][0]["verdict"] = "blocked"  # type: ignore[index]
    invalid["reviews"][0]["rubric"]["reader_goal"] = "fail"  # type: ignore[index]

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(invalid)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    with pytest.raises(WoonError, match="blocked quality review has no hard failure"):
        ollama_quality_review.run_ollama_quality_reviews(
            plan, results, model="qwen3:4b-instruct", max_attempts=1
        )

    assert not (results / "quality-001.result.json").exists()


def test_retries_a_rejected_model_result_with_the_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    calls: list[dict[str, object]] = []
    invalid = _result()
    invalid["reviews"][0]["criterion_evidence"]["reader_goal"]["anchor"] = (  # type: ignore[index]
        "문서에 없는 질문"
    )

    def fake_urlopen(request: object, *, timeout: int) -> _Response:
        assert hasattr(request, "data")
        calls.append(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        result = invalid if len(calls) == 1 else _result()
        return _Response({"done": True, "response": json.dumps(result)})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", max_attempts=2
    )

    assert report["retried_batches"] == ("quality-001",)
    assert len(calls) == 2
    assert "직전 출력은 저장되지 않았습니다" in calls[1]["prompt"]


def test_reports_rejected_batch_without_writing_when_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(_result("b" * 64))})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan,
        results,
        model="qwen3:4b-instruct",
        max_attempts=1,
        continue_on_error=True,
    )

    assert report["reviewed_pages"] == 0
    assert report["failed_batches"][0]["batch_id"] == "quality-001"
    assert "stale or unknown" in report["failed_batches"][0]["error"]
    assert not (results / "quality-001.result.json").exists()


def test_refuses_to_resume_results_with_a_different_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": json.dumps(_result())})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)
    ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", context_tokens=4_096
    )

    with pytest.raises(WoonError, match="different execution manifest"):
        ollama_quality_review.run_ollama_quality_reviews(
            plan, results, model="qwen3:4b-instruct", context_tokens=8_192
        )


def test_uses_a_manifest_bound_adaptive_context_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    observed: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: int) -> _Response:
        assert hasattr(request, "data")
        observed.update(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return _Response({"done": True, "response": json.dumps(_result())})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", adaptive_context=True
    )

    assert observed["options"] == {
        "temperature": 0,
        "num_ctx": 16_384,
        "num_predict": ollama_quality_review.DEFAULT_RESPONSE_TOKENS,
    }
    assert report["adaptive_context"] is True
    assert report["context_token_counts"] == {"16384": 1}
    manifest = json.loads((results / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["adaptive_context"] is True
    assert manifest["adaptive_context_policy"] == {
        "version": 1,
        "tiers": [
            {"maximum_prompt_characters": 12_000, "context_tokens": 16_384},
            {"maximum_prompt_characters": 20_000, "context_tokens": 24_576},
            {"maximum_prompt_characters": None, "context_tokens": 32_768},
        ],
    }


def test_accepts_only_provenance_bound_rebased_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)
    results.mkdir()
    result_path = results / "quality-001.result.json"
    result_path.write_text(json.dumps(_result()), encoding="utf-8")
    marker_path = results / ".inherited-results.json"
    marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "prior_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
                "prior_run_manifest_sha256": "b" * 64,
                "result_files": [
                    {
                        "path": result_path.name,
                        "sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ollama_quality_review,
        "urlopen",
        lambda *_, **__: pytest.fail("inherited result must be skipped"),
    )
    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct", adaptive_context=True
    )

    assert report["skipped_batches"] == ("quality-001",)
    run_manifest = json.loads((results / "run-manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["inherited_results_sha256"] == hashlib.sha256(
        marker_path.read_bytes()
    ).hexdigest()


def test_accepts_json_surrounded_by_model_explanation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    results = tmp_path / "results"
    _plan(plan)

    def fake_urlopen(_: object, *, timeout: int) -> _Response:
        return _Response({"done": True, "response": "판정 결과입니다.\n" + json.dumps(_result())})

    monkeypatch.setattr(ollama_quality_review, "urlopen", fake_urlopen)

    report = ollama_quality_review.run_ollama_quality_reviews(
        plan, results, model="qwen3:4b-instruct"
    )

    assert report["reviewed_pages"] == 1
    assert (results / "quality-001.result.json").is_file()


def test_rejects_non_loopback_ollama_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = tmp_path / "plan.json"
    _plan(plan)
    monkeypatch.setenv("OLLAMA_HOST", "https://remote.example.test")

    with pytest.raises(WoonError, match="loopback"):
        ollama_quality_review.run_ollama_quality_reviews(
            plan, tmp_path / "results", model="qwen3:4b-instruct"
        )
