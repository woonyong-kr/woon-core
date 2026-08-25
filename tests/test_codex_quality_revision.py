from __future__ import annotations

import hashlib

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import codex_quality_revision as revision
from woon_core.knowledge.codex_quality_revision import (
    RevisionCandidate,
    _add_learning_scaffold,
    _collect_proposal_records,
    _compiled_body,
    _expand_evidence_scope,
    _mask_protected_material,
    _proposal_file_name,
    _propose_revision,
    _restore_protected_material,
    _validate_proposal,
    apply_codex_quality_revisions,
    create_codex_quality_revision_proposals,
)

BODY = """## 시작

`reader`가 보는 값이 바뀌는 이유를 먼저 확인한다.
[[관련 문서]]와 [설명](https://example.com)를 함께 본다.

```python
value = 1
```

값을 한 곳에서 바꾸면 다른 곳에도 영향을 줄 수 있다.
"""


def _candidate() -> RevisionCandidate:
    return RevisionCandidate(
        page_id="os/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
        title="예시",
        purpose="값이 바뀌는 흐름을 다시 설명할 때 사용한다.",
        body=BODY,
        failures=("natural_korean",),
        failure_reasons=("문장 연결을 보완해야 한다.",),
    )


def test_revision_accepts_changed_prose_that_preserves_protected_material() -> None:
    proposal = {
        "body": """## 시작

`reader`가 값을 읽을 때는 한 곳의 변경이 어디까지 이어지는지 먼저 살펴봐야 한다.
[[관련 문서]]와 [설명](https://example.com)를 함께 보면 흐름을 따라가기 쉽다.

```python
value = 1
```

그래서 값을 한 곳에서 바꾼 뒤에는 다른 곳에 예상하지 못한 영향이 남는지도 확인한다.
""",
        "statement": "한 곳의 값 변경이 다른 곳에 미치는 영향을 설명한다.",
        "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
    }

    _validate_proposal(proposal, _candidate())


def test_revision_runtime_artifacts_are_private(tmp_path, monkeypatch) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "proposals"
    output.mkdir(mode=0o755)
    proposal = {
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }
    monkeypatch.setattr(
        revision,
        "_revision_candidates",
        lambda *_: ((_candidate(),), "a" * 64),
    )
    monkeypatch.setattr(revision, "_codex_binary", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(revision, "_require_chatgpt_login", lambda _: None)
    monkeypatch.setattr(revision, "_propose_revision", lambda *_: proposal)

    create_codex_quality_revision_proposals(
        tmp_path,
        plan,
        tmp_path / "reviews",
        output,
        model="gpt-5.6-sol",
        max_attempts=2,
    )

    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "run-manifest.json").stat().st_mode & 0o777 == 0o600
    assert (output / _proposal_file_name("os/example")).stat().st_mode & 0o777 == 0o600


def test_apply_can_select_an_adjudicated_subset(tmp_path, monkeypatch) -> None:
    first = _candidate()
    second = RevisionCandidate(
        page_id="os/second",
        output_sha256="b" * 64,
        source_body_sha256=first.source_body_sha256,
        title="두 번째",
        purpose=first.purpose,
        body=first.body,
        failures=first.failures,
        failure_reasons=first.failure_reasons,
    )
    monkeypatch.setattr(
        revision,
        "_revision_candidates",
        lambda *_: ((first, second), "c" * 64),
    )
    monkeypatch.setattr(
        revision,
        "_collect_proposal_records",
        lambda candidates, *_args, **_kwargs: (
            {
                candidates[0].page_id: {
                    "body": BODY,
                    "statement": "값 변경의 영향을 설명한다.",
                    "current_use": first.purpose,
                }
            },
            {candidates[0].page_id: "proposal.json"},
        ),
    )

    class FakeReport:
        curated = 1
        compiled = 1
        unchanged = 0
        page_ids = ("os/example",)

    class FakeService:
        def curate_compiled_wiki_revisions(self, records):
            assert [record.page_id for record in records] == ["os/example"]
            return FakeReport()

    monkeypatch.setattr(revision, "build_knowledge_service", lambda _: (None, FakeService()))

    report = apply_codex_quality_revisions(
        tmp_path,
        tmp_path / "plan.json",
        tmp_path / "reviews",
        (tmp_path / "proposals",),
        page_ids=("os/example",),
    )

    assert report["page_ids"] == ["os/example"]


def test_revision_rejects_changed_code_fence() -> None:
    proposal = {
        "body": BODY.replace("value = 1", "value = 2"),
        "statement": "한 곳의 값 변경이 다른 곳에 미치는 영향을 설명한다.",
        "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
    }

    with pytest.raises(WoonError, match="protected fenced block"):
        _validate_proposal(proposal, _candidate())


def test_compiled_body_excludes_compiler_frontmatter_and_h1() -> None:
    markdown = "---\ntitle: 예시\n---\n\n# 예시\n\n" + BODY

    assert _compiled_body(markdown) == BODY


def test_evidence_scope_adds_a_plain_boundary_without_rewriting_source() -> None:
    candidate = _candidate()
    evidence_only = RevisionCandidate(
        page_id=candidate.page_id,
        output_sha256=candidate.output_sha256,
        source_body_sha256=candidate.source_body_sha256,
        title=candidate.title,
        purpose=candidate.purpose,
        body=candidate.body,
        failures=("evidence_boundary",),
        failure_reasons=("사실과 적용 범위의 경계를 보완해야 한다.",),
    )

    proposal = _expand_evidence_scope(
        {
            "scope": (
                "이 문서는 기본 원리를 설명하며, 실제 동작은 해당 코드와 실행 기록에서 다시 "
                "확인해야 한다."
            )
        },
        evidence_only,
    )

    _validate_proposal(proposal, evidence_only)
    assert proposal["body"].startswith("> 확인 범위: 이 문서는 기본 원리를 설명하며")


def test_collect_proposals_rejects_duplicate_page_across_retry_runs(tmp_path, monkeypatch) -> None:
    candidate = _candidate()
    record = {
        "version": 1,
        "page_id": candidate.page_id,
        "output_sha256": candidate.output_sha256,
        "source_body_sha256": candidate.source_body_sha256,
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }
    monkeypatch.setattr(revision, "_validate_manifest", lambda *_: None)
    paths = []
    for name in ("first", "retry"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "run-manifest.json").write_text("{}", encoding="utf-8")
        (directory / _proposal_file_name(candidate.page_id)).write_bytes(
            revision.encode_json(record)
        )
        paths.append(directory)

    # The loader must reject duplicate page IDs before it can promote either run.
    with pytest.raises(WoonError, match="proposal is duplicated"):
        _collect_proposal_records((candidate,), tuple(paths), tmp_path / "plan.json", "reviews")


def test_collect_proposals_keeps_first_valid_duplicate_when_requested(
    tmp_path, monkeypatch
) -> None:
    candidate = _candidate()
    first = {
        "version": 1,
        "page_id": candidate.page_id,
        "output_sha256": candidate.output_sha256,
        "source_body_sha256": candidate.source_body_sha256,
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }
    second = {**first, "body": BODY.replace("값을 한 곳에서", "값을 한 위치에서")}
    monkeypatch.setattr(revision, "_validate_manifest", lambda *_: None)
    paths = []
    for name, record in (("first", first), ("retry", second)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "run-manifest.json").write_text("{}", encoding="utf-8")
        (directory / _proposal_file_name(candidate.page_id)).write_bytes(
            revision.encode_json(record)
        )
        paths.append(directory)

    records, sources = _collect_proposal_records(
        (candidate,), tuple(paths), tmp_path / "plan.json", "reviews", "first-valid"
    )

    assert records[candidate.page_id]["body"] == first["body"]
    assert sources[candidate.page_id].endswith("first/" + _proposal_file_name(candidate.page_id))


def test_protected_template_restores_original_tokens_in_order() -> None:
    template, replacements = _mask_protected_material(BODY)

    assert "@@WOON_KEEP_001@@" in template
    assert _restore_protected_material(template, replacements) == BODY
    with pytest.raises(WoonError, match="protected material order or count"):
        _restore_protected_material(template.replace("@@WOON_KEEP_001@@", ""), replacements)


def test_learning_scaffold_preserves_entire_original_body() -> None:
    revised = _add_learning_scaffold(
        BODY,
        "이 글은 값이 바뀌는 흐름을 따라가며 어디까지 영향이 이어지는지 살펴본다.",
        "값을 바꾼 뒤에는 다른 곳에 남는 영향도 함께 확인하면 된다.",
    )

    assert BODY.rstrip() in revised
    _validate_proposal(
        {
            "body": revised,
            "statement": "값 변경이 미치는 영향을 설명한다.",
            "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
        },
        _candidate(),
    )


def test_revision_reaches_learning_scaffold_after_protected_material_failures(monkeypatch) -> None:
    candidate = _candidate()
    responses = iter(
        (
            {
                "body": BODY.replace("`reader`", "reader"),
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
            {
                "body": "보호 표식을 잃은 수정본입니다. " * 5,
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
            {
                "opening": (
                    "이 글은 값이 바뀌는 흐름을 따라가며 어디까지 영향이 이어지는지 살펴본다."
                ),
                "revisit": "값을 바꾼 뒤에는 다른 곳에 남는 영향도 함께 확인하면 된다.",
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
        )
    )
    monkeypatch.setattr(revision, "_run_codex", lambda *_: next(responses))

    proposal = _propose_revision(candidate, "codex", "subscription-default", 60, 1)

    _validate_proposal(proposal, candidate)
    assert BODY.rstrip() in proposal["body"]
