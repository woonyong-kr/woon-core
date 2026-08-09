"""Deterministic retrieval evaluation for a configured private knowledge vault."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.factory import build_knowledge_service


@dataclass(frozen=True, slots=True)
class CaseResult:
    identifier: str
    query: str
    passed: bool
    reciprocal_rank: float
    result_paths: tuple[str, ...]
    excerpt_chars: int
    payload_chars: int
    latency_ms: tuple[float, ...]


def evaluate(vault: Path, cases_path: Path) -> dict[str, object]:
    config = _load_json(cases_path)
    repeat = _positive_int(config.get("repeat"), "repeat")
    top_k = _positive_int(config.get("top_k"), "top_k")
    if top_k > 20:
        raise WoonError("evaluation top_k must not exceed 20")
    raw_cases = config.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise WoonError("evaluation requires at least one case")
    thresholds = _mapping(config.get("thresholds"), "thresholds")

    _, service = build_knowledge_service(vault)
    indexed = service.reindex()
    index_statistics = service.index_statistics()
    results: list[CaseResult] = []
    all_latencies: list[float] = []
    positive_cases = 0
    recall_hits = 0
    reciprocal_ranks: list[float] = []
    selected_context_chars = 0

    for raw_case in raw_cases:
        case = _mapping(raw_case, "case")
        identifier = _string(case.get("id"), "case.id")
        query = _string(case.get("query"), f"{identifier}.query")
        expected = set(_strings(case.get("expected_any", []), f"{identifier}.expected_any"))
        forbidden = set(_strings(case.get("forbidden", []), f"{identifier}.forbidden"))
        expect_empty = bool(case.get("expect_empty", False))
        if expect_empty == bool(expected):
            raise WoonError(
                f"evaluation case {identifier!r} must define expected_any or expect_empty"
            )
        runs: list[tuple[str, ...]] = []
        latencies: list[float] = []
        first_results = []
        for run in range(repeat):
            started = time.perf_counter()
            search_results = service.search(query, top_k)
            latency = (time.perf_counter() - started) * 1000
            latencies.append(round(latency, 3))
            all_latencies.append(latency)
            paths = tuple(result.relative_path for result in search_results)
            runs.append(paths)
            if run == 0:
                first_results = search_results
        paths = runs[0]
        agreement = len(set(runs)) == 1
        forbidden_hit = bool(forbidden.intersection(paths))
        reciprocal_rank = 0.0
        excerpt_chars = 0
        if expected:
            positive_cases += 1
            for rank, path in enumerate(paths, start=1):
                if path in expected:
                    reciprocal_rank = 1 / rank
                    recall_hits += 1
                    chosen = first_results[rank - 1]
                    excerpt = service.read_excerpt(chosen.document_id, chosen.chunk_id)
                    excerpt_chars = len(excerpt.text)
                    selected_context_chars += excerpt_chars
                    break
            reciprocal_ranks.append(reciprocal_rank)
            passed = reciprocal_rank > 0 and not forbidden_hit and agreement
        else:
            passed = not paths and not forbidden_hit and agreement
        payload_chars = len(
            json.dumps([asdict(result) for result in first_results], ensure_ascii=False)
        )
        selected_context_chars += payload_chars
        results.append(
            CaseResult(
                identifier=identifier,
                query=query,
                passed=passed,
                reciprocal_rank=reciprocal_rank,
                result_paths=paths,
                excerpt_chars=excerpt_chars,
                payload_chars=payload_chars,
                latency_ms=tuple(latencies),
            )
        )

    recall_at_k = recall_hits / positive_cases if positive_cases else 1.0
    mean_reciprocal_rank = statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 1.0
    latency_p50 = _percentile(all_latencies, 0.50)
    latency_p95 = _percentile(all_latencies, 0.95)
    naive_context_chars = index_statistics.total_chars * max(positive_cases, 1)
    context_reduction = (
        1 - selected_context_chars / naive_context_chars if naive_context_chars else 0.0
    )
    passed = (
        all(result.passed for result in results)
        and recall_at_k >= _ratio(thresholds.get("recall_at_k"), "recall_at_k")
        and mean_reciprocal_rank
        >= _ratio(thresholds.get("mean_reciprocal_rank"), "mean_reciprocal_rank")
        and latency_p95 <= _positive_number(thresholds.get("latency_p95_ms"), "latency_p95_ms")
        and context_reduction >= _ratio(thresholds.get("context_reduction"), "context_reduction")
    )
    return {
        "version": 1,
        "passed": passed,
        "indexed": indexed,
        "index": asdict(index_statistics),
        "metrics": {
            "recall_at_k": recall_at_k,
            "mean_reciprocal_rank": mean_reciprocal_rank,
            "latency_ms": {"p50": latency_p50, "p95": latency_p95},
            "selected_context_chars": selected_context_chars,
            "naive_context_chars": naive_context_chars,
            "context_reduction": context_reduction,
        },
        "cases": [asdict(result) for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = evaluate(arguments.vault, arguments.cases)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if not result["passed"]:
        raise SystemExit(1)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"load evaluation cases: {error}") from error
    return _mapping(loaded, "evaluation root")


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"{field} must be a mapping")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{field} must be a non-empty string")
    return value.strip()


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise WoonError(f"{field} must be a string list")
    return tuple(item.strip() for item in value if item.strip())


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WoonError(f"{field} must be a positive integer")
    return value


def _positive_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise WoonError(f"{field} must be a positive number")
    return float(value)


def _ratio(value: object, field: str) -> float:
    number = _positive_number(value, field)
    if number > 1:
        raise WoonError(f"{field} must be between 0 and 1")
    return number


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * ratio), len(ordered) - 1)
    return round(ordered[index], 3)


if __name__ == "__main__":
    main()
