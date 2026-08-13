from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import CompiledWiki, CompiledWikiSettings
from woon_core.knowledge.domain import DocumentMetadata, IndexedDocument
from woon_core.knowledge.service import KnowledgeService


class FailOnceIndex(SQLiteFtsSearchIndex):
    def __init__(self, database: Path) -> None:
        super().__init__(database)
        self.fail_next = False

    def rebuild(self, documents: list[IndexedDocument]) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("injected index failure")
        return super().rebuild(documents)


def compiled_settings(vault: Path) -> CompiledWikiSettings:
    return CompiledWikiSettings(
        vault=vault,
        output_root=vault / "wiki",
        sources_path=vault / "catalog/llm-wiki/sources.yaml",
        claims_path=vault / "catalog/llm-wiki/claims.yaml",
        pages_path=vault / "catalog/llm-wiki/pages.yaml",
        relations_path=vault / "catalog/llm-wiki/relations.yaml",
        receipts_path=vault / "catalog/llm-wiki/receipts.yaml",
        review_queue_path=vault / "catalog/llm-wiki/review-queue.yaml",
    )


def write_page(vault: Path, relative: str, title: str, body: str) -> None:
    path = vault / "wiki" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        f"title: {title}",
        "summary: 컴파일러 이관 검증 문서.",
        "access: local-only",
    ]
    if relative.startswith("canonical/"):
        canonical_id = Path(relative).with_suffix("").as_posix().removeprefix("canonical/")
        domain = canonical_id.split("/", 1)[0]
        frontmatter = [
            "type: Wiki",
            f"canonical_id: {canonical_id}",
            f"title: {title}",
            f"domain: {domain}",
            "summary: 컴파일러 이관 검증 문서.",
            "status: Canonical",
            "publish: false",
            "access: local-only",
            "difficulty: foundation",
            "prerequisites: []",
            "next_concepts: []",
            "related: []",
            "source_ids: []",
        ]
    path.write_text(
        "---\n" + "\n".join(frontmatter) + f"\n---\n\n# {title}\n\n{body}\n",
        encoding="utf-8",
    )


def test_migration_creates_reproducible_source_schema_and_incremental_build(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))

    migrated = compiler.migrate()
    audit = compiler.audit()

    assert migrated.migrated == 2
    assert migrated.compiled == 2
    assert audit.complete
    output = (tmp_path / "wiki/os/virtual-memory.md").read_text(encoding="utf-8")
    assert "llm_wiki:" in output
    assert compiler.compile().compiled == 0

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    source = next(item for item in sources["sources"] if item["locator"] == "os/virtual-memory.md")
    source["body"] = "페이지 폴트와 보조 페이지 테이블을 함께 처리한다.\n"
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = compiler.compile()

    assert report.compiled == 1
    assert report.page_ids == ("os/virtual-memory",)
    assert "보조 페이지 테이블" in (tmp_path / "wiki/os/virtual-memory.md").read_text(
        encoding="utf-8"
    )
    assert compiler.audit().complete


def test_compiled_service_fails_closed_until_source_change_is_built(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    canonical_root = tmp_path / "wiki/canonical"
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    service.reindex()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["sources"][0]["body"] = "헥사고날 아키텍처로 외부 기술의 의존 방향을 분리한다.\n"
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="compiled Wiki is stale"):
        service.search("헥사고날", 1)

    assert service.compile().compiled == 1
    assert service.search("헥사고날", 1)[0].canonical_id == "backend/ports-and-adapters"


def test_compiler_rebuilds_derived_relations_from_page_specs(tmp_path: Path) -> None:
    write_page(tmp_path, "canonical/backend/first.md", "첫 문서", "첫 설명.")
    write_page(tmp_path, "canonical/backend/second.md", "둘째 문서", "둘째 설명.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = next(item for item in pages["pages"] if item["page_id"] == "canonical/backend/first")
    page["frontmatter"]["related"] = ["backend/second"]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    relations_path = tmp_path / "catalog/llm-wiki/relations.yaml"
    relations_path.write_text("version: 1\nrelations: []\n", encoding="utf-8")

    assert compiler.compile().compiled == 1
    relations = yaml.safe_load(relations_path.read_text(encoding="utf-8"))
    assert relations["relations"] == [
        {
            "from_page_id": "canonical/backend/first",
            "type": "related",
            "to_id": "backend/second",
        }
    ]
    assert compiler.audit().complete


def test_compiled_archive_restores_inputs_and_output_when_reindex_fails(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "외부 기술의 의존 방향을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    canonical_root = tmp_path / "wiki/canonical"
    index = FailOnceIndex(tmp_path / ".local/search.sqlite3")
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, canonical_root),
        index,
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    service.reindex()
    index.fail_next = True

    with pytest.raises(RuntimeError, match="injected index failure"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/transaction-boundary",
                title="트랜잭션 경계",
                domain="backend",
                summary="요청 처리의 원자성을 정의한다.",
                source_ids=("session://2026-08-14/001",),
            ),
            "## 경계\n\n데이터 변경과 외부 호출의 순서를 분리한다.",
        )

    assert not (canonical_root / "backend/transaction-boundary.md").exists()
    assert compiler.audit().complete
