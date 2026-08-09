from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    CorpusRoot,
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    MarkdownKnowledgeCorpus,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.config import KnowledgeSettings
from woon_core.knowledge.domain import DocumentMetadata
from woon_core.knowledge.service import KnowledgeService


def make_service(tmp_path: Path) -> KnowledgeService:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    return KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
    )


def metadata(canonical_id: str = "backend/ports-and-adapters") -> DocumentMetadata:
    return DocumentMetadata(
        canonical_id=canonical_id,
        title="포트와 어댑터",
        domain="backend",
        summary="도메인 로직과 외부 기술의 의존 방향을 분리하는 구조.",
        prerequisites=("backend/dependency-inversion",),
    )


def test_archive_is_optimistic_and_reindexes(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = service.archive(
        metadata(), "## 왜 필요한가\n\n외부 기술 교체 비용을 경계 밖으로 밀어낸다."
    )

    assert first.created is True
    assert first.document.relative_path == "wiki/canonical/backend/ports-and-adapters.md"
    assert service.search("외부 기술", 5)[0].canonical_id == "backend/ports-and-adapters"

    with pytest.raises(WoonError, match="read it first"):
        service.archive(metadata(), "## 변경\n\n동시성 검증 없는 덮어쓰기는 거부한다.")

    updated = service.archive(
        metadata(),
        "## 왜 필요한가\n\n의존 방향과 변경 비용을 함께 통제한다.",
        first.document.revision,
    )
    assert updated.created is False
    assert updated.document.revision != first.document.revision


def test_archive_rejects_duplicate_normalized_title(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.archive(metadata(), "## 설명\n\n첫 문서.")
    duplicate = DocumentMetadata(
        canonical_id="architecture/duplicate",
        title="포트 와 어댑터",
        domain="architecture",
        summary="중복 제목.",
    )

    with pytest.raises(WoonError, match="same normalized title"):
        service.archive(duplicate, "## 설명\n\n두 번째 문서.")


def test_audit_reports_unresolved_learning_relation(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.archive(metadata(), "## 설명\n\n선수 개념이 필요하다.")

    assert service.audit() == [
        "backend/ports-and-adapters: unresolved relation 'backend/dependency-inversion'"
    ]


def test_settings_reject_path_escape(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "canonical-knowledge.yaml").write_text(
        """
version: 1
runtime_root: .local/knowledge
canonical:
  root: ../outside
search:
  adapter: sqlite-fts
  database: .local/knowledge/search.sqlite3
style:
  document_guide: ai-reference/document.md
  diagram_guide: ai-reference/diagram.md
""".lstrip()
    )

    with pytest.raises(WoonError, match="escapes the vault"):
        KnowledgeSettings.load(tmp_path)


def test_git_history_can_restore_previous_document(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.email", "test@example.com"], check=True)
    service = make_service(tmp_path)
    first = service.archive(metadata(), "## 설명\n\n첫 번째 내용.")
    subprocess.run(["git", "-C", tmp_path, "add", "wiki"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "docs: 첫 버전"], check=True)
    first_commit = subprocess.run(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second = service.archive(metadata(), "## 설명\n\n두 번째 내용.", first.document.revision)

    restored = service.restore(
        metadata().canonical_id,
        first_commit,
        second.document.revision,
        confirmed=True,
    )

    assert "첫 번째 내용" in restored.document.body


def test_read_only_corpus_is_searchable_without_becoming_canonical(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    legacy = tmp_path / "wiki/os/virtual-memory.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        """
---
title: 가상 메모리
summary: 페이지를 필요한 시점에 적재하는 주소 공간 관리 방식.
---

# 가상 메모리

## 페이지 폴트 처리

페이지 폴트가 발생하면 보조 페이지 테이블에서 적재 정보를 찾는다.
""".lstrip(),
        encoding="utf-8",
    )
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        MarkdownKnowledgeCorpus(
            tmp_path,
            (CorpusRoot(tmp_path / "wiki", "wiki"),),
            ("wiki/canonical/**",),
        ),
    )

    assert service.reindex() == 1
    result = service.search("보조 페이지 테이블", 5)[0]
    excerpt = service.read_excerpt(result.document_id, result.chunk_id)

    assert result.canonical_id is None
    assert result.document_id == "wiki/os/virtual-memory.md"
    assert result.source_type == "wiki"
    assert result.heading == "페이지 폴트 처리"
    assert "보조 페이지 테이블" in excerpt.text
    with pytest.raises(WoonError, match="canonical document not found"):
        service.get("os/virtual-memory")


def test_search_chunks_keep_large_sections_bounded(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    source = tmp_path / "wiki/os/large.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# 큰 문서\n\n## 세부 흐름\n\n" + "페이지 교체 흐름을 설명한다. " * 200,
        encoding="utf-8",
    )
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3", max_chunk_chars=1000),
        GitKnowledgeHistory(tmp_path),
        MarkdownKnowledgeCorpus(
            tmp_path,
            (CorpusRoot(tmp_path / "wiki", "wiki"),),
            (),
        ),
    )

    service.reindex()
    result = service.search("페이지 교체", 1)[0]
    excerpt = service.read_excerpt(result.document_id, result.chunk_id)

    assert len(excerpt.text) <= 1000


def test_read_only_corpus_tolerates_legacy_invalid_frontmatter(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    source = tmp_path / "wiki/legacy.md"
    source.write_text(
        "---\nsummary: `잘못된 YAML`\n---\n\n# 레거시 문서\n\n본문은 색인한다.\n",
        encoding="utf-8",
    )
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        MarkdownKnowledgeCorpus(
            tmp_path,
            (CorpusRoot(tmp_path / "wiki", "wiki"),),
            (),
        ),
    )

    assert service.reindex() == 1
    assert service.search("본문", 1)[0].title == "레거시 문서"


def test_search_ignores_generic_intent_words_and_generated_breadcrumbs(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    source = tmp_path / "maps/virtual-memory.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        """
# 가상 메모리와 Demand Paging

<!-- breadcrumb:start -->
상위 링크: [WIKI](../)
<!-- breadcrumb:end -->

## 학습 흐름

Demand Paging은 페이지 폴트가 발생한 시점에 필요한 페이지를 적재한다.
""".lstrip(),
        encoding="utf-8",
    )
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        MarkdownKnowledgeCorpus(
            tmp_path,
            (CorpusRoot(tmp_path / "maps", "map"),),
            (),
        ),
    )

    service.reindex()
    result = service.search("가상 메모리 Demand Paging 관계", 1)[0]
    excerpt = service.read_excerpt(result.document_id, result.chunk_id)

    assert result.document_id == "maps/virtual-memory.md"
    assert result.heading == "학습 흐름"
    assert "상위 링크" not in excerpt.text
