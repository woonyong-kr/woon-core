from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import (
    CompiledWiki,
    CompiledWikiSettings,
    CuratedRevision,
)
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
        curation_path=vault / "catalog/llm-wiki/curation.yaml",
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


def approve_archive(vault: Path, review_id: str, body: str) -> str:
    """Add a human approval fixture bound to the exact normalized body."""

    normalized = "\n".join(
        line.rstrip() for line in body.replace("\r\n", "\n").split("\n")
    ).strip() + "\n"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    path = vault / "catalog/llm-wiki/review-queue.yaml"
    existing = (
        yaml.safe_load(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"version": 1, "items": []}
    )
    existing["items"].append(
        {
            "candidate_id": review_id,
            "status": "approved",
            "kind": "manual-archive",
            "input_sha256": digest,
            "approved_by": "test-user",
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return review_id


def replace_source_body(source: dict[str, object], body: str) -> None:
    source["body"] = body
    normalized = "\n".join(line.rstrip() for line in body.splitlines()).strip() + "\n"
    source["normalized_sha256"] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def add_curated_source(vault: Path, *, purpose: str) -> None:
    sources_path = vault / "catalog/llm-wiki/sources.yaml"
    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    body = "기존 학습 문서를 현재 기준으로 다시 편집한 기록입니다.\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    source_id = "source://curated-wiki/os/virtual-memory-v1"
    sources["sources"].append(
        {
            "source_id": source_id,
            "kind": "curated-wiki",
            "locator": "curation/os/virtual-memory-v1",
            "original_sha256": digest,
            "normalized_sha256": digest,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": "가상 메모리",
            "purpose": purpose,
            "body": body,
        }
    )
    pages["pages"][0]["source_ids"].append(source_id)
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
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
    replace_source_body(source, "페이지 폴트와 보조 페이지 테이블을 함께 처리한다.\n")
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


def test_current_use_curation_is_separate_from_legacy_source_purpose(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )
    assert "purpose" not in sources["sources"][0]

    curation_path = tmp_path / "catalog/llm-wiki/curation.yaml"
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    assert curation == {
        "page_id": "os/virtual-memory",
        "current_use": (
            "가상 메모리 내용을 다시 학습하거나 설명할 때, "
            "관련 개념과 자료를 찾는 기준으로 사용한다."
        ),
        "basis": "legacy-page-metadata",
        "status": "provisional",
    }

    curation["current_use"] = "가상 메모리의 주소 변환 흐름을 설명할 때 기준으로 사용한다."
    curation["basis"] = "manual-review"
    curation["status"] = "confirmed"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.compile().compiled == 1
    output = (tmp_path / "wiki/os/virtual-memory.md").read_text(encoding="utf-8")
    assert "purpose: 가상 메모리의 주소 변환 흐름을 설명할 때 기준으로 사용한다." in output
    assert "purpose_basis: manual-review" in output
    assert "purpose_status:" not in output
    curation["status"] = "provisional"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    assert compiler.compile().compiled == 0
    assert compiler.audit().complete


def test_curated_revision_preserves_legacy_source_and_archives_prior_curated_body(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "os/virtual-memory.md",
        "가상 메모리",
        "## 시작\n\n페이지 폴트를 처리한다.\n",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    first = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="os/virtual-memory",
                body="## 시작\n\n페이지 폴트가 나면 필요한 페이지를 찾는다.\n",
                statement="페이지 폴트가 발생했을 때 필요한 페이지를 찾는 흐름을 설명한다.",
                current_use="페이지 폴트가 필요한 페이지를 찾는 흐름을 다시 설명할 때 사용한다.",
            ),
        )
    )

    assert first.curated == 1
    assert first.compiled == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    legacy = next(item for item in sources if item["kind"] == "legacy-wiki")
    curated = next(item for item in sources if item["kind"] == "curated-wiki")
    assert legacy["lifecycle"] == "compiled"
    assert curated["body"].startswith("## 시작")
    page = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8")
    )["pages"][0]
    assert legacy["source_id"] in page["source_ids"]
    assert page["render"] == {"kind": "source-body", "source_id": curated["source_id"]}
    curation = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )["curations"][0]
    assert curation["basis"] == "curated-revision"
    assert curation["status"] == "confirmed"
    assert curation["current_use"] == (
        "페이지 폴트가 필요한 페이지를 찾는 흐름을 다시 설명할 때 사용한다."
    )
    assert compiler.audit().complete

    second = compiler.curate_revisions(
        (
            CuratedRevision(
                page_id="os/virtual-memory",
                body="## 시작\n\n페이지 폴트가 나면 주소 변환에 필요한 페이지를 찾는다.\n",
                statement="페이지 폴트가 주소 변환에 필요한 페이지를 찾게 하는 흐름을 설명한다.",
            ),
        )
    )

    assert second.compiled == 1
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    archived = next(item for item in sources if item["source_id"] == curated["source_id"])
    assert archived["lifecycle"] == "archived"
    assert archived["superseded_by"].startswith("source://curated-wiki/")
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    assert any(
        item["status"] == "superseded" and item["kind"] == "curated-document"
        for item in claims
    )
    assert compiler.audit().complete


def test_initialize_curation_refuses_to_replace_existing_records(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    with pytest.raises(WoonError, match="already exists"):
        compiler.initialize_curation()


def test_refresh_provisional_curation_skips_manually_confirmed_records(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    curation_path = tmp_path / "catalog/llm-wiki/curation.yaml"
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    curation["current_use"] = "예전 자동 문구"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.refresh_provisional_curation() == 1
    curations = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    curation = curations["curations"][0]
    assert curation["current_use"].startswith("가상 메모리 내용")

    curation["current_use"] = "사람이 검토해 정한 현재 활용 목적"
    curation["basis"] = "manual-review"
    curation["status"] = "confirmed"
    curation_path.write_text(
        yaml.safe_dump(curations, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    assert compiler.refresh_provisional_curation() == 0
    current = yaml.safe_load(curation_path.read_text(encoding="utf-8"))
    assert current["curations"][0]["current_use"] == "사람이 검토해 정한 현재 활용 목적"


def test_refresh_provisional_curation_promotes_explicit_curated_page(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    add_curated_source(
        tmp_path,
        purpose="가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
    )

    assert compiler.refresh_provisional_curation() == 1
    curations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )
    assert curations["curations"][0] == {
        "page_id": "os/virtual-memory",
        "current_use": "가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
        "basis": "manual-review",
        "status": "confirmed",
    }


def test_initialize_curation_uses_explicit_source_provenance(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    add_curated_source(
        tmp_path,
        purpose="가상 메모리의 주소 변환 흐름을 다시 학습하고 설명하는 기준으로 사용한다.",
    )
    (tmp_path / "catalog/llm-wiki/curation.yaml").unlink()

    assert compiler.initialize_curation() == 1
    curations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/curation.yaml").read_text(encoding="utf-8")
    )
    assert curations["curations"][0]["basis"] == "manual-review"
    assert curations["curations"][0]["status"] == "confirmed"


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
    replace_source_body(
        sources["sources"][0], "헥사고날 아키텍처로 외부 기술의 의존 방향을 분리한다.\n"
    )
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


def test_compiler_derives_relations_from_legacy_related_to_wikilinks(tmp_path: Path) -> None:
    write_page(tmp_path, "ai/attention.md", "Attention", "관련 개념을 연결한다.")
    write_page(tmp_path, "ai/query-key-value.md", "Query, Key, Value", "입력의 역할을 나눈다.")
    map_path = tmp_path / "maps/transformer-attention-map.md"
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_text("# Transformer attention map\n", encoding="utf-8")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    page = next(item for item in pages["pages"] if item["page_id"] == "ai/attention")
    page["frontmatter"]["related_to"] = [
        "[[wiki/ai/query-key-value|Query, Key, Value]]",
        "[[maps/transformer-attention-map#핵심 링크|Attention 지도]]",
    ]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    compiler.compile()

    relations = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/relations.yaml").read_text(encoding="utf-8")
    )
    assert relations["relations"] == [
        {
            "from_page_id": "ai/attention",
            "type": "related",
            "to_id": "ai/query-key-value",
        },
        {
            "from_page_id": "ai/attention",
            "type": "related",
            "to_id": "maps/transformer-attention-map",
        },
    ]
    assert compiler.audit().complete


def test_compiler_audit_rejects_orphan_records_and_unresolved_relation_targets(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    orphan_source = dict(sources["sources"][0])
    orphan_source["source_id"] = "source://legacy-wiki/orphan.md"
    orphan_source["locator"] = "orphan.md"
    sources["sources"].append(orphan_source)
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    orphan_claim = dict(claims["claims"][0])
    orphan_claim["claim_id"] = "claim://legacy-wiki/orphan"
    claims["claims"].append(orphan_claim)
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    pages["pages"][0]["frontmatter"]["related"] = ["missing-target"]
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compiler.compile(force=True)

    audit = compiler.audit()

    assert "source://legacy-wiki/orphan.md: source has no page spec" in audit.errors
    assert "claim://legacy-wiki/orphan: claim has no page spec" in audit.errors
    assert "relation target does not resolve: missing-target" in audit.errors


def test_archive_preserves_replaced_conversation_revision_as_superseded_history(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/transaction-boundary",
        title="트랜잭션 경계",
        domain="backend",
        summary="데이터 변경과 외부 호출의 순서를 구분한다.",
        purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
    )

    first_body = "## 첫 기록\n\n처음 확인한 경계다."
    second_body = "## 갱신한 기록\n\n새 근거를 반영한 경계다."
    compiler.archive(
        metadata,
        first_body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-first", first_body),
    )
    compiler.archive(
        metadata,
        second_body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-second", second_body),
    )

    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    claims = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/claims.yaml").read_text(encoding="utf-8")
    )["claims"]
    prior_source = next(source for source in sources if "처음 확인한" in source["body"])
    current_source = next(source for source in sources if "새 근거를" in source["body"])
    prior_claim = next(claim for claim in claims if "처음 확인한" in claim["markdown"])
    current_claim = next(claim for claim in claims if "새 근거를" in claim["markdown"])

    assert prior_source["lifecycle"] == "archived"
    assert prior_source["superseded_by"] == current_source["source_id"]
    assert prior_claim["status"] == "superseded"
    assert prior_claim["superseded_by"] == current_claim["claim_id"]
    assert compiler.audit().complete


def test_archive_rejects_automated_or_sensitive_ingestion_origins(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/archive-boundary",
        title="아카이브 경계",
        domain="backend",
        summary="자동 수집은 검증 Wiki를 직접 쓰지 않는다.",
        purpose="수집과 검증의 경계를 확인한다.",
    )

    for origin in ("email", "chat", "novel", "system", "tool", "reasoning"):
        with pytest.raises(WoonError, match="only accepts"):
            compiler.archive(metadata, "## 원문\n\n차단되어야 한다.", (), archive_origin=origin)


def test_archive_requires_human_approval_bound_to_exact_body(tmp_path: Path) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/archive-approval",
        title="아카이브 승인",
        domain="backend",
        summary="검토 승인은 정확한 본문에만 묶인다.",
        purpose="검토 receipt 경계를 확인한다.",
    )
    approved_body = "## 승인 본문\n\n검토한 본문이다."
    review_id = approve_archive(tmp_path, "review-bound", approved_body)

    with pytest.raises(WoonError, match="bound to the input hash"):
        compiler.archive(
            metadata,
            "## 바뀐 본문\n\n승인 뒤에 변경됐다.",
            (),
            approved_review_id=review_id,
        )
    with pytest.raises(WoonError, match="requires approved_review_id"):
        compiler.archive(metadata, approved_body, ())


def test_audit_rejects_archived_source_without_current_matching_review(tmp_path: Path) -> None:
    write_page(tmp_path, "canonical/backend/review-recheck.md", "승인 재검증", "기존 기록.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    body = "## 승인 기록\n\n승인 receipt를 다시 확인한다."
    compiler.archive(
        DocumentMetadata(
            canonical_id="backend/review-recheck",
            title="승인 재검증",
            domain="backend",
            summary="승인 receipt가 현재도 일치해야 한다.",
            purpose="archive 승인 경계를 검증한다.",
        ),
        body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-recheck", body),
    )
    review_path = tmp_path / "catalog/llm-wiki/review-queue.yaml"
    review = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review["items"][0]["input_sha256"] = "0" * 64
    review_path.write_text(yaml.safe_dump(review, allow_unicode=True), encoding="utf-8")

    with pytest.raises(WoonError, match="no matching review evidence"):
        compiler.compile()
    audit = compiler.audit()

    assert not audit.complete
    assert any("no matching review evidence" in error for error in audit.errors)


def test_git_restore_rejects_hash_that_does_not_match_body(tmp_path: Path) -> None:
    compiler = CompiledWiki(compiled_settings(tmp_path))
    with pytest.raises(WoonError, match="body hash does not match"):
        compiler.restore_from_git(
            DocumentMetadata(
                canonical_id="backend/git-restore",
                title="Git 복구",
                domain="backend",
                summary="Git 복구의 본문 hash를 검증한다.",
                purpose="복구 경계를 검증한다.",
            ),
            "## 복구\n\nGit 본문.",
            "abc123",
            "0" * 64,
        )


def test_compiled_wiki_restores_confirmed_git_revision(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", tmp_path, "config", "user.email", "test@example.com"], check=True
    )
    write_page(tmp_path, "os/seed.md", "초기 정본", "컴파일러 입력을 초기화한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki/canonical"),
        SQLiteFtsSearchIndex(tmp_path / ".local/search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    metadata = DocumentMetadata(
        canonical_id="backend/git-backed-restore",
        title="Git 정본 복구",
        domain="backend",
        summary="확정한 Git revision만 복구한다.",
        purpose="정상 복구가 승인 queue와 충돌하지 않는지 검증한다.",
    )
    first_body = "## 첫 기록\n\n첫 번째 Git 정본이다."
    first = service.archive(
        metadata,
        first_body,
        approved_review_id=approve_archive(tmp_path, "review-git-first", first_body),
    )
    subprocess.run(["git", "-C", tmp_path, "add", "wiki"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "docs: first"], check=True)
    revision = subprocess.run(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    second_body = "## 두 번째 기록\n\n현재 정본이다."
    second = service.archive(
        metadata,
        second_body,
        first.document.revision,
        approved_review_id=approve_archive(tmp_path, "review-git-second", second_body),
    )

    restored = service.restore(
        metadata.canonical_id,
        revision,
        second.document.revision,
        confirmed=True,
    )

    assert "첫 번째 Git 정본" in restored.document.body
    sources = yaml.safe_load(
        (tmp_path / "catalog/llm-wiki/sources.yaml").read_text(encoding="utf-8")
    )["sources"]
    current = next(source for source in sources if source["lifecycle"] == "compiled")
    assert current["archive_origin"] == "git-restore"
    assert current["source_session_ids"] == [f"git:{revision}"]


def test_reconcile_marks_unreferenced_conversation_revision_without_deleting_it(
    tmp_path: Path,
) -> None:
    write_page(
        tmp_path,
        "canonical/backend/ports-and-adapters.md",
        "포트와 어댑터",
        "의존성을 분리한다.",
    )
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    metadata = DocumentMetadata(
        canonical_id="backend/transaction-boundary",
        title="트랜잭션 경계",
        domain="backend",
        summary="데이터 변경과 외부 호출의 순서를 구분한다.",
        purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
    )
    body = "## 현재 기록\n\n현재 정본이다."
    compiler.archive(
        metadata,
        body,
        (),
        approved_review_id=approve_archive(tmp_path, "review-current", body),
    )

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    current_source = next(
        source for source in sources["sources"] if source["kind"] == "conversation"
    )
    current_claim = next(
        claim for claim in claims["claims"] if claim["kind"] == "conversation-summary"
    )
    prior_body = "## 이전 기록\n\n나중에 복구할 수 있는 원본이다.\n"
    prior_digest = hashlib.sha256(prior_body.encode("utf-8")).hexdigest()
    prior_source_id = "source://conversation/backend/transaction-boundary/" + "a" * 24
    prior_claim_id = "claim://conversation/backend/transaction-boundary/" + "a" * 24
    prior_review_id = approve_archive(tmp_path, "review-prior", prior_body)
    sources["sources"].append(
        {
            **current_source,
            "source_id": prior_source_id,
            "original_sha256": prior_digest,
            "normalized_sha256": prior_digest,
            "body": prior_body,
            "approved_review_id": prior_review_id,
        }
    )
    claims["claims"].append(
        {
            **current_claim,
            "claim_id": prior_claim_id,
            "source_ids": [prior_source_id],
            "markdown": prior_body,
        }
    )
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = compiler.reconcile_superseded_revisions()

    assert report.archived_sources == 1
    assert report.superseded_claims == 1
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))["sources"]
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))["claims"]
    prior_source = next(source for source in sources if source["source_id"] == prior_source_id)
    prior_claim = next(claim for claim in claims if claim["claim_id"] == prior_claim_id)
    assert prior_source["body"] == prior_body
    assert prior_source["superseded_by"] == current_source["source_id"]
    assert prior_claim["markdown"] == prior_body
    assert prior_claim["superseded_by"] == current_claim["claim_id"]
    assert compiler.audit().complete


def test_composed_page_rejects_a_whole_document_as_one_claim(tmp_path: Path) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    pages_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    pages["pages"][0]["render"] = {"kind": "claims"}
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims = yaml.safe_load(claims_path.read_text(encoding="utf-8"))
    claims["claims"][0]["markdown"] = "가" * 1_801
    claims_path.write_text(
        yaml.safe_dump(claims, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="split it by revisitable claim"):
        compiler.compile(force=True)


def test_compiler_rejects_source_body_that_does_not_match_normalized_digest(
    tmp_path: Path,
) -> None:
    write_page(tmp_path, "os/virtual-memory.md", "가상 메모리", "페이지 폴트를 처리한다.")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()

    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["sources"][0]["body"] = "검증 없이 바뀐 source 본문.\n"
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="normalized_sha256 does not match"):
        compiler.compile(force=True)


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

    body = "## 경계\n\n데이터 변경과 외부 호출의 순서를 분리한다."
    with pytest.raises(RuntimeError, match="injected index failure"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/transaction-boundary",
                title="트랜잭션 경계",
                domain="backend",
                summary="요청 처리의 원자성을 정의한다.",
                purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
                source_ids=("session://2026-08-14/001",),
            ),
            body,
            approved_review_id=approve_archive(tmp_path, "review-index-failure", body),
        )

    assert not (canonical_root / "backend/transaction-boundary.md").exists()
    assert compiler.audit().complete


def test_compiled_archive_preserves_session_ownership_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
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

    body = "## 경계\n\n데이터 변경과 외부 호출의 순서를 분리한다."
    service.archive(
        DocumentMetadata(
            canonical_id="backend/transaction-boundary",
            title="트랜잭션 경계",
            domain="backend",
            summary="요청 처리의 원자성을 정의한다.",
            purpose="트랜잭션 경계와 복구 순서를 설계할 때 재사용한다.",
            source_ids=("session://2026-08-14/001",),
        ),
        body,
        approved_review_id=approve_archive(tmp_path, "review-session-owner", body),
    )

    archived = (canonical_root / "backend/transaction-boundary.md").read_text(encoding="utf-8")
    assert "source_ids:\n- session://2026-08-14/001" in archived
    assert "purpose: 트랜잭션 경계와 복구 순서를 설계할 때 재사용한다." in archived
    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    conversation = next(source for source in sources["sources"] if source["kind"] == "conversation")
    assert conversation["purpose"] == "트랜잭션 경계와 복구 순서를 설계할 때 재사용한다."

    del conversation["purpose"]
    sources_path.write_text(
        yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(WoonError, match="requires non-empty purpose"):
        compiler.compile()

    with pytest.raises(WoonError, match="already owned"):
        service.archive(
            DocumentMetadata(
                canonical_id="backend/transaction-order",
                title="트랜잭션 순서",
                domain="backend",
                summary="요청 처리 단계의 순서를 정의한다.",
                purpose="트랜잭션 실행 순서를 설계할 때 재사용한다.",
                source_ids=("session://2026-08-14/001",),
            ),
            "## 순서\n\n쓰기와 외부 호출의 실행 순서를 관리한다.",
        )
