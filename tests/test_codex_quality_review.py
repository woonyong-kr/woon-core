from __future__ import annotations

import hashlib
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import codex_quality_review

MARKDOWN = """---
purpose: 현재 학습에 다시 쓰기 위한 검토 문서다.
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


def _plan(path: Path, relative_path: str = "wiki/os/first.md") -> dict[str, object]:
    target = {
        "page_id": "os/first",
        "output_sha256": "a" * 64,
        "markdown": MARKDOWN,
        "relative_path": relative_path,
    }
    path.with_name("quality-001.input.json").write_text(
        json.dumps({"version": 1, "batch_id": "quality-001", "targets": [target]}),
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
    return target


def _compact(targets: dict[str, object], states: str = "pppppp") -> dict[str, object]:
    reviews: list[dict[str, object]] = []
    for page_id, target in sorted(targets.items()):
        assert isinstance(target, dict)
        candidates = codex_quality_review._compact_anchor_candidates(  # noqa: SLF001
            {page_id: target}
        )
        used: set[str] = set()
        indexes: list[int] = []
        for criterion in codex_quality_review.CRITERIA:
            index = next(
                (value for value, anchor in enumerate(candidates[criterion]) if anchor not in used),
                0,
            )
            used.add(candidates[criterion][index])
            indexes.append(index)
        reviews.append(
            {
                "r": states,
                "a": indexes,
            }
        )
    return {"reviews": reviews}


def test_review_prompt_uses_compiler_provenance_without_requiring_inline_citations() -> None:
    target = {"markdown": MARKDOWN, "output_sha256": "a" * 64}

    prompt = codex_quality_review._prompt(  # noqa: SLF001
        "quality-001",
        {"os/first": target},
    )

    assert "inline citation이 없다는 이유만으로" in prompt
    assert "`확인 범위:` anchor" in prompt
    assert "막연히" in prompt
    assert "명확한 결함을 입증하지 못하면 pass" in prompt


def test_scope_note_cannot_be_its_own_failing_evidence_boundary() -> None:
    markdown = MARKDOWN.replace(
        "# 첫 문서", "# 첫 문서\n\n> 확인 범위: 일반 원리만 설명하며 실행 결과는 별도로 검증한다."
    )
    target = {"markdown": markdown, "output_sha256": "a" * 64}
    candidates = codex_quality_review._compact_anchor_candidates(  # noqa: SLF001
        {"os/first": target}
    )
    evidence_index = candidates["evidence_boundary"].index(
        "확인 범위: 일반 원리만 설명하며 실행 결과는 별도로 검증한다."
    )
    raw = _compact({"os/first": target}, "pfpppp")
    evidence_position = codex_quality_review.CRITERIA.index("evidence_boundary")
    raw["reviews"][0]["a"][evidence_position] = evidence_index

    expanded = codex_quality_review._expand_codex_model_result(  # noqa: SLF001
        raw, "quality-001", {"os/first": target}
    )
    review = expanded["reviews"][0]

    assert review["rubric"]["evidence_boundary"] == "pass"
    assert review["verdict"] == "passed"


def _calibration() -> dict[str, object]:
    target = {
        "output_sha256": hashlib.sha256(
            codex_quality_review.CALIBRATION_MARKDOWN.encode("utf-8")
        ).hexdigest(),
        "markdown": codex_quality_review.CALIBRATION_MARKDOWN,
    }
    return _compact({"calibration/synthetic-negative": target}, "fffffp")


def test_uses_chatgpt_codex_in_an_isolated_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    target = _plan(plan)
    calls: list[list[str]] = []
    monkeypatch.setattr(codex_quality_review.shutil, "which", lambda _: "/usr/local/bin/codex")

    def fake_run(command: list[str], **options: object) -> CompletedProcess[str]:
        calls.append(command)
        environment = options.get("env")
        assert isinstance(environment, dict)
        assert "OPENAI_API_KEY" not in environment
        assert "OPENAI_BASE_URL" not in environment
        if command[1:3] == ["login", "status"]:
            return CompletedProcess(command, 0, "", "Logged in using ChatGPT\n")
        assert command[:2] == ["/usr/local/bin/codex", "exec"]
        assert "--ephemeral" in command
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
        assert "--model" not in command
        assert "apps" in command and "shell_tool" in command
        output = Path(command[command.index("--output-last-message") + 1])
        prompt = options["input"]
        assert isinstance(prompt, str)
        assert "catalog" not in prompt
        if '"batch_id":"calibration"' in prompt:
            output.write_text(json.dumps(_calibration()), encoding="utf-8")
        else:
            targets = codex_quality_review._targets([target], "quality-001")  # noqa: SLF001
            output.write_text(json.dumps(_compact(targets)), encoding="utf-8")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codex_quality_review.subprocess, "run", fake_run)
    results = tmp_path / "results"
    report = codex_quality_review.run_codex_quality_reviews(plan, results)

    assert report["reviewed_pages"] == 1
    assert len(calls) == 3
    manifest = json.loads((results / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["login"] == "ChatGPT subscription"
    assert manifest["reasoning_effort"] == "high"
    assert manifest["transmission_scope"] == "compiled wiki Markdown targets only"
    assert (results / "quality-001.result.json").is_file()
    assert results.stat().st_mode & 0o777 == 0o700
    assert (results / "run-manifest.json").stat().st_mode & 0o777 == 0o600
    assert (results / "quality-001.result.json").stat().st_mode & 0o777 == 0o600


def test_rejects_novel_before_starting_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "plan.json"
    _plan(plan, "novel/private.md")
    monkeypatch.setattr(
        codex_quality_review.subprocess,
        "run",
        lambda *_, **__: pytest.fail("Codex must not start for excluded material"),
    )

    with pytest.raises(WoonError, match="compiled wiki Markdown"):
        codex_quality_review.run_codex_quality_reviews(plan, tmp_path / "results")


def test_rejects_api_key_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = tmp_path / "plan.json"
    _plan(plan)
    monkeypatch.setattr(codex_quality_review.shutil, "which", lambda _: "/usr/local/bin/codex")
    monkeypatch.setattr(
        codex_quality_review.subprocess,
        "run",
        lambda command, **_: CompletedProcess(command, 0, "Logged in using API key\n", ""),
    )

    with pytest.raises(WoonError, match="ChatGPT subscription login"):
        codex_quality_review.run_codex_quality_reviews(plan, tmp_path / "results")
