"""Create and assemble resumable semantic quality review batches.

The quality evaluator intentionally does not judge Korean prose itself.  This
module supplies the missing operational boundary: it gives a human or an LLM
small, immutable batches to review, and refuses to assemble the result when a
page, writing standard, or planned receipt has changed in the meantime.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.content_quality_evaluation import (
    RUBRIC,
    VERDICTS,
    review_verdict_consistency_error,
    validate_criterion_evidence,
)

PLAN_VERSION = 1
INHERITED_RESULTS_FILE = ".inherited-results.json"
MAX_BATCH_SIZE = 64
DEFAULT_MAX_BATCH_CHARS = 24_000
MIN_MAX_BATCH_CHARS = 4_000
MAX_MAX_BATCH_CHARS = 200_000


def create_content_quality_review_plan(
    vault: Path,
    standard_path: Path,
    standard_uri: str,
    prompt_path: Path,
    prompt_uri: str,
    output_dir: Path,
    batch_size: int,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
) -> dict[str, object]:
    """Write immutable review inputs for every current compiled page.

    A plan directory is deliberately write-once.  Reusing an old plan after a
    compiler or writing-standard change would otherwise make stale reviews look
    current.
    """

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise WoonError(f"quality review batch_size must be between 1 and {MAX_BATCH_SIZE}")
    _validate_max_batch_chars(max_batch_chars)
    standard = _reference(standard_path, standard_uri, "writing standard")
    prompt = _reference(prompt_path, prompt_uri, "quality review prompt")
    targets = _current_targets(vault)
    destination = output_dir.expanduser().resolve()
    if destination.exists():
        raise WoonError(f"quality review plan output already exists: {destination}")

    batches = _batch_targets(targets, batch_size, max_batch_chars)
    manifest_batches: list[dict[str, object]] = []
    rendered_batches: list[tuple[Path, dict[str, object]]] = []
    for number, batch in enumerate(batches, start=1):
        batch_id = f"quality-{number:03d}"
        input_file = f"{batch_id}.input.json"
        result_file = f"{batch_id}.result.json"
        manifest_batches.append(
            {
                "batch_id": batch_id,
                "input_file": input_file,
                "result_file": result_file,
                "targets": [_target_manifest(target) for target in batch],
            }
        )
        rendered_batches.append(
            (
                destination / input_file,
                {
                    "version": PLAN_VERSION,
                    "batch_id": batch_id,
                    "standard": standard,
                    "prompt": prompt,
                    "prompt_text": prompt_path.expanduser().read_text(encoding="utf-8"),
                    "response_contract": {
                        "result_file": result_file,
                        "review_only": True,
                        "do_not_rewrite_markdown": True,
                        "required_local_review_fields": [
                            "page_id",
                            "output_sha256",
                            "verdict",
                            "rubric",
                            "hard_failures",
                            "evidence_anchors",
                        ],
                        "assembled_review_fields": [
                            "page_id",
                            "output_sha256",
                            "verdict",
                            "rubric",
                            "hard_failures",
                            "criterion_evidence",
                        ],
                    },
                    "targets": batch,
                },
            )
        )
    manifest: dict[str, object] = {
        "version": PLAN_VERSION,
        "purpose": (
            "현재 컴파일된 모든 Wiki의 한국어 학습 품질을 receipt와 표준 해시에 묶어 검토한다."
        ),
        "standard": standard,
        "prompt": prompt,
        "batch_size": batch_size,
        "max_batch_chars": max_batch_chars,
        "targets_sha256": _targets_digest(targets),
        "compiled_pages": len(targets),
        "batches": manifest_batches,
    }
    destination.mkdir(parents=True)
    try:
        for path, payload in rendered_batches:
            atomic_write(path, encode_json(payload))
        atomic_write(destination / "manifest.json", encode_json(manifest))
    except BaseException:
        # The directory did not exist before this call, so partial plan files
        # cannot be mistaken for a valid resumable plan on a later run.
        for path, _ in rendered_batches:
            path.unlink(missing_ok=True)
        (destination / "manifest.json").unlink(missing_ok=True)
        destination.rmdir()
        raise
    return {
        "version": PLAN_VERSION,
        "output": str(destination),
        "compiled_pages": len(targets),
        "batches": len(batches),
        "targets_sha256": manifest["targets_sha256"],
    }


def rebase_content_quality_review_plan(
    vault: Path,
    prior_plan_path: Path,
    prior_results_dir: Path,
    standard_path: Path,
    standard_uri: str,
    prompt_path: Path,
    prompt_uri: str,
    output_dir: Path,
    results_output_dir: Path,
    batch_size: int,
    max_batch_chars: int = DEFAULT_MAX_BATCH_CHARS,
) -> dict[str, object]:
    """Create a current plan and preserve only byte-identical prior reviews.

    A plan itself is immutable: it is never patched after a receipt changes.
    This operation creates a new plan and result directory, then copies a
    complete batch only when its prompt, standard, and every reviewed Markdown
    hash still match. A changed page therefore reopens its whole small batch.
    """

    prior = _load_object(prior_plan_path, "prior quality review plan")
    _validate_plan(prior)
    destination = output_dir.expanduser().resolve()
    results_destination = results_output_dir.expanduser().resolve()
    if results_destination.exists():
        raise WoonError(
            f"rebased quality review results output already exists: {results_destination}"
        )
    created = create_content_quality_review_plan(
        vault,
        standard_path,
        standard_uri,
        prompt_path,
        prompt_uri,
        destination,
        batch_size,
        max_batch_chars,
    )
    current = _load_object(destination / "manifest.json", "rebased quality review plan")
    _validate_plan(current)
    results_destination.mkdir(parents=True)

    can_reuse = _references_match(prior, current)
    prior_reviews: dict[str, dict[str, object]] = {}
    invalid_prior_batches: list[str] = []
    if can_reuse:
        prior_reviews, invalid_prior_batches = _read_reusable_reviews(
            prior,
            prior_plan_path.expanduser().resolve().parent,
            prior_results_dir.expanduser().resolve(),
        )

    reused_batches: list[str] = []
    reusable_pages = 0
    batches_to_review: list[str] = []
    for raw_batch in _list(current["batches"], "rebased quality review plan batches"):
        batch = _mapping(raw_batch, "rebased quality review plan batch")
        batch_id = _text(batch.get("batch_id"), "rebased quality review batch_id")
        targets = _targets_from_manifest(batch.get("targets"))
        reviews = [prior_reviews.get(page_id) for page_id in targets]
        if len(reviews) == len(targets) and all(
            review is not None and review["output_sha256"] == target["output_sha256"]
            for review, target in zip(reviews, targets.values(), strict=True)
        ):
            result_file = _safe_relative(
                _text(batch.get("result_file"), "rebased quality review result_file"),
                "rebased quality review result_file",
            )
            atomic_write(
                results_destination / result_file,
                encode_json(
                    {
                        "version": PLAN_VERSION,
                        "batch_id": batch_id,
                        "reviews": [review for review in reviews if review is not None],
                    }
                ),
            )
            reused_batches.append(batch_id)
            reusable_pages += len(targets)
        else:
            batches_to_review.append(batch_id)

    inherited_results: str | None = None
    prior_run_manifest = prior_results_dir.expanduser().resolve() / "run-manifest.json"
    if reused_batches and prior_run_manifest.is_file():
        inherited_path = results_destination / INHERITED_RESULTS_FILE
        atomic_write(
            inherited_path,
            encode_json(
                {
                    "version": 1,
                    "prior_plan_sha256": _file_sha256(prior_plan_path, "prior quality review plan"),
                    "prior_run_manifest_sha256": _file_sha256(
                        prior_run_manifest, "prior quality review run manifest"
                    ),
                    "result_files": _result_file_digests(results_destination, reused_batches),
                }
            ),
        )
        inherited_results = str(inherited_path)

    return {
        "version": PLAN_VERSION,
        "plan": created,
        "results": str(results_destination),
        "reused_batches": reused_batches,
        "reused_pages": reusable_pages,
        "batches_to_review": batches_to_review,
        "reuse_skipped_reason": None if can_reuse else "standard-or-prompt-changed",
        "invalid_prior_batches": invalid_prior_batches,
        "inherited_results": inherited_results,
    }


def assemble_content_quality_reviews(
    vault: Path,
    plan_path: Path,
    results_dir: Path,
    standard_path: Path,
    evaluator_name: str,
    evaluator_version: str,
    output_path: Path,
) -> dict[str, object]:
    """Assemble complete batch results into the evaluator's accepted payload."""

    plan = _load_object(plan_path, "quality review plan")
    _validate_plan(plan)
    current_targets = _current_targets(vault)
    if plan["targets_sha256"] != _targets_digest(current_targets):
        raise WoonError("quality review plan is stale for the current compiled pages")
    standard = _mapping(plan["standard"], "quality review plan standard")
    current_standard_sha256 = _file_sha256(standard_path, "writing standard")
    if standard["sha256"] != current_standard_sha256:
        raise WoonError("quality review plan used a stale or incorrect writing standard")

    destination = output_path.expanduser().resolve()
    if destination.exists():
        raise WoonError(f"assembled quality review output already exists: {destination}")
    results_root = results_dir.expanduser().resolve()
    expected = {target["page_id"]: target for target in current_targets}
    reviews: dict[str, dict[str, object]] = {}
    batches = _list(plan["batches"], "quality review plan batches")
    for raw_batch in batches:
        batch = _mapping(raw_batch, "quality review plan batch")
        batch_id = _text(batch.get("batch_id"), "quality review batch_id")
        result_file = _safe_relative(
            _text(batch.get("result_file"), "quality review batch result_file"),
            "quality review batch result_file",
        )
        batch_targets = _targets_from_manifest(batch.get("targets"))
        for page_id, batch_target in batch_targets.items():
            current_target = expected.get(page_id)
            if (
                current_target is None
                or batch_target["output_sha256"] != current_target["output_sha256"]
            ):
                raise WoonError(f"quality review plan is stale for page: {page_id}")
            batch_target["markdown"] = current_target["markdown"]
        result = _load_object(results_root / result_file, f"quality review result {batch_id}")
        if result.get("version") != PLAN_VERSION:
            raise WoonError(f"quality review result {batch_id} has unsupported version")
        if result.get("batch_id") != batch_id:
            raise WoonError(f"quality review result {batch_id} has a mismatched batch_id")
        raw_reviews = _list(result.get("reviews"), f"quality review result {batch_id} reviews")
        received = _reviews_for_batch(raw_reviews, batch_targets, batch_id)
        for page_id, review in received.items():
            if page_id in reviews:
                raise WoonError(f"duplicate quality review across batches: {page_id}")
            target = expected.get(page_id)
            if target is None or review["output_sha256"] != target["output_sha256"]:
                raise WoonError(f"quality review result {batch_id} is stale for page: {page_id}")
            reviews[page_id] = review
    if set(reviews) != set(expected):
        missing = sorted(set(expected).difference(reviews))
        raise WoonError("quality review results are incomplete: missing=" + ",".join(missing))

    payload = {
        "version": PLAN_VERSION,
        "standard": standard,
        "evaluator": {
            "name": _text(evaluator_name, "quality evaluator_name"),
            "version": _text(evaluator_version, "quality evaluator_version"),
            "prompt_sha256": _mapping(plan["prompt"], "quality review plan prompt")["sha256"],
        },
        "reviews": [reviews[page_id] for page_id in sorted(reviews)],
    }
    atomic_write(destination, encode_json(payload))
    return {
        "version": PLAN_VERSION,
        "output": str(destination),
        "compiled_pages": len(expected),
        "reviews": len(reviews),
        "targets_sha256": plan["targets_sha256"],
    }


def _current_targets(vault: Path) -> list[dict[str, str]]:
    root = vault.expanduser().resolve()
    pages = _load_yaml_list(root / "catalog/llm-wiki/pages.yaml", "pages")
    receipts = _load_yaml_list(root / "catalog/llm-wiki/receipts.yaml", "receipts")
    receipt_hashes = {
        _text(receipt.get("page_id"), "quality review receipt page_id"): _digest(
            receipt.get("output_sha256"), "quality review receipt output_sha256"
        )
        for receipt in receipts
    }
    if len(receipt_hashes) != len(receipts):
        raise WoonError("quality review receipts contain duplicate page_id")
    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for page in pages:
        page_id = _text(page.get("page_id"), "quality review page_id")
        if page_id in seen:
            raise WoonError(f"duplicate compiled page spec: {page_id}")
        seen.add(page_id)
        output_path = _safe_relative(
            _text(page.get("output_path"), "quality review output_path"),
            "quality review output_path",
        )
        output_sha256 = receipt_hashes.get(page_id)
        if output_sha256 is None:
            raise WoonError(f"compiled page has no receipt: {page_id}")
        markdown_path = root / "wiki" / output_path
        try:
            markdown = markdown_path.read_text(encoding="utf-8")
        except OSError as error:
            raise WoonError(f"cannot read compiled page {page_id}: {error}") from error
        if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != output_sha256:
            raise WoonError(f"compiled page bytes do not match receipt: {page_id}")
        targets.append(
            {
                "page_id": page_id,
                "relative_path": f"wiki/{output_path}",
                "title": _text(page.get("title"), "quality review page title"),
                "output_sha256": output_sha256,
                "markdown": markdown,
            }
        )
    extra = sorted(set(receipt_hashes).difference(seen))
    if extra:
        raise WoonError("quality review receipts have no page spec: " + ",".join(extra))
    return sorted(targets, key=lambda target: target["page_id"])


def _batch_targets(
    targets: list[dict[str, str]], batch_size: int, max_batch_chars: int
) -> list[list[dict[str, str]]]:
    """Keep each model input small without splitting a compiled document."""

    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_chars = 0
    for target in targets:
        target_chars = len(target["markdown"])
        exceeds_page_limit = current and current_chars + target_chars > max_batch_chars
        if current and (len(current) >= batch_size or exceeds_page_limit):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += target_chars
    if current:
        batches.append(current)
    return batches


def _validate_max_batch_chars(value: int) -> None:
    if not MIN_MAX_BATCH_CHARS <= value <= MAX_MAX_BATCH_CHARS:
        raise WoonError(
            "quality review max_batch_chars must be between "
            f"{MIN_MAX_BATCH_CHARS} and {MAX_MAX_BATCH_CHARS}"
        )


def _reference(path: Path, uri: str, label: str) -> dict[str, str]:
    normalized_uri = _text(uri, f"{label} uri")
    if not normalized_uri.startswith("repo://skills/"):
        raise WoonError(f"{label} uri must use repo://skills/")
    return {"uri": normalized_uri, "sha256": _file_sha256(path, label)}


def _target_manifest(target: dict[str, str]) -> dict[str, str]:
    return {key: target[key] for key in ("page_id", "relative_path", "title", "output_sha256")}


def _references_match(prior: dict[str, object], current: dict[str, object]) -> bool:
    """Reuse a review only under the exact standard and prompt that shaped it."""

    return prior.get("standard") == current.get("standard") and prior.get("prompt") == current.get(
        "prompt"
    )


def _read_reusable_reviews(
    plan: dict[str, object], plan_root: Path, results_root: Path
) -> tuple[dict[str, dict[str, object]], list[str]]:
    reviews: dict[str, dict[str, object]] = {}
    invalid_batches: list[str] = []
    for raw_batch in _list(plan["batches"], "prior quality review plan batches"):
        batch = _mapping(raw_batch, "prior quality review plan batch")
        batch_id = _text(batch.get("batch_id"), "prior quality review batch_id")
        result_file = _safe_relative(
            _text(batch.get("result_file"), "prior quality review result_file"),
            "prior quality review result_file",
        )
        result_path = results_root / result_file
        if not result_path.is_file():
            continue
        try:
            expected = _prior_batch_targets(batch, plan_root)
            result = _load_object(result_path, f"prior quality review result {batch_id}")
            if result.get("version") != PLAN_VERSION or result.get("batch_id") != batch_id:
                raise WoonError("result version or batch_id does not match the prior plan")
            received = _reviews_for_batch(
                _list(result.get("reviews"), f"prior quality review result {batch_id} reviews"),
                expected,
                batch_id,
            )
            for page_id, review in received.items():
                if page_id in reviews:
                    raise WoonError(f"duplicate prior quality review: {page_id}")
                reviews[page_id] = review
        except WoonError:
            invalid_batches.append(batch_id)
    return reviews, invalid_batches


def _prior_batch_targets(
    manifest_batch: dict[str, Any], plan_root: Path
) -> dict[str, dict[str, str]]:
    batch_id = _text(manifest_batch.get("batch_id"), "prior quality review batch_id")
    input_file = _safe_relative(
        _text(manifest_batch.get("input_file"), "prior quality review input_file"),
        "prior quality review input_file",
    )
    input_batch = _load_object(plan_root / input_file, f"prior quality review input {batch_id}")
    if input_batch.get("version") != PLAN_VERSION or input_batch.get("batch_id") != batch_id:
        raise WoonError("input version or batch_id does not match the prior plan")
    expected = _targets_from_manifest(manifest_batch.get("targets"))
    input_targets = _targets_from_manifest(input_batch.get("targets"))
    if expected != input_targets:
        raise WoonError("input targets do not match the prior plan manifest")
    raw_targets = _list(
        input_batch.get("targets"), f"prior quality review input {batch_id} targets"
    )
    for raw_target in raw_targets:
        target = _mapping(raw_target, f"prior quality review input {batch_id} target")
        page_id = _text(target.get("page_id"), f"prior quality review input {batch_id} page_id")
        expected[page_id]["markdown"] = _text(
            target.get("markdown"), f"prior quality review input {batch_id} markdown"
        )
    return expected


def _targets_from_manifest(value: object) -> dict[str, dict[str, str]]:
    targets = _list(value, "quality review batch targets")
    parsed: dict[str, dict[str, str]] = {}
    for raw_target in targets:
        target = _mapping(raw_target, "quality review batch target")
        page_id = _text(target.get("page_id"), "quality review batch target page_id")
        if page_id in parsed:
            raise WoonError(f"duplicate quality review batch target: {page_id}")
        parsed[page_id] = {
            "page_id": page_id,
            "relative_path": _safe_relative(
                _text(target.get("relative_path"), "quality review batch target relative_path"),
                "quality review batch target relative_path",
            ),
            "title": _text(target.get("title"), "quality review batch target title"),
            "output_sha256": _digest(
                target.get("output_sha256"), "quality review batch target output_sha256"
            ),
        }
    return parsed


def _reviews_for_batch(
    raw_reviews: list[object], expected: dict[str, dict[str, str]], batch_id: str
) -> dict[str, dict[str, object]]:
    received: dict[str, dict[str, object]] = {}
    for raw_review in raw_reviews:
        review = _mapping(raw_review, f"quality review result {batch_id} review")
        page_id = _text(review.get("page_id"), f"quality review result {batch_id} page_id")
        if page_id in received:
            raise WoonError(f"duplicate quality review in batch {batch_id}: {page_id}")
        output_sha256 = _digest(
            review.get("output_sha256"), f"quality review result {batch_id} output_sha256"
        )
        verdict = _text(review.get("verdict"), f"quality review result {batch_id} verdict")
        if verdict not in VERDICTS:
            raise WoonError(f"quality review result {batch_id} has invalid verdict: {page_id}")
        rubric = _mapping(review.get("rubric"), f"quality review result {batch_id} rubric")
        if set(rubric) != RUBRIC or any(score not in {"pass", "fail"} for score in rubric.values()):
            raise WoonError(f"quality review result {batch_id} has invalid rubric: {page_id}")
        hard_failures = _string_list(
            review.get("hard_failures"), f"quality review result {batch_id} hard_failures"
        )
        consistency_error = review_verdict_consistency_error(
            verdict,
            {criterion: str(rubric[criterion]) for criterion in RUBRIC},
            hard_failures,
            page_id,
        )
        if consistency_error is not None:
            raise WoonError(consistency_error)
        criterion_evidence = validate_criterion_evidence(
            review.get("criterion_evidence"),
            {criterion: str(rubric[criterion]) for criterion in RUBRIC},
            page_id,
            expected[page_id]["markdown"],
        )
        received[page_id] = {
            "page_id": page_id,
            "output_sha256": output_sha256,
            "verdict": verdict,
            "rubric": {criterion: str(rubric[criterion]) for criterion in sorted(RUBRIC)},
            "hard_failures": hard_failures,
            "criterion_evidence": criterion_evidence,
        }
    if set(received) != set(expected):
        missing = sorted(set(expected).difference(received))
        unexpected = sorted(set(received).difference(expected))
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise WoonError(
            f"quality review result {batch_id} does not match plan: " + " ".join(details)
        )
    return received


def _validate_plan(plan: dict[str, object]) -> None:
    if plan.get("version") != PLAN_VERSION:
        raise WoonError("quality review plan has unsupported version")
    _mapping(plan.get("standard"), "quality review plan standard")
    _mapping(plan.get("prompt"), "quality review plan prompt")
    _digest(plan.get("targets_sha256"), "quality review plan targets_sha256")
    _list(plan.get("batches"), "quality review plan batches")
    if "max_batch_chars" in plan:
        max_batch_chars = plan["max_batch_chars"]
        if isinstance(max_batch_chars, bool) or not isinstance(max_batch_chars, int):
            raise WoonError("quality review plan max_batch_chars must be an integer")
        _validate_max_batch_chars(max_batch_chars)


def _targets_digest(targets: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for target in sorted(targets, key=lambda item: item["page_id"]):
        for key in ("page_id", "relative_path", "title", "output_sha256"):
            digest.update(target[key].encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


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
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    return _mapping(payload, label)


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"{field} must be an object")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WoonError(f"{field} must be a list")
    return value


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
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WoonError(f"{field} must be a SHA-256 digest")
    return value


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.expanduser().read_bytes()).hexdigest()
    except OSError as error:
        raise WoonError(f"cannot read {label}: {error}") from error


def _result_file_digests(results_dir: Path, batch_ids: list[str]) -> list[dict[str, str]]:
    """Record exactly which validated prior results are inherited by a rebase."""

    records: list[dict[str, str]] = []
    for batch_id in sorted(batch_ids):
        relative = f"{batch_id}.result.json"
        records.append(
            {
                "path": relative,
                "sha256": _file_sha256(results_dir / relative, "inherited quality review result"),
            }
        )
    return records


def _safe_relative(value: str, field: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise WoonError(f"{field} must be a safe relative path")
    return path.as_posix()
