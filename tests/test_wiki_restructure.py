from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import woon_core.knowledge.wiki_restructure as wiki_restructure
from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import (
    CompilationAudit,
    CompiledWiki,
    CompiledWikiSettings,
    CompiledWikiTransaction,
    CuratedRevision,
    _normalize,
)
from woon_core.knowledge.service import KnowledgeService, ManualWikiWrite, ResourceFileRename
from woon_core.knowledge.wiki_restructure import (
    _canonical_record_sha256,
    apply_wiki_restructure,
    prepare_wiki_restructure_preflight,
    render_wiki_restructure_classification,
    render_wiki_restructure_inventory,
    render_wiki_restructure_template,
)
from woon_core.knowledge.wiki_tree import split_markdown


def _write_page(vault: Path, relative: str, canonical_id: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = "" if relative == "wiki/README.md" else "parent: '[[wiki/README|Vault]]'\n"
    kind = "root" if relative == "wiki/README.md" else "topic"
    body = (
        "<!-- woon-wiki-children:start -->\n<!-- woon-wiki-children:end -->\n"
        if relative == "wiki/README.md"
        else "테스트 본문입니다.\n"
    )
    path.write_text(
        "---\n"
        "type: Wiki\n"
        f"title: {path.stem}\n"
        f"canonical_id: {canonical_id}\n"
        f"node_kind: {kind}\n"
        f"{parent}"
        f"keywords: [{canonical_id}]\n"
        "aliases: []\n"
        "view_mode: tree\n"
        "updated: 2026-09-05\n"
        "summary: 테스트 문서입니다.\n"
        "knowledge_state: 확인 필요\n"
        "---\n\n"
        f"# {path.stem}\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def test_restructure_preflight_requires_every_active_page_once(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    child = _write_page(tmp_path, "wiki/old/topic.md", "old/topic")
    root_hash = hashlib.sha256(root.read_bytes()).hexdigest()
    child_hash = hashlib.sha256(child.read_bytes()).hexdigest()
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        f"- current_path: wiki/README.md\n  current_sha256: {root_hash}\n"
        "  canonical_id: README\n  source_owner: manual\n  disposition: keep\n"
        f"- current_path: wiki/old/topic.md\n  current_sha256: {child_hash}\n"
        "  canonical_id: old/topic\n  source_owner: manual\n  disposition: move\n"
        "  target_path: wiki/Wiki/programming-language-runtime/topic.md\n"
        "  target_parent: wiki/README.md\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert report.issues == ()
    assert report.document_count == 2
    assert report.disposition_counts == {"keep": 1, "move": 1}
    assert report.target_count == 1


def test_restructure_preflight_rejects_stale_hash_and_missing_record(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/old/topic.md", "old/topic")
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/README.md\n"
        f"  current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}\n"
        "  canonical_id: stale\n"
        "  source_owner: manual\n"
        "  disposition: keep\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert "records[1]: canonical_id does not match: wiki/README.md" in report.issues
    assert "manifest omits 1 active Wiki pages" in report.issues


def test_restructure_preflight_requires_compiler_ownership_from_page_catalog(
    tmp_path: Path,
) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    catalog = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "version: 1\npages:\n- page_id: README\n  output_path: README.md\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/README.md\n"
        f"  current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}\n"
        "  canonical_id: README\n"
        "  source_owner: manual\n"
        "  disposition: keep\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert report.issues == ("records[1]: source_owner must be 'compiler' for wiki/README.md",)


def test_restructure_template_covers_each_active_page_and_marks_owner(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    catalog = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "version: 1\npages:\n- page_id: README\n  output_path: README.md\n",
        encoding="utf-8",
    )

    template = render_wiki_restructure_template(tmp_path).decode("utf-8")

    assert "current_path: wiki/README.md" in template
    assert f"current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}" in template
    assert "source_owner: compiler" in template
    assert "disposition: review" in template


def test_restructure_classification_assigns_known_legacy_areas_once(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/ai/model.md", "ai/model")
    _write_page(tmp_path, "wiki/personal/kotlin-in-action/chapter-01.md", "book/kotlin/1")
    _write_page(tmp_path, "wiki/hubs/legacy.md", "hubs/legacy")
    _write_page(
        tmp_path,
        "wiki/personal/projects/(미정)소설.md",
        "personal/projects/private-novel-writing",
    )

    rendered = render_wiki_restructure_classification(tmp_path).decode("utf-8")

    assert "document_count: 5" in rendered
    assert "target_scope: Wiki > AI·머신러닝" in rendered
    assert "target_scope: Wiki > 책 > 프로그래밍 언어·설계" in rendered
    assert "rationale: legacy-navigation-wrapper" in rendered
    records = yaml.safe_load(rendered)["records"]
    novel = next(record for record in records if record["current_path"].endswith("(미정)소설.md"))
    assert novel["target_scope"] == "창작 > 창작 프로젝트"
    assert novel["rationale"] == "creative-project"
    catalog = tmp_path / "catalog/llm-wiki"
    catalog.mkdir(parents=True)
    for name, key in (("pages", "pages"), ("curation", "curations"), ("receipts", "receipts")):
        (catalog / f"{name}.yaml").write_text(f"version: 1\n{key}: []\n", encoding="utf-8")
    inventory = yaml.safe_load(render_wiki_restructure_inventory(tmp_path))
    novel_inventory = next(
        record
        for record in inventory["records"]
        if record["current_path"].endswith("(미정)소설.md")
    )
    assert novel_inventory["target_scope"] == novel["target_scope"]


def test_restructure_inventory_pins_ownership_hashes_and_wikilinks(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    _compiler, _service = _compiled_service(tmp_path)
    manual = _write_page(tmp_path, "wiki/manual/notes.md", "manual/notes")
    manual.write_text(
        manual.read_text(encoding="utf-8") + "\n[[wiki/legacy/topic|legacy topic]]\n",
        encoding="utf-8",
    )

    payload = yaml.safe_load(render_wiki_restructure_inventory(tmp_path).decode("utf-8"))

    assert payload["kind"] == "wiki-restructure-inventory"
    assert payload["document_count"] == 3
    records = {record["current_path"]: record for record in payload["records"]}
    compiler_record = records["wiki/legacy/topic.md"]
    manual_record = records["wiki/manual/notes.md"]
    assert compiler_record["source_owner"] == "compiler"
    assert compiler_record["page_id"] == "legacy/topic"
    assert compiler_record["receipt_id"] == "legacy/topic"
    assert manual_record["source_owner"] == "manual"
    assert manual_record["current_sha256"] == hashlib.sha256(manual.read_bytes()).hexdigest()
    assert manual_record["outbound_wikilinks"] == [
        "wiki/README.md",
        "wiki/legacy/topic.md",
    ]
    assert "wiki/manual/notes.md" in compiler_record["inbound_wikilinks"]
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    with pytest.raises(WoonError, match="inventory is read-only"):
        prepare_wiki_restructure_preflight(tmp_path, inventory_path)


def test_restructure_inventory_derives_legacy_publication_state_without_rewriting_pages(
    tmp_path: Path,
) -> None:
    public = _write_page(tmp_path, "wiki/legacy/public.md", "legacy/public")
    private = _write_page(tmp_path, "wiki/legacy/private.md", "legacy/private")
    _compiler, _service = _compiled_service(tmp_path)
    public.write_text(
        public.read_text(encoding="utf-8").replace(
            "type: Wiki\n",
            "type: Wiki\npublish: true\naccess: public\n",
        ),
        encoding="utf-8",
    )
    private.write_text(
        private.read_text(encoding="utf-8").replace(
            "type: Wiki\n",
            "type: Wiki\npublish: false\naccess: local-only\n",
        ),
        encoding="utf-8",
    )

    payload = yaml.safe_load(render_wiki_restructure_inventory(tmp_path).decode("utf-8"))
    records = {record["current_path"]: record for record in payload["records"]}

    assert records["wiki/legacy/public.md"]["publication_state"] == "publish"
    assert records["wiki/legacy/private.md"]["publication_state"] == "private"
    assert "publication_state:" not in public.read_text(encoding="utf-8")
    assert "publication_state:" not in private.read_text(encoding="utf-8")


def _compiled_service(vault: Path) -> tuple[CompiledWiki, KnowledgeService]:
    compiler = CompiledWiki(
        CompiledWikiSettings(
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
    )
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        SQLiteFtsSearchIndex(vault / ".local/search.sqlite3"),
        GitKnowledgeHistory(vault),
        compiled_wiki=compiler,
    )
    service.reindex()
    return compiler, service


def _native_transaction_fixture(
    vault: Path,
) -> tuple[CompiledWiki, KnowledgeService, tuple[Path, Path]]:
    _write_page(vault, "wiki/README.md", "README")
    compiler, service = _compiled_service(vault)
    parent = _write_page(vault, "wiki/private/person.md", "private/person")
    child = _write_page(vault, "wiki/private/fact.md", "private/fact")
    for path in (parent, child):
        text = path.read_text().replace(
            "type: Wiki\n", "type: Wiki\naccess: local-only\npublish: false\n"
        )
        if path == child:
            text = text.replace("[[wiki/README|Vault]]", "[[wiki/private/person|person]]")
        path.write_text(text)
    service.reindex()
    return compiler, service, (parent, child)


def _native_writes(vault: Path, paths: tuple[Path, ...]) -> tuple[ManualWikiWrite, ...]:
    return tuple(
        ManualWikiWrite(
            path.relative_to(vault).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.relative_to(vault).as_posix(),
            hashlib.sha256(path.read_bytes() + b"\nConfirmed fact and source.\n").hexdigest(),
            path.read_bytes() + b"\nConfirmed fact and source.\n",
        )
        for path in paths
    )


def test_native_transaction_updates_only_requested_pages_without_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler, service, paths = _native_transaction_fixture(tmp_path)
    # An unfinished unrelated claim draft must not couple a private native write to compilation.
    (tmp_path / "catalog/llm-wiki/claims.yaml").write_bytes(b"claims: [unfinished\n")
    untouched = [tmp_path / "wiki/README.md", *compiler.snapshot_inputs()]
    before = {path: path.read_bytes() for path in untouched if path.is_file()}
    writes = _native_writes(tmp_path, paths)

    def no_compile(*_args, **_kwargs):
        pytest.fail("native updates must not run a compiler transaction")

    monkeypatch.setattr(compiler, "apply_compiled_wiki_transaction", no_compile)
    result = service.apply_native_wiki_transaction(writes)
    assert result["written"] == 2
    assert result["compiler_written"] == result["catalog_written"] == 0
    for path, write in zip(paths, writes, strict=True):
        assert path.read_bytes() == write.content
    assert all(path.read_bytes() == content for path, content in before.items())
    receipt = tmp_path / str(result["receipt"])
    assert receipt.is_file() and receipt.stat().st_mode & 0o777 == 0o600
    for change in result["changes"]:
        backup = tmp_path / change["backup"]
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == change["before_sha256"]
    no_op = tuple(
        ManualWikiWrite(w.target_path, w.target_sha256, w.target_path, w.target_sha256, w.content)
        for w in writes
    )
    assert service.apply_native_wiki_transaction(no_op)["written"] == 0
    assert list(receipt.parent.parent.glob("*/receipt.json")) == [receipt]


def test_native_record_metadata_loss_fails_before_writes_or_receipt(tmp_path: Path) -> None:
    from woon_core.people.records import resolve_record_metadata

    _compiler, service, paths = _native_transaction_fixture(tmp_path)
    path = paths[1]
    metadata, body = split_markdown(path.read_text())
    metadata.update(
        record_kind="recording",
        recording_id="voice-001",
        candidate_person_ids=["lee-minjeong"],
        unresolved_speaker_count=2,
        recorded_at="2026-09-09T12:30:00+09:00",
    )

    def content(header: dict) -> bytes:
        return (
            "---\n" + yaml.safe_dump(header, allow_unicode=True, sort_keys=False) + "---\n" + body
        ).encode()

    original = content(metadata)
    path.write_bytes(original)
    current = service.get("private/fact")
    with pytest.raises(WoonError, match="native/source writer"):
        service.archive(
            replace(current.metadata, purpose="Keep the record evidence"),
            "## Updated\n\nA record update.",
            current.revision,
        )
    assert path.read_bytes() == original
    proposed = {key: value for key, value in metadata.items() if key != "candidate_person_ids"}
    after = content(proposed)
    write = ManualWikiWrite(
        path.relative_to(tmp_path).as_posix(),
        hashlib.sha256(original).hexdigest(),
        path.relative_to(tmp_path).as_posix(),
        hashlib.sha256(after).hexdigest(),
        after,
    )
    with pytest.raises(WoonError, match="metadata omitted"):
        service.apply_native_wiki_transaction((write,))
    assert path.read_bytes() == original
    assert not (tmp_path / ".local/woon-knowledge/native-wiki-transactions").exists()
    restored = resolve_record_metadata(metadata, proposed)
    restored["summary"] = "Updated record summary"
    after = content(restored)
    result = service.apply_native_wiki_transaction(
        (
            replace(
                write,
                content=after,
                target_sha256=hashlib.sha256(after).hexdigest(),
            ),
        )
    )
    assert result["written"] == 1
    assert split_markdown(path.read_text())[0]["candidate_person_ids"] == ["lee-minjeong"]


@pytest.mark.parametrize("failure", ["tree", "index", "receipt", "concurrent"])
def test_native_transaction_rolls_back_owned_writes_and_preserves_concurrent_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import woon_core.knowledge.service as service_module

    _compiler, service, paths = _native_transaction_fixture(tmp_path)
    before = {path: path.read_bytes() for path in paths}
    writes = _native_writes(tmp_path, paths)
    if failure == "tree":
        content = writes[1].content.replace(b"[[wiki/private/person|person]]", b"[[wiki/missing]]")
        writes = (
            writes[0],
            ManualWikiWrite(
                writes[1].current_path,
                writes[1].current_sha256,
                writes[1].target_path,
                hashlib.sha256(content).hexdigest(),
                content,
            ),
        )
    original_reindex = service._reindex_unlocked
    called = False

    def reindex():
        nonlocal called
        if not called:
            called = True
            if failure == "index":
                raise WoonError("injected index failure")
            if failure == "concurrent":
                paths[1].write_bytes(b"Concurrent user edit; preserve.\n")
                raise WoonError("concurrent edit while indexing")
        # The index stub keeps this test focused on concurrent-file rollback.
        return 2 if failure == "concurrent" else original_reindex()

    monkeypatch.setattr(service, "_reindex_unlocked", reindex)
    original_write = service_module.atomic_write

    def write(path: Path, content: bytes, **options):
        if failure == "receipt" and path.name == "receipt.json":
            raise OSError("injected receipt failure")
        original_write(path, content, **options)

    monkeypatch.setattr(service_module, "atomic_write", write)
    with pytest.raises((WoonError, OSError)):
        service.apply_native_wiki_transaction(writes)
    assert paths[0].read_bytes() == before[paths[0]]
    expected = b"Concurrent user edit; preserve.\n" if failure == "concurrent" else before[paths[1]]
    assert paths[1].read_bytes() == expected
    assert not list(
        (tmp_path / ".local/woon-knowledge/native-wiki-transactions").glob("*/receipt.json")
    )


@pytest.mark.parametrize(
    "failure", ["stale", "compiler", "compiler-altered-identity", "public", "identity"]
)
def test_native_transaction_rejects_wrong_ownership_and_stale_input_before_writing(
    tmp_path: Path,
    failure: str,
) -> None:
    _compiler, service, paths = _native_transaction_fixture(tmp_path)
    targets = (tmp_path / "wiki/README.md",) if failure.startswith("compiler") else paths
    if failure == "compiler-altered-identity":
        targets[0].write_text(
            targets[0].read_text().replace("canonical_id: README", "canonical_id: x")
        )
    writes = _native_writes(tmp_path, targets)
    if failure in {"public", "identity"}:
        content = writes[0].content.replace(
            b"access: local-only" if failure == "public" else b"canonical_id: private/person",
            b"access: public" if failure == "public" else b"canonical_id: private/another",
        )
        writes = (
            ManualWikiWrite(
                writes[0].current_path,
                writes[0].current_sha256,
                writes[0].target_path,
                hashlib.sha256(content).hexdigest(),
                content,
            ),
            *writes[1:],
        )
    if failure == "stale":
        targets[0].write_bytes(targets[0].read_bytes() + b"\nNewer user input.\n")
    before = {path: path.read_bytes() for path in targets}
    with pytest.raises(WoonError):
        service.apply_native_wiki_transaction(writes)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (tmp_path / ".local/woon-knowledge/native-wiki-transactions").exists()


def _v2_record(
    vault: Path,
    relative: str,
    *,
    action: str,
    target_path: str | None = None,
    target_parent: str | None = None,
    target_parent_canonical_id: str | None = None,
    target_sequence: int | None = None,
) -> dict[str, object]:
    pages = yaml.safe_load((vault / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8"))
    assert isinstance(pages, dict)
    records = pages.get("pages")
    assert isinstance(records, list)
    output = relative.removeprefix("wiki/")
    page = next(item for item in records if item.get("output_path") == output)
    assert isinstance(page, dict)
    source = vault / relative
    frontmatter, _body = split_markdown(source.read_text(encoding="utf-8"))
    record: dict[str, object] = {
        "action": action,
        "current_path": relative,
        "current_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "canonical_id": frontmatter["canonical_id"],
        "page_id": page["page_id"],
        "page_spec_sha256": _canonical_record_sha256(page),
        "source_owner": "compiler",
    }
    if target_path is not None:
        record["target_path"] = target_path
    if target_parent is not None:
        record["target_parent"] = target_parent
    if target_parent_canonical_id is not None:
        record["target_parent_canonical_id"] = target_parent_canonical_id
    if target_sequence is not None:
        record["target_sequence"] = target_sequence
    return record


def _hub_create_record() -> dict[str, object]:
    page = {
        "page_id": "Wiki/README",
        "output_path": "Wiki/README.md",
        "title": "공개 Wiki 경계",
        "frontmatter": {
            "type": "Wiki",
            "canonical_id": "Wiki",
            "title": "공개 Wiki 경계",
            "node_kind": "hub",
            "parent": "[[wiki/README|README]]",
            "sequence": 1,
            "keywords": ["Wiki"],
            "aliases": [],
            "view_mode": "tree",
            "updated": "2026-09-05",
            "summary": "정본 지식 영역을 탐색하는 허브입니다.",
            "knowledge_state": "확인 필요",
        },
        "source_ids": [],
        "claim_ids": [],
        "render": {"kind": "toc-only"},
        # Display-label paths intentionally differ from the stable canonical
        # page id; the compiler requires that migration boundary explicitly.
        "output_path_migration": True,
    }
    return {
        "action": "create",
        "canonical_id": "Wiki",
        "page_id": "Wiki/README",
        "source_owner": "compiler",
        "page_spec_sha256": _canonical_record_sha256(page),
        "target_path": "wiki/Wiki/README.md",
        "target_parent": "wiki/README.md",
        "target_parent_canonical_id": "README",
        "target_sequence": 1,
        "page_spec": page,
        "curation": {
            "page_id": "Wiki/README",
            "current_use": "지식 분야를 찾는 출발점으로 사용한다.",
            "basis": "manual-review",
            "status": "confirmed",
        },
    }


def test_v2_restructure_creates_hub_moves_compiler_page_and_rejects_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    compiler, service = _compiled_service(tmp_path)
    manifest = tmp_path / "restructure-v2.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "records": [
                    _v2_record(tmp_path, "wiki/README.md", action="keep"),
                    _hub_create_record(),
                    _v2_record(
                        tmp_path,
                        "wiki/legacy/topic.md",
                        action="move",
                        target_path="wiki/moved/topic.md",
                        target_parent="wiki/README.md",
                        target_parent_canonical_id="README",
                        target_sequence=2,
                    ),
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(wiki_restructure, "build_knowledge_service", lambda vault: (None, service))

    report = apply_wiki_restructure(tmp_path, manifest)

    assert report.created_pages == ("Wiki/README",)
    assert report.moved_pages == ("legacy/topic",)
    assert (tmp_path / "wiki/Wiki/README.md").is_file()
    assert (tmp_path / "wiki/moved/topic.md").is_file()
    assert not (tmp_path / "wiki/legacy/topic.md").exists()
    root_text = (tmp_path / "wiki/README.md").read_text(encoding="utf-8")
    assert "[[wiki/Wiki/README|Wiki]]" in root_text
    assert "wiki/legacy/topic" not in root_text
    moved_metadata, _body = split_markdown(
        (tmp_path / "wiki/moved/topic.md").read_text(encoding="utf-8")
    )
    assert moved_metadata["parent"] == "[[wiki/README|README]]"
    assert moved_metadata["sequence"] == 2
    assert compiler.audit().complete
    before_replay = (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes()

    with pytest.raises(WoonError, match="preflight failed"):
        apply_wiki_restructure(tmp_path, manifest)

    assert (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes() == before_replay


def test_v2_restructure_preflight_rejects_stale_compiler_page_spec(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    _compiler, _service = _compiled_service(tmp_path)
    record = _v2_record(
        tmp_path,
        "wiki/legacy/topic.md",
        action="move",
        target_path="wiki/moved/topic.md",
        target_parent="wiki/README.md",
        target_parent_canonical_id="README",
        target_sequence=1,
    )
    record["page_spec_sha256"] = "0" * 64
    manifest = tmp_path / "restructure-v2.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "records": [
                    _v2_record(tmp_path, "wiki/README.md", action="keep"),
                    record,
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert "records[2]: page_spec_sha256 does not match: wiki/legacy/topic.md" in report.issues


def test_v2_restructure_restores_old_output_after_compiler_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    compiler, service = _compiled_service(tmp_path)
    manifest = tmp_path / "restructure-v2.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "records": [
                    _v2_record(tmp_path, "wiki/README.md", action="keep"),
                    _v2_record(
                        tmp_path,
                        "wiki/legacy/topic.md",
                        action="move",
                        target_path="wiki/moved/topic.md",
                        target_parent="wiki/README.md",
                        target_parent_canonical_id="README",
                        target_sequence=1,
                    ),
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    old_output = (tmp_path / "wiki/legacy/topic.md").read_bytes()
    old_root = (tmp_path / "wiki/README.md").read_bytes()
    old_pages = (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes()
    original_audit = compiler.audit
    calls = 0

    def fail_once() -> CompilationAudit:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CompilationAudit(0, 0, ("injected audit failure",))
        return original_audit()

    monkeypatch.setattr(compiler, "audit", fail_once)
    monkeypatch.setattr(wiki_restructure, "build_knowledge_service", lambda vault: (None, service))

    with pytest.raises(WoonError, match="final audit failed"):
        apply_wiki_restructure(tmp_path, manifest)

    assert (tmp_path / "wiki/legacy/topic.md").read_bytes() == old_output
    assert not (tmp_path / "wiki/moved/topic.md").exists()
    assert (tmp_path / "wiki/README.md").read_bytes() == old_root
    assert (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes() == old_pages


def _resource_rename_fixture(vault: Path) -> tuple[ResourceFileRename, ManualWikiWrite]:
    current = "private/knowledge/local-only/writing/index.html"
    target = "private/knowledge/local-only/writing/technical-writing-introduction.html"
    raw = vault / current
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"<h1>Technical Writing</h1>\n")
    raw.chmod(0o600)
    catalog = vault / "catalog/sources/writing.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    before = f"source_id: writing\npath: {current}\n".encode()
    after = before.replace(current.encode(), target.encode())
    catalog.write_bytes(before)
    return (
        ResourceFileRename(current, target, hashlib.sha256(raw.read_bytes()).hexdigest()),
        ManualWikiWrite(
            "catalog/sources/writing.yaml",
            hashlib.sha256(before).hexdigest(),
            "catalog/sources/writing.yaml",
            hashlib.sha256(after).hexdigest(),
            after,
        ),
    )


@pytest.mark.parametrize("concurrent_resource_edit", [False, True])
def test_mixed_restructure_restores_manual_move_after_compiler_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent_resource_edit: bool
) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    compiler, service = _compiled_service(tmp_path)
    manual = _write_page(tmp_path, "wiki/manual/notes.md", "manual/notes")
    service.reindex()
    original_manual = manual.read_bytes()
    moved_manual = original_manual.replace(
        b"parent: '[[wiki/README|Vault]]'", b"parent: '[[wiki/moved/topic|topic]]'"
    )
    payload = {
        "version": 2,
        "records": [
            _v2_record(tmp_path, "wiki/README.md", action="keep"),
            _v2_record(
                tmp_path,
                "wiki/legacy/topic.md",
                action="move",
                target_path="wiki/moved/topic.md",
                target_parent="wiki/README.md",
                target_parent_canonical_id="README",
                target_sequence=1,
            ),
        ],
    }
    transaction, _created, _moved = wiki_restructure._v2_compiler_transaction(tmp_path, payload)
    old_compiler_output = (tmp_path / "wiki/legacy/topic.md").read_bytes()
    resource, reference = _resource_rename_fixture(tmp_path)
    raw_before = (tmp_path / resource.current_path).read_bytes()
    reference_before = (tmp_path / reference.current_path).read_bytes()
    original_audit = compiler.audit
    calls = 0

    def fail_once() -> CompilationAudit:
        nonlocal calls
        calls += 1
        if calls == 1:
            if concurrent_resource_edit:
                (tmp_path / resource.target_path).write_bytes(b"user changed the new file")
            return CompilationAudit(0, 0, ("injected audit failure",))
        return original_audit()

    monkeypatch.setattr(compiler, "audit", fail_once)
    with pytest.raises(WoonError, match="final audit failed"):
        service.apply_wiki_restructure_transaction(
            transaction,
            (
                ManualWikiWrite(
                    current_path="wiki/manual/notes.md",
                    current_sha256=hashlib.sha256(original_manual).hexdigest(),
                    target_path="wiki/manual/moved-notes.md",
                    target_sha256=hashlib.sha256(moved_manual).hexdigest(),
                    content=moved_manual,
                ),
            ),
            resource_renames=(resource,),
            resource_reference_writes=(reference,),
        )

    assert manual.read_bytes() == original_manual
    assert not (tmp_path / "wiki/manual/moved-notes.md").exists()
    assert (tmp_path / "wiki/legacy/topic.md").read_bytes() == old_compiler_output
    assert not (tmp_path / "wiki/moved/topic.md").exists()
    assert (tmp_path / resource.current_path).read_bytes() == raw_before
    assert (tmp_path / resource.current_path).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / reference.current_path).read_bytes() == reference_before
    if concurrent_resource_edit:
        assert (tmp_path / resource.target_path).read_bytes() == b"user changed the new file"
    else:
        assert not (tmp_path / resource.target_path).exists()


def test_mixed_restructure_moves_manual_page_with_hash_pinned_bytes(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    _compiler, service = _compiled_service(tmp_path)
    manual = _write_page(tmp_path, "wiki/manual/notes.md", "manual/notes")
    service.reindex()
    original_manual = manual.read_bytes()
    moved_manual = original_manual.replace(
        b"parent: '[[wiki/README|Vault]]'", b"parent: '[[wiki/moved/topic|topic]]'"
    )
    payload = {
        "version": 2,
        "records": [
            _v2_record(tmp_path, "wiki/README.md", action="keep"),
            _v2_record(
                tmp_path,
                "wiki/legacy/topic.md",
                action="move",
                target_path="wiki/moved/topic.md",
                target_parent="wiki/README.md",
                target_parent_canonical_id="README",
                target_sequence=1,
            ),
        ],
    }
    transaction, _created, _moved = wiki_restructure._v2_compiler_transaction(tmp_path, payload)

    resource, reference = _resource_rename_fixture(tmp_path)
    snapshots = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(WoonError, match="resource source SHA-256 changed"):
        service.apply_wiki_restructure_transaction(
            transaction,
            (),
            resource_renames=(replace(resource, current_sha256="0" * 64),),
            resource_reference_writes=(reference,),
        )
    assert all(path.read_bytes() == content for path, content in snapshots.items())
    target = tmp_path / resource.target_path
    target.write_bytes(b"existing user file")
    with pytest.raises(WoonError, match="target must be absent"):
        service.apply_wiki_restructure_transaction(
            transaction,
            (),
            resource_renames=(resource,),
            resource_reference_writes=(reference,),
        )
    assert target.read_bytes() == b"existing user file"
    target.unlink()
    report = service.apply_wiki_restructure_transaction(
        transaction,
        (
            ManualWikiWrite(
                current_path="wiki/manual/notes.md",
                current_sha256=hashlib.sha256(original_manual).hexdigest(),
                target_path="wiki/manual/moved-notes.md",
                target_sha256=hashlib.sha256(moved_manual).hexdigest(),
                content=moved_manual,
            ),
        ),
        resource_renames=(resource,),
        resource_reference_writes=(reference,),
    )

    assert report.manual_written == 1
    assert report.compiler.page_ids == ("legacy/topic",)
    assert not manual.exists()
    metadata, _body = split_markdown(
        (tmp_path / "wiki/manual/moved-notes.md").read_text(encoding="utf-8")
    )
    assert metadata["parent"] == "[[wiki/moved/topic|topic]]"
    assert (tmp_path / "wiki/moved/topic.md").is_file()
    assert report.resources_renamed == report.resource_references_written == 1
    assert not (tmp_path / resource.current_path).exists()
    assert hashlib.sha256(target.read_bytes()).hexdigest() == resource.current_sha256
    assert target.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / reference.current_path).read_bytes() == reference.content


@pytest.mark.parametrize("failure", [None, "index", "shared"])
def test_resource_rename_preserves_curated_history_in_same_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    compiler, service = _compiled_service(tmp_path)
    resource, reference = _resource_rename_fixture(tmp_path)
    body = f"[자료]({resource.current_path})\n"
    compiler.curate_revisions((CuratedRevision("legacy/topic", body, "검토한 자료를 찾는다."),))
    service.reindex()
    sources, claims, pages, curations, _ = compiler._load_inputs()
    old_page = pages["legacy/topic"]
    old_source_id = old_page["render"]["source_id"]
    old_claim_id = next(
        key for key in old_page["claim_ids"] if claims[key]["source_ids"] == [old_source_id]
    )
    old_source, old_claim = deepcopy(sources[old_source_id]), deepcopy(claims[old_claim_id])
    revised = body.replace(resource.current_path, resource.target_path)
    digest = hashlib.sha256(_normalize(revised).encode()).hexdigest()
    source_id = f"source://curated-wiki/legacy/topic/{digest[:24]}"
    claim_id = f"claim://curated-wiki/legacy/topic/{digest[:24]}"
    source = dict(
        old_source,
        source_id=source_id,
        body=revised,
        locator=f"curation/legacy/topic/{digest[:24]}",
        original_sha256=hashlib.sha256(revised.encode()).hexdigest(),
        normalized_sha256=digest,
    )
    claim = dict(old_claim, claim_id=claim_id, source_ids=[source_id])
    page = deepcopy(old_page)
    page["source_ids"] = [source_id if key == old_source_id else key for key in page["source_ids"]]
    page["claim_ids"] = [claim_id if key == old_claim_id else key for key in page["claim_ids"]]
    page["render"]["source_id"] = source_id
    if failure == "shared":
        claims["claim://shared"] = dict(old_claim, claim_id="claim://shared")
        compiler._write_inputs(sources, claims, pages, curations)
    transaction = CompiledWikiTransaction(
        expected_revisions={"legacy/topic": service.get("legacy/topic").revision},
        expected_catalog_revision=compiler.catalog_revision(),
        expected_page_spec_sha256={"legacy/topic": _canonical_record_sha256(old_page)},
        sources_upsert=(source,),
        claims_upsert=(claim,),
        pages_upsert=(page,),
        curations_upsert=(curations["legacy/topic"],),
        curated_successor_page_ids=("legacy/topic",),
    )
    inputs_before, outputs_before = compiler.snapshot_inputs(), compiler.snapshot_outputs()
    raw_before = (tmp_path / resource.current_path).read_bytes()
    reference_before = (tmp_path / reference.current_path).read_bytes()
    if failure == "index":
        reindex = service._reindex_unlocked
        calls = 0

        def fail_once() -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise WoonError("injected resource successor index failure")
            return reindex()

        monkeypatch.setattr(service, "_reindex_unlocked", fail_once)
    if failure:
        message = "index failure" if failure == "index" else "unshared owner"
        with pytest.raises(WoonError, match=message):
            service.apply_wiki_restructure_transaction(
                transaction,
                (),
                resource_renames=(resource,),
                resource_reference_writes=(reference,),
            )
        assert compiler.snapshot_inputs() == inputs_before
        assert compiler.snapshot_outputs() == outputs_before
        assert (tmp_path / resource.current_path).read_bytes() == raw_before
        assert not (tmp_path / resource.target_path).exists()
        assert (tmp_path / reference.current_path).read_bytes() == reference_before
        return
    report = service.apply_wiki_restructure_transaction(
        transaction, (), resource_renames=(resource,), resource_reference_writes=(reference,)
    )
    sources, claims, pages, _, _ = compiler._load_inputs()
    assert sources[old_source_id] == dict(old_source, lifecycle="archived", superseded_by=source_id)
    assert claims[old_claim_id] == dict(old_claim, status="superseded", superseded_by=claim_id)
    assert pages["legacy/topic"]["render"]["source_id"] == source_id
    assert report.compiler.source_revisions == report.compiler.claim_revisions == 1
    assert (tmp_path / resource.target_path).read_bytes() == raw_before
    assert compiler.audit().complete


_V3_REQUIRED_CHECKS = [
    "frontmatter",
    "links",
    "privacy",
    "structure",
    "deterministic-output",
]


def _set_publication_state(path: Path, state: str = "private") -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "knowledge_state: 확인 필요\n",
            f"publication_state: {state}\nknowledge_state: 확인 필요\n",
        ),
        encoding="utf-8",
    )


def _set_title(path: Path, title: str) -> None:
    metadata, _body = split_markdown(path.read_text(encoding="utf-8"))
    previous = metadata["title"]
    assert isinstance(previous, str)
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(f"title: {previous}\n", f"title: {title}\n", 1)
        .replace(f"# {path.stem}\n", f"# {title}\n", 1),
        encoding="utf-8",
    )


def _v3_record(
    vault: Path,
    relative: str,
    *,
    action: str,
    target_path: str | None = None,
    target_parent: str | None = None,
    target_parent_canonical_id: str | None = None,
    target_sequence: int | None = None,
) -> dict[str, object]:
    source = vault / relative
    metadata, _body = split_markdown(source.read_text(encoding="utf-8"))
    pages_payload = yaml.safe_load(
        (vault / "catalog/llm-wiki/pages.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(pages_payload, dict)
    rows = pages_payload.get("pages")
    assert isinstance(rows, list)
    page = next(
        (
            row
            for row in rows
            if isinstance(row, dict) and row.get("output_path") == relative.removeprefix("wiki/")
        ),
        None,
    )
    record: dict[str, object] = {
        "action": action,
        "current_path": relative,
        "current_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "canonical_id": metadata["canonical_id"],
        "source_owner": "compiler" if page is not None else "manual",
        "page_id": page["page_id"] if isinstance(page, dict) else None,
        "page_spec_sha256": _canonical_record_sha256(page) if isinstance(page, dict) else None,
        "publication_state": metadata["publication_state"],
        "status": "ready",
        "content_class": "topic",
        "schemas_required": [],
        "required_checks": list(_V3_REQUIRED_CHECKS),
    }
    if target_path is not None:
        record["target_path"] = target_path
    if target_parent is not None:
        record["target_parent"] = target_parent
    if target_parent_canonical_id is not None:
        record["target_parent_canonical_id"] = target_parent_canonical_id
    if target_sequence is not None:
        record["target_sequence"] = target_sequence
    return record


def _v3_map_node(
    canonical_id: str,
    *,
    node_key: str,
    title: str,
    parent: str,
    parent_title: str,
    parent_canonical_id: str,
    sequence: int,
    children: list[str],
) -> dict[str, object]:
    target_path = f"wiki/{node_key}/README.md"
    page_id = f"{canonical_id}/README"
    page = {
        "page_id": page_id,
        "output_path": target_path.removeprefix("wiki/"),
        "title": title,
        "frontmatter": {
            "type": "Wiki",
            "canonical_id": canonical_id,
            "title": title,
            "node_kind": "hub",
            "parent": f"[[{parent.removesuffix('.md')}|{parent_title}]]",
            "sequence": sequence,
            "publication_state": "private",
            "keywords": [title],
            "aliases": [],
            "view_mode": "tree",
            "updated": "2026-09-05",
            "summary": "개발 지식을 학습용 키워드로 재구성하는 허브입니다.",
            "knowledge_state": "확인 필요",
            "navigation_groups": [{"label": "학습 주제", "children": children}],
        },
        "source_ids": [],
        "claim_ids": [],
        "render": {"kind": "toc-only"},
        "output_path_migration": True,
    }
    return {
        "action": "create",
        "node_key": node_key,
        "canonical_id": canonical_id,
        "page_id": page_id,
        "source_owner": "compiler",
        "page_spec_sha256": _canonical_record_sha256(page),
        "target_path": target_path,
        "target_parent": parent,
        "target_parent_canonical_id": parent_canonical_id,
        "target_sequence": sequence,
        "publication_state": "private",
        "status": "ready",
        "content_class": "map",
        "schemas_required": ["navigation-map"],
        "required_checks": list(_V3_REQUIRED_CHECKS),
        "page_spec": page,
        "curation": {
            "page_id": page_id,
            "current_use": "개발 학습 키워드의 탐색 시작점으로 사용한다.",
            "basis": "manual-review",
            "status": "confirmed",
        },
    }


def _v3_hub_node() -> dict[str, object]:
    return _v3_map_node(
        "Wiki",
        node_key="Wiki",
        title="Wiki",
        parent="wiki/README.md",
        parent_title="README",
        parent_canonical_id="README",
        sequence=1,
        children=["legacy/topic", "legacy/ref", "manual/notes"],
    )


def _v3_manifest(
    vault: Path,
    *,
    records: list[dict[str, object]],
    nodes: list[dict[str, object]],
    link_rewrites: list[dict[str, object]],
) -> dict[str, object]:
    base = vault / "v3-base-inventory.yaml"
    base.write_text("v3 structural snapshot\n", encoding="utf-8")
    contract = vault / "docs/wiki-information-architecture.md"
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("# Approved keyword tree\n", encoding="utf-8")
    return {
        "kind": "wiki-restructure-apply",
        "version": 3,
        "base_inventory": {
            "path": base.relative_to(vault).as_posix(),
            "sha256": hashlib.sha256(base.read_bytes()).hexdigest(),
            "document_count": len(tuple(wiki_restructure.iter_wiki_pages(vault / "wiki"))),
            "active_tree_sha256": wiki_restructure._v3_active_tree_sha256(vault),
        },
        "target_contract": {
            "path": contract.relative_to(vault).as_posix(),
            "sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        },
        "map_id_policy": {
            "normalization": "NFC",
            "map_key": "<NFC target label hierarchy>",
            "map_path": "wiki/<node_key>/README.md",
            "map_page_id": "<canonical_id>/README",
        },
        "records": records,
        "nodes": nodes,
        "link_rewrites": link_rewrites,
    }


def _v3_merge_retirement_fixture(
    vault: Path, *, manual_action: str = "retire"
) -> tuple[CompiledWiki, KnowledgeService, dict[str, object]]:
    root = _write_page(vault, "wiki/README.md", "README")
    old = _write_page(vault, "wiki/legacy/old.md", "legacy/old")
    ref = _write_page(vault, "wiki/legacy/ref.md", "legacy/ref")
    for page in (root, old, ref):
        _set_publication_state(page)
    old.write_text(
        old.read_text(encoding="utf-8").replace(
            "knowledge_state:", "sequence: 2\nknowledge_state:"
        ),
        encoding="utf-8",
    )
    ref.write_text(
        ref.read_text(encoding="utf-8").replace(
            "knowledge_state:", "sequence: 1\nknowledge_state:"
        ),
        encoding="utf-8",
    )
    ref.write_text(
        ref.read_text(encoding="utf-8") + "\n[[wiki/legacy/old|old]]\n", encoding="utf-8"
    )
    compiler, service = _compiled_service(vault)
    manual = _write_page(vault, "wiki/manual/retired.md", "manual/retired")
    _set_publication_state(manual)
    manual.write_text(
        manual.read_text(encoding="utf-8") + "\n[[wiki/legacy/old|old]]\n", encoding="utf-8"
    )
    service.reindex()
    pages, _curations = wiki_restructure._compiler_catalog_records(vault)
    old_page = pages["legacy/old"]
    ref_page = pages["legacy/ref"]
    candidate = deepcopy(old_page)
    candidate.update(
        {
            "page_id": "learning/new",
            "output_path": "학습/새 주제.md",
            "output_path_migration": True,
            "title": "새 주제",
            "source_ids": list(ref_page["source_ids"]),
            "claim_ids": list(ref_page["claim_ids"]),
            "render": {"kind": "source-body", "source_id": ref_page["source_ids"][0]},
        }
    )
    frontmatter = candidate["frontmatter"]
    assert isinstance(frontmatter, dict)
    frontmatter.update(
        {
            "canonical_id": "learning/new",
            "title": "새 주제",
            "parent": "[[wiki/학습/README|학습]]",
        }
    )
    old_record = _v3_record(vault, "wiki/legacy/old.md", action="merge")
    old_record.update(
        {
            "status": "pending-successor",
            "link_successor": "wiki/학습/새 주제",
            "successor_canonical_id": "learning/new",
            "successor_page_id": "learning/new",
            "target_page_spec": candidate,
            "target_page_spec_sha256": _canonical_record_sha256(candidate),
            "target_curation": {
                "page_id": "learning/new",
                "current_use": "새 학습 경계의 정본 페이지다.",
                "basis": "manual-review",
                "status": "confirmed",
            },
        }
    )
    manual_record = _v3_record(vault, "wiki/manual/retired.md", action=manual_action)
    if manual_action == "retire":
        manual_record.update(
            {
                "status": "pending-successor",
                "link_successor": "wiki/학습/새 주제",
                "successor_canonical_id": "learning/new",
            }
        )
    compiler_ref = vault / "wiki/legacy/ref.md"
    payload = _v3_manifest(
        vault,
        records=[
            _v3_record(vault, "wiki/README.md", action="keep"),
            old_record,
            _v3_record(vault, "wiki/legacy/ref.md", action="keep"),
            manual_record,
        ],
        nodes=[
            _v3_map_node(
                "map/learning",
                node_key="학습",
                title="학습",
                parent="wiki/README.md",
                parent_title="README",
                parent_canonical_id="README",
                sequence=3,
                children=["learning/new"],
            )
        ],
        link_rewrites=[
            {
                "current_target": "wiki/legacy/old",
                "replacement_target": "wiki/학습/새 주제",
                "expected_source_occurrences": 1,
                "expected_claim_occurrences": 0,
                "compiler_referrers": [
                    {
                        "current_path": "wiki/legacy/ref.md",
                        "page_id": "legacy/ref",
                        "current_sha256": hashlib.sha256(compiler_ref.read_bytes()).hexdigest(),
                    }
                ],
                "manual_referrers": [],
            }
        ],
    )
    return compiler, service, payload


def test_v3_preflight_rejects_quality_contracts_and_missing_exact_rewrite(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    topic = _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    for page in (root, topic):
        _set_publication_state(page)
    _compiler, _service = _compiled_service(tmp_path)
    manual = _write_page(tmp_path, "wiki/manual/notes.md", "manual/notes")
    _set_publication_state(manual)
    manual.write_text(
        manual.read_text(encoding="utf-8") + "\n[[wiki/legacy/topic|legacy topic]]\n",
        encoding="utf-8",
    )
    records = [
        _v3_record(tmp_path, "wiki/README.md", action="keep"),
        _v3_record(
            tmp_path,
            "wiki/legacy/topic.md",
            action="move",
            target_path="wiki/Wiki/topic.md",
            target_parent="wiki/Wiki/README.md",
            target_parent_canonical_id="Wiki",
            target_sequence=1,
        ),
        _v3_record(
            tmp_path,
            "wiki/manual/notes.md",
            action="move",
            target_path="wiki/Wiki/notes.md",
            target_parent="wiki/Wiki/README.md",
            target_parent_canonical_id="Wiki",
            target_sequence=2,
        ),
    ]
    records[0]["learning_mode"] = "run"  # must remain in the quality receipt, not this tree plan.
    invalid_payload = tmp_path / "payloads/notes.md"
    invalid_payload.parent.mkdir()
    invalid_payload.write_bytes(manual.read_bytes())
    records[-1]["manual_payload"] = {
        "path": "payloads/notes.md",
        "sha256": "0" * 64,
    }
    records[-1]["target_sha256"] = "0" * 64
    payload = _v3_manifest(tmp_path, records=records, nodes=[_v3_hub_node()], link_rewrites=[])
    manifest = tmp_path / "restructure-v3.yaml"
    manifest.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert any(
        "quality, execution, and deployment fields belong to separate receipts" in issue
        for issue in report.issues
    )
    assert "link_rewrites omit wiki/legacy/topic -> wiki/Wiki/topic" in report.issues
    assert "records[3]: manual payload SHA-256 does not match target_sha256" in report.issues


def test_v3_apply_rewrites_compiler_provenance_and_hash_pinned_manual_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    topic = _write_page(tmp_path, "wiki/legacy/topic.md", "legacy/topic")
    referrer = _write_page(tmp_path, "wiki/legacy/ref.md", "legacy/ref")
    for page in (root, topic, referrer):
        _set_publication_state(page)
    _set_title(topic, "프로그래밍 기초")
    _set_title(referrer, "타입 시스템")
    referrer.write_text(
        referrer.read_text(encoding="utf-8") + "\n[[wiki/legacy/topic|legacy topic]]\n",
        encoding="utf-8",
    )
    compiler, service = _compiled_service(tmp_path)
    manual = _write_page(tmp_path, "wiki/manual/notes.md", "manual/notes")
    _set_publication_state(manual)
    _set_title(manual, "함수·호출")
    manual.write_text(
        manual.read_text(encoding="utf-8") + "\n[[wiki/legacy/topic|legacy topic]]\n",
        encoding="utf-8",
    )
    service.reindex()
    manual_payload = (
        manual.read_text(encoding="utf-8")
        .replace(
            "[[wiki/README|Vault]]",
            "[[wiki/개발 검증/README|개발 검증]]",
        )
        .replace(
            "[[wiki/legacy/topic|legacy topic]]",
            "[[wiki/개발 검증/프로그래밍 기초|legacy topic]]",
        )
        .replace("knowledge_state: 확인 필요\n", "sequence: 3\nknowledge_state: 확인 필요\n")
    ).encode("utf-8")
    payload_path = tmp_path / "payloads/notes.md"
    payload_path.parent.mkdir()
    payload_path.write_bytes(manual_payload)

    records = [
        _v3_record(tmp_path, "wiki/README.md", action="keep"),
        _v3_record(
            tmp_path,
            "wiki/legacy/topic.md",
            action="move",
            target_path="wiki/개발 검증/프로그래밍 기초.md",
            target_parent="wiki/개발 검증/README.md",
            target_parent_canonical_id="map/development-verification",
            target_sequence=1,
        ),
        _v3_record(
            tmp_path,
            "wiki/legacy/ref.md",
            action="move",
            target_path="wiki/개발 검증/타입 시스템.md",
            target_parent="wiki/개발 검증/README.md",
            target_parent_canonical_id="map/development-verification",
            target_sequence=2,
        ),
        _v3_record(
            tmp_path,
            "wiki/manual/notes.md",
            action="move",
            target_path="wiki/개발 검증/함수·호출.md",
            target_parent="wiki/개발 검증/README.md",
            target_parent_canonical_id="map/development-verification",
            target_sequence=3,
        ),
    ]
    records[-1]["manual_payload"] = {
        "path": "payloads/notes.md",
        "sha256": hashlib.sha256(manual_payload).hexdigest(),
    }
    records[-1]["target_sha256"] = hashlib.sha256(manual_payload).hexdigest()
    compiler_ref = tmp_path / "wiki/legacy/ref.md"
    link_rewrites = [
        {
            "current_target": "wiki/legacy/topic",
            "replacement_target": "wiki/개발 검증/프로그래밍 기초",
            "expected_source_occurrences": 1,
            "expected_claim_occurrences": 0,
            "compiler_referrers": [
                {
                    "current_path": "wiki/legacy/ref.md",
                    "page_id": "legacy/ref",
                    "current_sha256": hashlib.sha256(compiler_ref.read_bytes()).hexdigest(),
                }
            ],
            "manual_referrers": [
                {
                    "current_path": "wiki/manual/notes.md",
                    "current_sha256": hashlib.sha256(manual.read_bytes()).hexdigest(),
                    "expected_occurrences": 1,
                }
            ],
        }
    ]
    payload = _v3_manifest(
        tmp_path,
        records=records,
        nodes=[
            _v3_map_node(
                "map/development-verification",
                node_key="개발 검증",
                title="개발 검증",
                parent="wiki/README.md",
                parent_title="README",
                parent_canonical_id="README",
                sequence=1,
                children=["legacy/topic", "legacy/ref", "manual/notes"],
            ),
        ],
        link_rewrites=link_rewrites,
    )
    manifest = tmp_path / "restructure-v3.yaml"
    manifest.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(wiki_restructure, "build_knowledge_service", lambda vault: (None, service))

    report = apply_wiki_restructure(tmp_path, manifest)

    assert report.manual_written == 1
    assert report.source_revisions == 1
    assert (
        report.claim_revisions >= 1
    )  # referrer claim follows its source successor even without text link.
    assert not (tmp_path / "wiki/legacy/topic.md").exists()
    assert not (tmp_path / "wiki/manual/notes.md").exists()
    moved_manual = (tmp_path / "wiki/개발 검증/함수·호출.md").read_text(encoding="utf-8")
    assert "[[wiki/개발 검증/프로그래밍 기초|legacy topic]]" in moved_manual
    assert "[[wiki/legacy/topic|legacy topic]]" not in moved_manual
    referrer_text = (tmp_path / "wiki/개발 검증/타입 시스템.md").read_text(encoding="utf-8")
    assert "[[wiki/개발 검증/프로그래밍 기초|legacy topic]]" in referrer_text
    assert "source://wiki-restructure-revision/" in (
        tmp_path / "catalog/llm-wiki/sources.yaml"
    ).read_text(encoding="utf-8")
    assert "claim://wiki-restructure-revision/" in (
        tmp_path / "catalog/llm-wiki/claims.yaml"
    ).read_text(encoding="utf-8")
    assert compiler.audit().complete


def test_v3_merge_retires_compiler_predecessor_and_manual_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, payload = _v3_merge_retirement_fixture(tmp_path)
    manifest = tmp_path / "merge-v3.yaml"
    manifest.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(wiki_restructure, "build_knowledge_service", lambda vault: (None, service))

    report = apply_wiki_restructure(tmp_path, manifest)

    assert report.pages_retired == 1
    assert report.manual_written == 1
    assert not (tmp_path / "wiki/legacy/old.md").exists()
    assert not (tmp_path / "wiki/manual/retired.md").exists()
    assert (tmp_path / "wiki/학습/새 주제.md").is_file()
    pages = yaml.safe_load((tmp_path / "catalog/llm-wiki/pages.yaml").read_text("utf-8"))
    receipts = yaml.safe_load((tmp_path / "catalog/llm-wiki/receipts.yaml").read_text("utf-8"))
    assert "legacy/old" not in {page["page_id"] for page in pages["pages"]}
    assert "legacy/old" not in {receipt["page_id"] for receipt in receipts["receipts"]}
    assert "[[wiki/학습/새 주제|old]]" in (tmp_path / "wiki/legacy/ref.md").read_text("utf-8")
    assert compiler.audit().complete


@pytest.mark.parametrize("case", ["stale", "repair", "concurrent"])
def test_retirement_validates_proposed_tree_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from dataclasses import replace

    import woon_core.knowledge.compiled_wiki as compiled_module
    import woon_core.knowledge.service as service_module
    import woon_core.knowledge.wiki_tree as tree_module

    unrelated = _write_page(tmp_path, "wiki/appendix/stale.md", "appendix/stale")
    unrelated.write_text(
        unrelated.read_text(encoding="utf-8").replace(
            "knowledge_state:", "sequence: 4\nknowledge_state:"
        ),
        encoding="utf-8",
    )
    compiler, service, payload = _v3_merge_retirement_fixture(tmp_path)
    plan = wiki_restructure._v3_transaction_plan(tmp_path, payload, tmp_path / "merge.yaml")
    # The catalog is already correct; only the unselected output has a retired parent.
    metadata, body = tree_module.split_markdown(unrelated.read_text(encoding="utf-8"))
    metadata["parent"] = "[[wiki/appendix/retired]]"
    unrelated.write_text(tree_module.render_markdown(metadata, body), encoding="utf-8")
    _, _, pages, curations, _ = compiler._load_inputs()
    assert "retired" not in pages["appendix/stale"]["frontmatter"]["parent"]
    transaction = replace(plan.transaction, allow_preexisting_audit_errors=True)
    if case != "stale":
        transaction = replace(
            transaction,
            pages_upsert=(*transaction.pages_upsert, pages["appendix/stale"]),
            curations_upsert=(*transaction.curations_upsert, curations["appendix/stale"]),
            expected_revisions={
                **transaction.expected_revisions,
                "appendix/stale": hashlib.sha256(unrelated.read_bytes()).hexdigest(),
            },
            expected_page_spec_sha256={
                **transaction.expected_page_spec_sha256,
                "appendix/stale": _canonical_record_sha256(pages["appendix/stale"]),
            },
        )
    service.reindex()
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    prepared = []
    prepare = compiled_module.prepare_wiki_tree_refresh

    def track_prepare(*args, **kwargs):
        report = prepare(*args, **kwargs)
        prepared.append(report)
        if case == "concurrent":
            unrelated.write_bytes(unrelated.read_bytes() + b"\nConcurrent user edit.\n")
            before[unrelated] = (unrelated.read_bytes(), unrelated.stat().st_mtime_ns)
        return report

    monkeypatch.setattr(compiled_module, "prepare_wiki_tree_refresh", track_prepare)
    if case != "repair":

        def unexpected_write(*args, **kwargs):
            pytest.fail("invalid proposed tree must not write, delete, roll back, or reindex")

        for module in (compiled_module, service_module, tree_module):
            monkeypatch.setattr(module, "atomic_write", unexpected_write)
        monkeypatch.setattr(Path, "unlink", unexpected_write)
        monkeypatch.setattr(compiler, "restore_inputs", unexpected_write)
        monkeypatch.setattr(compiler, "restore_outputs", unexpected_write)
        monkeypatch.setattr(service, "_reindex_unlocked", unexpected_write)
        message = (
            "before write: .*parent is missing" if case == "stale" else "changed after preparation"
        )
        with pytest.raises(WoonError, match=message):
            service.apply_wiki_restructure_transaction(transaction, plan.manual_writes)
        assert before == {
            p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()
        }
    else:
        result = service.apply_wiki_restructure_transaction(transaction, plan.manual_writes)
        assert result.compiler.pages_retired == 1
        assert not (tmp_path / "wiki/legacy/old.md").exists()
        assert not (tmp_path / "wiki/manual/retired.md").exists()
        assert (tmp_path / "wiki/학습/새 주제.md").is_file()
        assert "retired" not in tree_module.split_markdown(unrelated.read_text())[0]["parent"]
        assert compiler.audit().complete
    assert len(prepared) == 1  # Reuse the same tree plan after writing; no repeated validation.


def test_v3_merge_rejects_missing_final_successor_and_live_manual_inbound(tmp_path: Path) -> None:
    _compiler, _service, payload = _v3_merge_retirement_fixture(tmp_path, manual_action="keep")
    records = payload["records"]
    assert isinstance(records, list)
    old = records[1]
    assert isinstance(old, dict)
    old["successor_page_id"] = "learning/missing"
    manifest = tmp_path / "merge-invalid-v3.yaml"
    manifest.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    preflight = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert preflight.issues
    assert any("manual_referrers" in issue for issue in preflight.issues)
    assert (tmp_path / "wiki/legacy/old.md").is_file()


def test_v3_merge_rolls_back_on_final_audit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, payload = _v3_merge_retirement_fixture(tmp_path)
    manifest = tmp_path / "merge-rollback-v3.yaml"
    manifest.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    old_output = (tmp_path / "wiki/legacy/old.md").read_bytes()
    old_pages = (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes()
    original_audit = compiler.audit
    calls = 0

    def fail_once() -> CompilationAudit:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CompilationAudit(0, 0, ("injected audit failure",))
        return original_audit()

    monkeypatch.setattr(compiler, "audit", fail_once)
    monkeypatch.setattr(wiki_restructure, "build_knowledge_service", lambda vault: (None, service))
    with pytest.raises(WoonError, match="final audit failed"):
        apply_wiki_restructure(tmp_path, manifest)

    assert (tmp_path / "wiki/legacy/old.md").read_bytes() == old_output
    assert (tmp_path / "wiki/manual/retired.md").is_file()
    assert not (tmp_path / "wiki/학습/새 주제.md").exists()
    assert (tmp_path / "catalog/llm-wiki/pages.yaml").read_bytes() == old_pages


@pytest.mark.parametrize(
    "allow_preexisting, introduce_error", [(False, False), (True, False), (True, True)]
)
def test_retirement_preserves_unrelated_empty_claim_error(
    tmp_path: Path, allow_preexisting: bool, introduce_error: bool
) -> None:
    from woon_core.knowledge.compiled_wiki import (
        CompiledWikiPageRetirement,
        CompiledWikiTransaction,
        CompiledWikiWikilinkRewrite,
        _count_exact_wikilinks,
        _sha256_canonical_json,
    )

    unrelated = _write_page(tmp_path, "wiki/private/unrelated.md", "private/unrelated")
    compiler, service, _payload = _v3_merge_retirement_fixture(tmp_path)
    catalog_path = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    unrelated_page = next(p for p in catalog["pages"] if p["page_id"] == "private/unrelated")
    unrelated_page["claim_ids"] = []
    catalog_path.write_text(yaml.safe_dump(catalog, allow_unicode=True), encoding="utf-8")
    before = unrelated.read_bytes()
    baseline = compiler.audit().errors
    assert baseline
    sources, claims, pages, curations, receipts = compiler._load_inputs()
    old, survivor = pages["legacy/old"], pages["legacy/ref"]
    source_ids = survivor["source_ids"]
    claim_ids = survivor["claim_ids"]
    target = "wiki/legacy/old"
    source_count = sum(_count_exact_wikilinks(sources[i]["body"], target) for i in source_ids)
    claim_count = sum(
        _count_exact_wikilinks(claims[i]["markdown"], target)
        + _count_exact_wikilinks(claims[i]["statement"], target)
        for i in claim_ids
    )
    if introduce_error:
        survivor = deepcopy(survivor)
        survivor["claim_ids"] = []
    transaction = CompiledWikiTransaction(
        expected_revisions={"legacy/ref": service._repository.get("legacy/ref").revision},
        expected_page_spec_sha256={"legacy/ref": _sha256_canonical_json(pages["legacy/ref"])},
        sources_upsert=(),
        claims_upsert=(),
        pages_upsert=(survivor,),
        curations_upsert=(curations["legacy/ref"],),
        wikilink_rewrites=(
            CompiledWikiWikilinkRewrite(target, "wiki/legacy/ref", source_count, claim_count),
        ),
        expected_source_record_sha256={i: _sha256_canonical_json(sources[i]) for i in source_ids},
        expected_claim_record_sha256={i: _sha256_canonical_json(claims[i]) for i in claim_ids},
        page_retirements=(
            CompiledWikiPageRetirement(
                "legacy/old",
                "legacy/ref",
                target,
                hashlib.sha256((tmp_path / "wiki/legacy/old.md").read_bytes()).hexdigest(),
                _sha256_canonical_json(old),
                _sha256_canonical_json(receipts["legacy/old"]),
            ),
        ),
        allow_preexisting_audit_errors=allow_preexisting,
    )
    manual_path = tmp_path / "wiki/manual/retired.md"
    manual = (
        ManualWikiWrite(
            "wiki/manual/retired.md",
            hashlib.sha256(manual_path.read_bytes()).hexdigest(),
            None,
            None,
            None,
        ),
    )
    if allow_preexisting and not introduce_error:
        result = service.apply_wiki_restructure_transaction(transaction, manual)
        assert result.compiler.pages_retired == 1
        assert not (tmp_path / "wiki/legacy/old.md").exists()
        assert "[[wiki/legacy/ref|old]]" in (tmp_path / "wiki/legacy/ref.md").read_text()
        assert set(compiler.audit().errors) <= set(baseline)
        assert any(
            "private/unrelated: page claim_ids" in error for error in compiler.audit().errors
        )
    else:
        with pytest.raises(
            WoonError,
            match="page claim_ids" if introduce_error else "final audit failed",
        ):
            service.apply_wiki_restructure_transaction(transaction, manual)
        assert (tmp_path / "wiki/legacy/old.md").exists()
        assert manual_path.exists()
    assert unrelated.read_bytes() == before
    after_catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    assert (
        next(p for p in after_catalog["pages"] if p["page_id"] == "private/unrelated")
        == unrelated_page
    )
