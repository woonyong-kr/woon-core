from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

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


def test_markdown_delta_adds_to_an_existing_h3_before_its_next_peer() -> None:
    target = (
        "# 예제\n\n"
        "## 상세 설명\n\n"
        "### PintOS\n\n기존 A.\n\n"
        "### QEMU\n\n기존 B.\n\n"
        "## 검증\n\n확인.\n"
    )

    candidate, errors = reconciliation.apply_markdown_additions(
        target,
        [{"after_heading": "### PintOS", "markdown": "추가 경계."}],
    )

    assert errors == []
    assert "### PintOS\n\n기존 A.\n\n추가 경계.\n\n### QEMU" in candidate


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
    assert '"target"' in prompt or '"source_target_diff"' in prompt


def test_reconciliation_compacts_similar_documents_to_a_lossless_diff() -> None:
    common = "\n".join(f"공통 설명 {index}" for index in range(100))
    source = f"# 예제\n\n{common}\n\nsource에만 있는 경계\n"
    target = f"# 예제\n\n{common}\n\ntarget에만 있는 검증\n"

    payload = reconciliation._comparison_payload(source, target)

    assert "source" not in payload
    assert "target" not in payload
    assert "-source에만 있는 경계" in payload["source_target_diff"]
    assert "+target에만 있는 검증" in payload["source_target_diff"]
    assert payload["source_outline"] == ["# 예제"]
    assert len(str(payload)) < len(source) + len(target)


def test_reconciliation_keeps_full_documents_when_diff_is_not_smaller() -> None:
    source = "# 원본\n\n서로 다른 내용 A\n"
    target = "# 정본\n\n완전히 다른 내용 B\n"

    payload = reconciliation._comparison_payload(source, target)

    assert payload == {"source": source, "target": target}


def test_normalized_content_subset_accepts_metadata_labels_and_extra_target_content() -> None:
    source = (
        "---\ntitle: 원본\n---\n\n# 원본\n\n## 흐름\n\n"
        "[[page-fault|페이지 폴트]]를 처리한다.\n\n```c\nvm_claim_page(va);\n```\n"
    )
    target = (
        "---\ntitle: 개선된 제목\npublish: true\n---\n\n# 개선된 제목\n\n"
        "<!-- breadcrumb:start -->생성 경로<!-- breadcrumb:end -->\n\n"
        "## 흐름\n\n[[page-fault|Page Fault]]를 처리한다.\n\n"
        "```c\nvm_claim_page(va);\n```\n\n## 검증\n\n추가 검증.\n"
    )

    assert reconciliation._normalized_content_subset(source, target) is True


def test_normalized_content_subset_rejects_missing_identifier_or_heading_claim() -> None:
    source = "# 예제\n\n## SYS_WRITE=10 흐름\n\nvm_claim_page(va);\n"
    missing_identifier = "# 예제\n\n## SYS_WRITE=10 흐름\n\nvm_alloc_page(va);\n"
    missing_heading_claim = "# 예제\n\n## syscall 흐름\n\nvm_claim_page(va);\n"

    assert reconciliation._normalized_content_subset(source, missing_identifier) is False
    assert reconciliation._normalized_content_subset(source, missing_heading_claim) is False


def test_normalized_content_subset_rejects_truncated_specific_heading() -> None:
    source = (
        "# 예제\n\n"
        "### 시나리오 3: fork 후 자식의 syscall이 부모와 다른 커널 스택을 사용하는지 확인\n\n"
        "본문은 동일하다.\n"
    )
    target = "# 예제\n\n### 시나리오 3: fork 후 자식의 syscall이 부모와 다른\n\n본문은 동일하다.\n"

    assert reconciliation._normalized_content_subset(source, target) is False


def test_restore_truncated_headings_preserves_level_and_body() -> None:
    source = "# 예제\n\n### vm_try_handle_fault의 함수 시그니처와 분기 구조\n\n본문은 동일하다.\n"
    target = "# 예제\n\n### vm_try_handle_fault의 함수 시그니처와\n\n본문은 동일하다.\n"

    candidate, repairs = reconciliation._restore_truncated_headings(source, target)

    assert repairs == [
        "### vm_try_handle_fault의 함수 시그니처와 -> "
        "### vm_try_handle_fault의 함수 시그니처와 분기 구조"
    ]
    assert "### vm_try_handle_fault의 함수 시그니처와 분기 구조" in candidate
    assert candidate.endswith("본문은 동일하다.\n")


def test_truncated_heading_detection_does_not_cross_heading_levels() -> None:
    source = "# 문서\n\n### QEMU는 kernel write와 CR0.WP를 함께 본다\n"
    target = "# 문서\n\n## QEMU\n\n### QEMU는 kernel write와 CR0.WP를 함께 본다\n"

    assert not reconciliation._has_truncated_heading(source, target)


def test_sanitize_absolute_local_paths_keeps_repository_relative_identity() -> None:
    users_root = "/" + "Users"
    home_root = "/" + "home"
    markdown = (
        f"`{users_root}/alice/workspace/Krafton-Jungle/SW_AI-W11-pintos/pintos/vm/vm.c`\n"
        f"`{home_root}/bob/workspace/vault/wiki/os/page.md`\n"
        f"`{users_root}/alice/Downloads/course.pdf`\n"
        f"`{users_root}/alice/private/note.md`\n"
    )

    sanitized = reconciliation._sanitize_absolute_local_paths(markdown)

    assert "`SW_AI-W11-pintos/pintos/vm/vm.c`" in sanitized
    assert "`vault/wiki/os/page.md`" in sanitized
    assert "`<local-source>/course.pdf`" in sanitized
    assert "`<local-home>/private/note.md`" in sanitized
    assert f"{users_root}/" not in sanitized
    assert f"{home_root}/" not in sanitized


def test_review_prompt_sends_only_source_target_and_candidate_deltas() -> None:
    common = "\n".join(f"공통 설명 {index}" for index in range(100))
    source = f"# 예제\n\n{common}\n\nsource 고유 정보\n"
    target = f"# 예제\n\n{common}\n\ntarget 정보\n"
    candidate = f"# 예제\n\n{common}\n\ntarget 정보\n\nsource 고유 정보\n"

    prompt = reconciliation._review_prompt("기준", source, target, candidate, {})

    assert '"source_target_diff"' in prompt
    assert '"target_candidate_delta"' in prompt
    assert '"candidate":' not in prompt
    assert prompt.count("공통 설명 50") <= 2


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


def test_source_decision_catalog_only_is_hash_bound_and_uses_zero_model_tokens(
    tmp_path: Path, monkeypatch: object
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_path = source_root / "legacy-prompt.md"
    source_path.parent.mkdir(parents=True)
    target_root.mkdir()
    source_path.write_text("# 일회성 실행 프롬프트\n", encoding="utf-8")
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    catalog = target_root / "catalog.yaml"
    ledger = target_root / "ledger.yaml"
    catalog.write_text(
        json.dumps(
            {
                "source": "fixture",
                "records": [
                    {
                        "source_id": "source/legacy",
                        "locator": "legacy-prompt.md",
                        "sha256": digest,
                        "role": "document",
                        "state": "new",
                        "target": None,
                        "target_sha256": None,
                    }
                ],
                "excluded": [],
            }
        ),
        encoding="utf-8",
    )
    decisions = target_root / "catalog/source-decisions/fixture.yaml"
    decisions.parent.mkdir(parents=True)
    decisions.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "fixture",
                "records": [
                    {
                        "locator": "legacy-prompt.md",
                        "source_sha256": digest,
                        "action": "catalog-only",
                        "target": None,
                        "reason": "외부 실행 지시이며 지속 지식이 아니다.",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    ledger.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "fixture",
                "records": [
                    {
                        "source_id": "source/legacy",
                        "locator": "legacy-prompt.md",
                        "source_sha256": digest,
                        "status": "failed",
                        "target": None,
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    def unexpected_codex(*_: object, **__: object) -> reconciliation._ModelResult:
        raise AssertionError("catalog-only decision must not call Codex")

    monkeypatch.setattr(reconciliation, "_run_codex", unexpected_codex)
    summary = reconciliation.reconcile_catalog(source_root, target_root, catalog, ledger)
    record = yaml.safe_load(ledger.read_text(encoding="utf-8"))["records"][0]

    assert summary.processed == 0
    assert record["status"] == "verified"
    assert record["action"] == "catalog-only"
    assert record["target"] is None
    assert record["usage"]["input_tokens"] == 0


def test_source_decision_rejects_changed_source_hash(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_path = source_root / "legacy.md"
    source_path.parent.mkdir(parents=True)
    target_root.mkdir()
    source_path.write_text("# 변경된 원본\n", encoding="utf-8")
    catalog = target_root / "catalog.yaml"
    ledger = target_root / "ledger.yaml"
    catalog.write_text(
        json.dumps(
            {
                "source": "fixture",
                "records": [
                    {
                        "source_id": "source/legacy",
                        "locator": "legacy.md",
                        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                        "role": "document",
                        "state": "new",
                        "target": None,
                        "target_sha256": None,
                    }
                ],
                "excluded": [],
            }
        ),
        encoding="utf-8",
    )
    decisions = target_root / "catalog/source-decisions/fixture.yaml"
    decisions.parent.mkdir(parents=True)
    decisions.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "fixture",
                "records": [
                    {
                        "locator": "legacy.md",
                        "source_sha256": "0" * 64,
                        "action": "catalog-only",
                        "target": None,
                        "reason": "오래된 결정",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="stale source decision hash"):
        reconciliation.reconcile_catalog(source_root, target_root, catalog, ledger)


def test_compact_same_target_decision_group_is_hash_bound(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    locator = "wiki/example.md"
    source_path = source_root / locator
    target_path = target_root / locator
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    source_path.write_text("# 이전 설명\n", encoding="utf-8")
    target_path.write_text(
        "---\ntype: Wiki\ntitle: 현행 설명\npublish: true\naccess: public\n"
        "status: Evergreen\n---\n\n# 현행 설명\n\n## 내용\n\n검증된 설명이다.\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    target_digest = hashlib.sha256(target_path.read_bytes()).hexdigest()
    catalog = target_root / "catalog.yaml"
    ledger = target_root / "ledger.yaml"
    catalog.write_text(
        json.dumps(
            {
                "source": "fixture",
                "records": [
                    {
                        "source_id": "source/example",
                        "locator": locator,
                        "sha256": digest,
                        "role": "document",
                        "state": "merge-required",
                        "target": locator,
                        "target_sha256": target_digest,
                    }
                ],
                "excluded": [],
            }
        ),
        encoding="utf-8",
    )
    decisions = target_root / "catalog/source-decisions/fixture.yaml"
    decisions.parent.mkdir(parents=True)
    decisions.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "fixture",
                "records": [],
                "groups": [
                    {
                        "action": "keep-target",
                        "target": "same",
                        "reason": "검토 후 현행 정본을 유지한다.",
                        "records": [{"locator": locator, "source_sha256": digest}],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    summary = reconciliation.reconcile_catalog(source_root, target_root, catalog, ledger, limit=0)
    record = yaml.safe_load(ledger.read_text(encoding="utf-8"))["records"][0]

    assert summary.input_tokens == 0
    assert record["action"] == "keep-target"
    assert record["target"] == locator
    assert record["target_after_sha256"] == target_digest
