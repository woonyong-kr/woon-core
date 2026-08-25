from __future__ import annotations

import subprocess
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
from woon_core.knowledge.domain import CanonicalDocument, DocumentMetadata, IndexedDocument
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.service import KnowledgeService


class FailOnceIndex(SQLiteFtsSearchIndex):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.fail_next = False

    def rebuild(self, documents: Iterable[IndexedDocument]) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected index failure")
        return super().rebuild(documents)


class CountingRepository(MarkdownDocumentRepository):
    def __init__(self, vault: Path, canonical_root: Path) -> None:
        super().__init__(vault, canonical_root)
        self.list_calls = 0

    def list_documents(self) -> Iterable[CanonicalDocument]:
        self.list_calls += 1
        return super().list_documents()


def make_service(tmp_path: Path) -> KnowledgeService:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True, exist_ok=True)
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
        purpose="외부 기술 교체와 설계 판단에 재사용할 구조 원칙을 보존한다.",
        prerequisites=("backend/dependency-inversion",),
    )


def write_knowledge_config(
    vault: Path,
    document_guide: str,
    diagram_guide: str,
    canonical_root: str = "wiki/canonical",
) -> None:
    config = vault / "config"
    config.mkdir(parents=True)
    (config / "canonical-knowledge.yaml").write_text(
        f"""
version: 1
runtime_root: .local/knowledge
canonical:
  root: {canonical_root}
search:
  adapter: sqlite-fts
  database: .local/knowledge/search.sqlite3
  roots: []
style:
  document_guide: {document_guide}
  diagram_guide: {diagram_guide}
""".lstrip(),
        encoding="utf-8",
    )


def test_version_2_rejects_split_canonical_and_compiled_wiki_roots(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "document.md").write_text("# 문서 기준\n", encoding="utf-8")
    (config / "diagram.md").write_text("# 다이어그램 기준\n", encoding="utf-8")
    (config / "canonical-knowledge.yaml").write_text(
        """
version: 2
runtime_root: .local/knowledge
canonical:
  root: wiki/canonical
search:
  adapter: sqlite-fts
  database: .local/knowledge/search.sqlite3
  roots: []
style:
  document_guide: config/document.md
  diagram_guide: config/diagram.md
compiled_wiki:
  output_root: wiki
  sources: catalog/llm-wiki/sources.yaml
  claims: catalog/llm-wiki/claims.yaml
  pages: catalog/llm-wiki/pages.yaml
  curation: catalog/llm-wiki/curation.yaml
  relations: catalog/llm-wiki/relations.yaml
  receipts: catalog/llm-wiki/receipts.yaml
  review_queue: catalog/llm-wiki/review-queue.yaml
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="must be the same Wiki root"):
        KnowledgeSettings.load(tmp_path)


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
        purpose="중복 제목 검증을 위한 후보 문서다.",
    )

    with pytest.raises(WoonError, match="same normalized title"):
        service.archive(duplicate, "## 설명\n\n두 번째 문서.")


def test_concurrent_update_allows_exactly_one_revision(tmp_path: Path) -> None:
    service_a = make_service(tmp_path)
    service_b = make_service(tmp_path)
    first = service_a.archive(metadata(), "## 설명\n\n첫 문서.")

    def update(service: KnowledgeService, suffix: str) -> str:
        result = service.archive(
            metadata(),
            f"## 설명\n\n{suffix} 수정.",
            first.document.revision,
        )
        return result.document.revision

    successes: list[str] = []
    failures: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, service_a, "A"), executor.submit(update, service_b, "B")]
        for future in futures:
            try:
                successes.append(future.result())
            except Exception as error:
                failures.append(error)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "changed after it was read" in str(failures[0])


def test_concurrent_duplicate_title_allows_exactly_one_document(tmp_path: Path) -> None:
    service_a = make_service(tmp_path)
    service_b = make_service(tmp_path)
    first = metadata("backend/first")
    second = DocumentMetadata(
        canonical_id="architecture/second",
        title="포트 와 어댑터",
        domain="architecture",
        summary="같은 개념의 두 번째 후보.",
        purpose="동시 중복 생성 검증을 위한 후보 문서다.",
    )

    def create(service: KnowledgeService, value: DocumentMetadata) -> str:
        return service.archive(value, "## 설명\n\n동시 생성 후보.").document.metadata.canonical_id

    successes: list[str] = []
    failures: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(create, service_a, first),
            executor.submit(create, service_b, second),
        ]
        for future in futures:
            try:
                successes.append(future.result())
            except Exception as error:
                failures.append(error)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "same normalized title" in str(failures[0])


def test_archive_rolls_back_canonical_file_when_reindex_fails(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    index = FailOnceIndex(tmp_path / ".local/search.sqlite3")
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        index,
        GitKnowledgeHistory(tmp_path),
    )
    first = service.archive(metadata(), "## 설명\n\n첫 문서.")
    index.fail_next = True

    with pytest.raises(RuntimeError, match="injected index failure"):
        service.archive(metadata(), "## 설명\n\n저장되면 안 되는 수정.", first.document.revision)

    current = service.get(metadata().canonical_id)
    assert current.revision == first.document.revision
    assert "첫 문서" in current.body
    assert service.search("첫 문서", 1)[0].canonical_id == metadata().canonical_id


def test_archive_rejects_source_identity_owned_by_another_document(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    first = replace(metadata(), source_ids=("repo://vault/wiki/source.md",))
    service.archive(first, "## 설명\n\n첫 문서.")
    duplicate = DocumentMetadata(
        canonical_id="architecture/duplicate-source",
        title="다른 제목",
        domain="architecture",
        summary="같은 원천을 잘못 중복 소유한 문서.",
        purpose="원천 소유권 중복 검증을 위한 후보 문서다.",
        source_ids=("repo://vault/wiki/source.md",),
    )

    with pytest.raises(WoonError, match="already owned"):
        service.archive(duplicate, "## 설명\n\n중복 원천.")


def test_archive_rejects_missing_purpose(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(WoonError, match="purpose must not be empty"):
        service.archive(
            replace(metadata(), purpose="   "),
            "## 설명\n\n보존 이유 없이 저장하면 안 되는 문서.",
        )


def test_search_fails_closed_when_canonical_file_changes_outside_service(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    saved = service.archive(metadata(), "## 설명\n\n첫 문서.")
    path = tmp_path / saved.document.relative_path
    path.write_text(
        path.read_text(encoding="utf-8").replace("첫 문서", "외부 수정"), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="index is stale"):
        service.search("첫 문서", 1)


def test_search_reuses_generation_while_file_state_is_unchanged(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    repository = CountingRepository(tmp_path, canonical_root)
    service = KnowledgeService(
        repository,
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
    )
    service.archive(metadata(), "## 설명\n\n반복 검색 문서.")
    repository.list_calls = 0

    assert service.search("반복 검색", 1)
    assert service.search("반복 검색", 1)

    assert repository.list_calls == 0


def test_invalid_canonical_file_is_not_silently_omitted(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    invalid = tmp_path / "wiki/canonical/backend/invalid.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("# frontmatter 없음\n", encoding="utf-8")

    with pytest.raises(WoonError, match="invalid canonical document"):
        service.reindex()


def test_audit_reports_unresolved_learning_relation(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.archive(metadata(), "## 설명\n\n선수 개념이 필요하다.")

    assert service.audit() == [
        "backend/ports-and-adapters: unresolved relation 'backend/dependency-inversion'"
    ]


def test_settings_reject_path_escape(tmp_path: Path) -> None:
    write_knowledge_config(
        tmp_path,
        "ai-reference/document.md",
        "ai-reference/diagram.md",
        canonical_root="../outside",
    )

    with pytest.raises(WoonError, match="escapes the vault"):
        KnowledgeSettings.load(tmp_path)


def test_settings_resolves_repository_style_guides(tmp_path: Path) -> None:
    vault = tmp_path / "woon-knowledge"
    write_knowledge_config(
        vault,
        "repo://skills/standards/learning-content-quality.md",
        "repo://skills/skills/docs/diagram",
    )
    skills = tmp_path / "woon-skills"
    (skills / "standards").mkdir(parents=True)
    (skills / "standards/learning-content-quality.md").touch()
    (skills / "skills/docs/diagram").mkdir(parents=True)

    settings = KnowledgeSettings.load(
        vault,
        repository_resolver=lambda reference: skills / reference.removeprefix("repo://skills/"),
    )

    assert settings.style_guide == (skills / "standards/learning-content-quality.md").resolve()
    assert settings.diagram_guide == (skills / "skills/docs/diagram").resolve()


def test_settings_rejects_repository_guide_without_resolver(tmp_path: Path) -> None:
    vault = tmp_path / "woon-knowledge"
    write_knowledge_config(
        vault,
        "repo://skills/standards/learning-content-quality.md",
        "repo://skills/skills/docs/diagram",
    )

    with pytest.raises(WoonError, match="requires a repository resolver"):
        KnowledgeSettings.load(vault)


def test_settings_rejects_missing_repository_style_guide(tmp_path: Path) -> None:
    vault = tmp_path / "woon-knowledge"
    write_knowledge_config(
        vault,
        "repo://skills/standards/missing.md",
        "repo://skills/skills/docs/diagram",
    )

    with pytest.raises(WoonError, match="target does not exist"):
        KnowledgeSettings.load(
            vault,
            repository_resolver=lambda reference: (
                tmp_path / reference.removeprefix("repo://skills/")
            ),
        )


def test_build_service_resolves_registered_style_guides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".woon-root").write_text("version: 1\n", encoding="utf-8")
    registry = tmp_path / "woon-core/registry"
    registry.mkdir(parents=True)
    (registry / "repositories.yaml").write_text(
        """
version: 1
repositories:
  skills:
    remote: https://github.com/example/skills.git
    directory: woon-skills
  knowledge:
    remote: https://github.com/example/knowledge.git
    directory: woon-knowledge
""".lstrip(),
        encoding="utf-8",
    )
    skills = tmp_path / "woon-skills"
    (skills / "standards").mkdir(parents=True)
    (skills / "standards/learning-content-quality.md").touch()
    (skills / "skills/docs/diagram").mkdir(parents=True)
    vault = tmp_path / "woon-knowledge"
    write_knowledge_config(
        vault,
        "repo://skills/standards/learning-content-quality.md",
        "repo://skills/skills/docs/diagram",
    )

    settings, _ = build_knowledge_service(vault)

    assert settings.style_guide == (skills / "standards/learning-content-quality.md").resolve()
    assert settings.diagram_guide == (skills / "skills/docs/diagram").resolve()


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


def test_read_only_corpus_excludes_archived_documents(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    source = tmp_path / "maps/retired.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "---\ntitle: 퇴역 지도\nstatus: Archived\n---\n\n# 퇴역 지도\n\n이전 검색 문서.\n",
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

    assert service.reindex() == 0
    assert service.search("이전 검색 문서", 1) == []


def test_read_only_corpus_indexes_a_configured_markdown_file_root(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    activity_log = tmp_path / "brain/log.md"
    activity_log.parent.mkdir(parents=True)
    activity_log.write_text(
        "# 활동 이력\n\n사용자가 확인한 결정과 완료를 날짜별로 다시 찾는다.\n",
        encoding="utf-8",
    )
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        MarkdownKnowledgeCorpus(
            tmp_path,
            (CorpusRoot(activity_log, "activity"),),
            (),
        ),
    )

    assert service.reindex() == 1
    result = service.search("사용자 확인 결정 완료", 1)[0]

    assert result.relative_path == "brain/log.md"
    assert result.source_type == "activity"


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


def test_search_falls_back_to_any_discriminative_term(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki/canonical"
    canonical_root.mkdir(parents=True)
    source = tmp_path / "wiki/backend/transaction.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# 트랜잭션 경계\n\n멱등성 키로 중복 요청을 막는다.\n",
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
    service.reindex()

    result = service.search("트랜잭션 멱등성 원자성", 1)

    assert result[0].relative_path == "wiki/backend/transaction.md"


def test_repository_resolves_stable_canonical_id_after_page_move(tmp_path: Path) -> None:
    canonical_root = tmp_path / "wiki"
    moved = canonical_root / "new-location/topic.md"
    moved.parent.mkdir(parents=True)
    moved.write_text(
        "---\n"
        "type: Wiki\ncanonical_id: concepts/stable-topic\ntitle: 안정된 주제\n"
        "summary: 경로가 바뀌어도 정체성은 유지한다.\n"
        "---\n\n# 안정된 주제\n\n이전한 본문이다.\n",
        encoding="utf-8",
    )
    repository = MarkdownDocumentRepository(tmp_path, canonical_root)

    current = repository.get("concepts/stable-topic")
    assert current is not None
    assert current.relative_path == "wiki/new-location/topic.md"
    saved = repository.save(
        DocumentMetadata(
            canonical_id="concepts/stable-topic",
            title="안정된 주제",
            domain="concepts",
            summary="경로 이동 뒤에도 같은 파일을 갱신한다.",
        ),
        "갱신한 본문이다.",
        current.revision,
    )

    assert saved.document.relative_path == "wiki/new-location/topic.md"
    assert moved.is_file()
    assert not (canonical_root / "concepts/stable-topic.md").exists()
