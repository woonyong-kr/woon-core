from __future__ import annotations

import json
from pathlib import Path

from woon_core.knowledge import answer_citation_evaluation as evaluation
from woon_core.knowledge.domain import KnowledgeExcerpt


class FakeKnowledgeService:
    def reindex(self) -> int:
        return 1

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt:
        assert document_id == "target-document"
        assert chunk_id == "target-chunk"
        return KnowledgeExcerpt(
            document_id=document_id,
            relative_path="target.md",
            revision="current-revision",
            source_type="canonical",
            chunk_id=chunk_id,
            heading="근거",
            text="target evidence supports the stated fact",
        )


def _write_cases(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "target", "relevant": ["target.md"]},
                    {"id": "empty", "expect_empty": True},
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_answers(path: Path, *, quote: str = "target evidence") -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generator": {"name": "test-llm", "version": "1", "run_id": "run-1"},
                "answers": [
                    {
                        "case_id": "target",
                        "answer": "The stated fact is supported.",
                        "claims": [
                            {
                                "id": "claim-1",
                                "text": "The stated fact is supported.",
                                "citation_ids": ["citation-1"],
                            }
                        ],
                        "citations": [
                            {
                                "id": "citation-1",
                                "document_id": "target-document",
                                "chunk_id": "target-chunk",
                                "relative_path": "target.md",
                                "revision": "current-revision",
                                "quote": quote,
                            }
                        ],
                    },
                    {"case_id": "empty", "answer": "", "claims": [], "citations": []},
                ],
                "semantic_judgments": {
                    "evaluator": {
                        "name": "test-judge",
                        "version": "1",
                        "prompt_sha256": "a" * 64,
                    },
                    "claims": [
                        {
                            "case_id": "target",
                            "claim_id": "claim-1",
                            "verdict": "supported",
                            "rationale": "The quoted text directly supports the claim.",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )


def test_evaluates_current_quotes_and_semantic_judgments(tmp_path: Path, monkeypatch) -> None:
    cases = tmp_path / "cases.json"
    answers = tmp_path / "answers.json"
    _write_cases(cases)
    _write_answers(answers)
    monkeypatch.setattr(
        evaluation, "build_knowledge_service", lambda _vault: (None, FakeKnowledgeService())
    )

    result = evaluation.evaluate_answer_citations(tmp_path, cases, answers)

    assert result["passed"] is True
    assert result["mechanical"] == {
        "passed": True,
        "cases": 2,
        "claims": 1,
        "citations": 1,
    }
    assert result["semantic"]["status"] == "passed"


def test_rejects_a_quote_that_is_not_in_the_current_excerpt(tmp_path: Path, monkeypatch) -> None:
    cases = tmp_path / "cases.json"
    answers = tmp_path / "answers.json"
    _write_cases(cases)
    _write_answers(answers, quote="invented evidence")
    monkeypatch.setattr(
        evaluation, "build_knowledge_service", lambda _vault: (None, FakeKnowledgeService())
    )

    result = evaluation.evaluate_answer_citations(tmp_path, cases, answers)

    assert result["passed"] is False
    assert result["mechanical"]["passed"] is False
    assert any(
        "quote is not in the current excerpt" in error for error in result["cases"][0]["errors"]
    )


def test_keeps_semantics_unverified_without_a_judgment_payload(tmp_path: Path, monkeypatch) -> None:
    cases = tmp_path / "cases.json"
    answers = tmp_path / "answers.json"
    _write_cases(cases)
    _write_answers(answers)
    payload = json.loads(answers.read_text(encoding="utf-8"))
    del payload["semantic_judgments"]
    answers.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        evaluation, "build_knowledge_service", lambda _vault: (None, FakeKnowledgeService())
    )

    result = evaluation.evaluate_answer_citations(tmp_path, cases, answers)

    assert result["mechanical"]["passed"] is True
    assert result["semantic"]["status"] == "not-evaluated"
    assert result["passed"] is False
