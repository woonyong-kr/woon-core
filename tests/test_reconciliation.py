from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from woon_core.knowledge import reconciliation


def test_codex_reconciliation_isolates_tools_and_records_all_usage(
    tmp_path: Path, monkeypatch: object
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    observed: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({"passed": True}), encoding="utf-8")
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 7,
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(reconciliation.subprocess, "run", fake_run)  # type: ignore[attr-defined]

    result = reconciliation._run_codex("prompt", schema, "test-model", "medium")

    disabled = {observed[index + 1] for index, value in enumerate(observed) if value == "--disable"}
    assert {"plugins", "apps", "unified_exec", "shell_tool"} <= disabled
    assert 'web_search="disabled"' in observed
    assert 'model_reasoning_effort="medium"' in observed
    assert result.input_tokens == 100
    assert result.cached_input_tokens == 80
    assert result.output_tokens == 20
    assert result.reasoning_output_tokens == 7


def test_markdown_delta_only_adds_to_an_existing_h2() -> None:
    target = "---\ntitle: 예제\n---\n\n# 예제\n\n도입.\n\n## 흐름\n\n기존.\n\n## 검증\n\n확인.\n"

    candidate, errors = reconciliation.apply_markdown_additions(
        target,
        [{"after_heading": "## 흐름", "markdown": "### 추가 예제\n\n새 정보."}],
    )

    assert errors == []
    assert "기존.\n\n### 추가 예제\n\n새 정보.\n\n## 검증" in candidate
    assert candidate.startswith("---\ntitle: 예제\n---")


def test_markdown_delta_rejects_unknown_heading_and_h1() -> None:
    target = "# 예제\n\n## 흐름\n\n기존.\n"

    candidate, errors = reconciliation.apply_markdown_additions(
        target,
        [
            {"after_heading": "## 없음", "markdown": "추가"},
            {"after_heading": "## 흐름", "markdown": "# 새 제목"},
        ],
    )

    assert candidate == target
    assert errors == [
        "delta heading does not exist exactly once: ## 없음",
        "delta addition 1 may not add frontmatter or H1",
    ]


def test_markdown_delta_ignores_heading_like_code_comments() -> None:
    target = (
        "# 예제\n\n"
        "## 사용법\n\n"
        "```python\n"
        "## 사용법\n"
        "# 코드 주석\n"
        "print('ok')\n"
        "```\n\n"
        "## 검증\n\n"
        "확인.\n"
    )

    candidate, errors = reconciliation.apply_markdown_additions(
        target,
        [{"after_heading": "## 사용법", "markdown": "### 경계\n\n추가."}],
    )

    assert errors == []
    assert "```python\n## 사용법\n# 코드 주석\nprint('ok')\n```" in candidate
    assert "```\n\n### 경계\n\n추가.\n\n## 검증" in candidate


def test_delta_review_does_not_repeat_the_full_candidate() -> None:
    prompt = reconciliation._delta_review_prompt(
        "기준",
        "SOURCE_UNIQUE_TOKEN",
        "TARGET_UNIQUE_TOKEN",
        {"additions": [{"after_heading": "## 흐름", "markdown": "ADDITION_UNIQUE_TOKEN"}]},
        {},
    )

    assert prompt.count("SOURCE_UNIQUE_TOKEN") == 1
    assert prompt.count("TARGET_UNIQUE_TOKEN") == 1
    assert prompt.count("ADDITION_UNIQUE_TOKEN") == 1
    assert '"target_before"' in prompt


def test_existing_wiki_reconciliation_applies_only_reviewed_delta(
    tmp_path: Path, monkeypatch: object
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    locator = "wiki/example.md"
    source_path = source_root / locator
    target_path = target_root / locator
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    source_path.write_text("# 원본\n\n새로운 경계 조건.\n", encoding="utf-8")
    target = (
        "---\n"
        "type: wiki\n"
        "canonical_id: wiki/example\n"
        "title: 예제\n"
        "status: Active\n"
        "publish: false\n"
        "access: private\n"
        "---\n\n"
        "# 예제\n\n기존 도입.\n\n## 흐름\n\n기존 핵심.\n\n## 검증\n\n기존 검증.\n"
    )
    target_path.write_text(target, encoding="utf-8")
    eval_root = target_root / "evals/source-reconciliation"
    eval_root.mkdir(parents=True)
    (eval_root / "rubric.md").write_text("검증 기준", encoding="utf-8")
    for name in ("decision.schema.json", "delta.schema.json", "review.schema.json"):
        (eval_root / name).write_text("{}", encoding="utf-8")
    record = {
        "source_id": "source/example",
        "locator": locator,
        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "role": "document",
        "state": "merge-required",
        "target": locator,
        "target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
    }
    catalog = target_root / "catalog.yaml"
    ledger = target_root / "ledger.yaml"
    catalog.write_text(
        json.dumps({"source": "fixture", "records": [record], "excluded": []}),
        encoding="utf-8",
    )
    generated = iter(
        [
            reconciliation._ModelResult(
                {
                    "action": "merge",
                    "target_path": locator,
                    "additions": [
                        {
                            "after_heading": "## 흐름",
                            "markdown": "### 경계 조건\n\n새로운 경계 조건.",
                        }
                    ],
                    "decision": "원본에만 있는 경계 조건을 추가한다.",
                },
                10,
                5,
                2,
                1,
            ),
            reconciliation._ModelResult(
                {"passed": True, "violations": [], "unresolved_conflicts": []},
                8,
                4,
                1,
                0,
            ),
        ]
    )

    def fake_codex(*_: object, **__: object) -> reconciliation._ModelResult:
        return next(generated)

    monkeypatch.setattr(reconciliation, "_run_codex", fake_codex)

    summary = reconciliation.reconcile_catalog(
        source_root,
        target_root,
        catalog,
        ledger,
        max_attempts=1,
        reasoning_effort="medium",
    )

    result = target_path.read_text(encoding="utf-8")
    assert summary.verified == 1
    assert "기존 도입." in result
    assert "기존 핵심.\n\n### 경계 조건\n\n새로운 경계 조건." in result
    assert result.endswith("## 검증\n\n기존 검증.\n")
    assert result.count("# 예제") == 1
