from __future__ import annotations

import json
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import evaluation
from woon_core.knowledge.domain import IndexStatistics, KnowledgeExcerpt, SearchResult


class FakeKnowledgeService:
    def reindex(self) -> int:
        return 2

    def index_statistics(self) -> IndexStatistics:
        return IndexStatistics(documents=2, chunks=2, total_chars=10_000, max_chunk_chars=5_000)

    def search(self, query: str, _limit: int) -> list[SearchResult]:
        if query == "target query":
            return [_result("other.md"), _result("target.md")]
        return []

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt:
        return KnowledgeExcerpt(
            document_id=document_id,
            relative_path="target.md",
            revision="revision",
            source_type="canonical",
            chunk_id=chunk_id,
            heading="target",
            text="target evidence",
        )


def _result(relative_path: str) -> SearchResult:
    return SearchResult(
        document_id=relative_path,
        canonical_id=None,
        title=relative_path,
        summary="summary",
        relative_path=relative_path,
        revision="revision",
        source_type="canonical",
        chunk_id="chunk",
        heading="heading",
        score=1.0,
        snippet="snippet",
    )


def _write_cases(path: Path, relevant: list[str]) -> None:
    path.write_text(
        json.dumps(
            {
                "repeat": 2,
                "top_k": 2,
                "thresholds": {
                    "recall_at_k": 1.0,
                    "precision_at_k": 0.5,
                    "mean_reciprocal_rank": 0.5,
                    "ndcg_at_k": 0.63,
                    "latency_p95_ms": 1000,
                    "context_reduction": 0.01,
                },
                "answer_and_citation": {
                    "mode": "manual-review",
                    "reason": "retrieval-only evaluation has no answer generator",
                },
                "cases": [
                    {"id": "target", "query": "target query", "relevant": relevant},
                    {"id": "empty", "query": "unknown", "expect_empty": True},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_evaluation_reports_rank_metrics_and_answer_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "target.md").write_text("target", encoding="utf-8")
    cases = tmp_path / "cases.json"
    _write_cases(cases, ["target.md"])
    monkeypatch.setattr(
        evaluation,
        "build_knowledge_service",
        lambda _vault: (None, FakeKnowledgeService()),
    )

    result = evaluation.evaluate(tmp_path, cases)

    assert result["passed"] is True
    assert result["version"] == 2
    metrics = result["metrics"]
    assert metrics["recall_at_k"] == 1.0
    assert metrics["precision_at_k"] == 0.5
    assert metrics["mean_reciprocal_rank"] == 0.5
    assert metrics["ndcg_at_k"] == pytest.approx(1 / 1.584962500721156)
    assert result["answer_and_citation"]["status"] == "not-evaluated"


def test_evaluation_rejects_stale_gold_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases.json"
    _write_cases(cases, ["missing.md"])
    monkeypatch.setattr(
        evaluation,
        "build_knowledge_service",
        lambda _vault: (None, FakeKnowledgeService()),
    )

    with pytest.raises(WoonError, match="path does not exist: missing.md"):
        evaluation.evaluate(tmp_path, cases)
