from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
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
