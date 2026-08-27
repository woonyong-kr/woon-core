from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.context_bundle import build_context_bundle, build_wiki_context_bundle
from woon_core.knowledge.domain import (
    DocumentMetadata,
    KnowledgeExcerpt,
    SearchResult,
)
from woon_core.knowledge.service import KnowledgeService


class FakeKnowledgeService:
    def search(self, query: str, limit: int) -> list[SearchResult]:
        return [
            SearchResult(
                document_id="wiki/project",
                canonical_id="project",
                title="프로젝트",
                summary="근거",
                relative_path="wiki/project.md",
                revision="r1",
                source_type="canonical",
                chunk_id="overview",
                heading="개요",
                score=1.0,
                snippet=query,
            )
        ]

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt:
        return KnowledgeExcerpt(
            document_id=document_id,
            relative_path="wiki/project.md",
            revision="r1",
            source_type="canonical",
            chunk_id=chunk_id,
            heading="개요",
            text="검증 가능한 프로젝트 근거",
        )


def test_context_bundle_deduplicates_the_same_revision_chunk() -> None:
    bundle = build_context_bundle(
        FakeKnowledgeService(),  # type: ignore[arg-type]
        ("Kubernetes", "Python"),
        max_items=5,
        max_chars=1_000,
    )

    assert len(bundle.items) == 1
    assert bundle.items[0].relative_path == "wiki/project.md"
    assert bundle.total_chars == len("검증 가능한 프로젝트 근거")


def test_context_bundle_rejects_unbounded_limits() -> None:
    with pytest.raises(WoonError, match="max_items"):
        build_context_bundle(FakeKnowledgeService(), ("query",), max_items=51)  # type: ignore[arg-type]


def test_context_bundle_reads_the_real_local_search_index(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki"
    canonical_root.mkdir()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
    )
    service.archive(
        DocumentMetadata(
            canonical_id="projects/kubernetes-recovery",
            title="Kubernetes 장애 복구 서비스",
            domain="projects",
            summary="장애 원인을 근거로 좁히고 승인 가능한 복구 제안을 만든다.",
            purpose="Kubernetes 장애 대응 프로젝트의 검증된 경계를 재사용한다.",
        ),
        "## 현재 이해\n\n직접 변경하지 않고 근거와 Draft PR로 복구를 제안한다.",
    )

    bundle = build_context_bundle(service, ("Kubernetes 장애 복구",), max_items=3)

    assert len(bundle.items) == 1
    assert bundle.items[0].relative_path == "wiki/projects/kubernetes-recovery.md"
    assert "직접 변경하지 않고" in bundle.items[0].text


def test_wiki_context_follows_tree_and_includes_history_and_evidence(tmp_path: Path) -> None:
    def write(
        relative: str,
        title: str,
        canonical_id: str,
        parent: str | None,
        body: str,
        *,
        node_kind: str,
        entity_kind: str | None = None,
        entity_section: str | None = None,
        sequence: int | None = None,
        extra: str = "",
    ) -> None:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_row = f"parent: '{parent}'\n" if parent else ""
        entity_kind_row = f"entity_kind: {entity_kind}\n" if entity_kind else ""
        entity_section_row = f"entity_section: {entity_section}\n" if entity_section else ""
        lifecycle_row = "lifecycle_status: active\n" if entity_kind == "project" else ""
        sequence_row = f"sequence: {sequence}\n" if sequence is not None else ""
        path.write_text(
            "---\n"
            f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
            f"node_kind: {node_kind}\n{entity_kind_row}{entity_section_row}"
            f"{lifecycle_row}{sequence_row}"
            f"{parent_row}keywords: [{title}]\naliases: []\n"
            "view_mode: tree\nupdated: 2026-08-25\nsummary: 문맥 요약이다.\n"
            f"knowledge_state: 확인 필요\n{extra}---\n\n"
            f"# {title}\n\n{body}\n",
            encoding="utf-8",
        )

    write(
        "wiki/README.md",
        "Wiki",
        "README",
        None,
        "<!-- 직접 하위 키워드 링크만 표시한다. -->",
        node_kind="root",
    )
    write(
        "wiki/projects.md",
        "프로젝트",
        "projects",
        "[[wiki/README|Wiki]]",
        "<!-- 직접 하위 키워드 링크만 표시한다. -->",
        node_kind="hub",
    )
    write(
        "wiki/project.md",
        "복구 프로젝트",
        "project",
        "[[wiki/projects|프로젝트]]",
        "현재 판단이다.\n\n"
        "## 확인된 근거\n\n"
        "- 계약 테스트가 통과했다.\n\n"
        "## 키워드\n\n"
        "- [[wiki/project-history|히스토리]]\n"
        "- [[wiki/project-detail|복구 계약]]",
        node_kind="entity",
        entity_kind="project",
        extra=(
            "navigation_groups:\n"
            "- label: 이해 순서\n"
            "  children:\n"
            "  - project-detail\n"
            "- label: 변경 이력\n"
            "  children:\n"
            "  - project-history\n"
        ),
    )
    write(
        "wiki/project-history.md",
        "복구 프로젝트 히스토리",
        "project-history",
        "[[wiki/project|복구 프로젝트]]",
        "<!-- woon-wiki-timeline:start -->\n"
        "- 2026-08-25 · 변경 — 판단 기준을 수정했다.\n"
        "<!-- woon-wiki-timeline:end -->",
        node_kind="detail",
        entity_section="history",
        sequence=1,
    )
    write(
        "wiki/project-detail.md",
        "복구 계약",
        "project-detail",
        "[[wiki/project|복구 프로젝트]]",
        "상세 계약이다.",
        node_kind="detail",
        sequence=2,
    )

    bundle = build_wiki_context_bundle(tmp_path, "복구 프로젝트")

    roles = [item.role for item in bundle.items]
    assert roles[:3] == ["ancestor", "ancestor", "current"]
    assert roles.count("navigation-group") == 2
    assert roles.count("child") == 2
    assert "information" not in roles
    assert "history" in roles
    assert "evidence" in roles
    assert any("판단 기준을 수정했다" in item.text for item in bundle.items)
    assert any("계약 테스트가 통과했다" in item.text for item in bundle.items)
