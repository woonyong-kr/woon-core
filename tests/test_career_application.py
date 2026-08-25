from pathlib import Path

import pytest
from pypdf import PdfWriter

import woon_core.career.service as career_service_module
from woon_core.career.service import CareerApplicationService
from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.domain import DocumentMetadata
from woon_core.knowledge.service import KnowledgeService


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    evidence = vault / "wiki/projects/kubernetes-recovery.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("---\ntitle: Kubernetes 장애 복구 서비스\n---\n# 근거\n", encoding="utf-8")
    return vault


def _pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def test_application_uses_one_wiki_record_and_private_sources(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("# 요구사항\n- Kubernetes 운영 자동화 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)

    created = service.create(
        application_id="krafton-ai-engineer-2026",
        company="KRAFTON",
        role="AI Engineer",
        jd_path=jd,
    )

    assert created.state == "discovered"
    page = vault / created.relative_path
    assert page.is_file()
    assert "단계: 검토 시작" in page.read_text(encoding="utf-8")
    assert (
        vault
        / "wiki/private/_sources/knowledge/private/career/applications"
        / "krafton-ai-engineer-2026/jd.md"
    ).read_bytes() == jd.read_bytes()
    assert not list((vault / ".local").rglob("*.json"))


def test_application_requires_reviewed_evidence_before_drafting(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Kubernetes 운영 자동화 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)
    service.create(
        application_id="krafton-ai-engineer-2026",
        company="KRAFTON",
        role="AI Engineer",
        jd_path=jd,
    )

    with pytest.raises(WoonError, match="evaluated state"):
        service.approve_draft("krafton-ai-engineer-2026", confirmed=True)

    service.evaluate(
        "krafton-ai-engineer-2026",
        [
            {
                "requirement": "Kubernetes 운영 자동화 경험",
                "classification": "verified",
                "ownership": "personal",
                "rationale": "프로젝트 정본에서 개인 구현 범위를 확인했다.",
                "evidence_paths": ["wiki/projects/kubernetes-recovery.md"],
            }
        ],
    )
    approved = service.approve_draft("krafton-ai-engineer-2026", confirmed=True)

    assert approved.state == "approved_for_draft"
    assert "[[wiki/projects/kubernetes-recovery]]" in (vault / approved.relative_path).read_text(
        encoding="utf-8"
    )


def test_pdf_and_submission_record_advance_atomically(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    pdf = _pdf(tmp_path / "resume.pdf")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(
        identifier,
        [
            {
                "requirement": "Python 개발 경험",
                "classification": "gap",
                "ownership": "unknown",
                "rationale": "제출 전 직접 근거를 더 확인한다.",
                "evidence_paths": [],
            }
        ],
    )
    service.approve_draft(identifier, confirmed=True)
    assert service.attach_pdf(identifier, pdf, kind="draft").state == "drafted"
    service.mark_reviewed(identifier, confirmed=True)
    service.mark_ready(identifier, confirmed=True)

    with pytest.raises(WoonError, match="explicit confirmation"):
        service.attach_pdf(identifier, pdf, kind="submitted")

    submitted = service.attach_pdf(identifier, pdf, kind="submitted", confirmed=True)
    record = service.show(identifier)

    assert submitted.state == "submitted"
    assert {item["kind"] for item in record["artifacts"]} == {"draft", "submitted"}
    assert (
        len(
            list(
                (
                    vault
                    / "wiki/private/_sources/knowledge/private/career/applications"
                    / identifier
                ).glob("submitted-*.pdf")
            )
        )
        == 1
    )

    with pytest.raises(WoonError, match="explicit confirmation"):
        service.outcome(identifier, "interview", confirmed=False)
    assert service.outcome(identifier, "interview", confirmed=True).state == "interview"


def test_revised_drafts_remain_linked_to_the_same_application(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    first = _pdf(tmp_path / "first.pdf")
    second = _pdf(tmp_path / "second.pdf")
    second.write_bytes(second.read_bytes() + b"\n% revised\n")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(
        identifier,
        [
            {
                "requirement": "Python 개발 경험",
                "classification": "gap",
                "ownership": "unknown",
                "rationale": "근거 확인 전",
                "evidence_paths": [],
            }
        ],
    )
    service.approve_draft(identifier, confirmed=True)
    service.attach_pdf(identifier, first, kind="draft")
    service.mark_reviewed(identifier, confirmed=True)
    service.attach_pdf(identifier, second, kind="draft")

    drafts = [item for item in service.show(identifier)["artifacts"] if item["kind"] == "draft"]
    assert len(drafts) == 2
    assert all((vault / item["source"]).is_file() for item in drafts)


def test_pdf_failure_rolls_back_source_and_application_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    pdf = _pdf(tmp_path / "resume.pdf")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(
        identifier,
        [
            {
                "requirement": "Python 개발 경험",
                "classification": "gap",
                "ownership": "unknown",
                "rationale": "근거 확인 전",
                "evidence_paths": [],
            }
        ],
    )
    service.approve_draft(identifier, confirmed=True)
    before = (vault / "wiki/personal/career/applications" / f"{identifier}.md").read_bytes()
    original_atomic_write = career_service_module.atomic_write
    failed = False

    def fail_on_record(path: Path, data: bytes, *, mode: int) -> None:
        nonlocal failed
        if path.name == f"{identifier}.md" and not failed:
            failed = True
            raise OSError("injected record failure")
        original_atomic_write(path, data, mode=mode)

    monkeypatch.setattr(career_service_module, "atomic_write", fail_on_record)
    with pytest.raises(OSError, match="injected record failure"):
        service.attach_pdf(identifier, pdf, kind="draft")

    assert (vault / "wiki/personal/career/applications" / f"{identifier}.md").read_bytes() == before
    assert not list(
        (vault / "wiki/private/_sources/knowledge/private/career/applications" / identifier).glob(
            "draft-*.pdf"
        )
    )


def test_verified_requirement_rejects_missing_or_external_evidence(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)
    service.create(
        application_id="krafton-ai-engineer-2026",
        company="KRAFTON",
        role="AI Engineer",
        jd_path=jd,
    )

    with pytest.raises(WoonError, match="existing wiki Markdown"):
        service.evaluate(
            "krafton-ai-engineer-2026",
            [
                {
                    "requirement": "Python 개발 경험",
                    "classification": "verified",
                    "ownership": "personal",
                    "rationale": "외부 파일은 근거 정본이 아니다.",
                    "evidence_paths": ["wiki/private/_sources/knowledge/private/resume.pdf"],
                }
            ],
        )


def test_unsubmitted_record_can_be_reopened_with_history(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(
        identifier,
        [
            {
                "requirement": "Python 개발 경험",
                "classification": "gap",
                "ownership": "unknown",
                "rationale": "근거 확인 전",
                "evidence_paths": [],
            }
        ],
    )
    service.approve_draft(identifier, confirmed=True)
    service.attach_pdf(identifier, _pdf(tmp_path / "resume.pdf"), kind="draft")
    service.mark_reviewed(identifier, confirmed=True)

    reopened = service.reopen(
        identifier,
        state="drafted",
        reason="사용자 최종 검토 전으로 바로잡음",
        confirmed=True,
    )

    assert reopened.state == "drafted"
    assert service.show(identifier)["history"][-1]["reason"] == "사용자 최종 검토 전으로 바로잡음"


def test_jd_analysis_uses_search_as_unverified_candidates(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    knowledge = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        SQLiteFtsSearchIndex(vault / ".local/search.sqlite3"),
        GitKnowledgeHistory(vault),
    )
    knowledge.archive(
        DocumentMetadata(
            canonical_id="projects/kubernetes-automation",
            title="Kubernetes 운영 자동화",
            domain="projects",
            summary="Kubernetes 장애 근거를 좁히고 안전한 복구를 제안한다.",
            purpose="프로젝트의 개인 구현 범위와 검증 한계를 보존한다.",
        ),
        "## 현재 이해\n\nKubernetes 운영 자동화는 직접 변경 대신 Draft PR을 사용한다.",
    )
    jd = tmp_path / "jd.md"
    jd.write_text("- Kubernetes 운영 자동화 경험이 필요합니다.\n", encoding="utf-8")
    service = CareerApplicationService(vault, knowledge)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)

    result = service.analyze(identifier)
    requirement = service.show(identifier)["requirements"][0]

    assert result.state == "evaluated"
    assert requirement["classification"] == "adjacent"
    assert requirement["ownership"] == "unknown"
    assert requirement["reviewed"] is False
    assert requirement["evidence_paths"] == ["wiki/projects/kubernetes-automation.md"]


def test_verified_requirement_rejects_unknown_ownership(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Kubernetes 운영 자동화 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)

    with pytest.raises(WoonError, match="known ownership"):
        service.evaluate(
            identifier,
            [
                {
                    "requirement": "Kubernetes 운영 자동화 경험",
                    "classification": "verified",
                    "ownership": "unknown",
                    "rationale": "범위 미확인",
                    "evidence_paths": ["wiki/projects/kubernetes-recovery.md"],
                }
            ],
        )


def test_re_evaluation_preserves_a_drafted_lifecycle_state(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    matrix = [
        {
            "requirement": "Python 개발 경험",
            "classification": "gap",
            "ownership": "unknown",
            "rationale": "근거 확인 전",
            "evidence_paths": [],
        }
    ]
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(identifier, matrix)
    service.approve_draft(identifier, confirmed=True)
    service.attach_pdf(identifier, _pdf(tmp_path / "resume.pdf"), kind="draft")
    updated_matrix = [
        {
            **matrix[0],
            "rationale": "제출 전 원문 근거를 다시 확인한다.",
        }
    ]

    result = service.evaluate(identifier, updated_matrix)

    assert result.state == "drafted"
    assert service.show(identifier)["history"][-1]["event"] == "JD와 경력 근거 재검토"
    assert service.evaluate(identifier, updated_matrix).changed is False


def test_submitted_record_keeps_confirmation_needed_when_a_gap_remains(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    jd = tmp_path / "jd.md"
    jd.write_text("- Python 개발 경험\n", encoding="utf-8")
    pdf = _pdf(tmp_path / "resume.pdf")
    service = CareerApplicationService(vault)
    identifier = "krafton-ai-engineer-2026"
    service.create(application_id=identifier, company="KRAFTON", role="AI Engineer", jd_path=jd)
    service.evaluate(
        identifier,
        [
            {
                "requirement": "실제 JD 원문",
                "classification": "gap",
                "ownership": "unknown",
                "rationale": "원문 미확인",
                "evidence_paths": [],
            }
        ],
    )
    service.approve_draft(identifier, confirmed=True)
    service.attach_pdf(identifier, pdf, kind="draft")
    service.mark_reviewed(identifier, confirmed=True)
    service.mark_ready(identifier, confirmed=True)
    service.attach_pdf(identifier, pdf, kind="submitted", confirmed=True)

    assert service.show(identifier)["knowledge_state"] == "확인 필요"
