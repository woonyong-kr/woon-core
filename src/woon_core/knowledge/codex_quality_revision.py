"""Create and safely apply Codex-proposed Korean Wiki prose revisions.

The proposal step sends only a failed compiled ``wiki/**`` page and its review
findings to a ChatGPT-subscribed Codex CLI.  Applying a proposal never replaces
the legacy input: it promotes a separate ``curated-wiki`` source through the
compiler, rebuilds the page, and refreshes the local search index.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.codex_quality_review import (
    HOSTED_PROVIDER,
    _codex_binary,
    _hosted_targets,
    _require_chatgpt_login,
    _run_codex,
)
from woon_core.knowledge.compiled_wiki import CuratedRevision
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.ollama_quality_review import (
    _input_batch,
    _load_json,
    _load_plan,
    _safe_relative,
    _text,
    _validate_result,
)

REVISION_VERSION = 1
RUN_MANIFEST_FILE = "run-manifest.json"
DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_MAX_ATTEMPTS = 1
_DISABLED_TOOLS = (
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
)
_FENCED_BLOCK = re.compile(r"(?ms)^(?P<fence>`{3,}|~{3,})[^\n]*\n.*?^(?P=fence)[ \t]*$")
_WIKILINK = re.compile(r"\[\[[^\]\n]+]]")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*]\(([^)\n]+)\)")
_INLINE_CODE = re.compile(r"(?<!`)`[^`\n]+`(?!`)")
_H1 = re.compile(r"\A\s*#\s+", re.MULTILINE)
_KEEP_MARKER = re.compile(r"@@WOON_KEEP_[0-9]{3}@@")


@dataclass(frozen=True, slots=True)
class RevisionCandidate:
    """One quality-failed compiled page, bound to the review receipt."""

    page_id: str
    output_sha256: str
    source_body_sha256: str
    title: str
    purpose: str
    body: str
    failures: tuple[str, ...]
    failure_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexRevisionReport:
    """Observable result of a resumable Codex proposal run."""

    model: str
    proposed_pages: int
    skipped_pages: int
    failed_pages: tuple[dict[str, str], ...]
    output: str


def create_codex_quality_revision_proposals(
    vault: Path,
    plan_path: Path,
    reviews_dir: Path,
    output_dir: Path,
    *,
    model: str | None = None,
    codex_binary: str = "codex",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    continue_on_error: bool = False,
    page_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Write checked prose proposals for every failed general-Wiki page.

    The result directory is a review artifact, not a compiler input.  Its
    contents cannot mutate the vault until ``apply_codex_quality_revisions``
    validates it again against current receipts and promotes curated sources.
    """

    normalized_model = _model(model)
    _validate_timeout(timeout_seconds)
    _validate_attempts(max_attempts)
    candidates, reviews_sha256 = _revision_candidates(vault, plan_path, reviews_dir)
    candidates = _selected_candidates(candidates, page_ids)
    binary = _codex_binary(codex_binary)
    _require_chatgpt_login(binary)
    destination = output_dir.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    _prepare_manifest(
        destination,
        plan_path,
        reviews_sha256,
        normalized_model,
        timeout_seconds,
        max_attempts,
        binary,
    )

    proposed = 0
    skipped = 0
    failures: list[dict[str, str]] = []
    for candidate in candidates:
        path = destination / _proposal_file_name(candidate.page_id)
        try:
            if path.exists():
                _validate_proposal(_load_json(path, "Codex quality revision"), candidate)
                skipped += 1
                continue
            proposal = _propose_revision(
                candidate, binary, normalized_model, timeout_seconds, max_attempts
            )
            _validate_proposal(proposal, candidate)
            atomic_write(path, encode_json(_proposal_record(candidate, proposal)))
            proposed += 1
        except WoonError as error:
            _write_failure(destination, candidate.page_id, str(error))
            if not continue_on_error:
                raise
            failures.append({"page_id": candidate.page_id, "error": str(error)})
    return asdict(
        CodexRevisionReport(
            model=normalized_model,
            proposed_pages=proposed,
            skipped_pages=skipped,
            failed_pages=tuple(failures),
            output=str(destination),
        )
    )


def apply_codex_quality_revisions(
    vault: Path,
    plan_path: Path,
    reviews_dir: Path,
    proposals_dirs: tuple[Path, ...],
    *,
    duplicate_policy: str = "error",
) -> dict[str, object]:
    """Promote complete, current proposals and rebuild the local search index.

    A retry run is written to a separate directory so its execution contract is
    auditable instead of rewriting an earlier proposal receipt.  Every failed
    page must still have exactly one valid proposal across all supplied runs.
    """

    candidates, reviews_sha256 = _revision_candidates(vault, plan_path, reviews_dir)
    proposal_records, proposal_sources = _collect_proposal_records(
        candidates, proposals_dirs, plan_path, reviews_sha256, duplicate_policy
    )
    records: list[CuratedRevision] = []
    for candidate in candidates:
        proposal = proposal_records[candidate.page_id]
        records.append(
            CuratedRevision(
                page_id=candidate.page_id,
                body=_text(proposal.get("body"), "Codex revision body"),
                statement=_text(proposal.get("statement"), "Codex revision statement"),
                current_use=_text(proposal.get("current_use"), "Codex revision current_use"),
            )
        )
    _, service = build_knowledge_service(vault.expanduser().resolve())
    report = service.curate_compiled_wiki_revisions(tuple(records))
    return {
        "curated": report.curated,
        "compiled": report.compiled,
        "unchanged": report.unchanged,
        "page_ids": list(report.page_ids),
        "search_reindexed": True,
        "proposal_sources": proposal_sources,
    }


def _collect_proposal_records(
    candidates: tuple[RevisionCandidate, ...],
    proposals_dirs: tuple[Path, ...],
    plan_path: Path,
    reviews_sha256: str,
    duplicate_policy: str = "error",
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    """Load one proposal per candidate from one or more isolated run receipts."""

    if not proposals_dirs:
        raise WoonError("at least one Codex revision proposal directory is required")
    if duplicate_policy not in {"error", "first-valid"}:
        raise WoonError("Codex revision duplicate policy must be error or first-valid")
    expected = {candidate.page_id: candidate for candidate in candidates}
    records: dict[str, dict[str, object]] = {}
    sources: dict[str, str] = {}
    roots: set[Path] = set()
    for proposals_dir in proposals_dirs:
        root = proposals_dir.expanduser().resolve()
        if root in roots:
            raise WoonError(f"Codex revision proposal directory is duplicated: {root}")
        roots.add(root)
        manifest = _load_json(root / RUN_MANIFEST_FILE, "Codex revision run manifest")
        _validate_manifest(manifest, plan_path, reviews_sha256)
        for proposal_path in sorted(root.glob("*.proposal.json")):
            proposal = _load_json(proposal_path, "Codex quality revision")
            page_id = _text(proposal.get("page_id"), "Codex revision page_id")
            candidate = expected.get(page_id)
            if candidate is None:
                raise WoonError(f"Codex revision proposal is not a failed page: {page_id}")
            if proposal_path.name != _proposal_file_name(page_id):
                raise WoonError(f"Codex revision proposal has an invalid file name: {page_id}")
            _validate_proposal(proposal, candidate)
            if page_id in records:
                if duplicate_policy == "first-valid":
                    continue
                raise WoonError(f"Codex revision proposal is duplicated: {page_id}")
            records[page_id] = proposal
            sources[page_id] = str(proposal_path)
    missing = sorted(set(expected).difference(records))
    if missing:
        raise WoonError(f"Codex revision proposal is missing: {missing[0]}")
    return records, sources


def _revision_candidates(
    vault: Path, plan_path: Path, reviews_dir: Path
) -> tuple[tuple[RevisionCandidate, ...], str]:
    plan = _load_plan(plan_path)
    plan_root = plan_path.expanduser().resolve().parent
    results_root = reviews_dir.expanduser().resolve()
    run_manifest = _load_json(results_root / RUN_MANIFEST_FILE, "Codex quality review run manifest")
    if run_manifest.get("provider") != HOSTED_PROVIDER:
        raise WoonError("Codex revision requires reviews made by the ChatGPT Codex CLI runner")
    if run_manifest.get("plan_sha256") != _sha256_file(plan_path):
        raise WoonError("Codex quality review results belong to a different plan")

    pages, curations = _catalog_pages(vault)
    candidates: list[RevisionCandidate] = []
    reviewed_files: list[Path] = []
    for raw_batch in plan["batches"]:
        if not isinstance(raw_batch, dict):
            raise WoonError("quality review plan batch must be an object")
        batch_id = _text(raw_batch.get("batch_id"), "quality review batch_id")
        batch = _input_batch(plan, raw_batch, plan_root)
        targets = _hosted_targets(batch, batch_id)
        relative_paths = {
            _text(item.get("page_id"), "quality review page_id"): _text(
                item.get("relative_path"), "quality review relative_path"
            )
            for item in _target_mappings(batch.get("targets"), batch_id)
        }
        result_path = results_root / _safe_relative(
            _text(raw_batch.get("result_file"), "quality review result_file")
        )
        if not result_path.is_file():
            raise WoonError(f"Codex quality review result is missing: {batch_id}")
        result = _load_json(result_path, "Codex quality review result")
        _validate_result(result, batch_id, targets)
        reviewed_files.append(result_path)
        reviews = result.get("reviews")
        assert isinstance(reviews, list)
        for review in reviews:
            assert isinstance(review, dict)
            if review.get("verdict") != "needs-revision":
                continue
            page_id = _text(review.get("page_id"), "Codex quality review page_id")
            target = targets[page_id]
            page = pages.get(page_id)
            if page is None:
                raise WoonError(f"compiled Wiki page spec not found: {page_id}")
            output_path = vault.expanduser().resolve() / relative_paths[page_id]
            if not output_path.is_file() or _sha256_file(output_path) != target["output_sha256"]:
                raise WoonError(f"compiled Wiki target is stale: {page_id}")
            body = _compiled_body(target["markdown"])
            rubric = review.get("rubric")
            criterion_evidence = review.get("criterion_evidence")
            if not isinstance(rubric, dict) or not isinstance(criterion_evidence, dict):
                raise WoonError(f"Codex quality review is malformed: {page_id}")
            failures = tuple(key for key, value in sorted(rubric.items()) if value == "fail")
            if not failures:
                raise WoonError(f"Codex quality review has no failed criterion: {page_id}")
            failure_reasons = tuple(
                _text(
                    _mapping(criterion_evidence.get(key), "criterion evidence").get("reason"),
                    "criterion evidence reason",
                )
                for key in failures
            )
            curation = curations.get(page_id)
            if curation is None:
                raise WoonError(f"compiled Wiki page has no curation: {page_id}")
            candidates.append(
                RevisionCandidate(
                    page_id=page_id,
                    output_sha256=target["output_sha256"],
                    source_body_sha256=_sha256_text(body),
                    title=_text(page.get("title"), "page title"),
                    purpose=_text(curation.get("current_use"), "page current_use"),
                    body=body,
                    failures=failures,
                    failure_reasons=failure_reasons,
                )
            )
    return tuple(sorted(candidates, key=lambda item: item.page_id)), _digest_files(reviewed_files)


def _catalog_pages(vault: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    root = vault.expanduser().resolve() / "catalog/llm-wiki"
    raw_pages = _yaml_list(root / "pages.yaml", "pages")
    raw_curations = _yaml_list(root / "curation.yaml", "curations")
    return (
        {_text(item.get("page_id"), "page_id"): item for item in raw_pages},
        {_text(item.get("page_id"), "curation page_id"): item for item in raw_curations},
    )


def _target_mappings(value: object, batch_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise WoonError(f"Codex quality review targets are invalid: {batch_id}")
    if not all(isinstance(item, dict) for item in value):
        raise WoonError(f"Codex quality review targets are invalid: {batch_id}")
    return [dict(item) for item in value]


def _yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"could not read compiler catalog: {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get(key), list):
        raise WoonError(f"compiler catalog {path} has no {key} list")
    values = raw[key]
    if not all(isinstance(item, dict) for item in values):
        raise WoonError(f"compiler catalog {path} has an invalid {key} entry")
    return [dict(item) for item in values]


def _compiled_body(markdown: str) -> str:
    if not markdown.startswith("---\n"):
        raise WoonError("compiled Wiki target is missing frontmatter")
    _, separator, after_frontmatter = markdown.partition("\n---\n")
    if not separator:
        raise WoonError("compiled Wiki target has invalid frontmatter")
    h1, separator, body = after_frontmatter.lstrip("\n").partition("\n\n")
    if not h1.startswith("# ") or not separator:
        raise WoonError("compiled Wiki target is missing its compiler-owned H1")
    return body.rstrip() + "\n"


def _prepare_manifest(
    destination: Path,
    plan_path: Path,
    reviews_sha256: str,
    model: str,
    timeout_seconds: int,
    max_attempts: int,
    binary: str,
) -> None:
    expected = {
        "version": REVISION_VERSION,
        "provider": HOSTED_PROVIDER,
        "model": model,
        "codex_binary": Path(binary).name,
        "plan_sha256": _sha256_file(plan_path),
        "reviews_sha256": reviews_sha256,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "login": "ChatGPT subscription",
        "sandbox": "read-only",
        "ephemeral": True,
        "ignore_user_config": True,
        "ignore_rules": True,
        "disabled_tools": list(_DISABLED_TOOLS),
        "transmission_scope": "failed compiled wiki Markdown targets only",
    }
    path = destination / RUN_MANIFEST_FILE
    if path.exists():
        current = _load_json(path, "Codex revision run manifest")
        _validate_manifest(current, plan_path, reviews_sha256)
        if current != expected:
            raise WoonError("Codex revision proposals use a different execution manifest")
        return
    if any(destination.glob("*.proposal.json")) or any(destination.glob("*.failure.json")):
        raise WoonError("Codex revision proposals are missing their execution manifest")
    atomic_write(path, encode_json(expected))


def _validate_manifest(value: dict[str, object], plan_path: Path, reviews_sha256: str) -> None:
    if value.get("version") != REVISION_VERSION:
        raise WoonError("Codex revision manifest has an unsupported version")
    if value.get("provider") != HOSTED_PROVIDER or value.get("login") != "ChatGPT subscription":
        raise WoonError("Codex revision manifest is not bound to the ChatGPT Codex CLI")
    if value.get("plan_sha256") != _sha256_file(plan_path):
        raise WoonError("Codex revision manifest belongs to a different plan")
    if value.get("reviews_sha256") != reviews_sha256:
        raise WoonError("Codex revision manifest belongs to different quality results")


def _propose_revision(
    candidate: RevisionCandidate,
    binary: str,
    model: str,
    timeout_seconds: int,
    max_attempts: int,
) -> dict[str, object]:
    last_error: WoonError | None = None
    for attempt in range(1, max_attempts + 1):
        if candidate.failures == ("evidence_boundary",):
            raw = _run_codex(
                _evidence_scope_prompt(candidate, last_error),
                _evidence_scope_schema(),
                binary,
                model,
                timeout_seconds,
            )
            proposal = _expand_evidence_scope(raw, candidate)
        else:
            proposal = _run_codex(
                _revision_prompt(candidate, last_error),
                _revision_schema(),
                binary,
                model,
                timeout_seconds,
            )
        try:
            _validate_proposal(proposal, candidate)
        except WoonError as error:
            last_error = error
            if attempt == max_attempts:
                break
        else:
            return proposal
    if last_error is None:
        raise AssertionError("Codex revision retry loop must return or raise")
    try:
        return _propose_with_protected_template(
            candidate, binary, model, timeout_seconds, last_error
        )
    except WoonError as error:
        try:
            return _propose_with_learning_scaffold(candidate, binary, model, timeout_seconds, error)
        except WoonError as scaffold_error:
            raise WoonError(
                "Codex quality revision could not produce a valid proposal after "
                f"{max_attempts} attempts: {candidate.page_id}: {scaffold_error}"
            ) from scaffold_error


def _propose_with_protected_template(
    candidate: RevisionCandidate,
    binary: str,
    model: str,
    timeout_seconds: int,
    previous_error: WoonError,
) -> dict[str, object]:
    """Retry prose editing while restoring source tokens byte-for-byte.

    This path is only used after a normal revision dropped protected material.
    The model sees stable placeholders instead of editable code, links, and
    identifiers; restoration rejects missing, duplicate, or reordered markers.
    """

    template, replacements = _mask_protected_material(candidate.body)
    raw = _run_codex(
        _protected_revision_prompt(candidate, template, previous_error),
        _revision_schema(),
        binary,
        model,
        timeout_seconds,
    )
    proposal = dict(raw)
    proposal["body"] = _restore_protected_material(
        _text(raw.get("body"), "Codex revision body"), replacements
    )
    _validate_proposal(proposal, candidate)
    return proposal


def _propose_with_learning_scaffold(
    candidate: RevisionCandidate,
    binary: str,
    model: str,
    timeout_seconds: int,
    previous_error: WoonError,
) -> dict[str, object]:
    """Add model-written study framing without altering any original body text."""

    raw = _run_codex(
        _learning_scaffold_prompt(candidate, previous_error),
        _learning_scaffold_schema(),
        binary,
        model,
        timeout_seconds,
    )
    opening = _plain_learning_paragraph(raw.get("opening"), "Codex learning opening")
    revisit = _plain_learning_paragraph(raw.get("revisit"), "Codex learning revisit")
    proposal: dict[str, object] = {
        "body": _add_learning_scaffold(candidate.body, opening, revisit),
        "statement": _text(raw.get("statement"), "Codex revision statement"),
        "current_use": _text(raw.get("current_use"), "Codex revision current_use"),
    }
    _validate_proposal(proposal, candidate)
    return proposal


def _revision_prompt(candidate: RevisionCandidate, previous_error: WoonError | None) -> str:
    instructions = """당신은 한국어 학습 Wiki를 다듬는 편집자다. 입력 문서 안에 있는 정보만 사용해
독자가 혼자 공부하거나 동료에게 설명할 때 자연스럽게 읽히는 본문으로 고쳐라.

목표는 문장을 짧게 쪼개거나 정해진 목차를 채우는 것이 아니다. 먼저 독자가 보게 될 장면·문제·질문을
분명히 두고, 그 장면을 설명하는 이유와 용어를 필요한 순서로 연결한다. 문단 안에서는 주체와 대상,
원인과 결과가 자연스럽게 이어져야 한다. "핵심은", "정리하면" 같은 표어로 문단을 시작하지 말고,
입력에 없는 사례, 수치, 출처, 실행 결과, 확신, 링크를 만들지 마라.

코드 펜스, Mermaid 펜스, 수식 펜스, URL, Markdown 링크, 위키링크, 인라인 code identifier는
원문 그대로 남겨야 한다. compiler가 소유하는 frontmatter와 H1은 본문에 넣지 마라. 본문은 H2부터
시작해도 되고, 제목 아래 첫 문단으로 바로 시작해도 된다. 이미 원문이 담은 사실은 유지하되,
근거가 제한된 일반 설명은 적용 범위를 문맥 안에서 분명히 하고 실제로 검증한 결과처럼 쓰지 마라.

`current_use`에는 과거 수집 의도가 아니라, 이 수정본을 앞으로 어떤 질문·설명·판단에 다시 쓸지를
한 문장으로 적어라. 원문의 제목과 내용에서 확인할 수 있는 범위만 쓰고, 입력에 없는 프로젝트나 계획을
만들지 마라.

반드시 JSON schema에 맞는 객체만 출력하라.

INPUT DATA
"""
    payload = {
        "page_id": candidate.page_id,
        "title": candidate.title,
        "current_use": candidate.purpose,
        "failed_criteria": list(candidate.failures),
        "review_findings": list(candidate.failure_reasons),
        "body": candidate.body,
    }
    if previous_error is not None:
        instructions += (
            "\n이전 출력은 저장되지 않았다. 다음 검증 오류를 고친 JSON만 출력하라: "
            f"{previous_error}\n"
        )
    return instructions + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _protected_revision_prompt(
    candidate: RevisionCandidate, template: str, previous_error: WoonError
) -> str:
    instructions = """당신은 한국어 학습 Wiki를 다듬는 편집자다. 이전 전체 본문 수정은 원문의
코드·링크·식별자를 빠뜨려 저장되지 않았다. 이번에는 `body` 안의 `@@WOON_KEEP_NNN@@` 표식을
반드시 각각 한 번, 입력 순서 그대로 유지하고 표식 바깥의 한국어 문장만 자연스럽게 다듬어라.

표식은 원문 코드 펜스, Mermaid, 수식, URL, Markdown 링크, 위키링크, inline identifier를 대신한다.
표식의 철자·개수·순서를 바꾸거나 새 표식을 만들지 마라. 표식 안에 있던 정보를 바깥 문장으로
반복할 필요는 없다. 입력에 없는 사실·수치·출처·링크·실행 결과를 만들지 말고, 독자가 혼자
공부하거나 동료에게 설명할 때 자연스럽게 이해하도록 장면·이유·용어를 필요한 순서로 연결하라.

`current_use`에는 이 수정본을 앞으로 어떤 질문·설명·판단에 다시 쓸지를 한 문장으로 적어라.
과거 수집 의도나 입력에 없는 프로젝트를 만들지 마라. compiler가 소유하는 frontmatter와 H1은
본문에 넣지 말고, 반드시 JSON schema에 맞는 객체만 출력하라.

INPUT DATA
"""
    payload = {
        "page_id": candidate.page_id,
        "title": candidate.title,
        "current_use": candidate.purpose,
        "failed_criteria": list(candidate.failures),
        "review_findings": list(candidate.failure_reasons),
        "body_template": template,
        "previous_validation_error": str(previous_error),
    }
    return instructions + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _learning_scaffold_prompt(candidate: RevisionCandidate, previous_error: WoonError) -> str:
    instructions = """당신은 한국어 학습 Wiki를 다듬는 편집자다. 이전 본문 수정은 원문의
코드·링크·식별자를 보존하지 못해 저장되지 않았다. 이번에는 기존 본문을 절대 고치지 않는다.
대신 원문만 근거로, 본문 앞에 놓을 자연스러운 도입 문단과 본문 뒤에 놓을 '다시 설명할 때'
문단을 각각 한 문단씩 쓴다.

`opening`은 독자가 처음 읽을 때 이 문서에서 무엇을 이해해야 하는지, 왜 그 순서로 읽는지를
일상적인 한국어로 연결한다. `revisit`은 나중에 동료에게 설명하거나 문제를 판단할 때 어떤
질문과 구분을 다시 보면 되는지를 자연스럽게 잇는다. 두 문단 모두 표제어, Markdown, code,
링크, 인용, 목록, 줄바꿈을 넣지 말고 한 문단으로 쓴다. 입력에 없는 사실·수치·출처·실행 결과·
프로젝트를 만들지 마라. `statement`와 `current_use`도 한 줄로 작성하고, 반드시 JSON schema에
맞는 객체만 출력하라.

INPUT DATA
"""
    payload = {
        "page_id": candidate.page_id,
        "title": candidate.title,
        "current_use": candidate.purpose,
        "failed_criteria": list(candidate.failures),
        "review_findings": list(candidate.failure_reasons),
        "body": candidate.body,
        "previous_validation_error": str(previous_error),
    }
    return instructions + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _evidence_scope_prompt(candidate: RevisionCandidate, previous_error: WoonError | None) -> str:
    instructions = """당신은 한국어 학습 Wiki의 적용 범위를 명확히 하는 편집자다.
입력 본문에는 설명의 근거 경계가 부족하다는 검토 결과가 있다. 입력에 있는 정보만 사용해,
독자가 이 문서가 개념 설명인지 특정 구현의 검증 결과인지 혼동하지 않도록 하는 자연스러운
한두 문장짜리 `scope`를 작성하라. 일반 원리·선택 기준과 실제 코드, 버전, 실행 기록,
성능 측정이 필요한 판단을 구분하되, 입력에 없는 사실·수치·출처·링크·identifier를 만들지 마라.
`scope`에는 Markdown, code, 제목, blockquote 기호를 넣지 마라. JSON schema에 맞는 객체만 출력하라.

INPUT DATA
"""
    payload = {"title": candidate.title, "body": candidate.body}
    if previous_error is not None:
        instructions += (
            "\n이전 출력은 저장되지 않았다. 다음 검증 오류를 고친 JSON만 출력하라: "
            f"{previous_error}\n"
        )
    return instructions + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _revision_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["body", "statement", "current_use"],
        "properties": {
            "body": {"type": "string", "minLength": 80},
            "statement": {"type": "string", "minLength": 10, "maxLength": 360},
            "current_use": {"type": "string", "minLength": 10, "maxLength": 240},
        },
    }


def _evidence_scope_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope"],
        "properties": {"scope": {"type": "string", "minLength": 24, "maxLength": 280}},
    }


def _learning_scaffold_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["opening", "revisit", "statement", "current_use"],
        "properties": {
            "opening": {"type": "string", "minLength": 20, "maxLength": 600},
            "revisit": {"type": "string", "minLength": 20, "maxLength": 600},
            "statement": {"type": "string", "minLength": 10, "maxLength": 360},
            "current_use": {"type": "string", "minLength": 10, "maxLength": 240},
        },
    }


def _expand_evidence_scope(
    value: dict[str, object], candidate: RevisionCandidate
) -> dict[str, object]:
    if set(value) != {"scope"}:
        raise WoonError("Codex evidence scope response has unexpected fields")
    scope = _text(value.get("scope"), "Codex evidence scope")
    if "\n" in scope or any(token in scope for token in ("`", "[", "]", ">")):
        raise WoonError("Codex evidence scope must be plain single-line text")
    return {
        "body": _insert_evidence_scope(candidate.body, scope),
        "statement": f"{candidate.title}의 개념 설명과 확인이 필요한 범위를 구분한다.",
        "current_use": candidate.purpose,
    }


def _insert_evidence_scope(body: str, scope: str) -> str:
    note = f"> 확인 범위: {scope.strip()}\n\n"
    marker = "<!-- breadcrumb:end -->"
    if marker not in body:
        return note + body
    before, _, after = body.partition(marker)
    return before + marker + "\n\n" + note + after.lstrip("\n")


def _mask_protected_material(body: str) -> tuple[str, dict[str, str]]:
    """Replace immutable Markdown spans with ordered tokens for a prose-only edit."""

    spans: list[tuple[int, int]] = []
    for pattern in (_FENCED_BLOCK, _MARKDOWN_LINK, _WIKILINK, _INLINE_CODE):
        spans.extend((match.start(), match.end()) for match in pattern.finditer(body))
    selected: list[tuple[int, int]] = []
    end = 0
    for start, finish in sorted(spans, key=lambda item: (item[0], -(item[1] - item[0]))):
        if start < end:
            continue
        selected.append((start, finish))
        end = finish

    parts: list[str] = []
    replacements: dict[str, str] = {}
    cursor = 0
    for index, (start, finish) in enumerate(selected, start=1):
        marker = f"@@WOON_KEEP_{index:03d}@@"
        parts.extend((body[cursor:start], marker))
        replacements[marker] = body[start:finish]
        cursor = finish
    parts.append(body[cursor:])
    return "".join(parts), replacements


def _restore_protected_material(template: str, replacements: dict[str, str]) -> str:
    """Require every immutable span exactly once and restore its original bytes."""

    markers = _KEEP_MARKER.findall(template)
    if markers != list(replacements):
        raise WoonError("Codex revision changed protected material order or count")
    restored = template
    for marker, value in replacements.items():
        restored = restored.replace(marker, value)
    return restored


def _plain_learning_paragraph(value: object, label: str) -> str:
    paragraph = _text(value, label).strip()
    if "\n" in paragraph or any(token in paragraph for token in ("`", "[", "]", ">")):
        raise WoonError(f"{label} must be a plain single-line paragraph")
    return paragraph


def _add_learning_scaffold(body: str, opening: str, revisit: str) -> str:
    marker = "<!-- breadcrumb:end -->"
    opening_paragraph = opening + "\n\n"
    if marker in body:
        before, _, after = body.partition(marker)
        body = before + marker + "\n\n" + opening_paragraph + after.lstrip("\n")
    else:
        body = opening_paragraph + body
    return body.rstrip() + "\n\n다시 설명해야 할 때는 " + revisit + "\n"


def _proposal_record(
    candidate: RevisionCandidate, proposal: dict[str, object]
) -> dict[str, object]:
    return {
        "version": REVISION_VERSION,
        "page_id": candidate.page_id,
        "output_sha256": candidate.output_sha256,
        "source_body_sha256": candidate.source_body_sha256,
        "body": proposal["body"],
        "statement": proposal["statement"],
        "current_use": proposal["current_use"],
    }


def _validate_proposal(value: dict[str, object], candidate: RevisionCandidate) -> None:
    expected_keys = {
        "version",
        "page_id",
        "output_sha256",
        "source_body_sha256",
        "body",
        "statement",
        "current_use",
    }
    raw = value
    if set(raw) == {"body", "statement", "current_use"}:
        body = _text(raw.get("body"), "Codex revision body")
        statement = _text(raw.get("statement"), "Codex revision statement")
        current_use = _text(raw.get("current_use"), "Codex revision current_use")
    else:
        if set(raw) != expected_keys:
            raise WoonError("Codex revision proposal has unexpected fields")
        if raw.get("version") != REVISION_VERSION:
            raise WoonError("Codex revision proposal has an unsupported version")
        if raw.get("page_id") != candidate.page_id:
            raise WoonError("Codex revision proposal has a different page_id")
        if raw.get("output_sha256") != candidate.output_sha256:
            raise WoonError("Codex revision proposal has a different output receipt")
        if raw.get("source_body_sha256") != candidate.source_body_sha256:
            raise WoonError("Codex revision proposal has a different source body")
        body = _text(raw.get("body"), "Codex revision body")
        statement = _text(raw.get("statement"), "Codex revision statement")
        current_use = _text(raw.get("current_use"), "Codex revision current_use")
    if _H1.match(body) or body.startswith("---\n"):
        raise WoonError("Codex revision body must not contain frontmatter or an H1")
    if _sha256_text(body) == candidate.source_body_sha256:
        raise WoonError("Codex revision body did not change")
    if len(body) < max(80, len(candidate.body) * 35 // 100):
        raise WoonError("Codex revision body removed too much source material")
    if len(body) > max(1600, len(candidate.body) * 5 // 2):
        raise WoonError("Codex revision body grew beyond its source boundary")
    if _fenced_blocks(body) != _fenced_blocks(candidate.body):
        raise WoonError("Codex revision changed a protected fenced block")
    if not _preserves(_WIKILINK, candidate.body, body):
        raise WoonError("Codex revision removed a Wiki link")
    if not _preserves(_MARKDOWN_LINK, candidate.body, body):
        raise WoonError("Codex revision removed a Markdown link")
    if not _preserves(_INLINE_CODE, candidate.body, body):
        raise WoonError("Codex revision removed an inline code identifier")
    if "\n" in statement or not statement.strip():
        raise WoonError("Codex revision statement must be one non-empty line")
    if "\n" in current_use or not current_use.strip():
        raise WoonError("Codex revision current_use must be one non-empty line")


def _preserves(pattern: re.Pattern[str], original: str, revision: str) -> bool:
    before = Counter(pattern.findall(original))
    after = Counter(pattern.findall(revision))
    return all(after[value] >= count for value, count in before.items())


def _fenced_blocks(value: str) -> Counter[str]:
    return Counter(match.group(0) for match in _FENCED_BLOCK.finditer(value))


def _proposal_file_name(page_id: str) -> str:
    digest = hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:12]
    return f"revision-{digest}.proposal.json"


def _selected_candidates(
    candidates: tuple[RevisionCandidate, ...], page_ids: tuple[str, ...]
) -> tuple[RevisionCandidate, ...]:
    if not page_ids:
        return candidates
    requested = set(page_ids)
    known = {candidate.page_id for candidate in candidates}
    unknown = requested.difference(known)
    if unknown:
        raise WoonError(f"Codex revision page is not eligible: {sorted(unknown)[0]}")
    return tuple(candidate for candidate in candidates if candidate.page_id in requested)


def _write_failure(destination: Path, page_id: str, error: str) -> None:
    digest = hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:12]
    atomic_write(
        destination / f"revision-{digest}.failure.json",
        encode_json({"version": REVISION_VERSION, "page_id": page_id, "error": error}),
    )


def _model(value: str | None) -> str:
    return "subscription-default" if value is None else _text(value, "Codex model")


def _validate_timeout(value: int) -> None:
    if not 30 <= value <= 3600:
        raise WoonError("Codex revision timeout must be between 30 and 3600 seconds")


def _validate_attempts(value: int) -> None:
    if not 1 <= value <= 3:
        raise WoonError("Codex revision max_attempts must be between 1 and 3")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.expanduser().read_bytes()).hexdigest()
    except OSError as error:
        raise WoonError(f"could not read quality review plan: {path}") from error


def _digest_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be an object")
    return value
