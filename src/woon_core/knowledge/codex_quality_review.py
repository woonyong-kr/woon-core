"""Run scope-limited compiled Wiki quality reviews through Codex CLI.

The runner requires the local ChatGPT subscription login.  It sends only
selected compiled ``wiki/**`` Markdown in the prompt and starts Codex in an
empty temporary directory without agent tools, user configuration, or rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.ollama_quality_review import (
    CRITERIA,
    PLAN_VERSION,
    RUN_MANIFEST_FILE,
    ReviewTarget,
    _canonicalize_model_evidence_reasons,
    _compact_anchor_candidates,
    _expand_local_model_result,
    _input_batch,
    _load_json,
    _load_plan,
    _safe_relative,
    _selected_batches,
    _targets,
    _text,
    _validate_result,
)

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_ATTEMPTS = 1
HOSTED_PROVIDER = "openai-codex-cli-chatgpt"
REASONING_EFFORT = "high"

CALIBRATION_MARKDOWN = """---
purpose: 아무거나
---

# 메모

## 내용

이것은 이것이다. 이것은 이것이다. 이것은 이것이다.

## 결론

좋다.
"""


@dataclass(frozen=True, slots=True)
class CodexQualityReviewReport:
    """Observable review run bound to a ChatGPT-authenticated Codex CLI."""

    model: str
    reviewed_batches: tuple[str, ...]
    skipped_batches: tuple[str, ...]
    failed_batches: tuple[dict[str, str], ...]
    reviewed_pages: int
    calibration: str
    results: str


def run_codex_quality_reviews(
    plan_path: Path,
    results_dir: Path,
    *,
    model: str | None = None,
    codex_binary: str = "codex",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    continue_on_error: bool = False,
    batch_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Review compiled general Wiki pages through the local Codex subscription.

    The model has no vault path, source path, catalog, or Novel input.  Existing
    results are only reused when the plan and isolation contract match exactly.
    """

    normalized_model = _model(model)
    _validate_timeout(timeout_seconds)
    _validate_attempts(max_attempts)
    plan = _load_plan(plan_path)
    batches = _selected_batches(plan, batch_ids)
    plan_root = plan_path.expanduser().resolve().parent
    for manifest_batch in batches:
        batch_id = _text(manifest_batch.get("batch_id"), "quality review batch_id")
        _hosted_targets(_input_batch(plan, manifest_batch, plan_root), batch_id)

    binary = _codex_binary(codex_binary)
    _require_chatgpt_login(binary)
    destination = results_dir.expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)
    _prepare_or_load_manifest(
        destination,
        plan_path,
        normalized_model,
        timeout_seconds,
        max_attempts,
        binary,
    )
    reviewed: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    pages = 0

    for manifest_batch in batches:
        batch_id = _text(manifest_batch.get("batch_id"), "quality review batch_id")
        result_path = destination / _safe_relative(
            _text(manifest_batch.get("result_file"), "quality review result_file")
        )
        try:
            batch = _input_batch(plan, manifest_batch, plan_root)
            targets = _hosted_targets(batch, batch_id)
            if result_path.exists():
                _validate_result(
                    _load_json(result_path, "quality review result"), batch_id, targets
                )
                skipped.append(batch_id)
                continue
            result = _review_batch(
                batch_id,
                targets,
                binary,
                normalized_model,
                timeout_seconds,
                max_attempts,
            )
            _validate_result(result, batch_id, targets)
            atomic_write(result_path, encode_json(result), mode=0o600)
            reviewed.append(batch_id)
            pages += len(targets)
        except WoonError as error:
            _write_failure(destination, batch_id, str(error))
            if not continue_on_error:
                raise
            failed.append({"batch_id": batch_id, "error": str(error)})

    return asdict(
        CodexQualityReviewReport(
            model=normalized_model,
            reviewed_batches=tuple(reviewed),
            skipped_batches=tuple(skipped),
            failed_batches=tuple(failed),
            reviewed_pages=pages,
            calibration="passed",
            results=str(destination),
        )
    )


def _model(value: str | None) -> str:
    if value is None:
        return "subscription-default"
    return _text(value, "Codex model")


def _codex_binary(value: str) -> str:
    binary = _text(value, "Codex binary")
    resolved = shutil.which(binary)
    if resolved is None:
        raise WoonError("Codex quality review requires an installed Codex CLI binary")
    return resolved


def _require_chatgpt_login(binary: str) -> None:
    try:
        completed = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_subscription_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError("Codex quality review could not verify its login") from error
    login_output = completed.stdout + completed.stderr
    if completed.returncode != 0 or "Logged in using ChatGPT" not in login_output:
        raise WoonError(
            "Codex quality review requires a ChatGPT subscription login, not an API key"
        )


def _prepare_or_load_manifest(
    destination: Path,
    plan_path: Path,
    model: str,
    timeout_seconds: int,
    max_attempts: int,
    binary: str,
) -> None:
    expected = {
        "version": 1,
        "provider": HOSTED_PROVIDER,
        "model": model,
        "reasoning_effort": REASONING_EFFORT,
        "codex_binary": Path(binary).name,
        "plan_sha256": hashlib.sha256(plan_path.expanduser().read_bytes()).hexdigest(),
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "login": "ChatGPT subscription",
        "sandbox": "read-only",
        "ephemeral": True,
        "ignore_user_config": True,
        "ignore_rules": True,
        "disabled_tools": [
            "apps",
            "browser_use",
            "code_mode_host",
            "computer_use",
            "goals",
            "image_generation",
            "memories",
            "multi_agent",
            "plugins",
            "shell_tool",
            "tool_suggest",
            "unified_exec",
            "workspace_dependencies",
        ],
        "transmission_scope": "compiled wiki Markdown targets only",
    }
    path = destination / RUN_MANIFEST_FILE
    if path.exists():
        actual = _load_json(path, "Codex quality review run manifest")
        calibration = actual.get("calibration")
        if not isinstance(calibration, dict) or calibration.get("passed") is not True:
            raise WoonError("Codex quality review run manifest has no passing calibration")
        if {key: value for key, value in actual.items() if key != "calibration"} != expected:
            raise WoonError("quality review results use a different execution manifest")
        return
    if any(destination.glob("*.failure.json")):
        raise WoonError("quality review results are missing their execution manifest")
    # A rebase writes only receipt-validated inherited result files before the
    # hosted runner creates its own execution manifest. Each inherited result
    # is revalidated against the new plan in the normal batch loop below.
    calibration = _run_calibration(binary, model, timeout_seconds)
    atomic_write(
        path,
        encode_json({**expected, "calibration": calibration}),
        mode=0o600,
    )


def _run_calibration(binary: str, model: str, timeout_seconds: int) -> dict[str, object]:
    target: ReviewTarget = {
        "output_sha256": hashlib.sha256(CALIBRATION_MARKDOWN.encode("utf-8")).hexdigest(),
        "markdown": CALIBRATION_MARKDOWN,
    }
    targets = {"calibration/synthetic-negative": target}
    raw = _run_codex(
        _prompt("calibration", targets),
        _response_schema(targets),
        binary,
        model,
        timeout_seconds,
    )
    result = _expand_codex_model_result(raw, "calibration", targets)
    reviews = result.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != 1 or not isinstance(reviews[0], dict):
        raise WoonError("Codex quality review calibration response has no review")
    review = reviews[0]
    rubric = review.get("rubric")
    if not isinstance(rubric, dict):
        raise WoonError("Codex quality review calibration has no rubric")
    if review.get("verdict") != "needs-revision" or rubric.get("natural_korean") != "fail":
        raise WoonError(
            "Codex quality review calibration did not reject the synthetic Korean writing failure"
        )
    return {
        "passed": True,
        "synthetic_markdown_sha256": target["output_sha256"],
        "verdict": review["verdict"],
        "natural_korean": rubric["natural_korean"],
    }


def _hosted_targets(batch: dict[str, Any], batch_id: str) -> dict[str, ReviewTarget]:
    targets = _targets(batch.get("targets"), batch_id)
    raw_targets = batch.get("targets")
    assert isinstance(raw_targets, list)
    for raw_target in raw_targets:
        assert isinstance(raw_target, dict)
        relative_path = _text(raw_target.get("relative_path"), "quality review relative_path")
        lowered_path = relative_path.lower()
        if not relative_path.startswith("wiki/") or not relative_path.endswith(".md"):
            raise WoonError("Codex quality review only permits compiled wiki Markdown targets")
        if "/novel/" in f"/{lowered_path}/" or lowered_path.startswith("wiki/novel/"):
            raise WoonError("Codex quality review must not transmit Novel material")
    return targets


def _review_batch(
    batch_id: str,
    targets: dict[str, ReviewTarget],
    binary: str,
    model: str,
    timeout_seconds: int,
    max_attempts: int,
) -> dict[str, object]:
    last_error: WoonError | None = None
    for attempt in range(1, max_attempts + 1):
        raw = _run_codex(
            _prompt(batch_id, targets, last_error),
            _response_schema(targets),
            binary,
            model,
            timeout_seconds,
        )
        try:
            result = _expand_codex_model_result(raw, batch_id, targets)
            _canonicalize_model_evidence_reasons(result)
            _validate_result(result, batch_id, targets)
        except WoonError as error:
            last_error = error
            if attempt == max_attempts:
                raise WoonError(
                    "Codex quality review could not produce a valid result after "
                    f"{max_attempts} attempts: {batch_id}: {error}"
                ) from error
        else:
            return result
    raise AssertionError("Codex quality review retry loop must return or raise")


def _prompt(
    batch_id: str,
    targets: dict[str, ReviewTarget],
    previous_validation_error: WoonError | None = None,
) -> str:
    pages = [
        {"page_id": page_id, "markdown": target["markdown"]}
        for page_id, target in sorted(targets.items())
    ]
    contract = _compact_response_contract(targets)
    instructions = """당신은 한국어 Wiki의 현재 품질만 판정하는 편집자다.
입력에는 컴파일된 일반 Wiki Markdown만 들어 있다.

각 페이지를 독립적으로 본다. 문서를 고치거나 새 문장을 만들거나
문서 밖의 사실과 저자의 의도를 보완하지 마라.
reader_goal, logical_flow, natural_korean, evidence_boundary,
revisitability, current_use를 pass 또는 fail로 판정한다.
- reader_goal: 독자가 이해하거나 판단할 대상을 알 수 있다.
- logical_flow: 문제, 이유, 용어, 적용 경계가 필요한 순서로 이어진다.
- natural_korean: 주어와 서술어가 호응하고 인과, 대조, 조건이 실제 연결 표현으로 드러난다.
- evidence_boundary: 사실, 해석, 미결정, 실제 실행 결과와 예상 결과가 섞이지 않는다.
- revisitability: H1/H2, 정확한 용어와 경계로 나중에 다시 찾을 수 있다.
- current_use: frontmatter purpose가 본문의 현재 학습, 설명, 검색 목적과 맞는다.

일반 교과 개념이나 원리 설명은 compiler의 source·claim·receipt 계층이 출처를 소유한다.
따라서 본문에 inline citation이 없다는 이유만으로 evidence_boundary를 fail로 두지 마라.
본문이 특정 버전의 실제 실행·측정 결과처럼 말하거나 사실·해석·미결정을 서로 섞을 때만 fail이다.
`확인 범위:` anchor가 있으면 그 문장을 evidence_boundary의 첫 근거로 선택하고, 본문이 그 범위를
직접 모순하지 않는 한 pass로 판정하라. 일반 설명을 검증된 실행 결과로 바꾸어 읽지 마라.
이 예외는 evidence_boundary에만 적용한다. 같은 말을 반복하는 동어반복, 연결 없는 짧은 단문,
"이것은 이것이다" 같은 무의미한 문장은 natural_korean을 반드시 fail로 판정하라.

오탐을 막기 위해 결함을 현재 Markdown의 선택한 anchor에서 직접 입증할 수 있을 때만 fail로
판정한다. 문장이 자연스럽거나 사실·해석의 경계를 위반하지 않는 anchor를 고른 뒤 막연히
"충분하지 않다"고 평가해서는 안 된다. natural_korean은 선택한 문장 자체의 문법·호응·연결에
구체적인 결함이 있어야 fail이고, 완전한 의문문·설명문·도입문은 짧다는 이유만으로 fail이 아니다.
evidence_boundary는 특정 실행·측정·버전 주장과 그 근거 경계가 실제로 충돌하는 문장을 선택할 수
있을 때만 fail이다. 명확한 결함을 입증하지 못하면 pass로 판정하라.

하나라도 fail이면 해당 페이지 verdict는 needs-revision이다.
anchor는 페이지에서 실제로 찾은 후보를 골라야 한다.
후보가 약해도 문서 밖에서 보완하지 말고 JSON schema에 맞는 객체만 출력하라.

INPUT DATA
"""
    payload = {"batch_id": batch_id, "pages": pages, "compact_response_contract": contract}
    if previous_validation_error is not None:
        instructions += (
            "\n이전 출력은 저장되지 않았다. 다음 검증 오류를 고친 JSON만 출력하라: "
            f"{previous_validation_error}\n"
        )
    return instructions + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_response_contract(targets: dict[str, ReviewTarget]) -> dict[str, object]:
    return {
        "criterion_order": list(CRITERIA),
        "pages": [
            {
                "page_id": page_id,
                "anchor_candidates": _compact_anchor_candidates({page_id: target}),
            }
            for page_id, target in sorted(targets.items())
        ],
        "response": {
            "reviews": [{"r": "pppppp", "a": [0] * len(CRITERIA)} for _ in sorted(targets)]
        },
    }


def _response_schema(targets: dict[str, ReviewTarget]) -> dict[str, object]:
    maximum = max(
        len(options)
        for page_id, target in targets.items()
        for options in _compact_anchor_candidates({page_id: target}).values()
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviews"],
        "properties": {
            "reviews": {
                "type": "array",
                "minItems": len(targets),
                "maxItems": len(targets),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["r", "a"],
                    "properties": {
                        "r": {"type": "string", "pattern": f"^[pf]{{{len(CRITERIA)}}}$"},
                        "a": {
                            "type": "array",
                            "minItems": len(CRITERIA),
                            "maxItems": len(CRITERIA),
                            "items": {"type": "integer", "minimum": 0, "maximum": maximum - 1},
                        },
                    },
                },
            }
        },
    }


def _expand_codex_model_result(
    value: dict[str, object], batch_id: str, targets: dict[str, ReviewTarget]
) -> dict[str, object]:
    if set(value) != {"reviews"} or not isinstance(value["reviews"], list):
        raise WoonError("Codex quality review response must contain only reviews")
    raw_reviews = value["reviews"]
    if len(raw_reviews) != len(targets):
        raise WoonError("Codex quality review response does not cover every page")
    reviews: list[object] = []
    for raw_review, (page_id, target) in zip(raw_reviews, sorted(targets.items()), strict=True):
        if not isinstance(raw_review, dict):
            raise WoonError("Codex quality review compact entry must be an object")
        expanded = _expand_local_model_result(raw_review, batch_id, {page_id: target})
        expanded_reviews = expanded.get("reviews")
        if (
            not isinstance(expanded_reviews, list)
            or len(expanded_reviews) != 1
            or not isinstance(expanded_reviews[0], dict)
        ):
            raise WoonError("Codex quality review compact entry has an invalid review")
        _honor_scope_note_evidence_boundary(expanded_reviews[0])
        _replace_duplicate_anchors(expanded_reviews[0], target)
        reviews.extend(expanded_reviews)
    return {"version": PLAN_VERSION, "batch_id": batch_id, "reviews": reviews}


def _honor_scope_note_evidence_boundary(review: dict[str, object]) -> None:
    """Reject a self-contradictory failure that cites the scope note itself.

    The writing contract says a ``확인 범위:`` note is the primary passing
    evidence unless another sentence contradicts it.  A reviewer that selects
    the note itself as the failing anchor has not identified that contradiction.
    """

    rubric = review.get("rubric")
    anchors = review.get("evidence_anchors")
    if not isinstance(rubric, dict) or not isinstance(anchors, dict):
        return
    anchor = anchors.get("evidence_boundary")
    if rubric.get("evidence_boundary") != "fail" or not isinstance(anchor, str):
        return
    if not anchor.lstrip().startswith("확인 범위:"):
        return
    rubric["evidence_boundary"] = "pass"
    if all(rubric.get(criterion) == "pass" for criterion in CRITERIA):
        review["verdict"] = "passed"


def _replace_duplicate_anchors(review: dict[str, object], target: ReviewTarget) -> None:
    anchors = review.get("evidence_anchors")
    if not isinstance(anchors, dict) or set(anchors) != set(CRITERIA):
        raise WoonError("Codex quality review compact entry has invalid evidence anchors")
    candidates = _compact_anchor_candidates({"page": target})
    used: set[str] = set()
    for criterion in CRITERIA:
        anchor = anchors.get(criterion)
        if not isinstance(anchor, str) or not anchor:
            raise WoonError("Codex quality review compact entry has an invalid evidence anchor")
        if anchor in used:
            replacement = next(
                (candidate for candidate in candidates[criterion] if candidate not in used),
                None,
            )
            if replacement is not None:
                anchors[criterion] = replacement
                anchor = replacement
        used.add(anchor)
    if len(used) < 4:
        raise WoonError("Codex quality review has fewer than four distinct evidence anchors")


def _run_codex(
    prompt: str,
    response_schema: dict[str, object],
    binary: str,
    model: str,
    timeout_seconds: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="woon-codex-quality-") as temporary:
        directory = Path(temporary)
        schema_path = directory / "response-schema.json"
        output_path = directory / "result.json"
        schema_path.write_text(
            json.dumps(response_schema, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        command = [
            binary,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
        ]
        if model != "subscription-default":
            command.extend(["--model", model])
        command.extend(["--config", f'model_reasoning_effort="{REASONING_EFFORT}"'])
        for feature in (
            "apps",
            "browser_use",
            "code_mode_host",
            "computer_use",
            "goals",
            "image_generation",
            "memories",
            "multi_agent",
            "plugins",
            "shell_tool",
            "tool_suggest",
            "unified_exec",
            "workspace_dependencies",
        ):
            command.extend(["--disable", feature])
        command.extend(
            [
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-C",
                str(directory),
                "-",
            ]
        )
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=directory,
                check=False,
                env=_subscription_environment(),
            )
        except subprocess.TimeoutExpired as error:
            raise WoonError(
                f"Codex quality review timed out after {timeout_seconds} seconds"
            ) from error
        except OSError as error:
            raise WoonError(f"Codex quality review could not start: {error}") from error
        if completed.returncode != 0 or not output_path.is_file():
            detail = _cli_error_detail(completed.stderr, completed.stdout)
            suffix = f": {detail}" if detail else ""
            raise WoonError(f"Codex quality review CLI failed{suffix}")
        try:
            value = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WoonError("Codex quality review CLI returned invalid JSON") from error
        if not isinstance(value, dict):
            raise WoonError("Codex quality review CLI must return a JSON object")
        return value


def _write_failure(destination: Path, batch_id: str, error: str) -> None:
    sequence = 1
    while (destination / f"{batch_id}.attempt-{sequence:03d}.failure.json").exists():
        sequence += 1
    atomic_write(
        destination / f"{batch_id}.attempt-{sequence:03d}.failure.json",
        encode_json({"version": 1, "batch_id": batch_id, "attempt": sequence, "error": error}),
        mode=0o600,
    )


def _cli_error_detail(*values: str) -> str:
    for value in values:
        normalized = " ".join(value.split())
        if normalized:
            return normalized[:400]
    return ""


def _subscription_environment() -> dict[str, str]:
    """Prevent an inherited key or endpoint from changing the billing path."""

    blocked = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    }
    return {key: value for key, value in os.environ.items() if key not in blocked}


def _validate_timeout(value: int) -> None:
    if not 30 <= value <= 3600:
        raise WoonError("Codex quality review timeout must be between 30 and 3600 seconds")


def _validate_attempts(value: int) -> None:
    if not 1 <= value <= 3:
        raise WoonError("Codex quality review max_attempts must be between 1 and 3")
