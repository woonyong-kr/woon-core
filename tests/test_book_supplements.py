from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
from test_book_coverage import (
    _upgrade_manifest_to_v7,
    _verified_fixture,
    _write_verified_root_map,
)
from test_compiled_wiki import FailOnceIndex, compiled_settings

import woon_core.cli as cli
from woon_core.errors import WoonError
from woon_core.knowledge.adapters import GitKnowledgeHistory, MarkdownDocumentRepository
from woon_core.knowledge.book_coverage import audit_book_coverage_scope
from woon_core.knowledge.compiled_wiki import (
    BookCoverageManifestUpdate,
    CompiledWiki,
    CompiledWikiTransaction,
)
from woon_core.knowledge.service import KnowledgeService


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _record_sha(record: object) -> str:
    return _sha(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )


def _fixture(vault: Path, *, failure: str = ""):
    base_path, manifest = _verified_fixture(vault)
    _write_verified_root_map(vault)
    _upgrade_manifest_to_v7(vault, manifest, workflow_phase="translated")
    (vault / "wiki/private/_sources").rename(vault / "private")
    archive = manifest["source_archive"]
    archive["relative_path"] = archive["relative_path"].replace(
        "wiki/private/_sources/", "private/", 1
    )
    base_path.write_text(json.dumps(manifest))
    owner = "books/kotlin/chapter-01"
    scope_path = vault / "catalog/book-coverage-scopes/kotlin/chapter-01.json"
    manifest["coverage_scope"] = {
        "root_id": owner,
        "base_relative_path": base_path.relative_to(vault).as_posix(),
        "base_sha256": _sha(base_path.read_bytes()),
    }
    scope_path.parent.mkdir(parents=True)
    scope_path.write_text(json.dumps(manifest))
    compiler = CompiledWiki(compiled_settings(vault))
    compiler.migrate()
    index = FailOnceIndex(vault / ".local/search.sqlite3")
    service = KnowledgeService(
        MarkdownDocumentRepository(vault, vault / "wiki"),
        index,
        GitKnowledgeHistory(vault),
        compiled_wiki=compiler,
    )
    service.reindex()
    baseline = audit_book_coverage_scope(vault, scope_path.relative_to(vault).as_posix())
    assert baseline.complete, baseline.errors
    _, _, pages, curations, _ = compiler._load_inputs()
    page = copy.deepcopy(pages[owner])
    code = "fun main() = println(3)\n"
    body = f"### 검토한 보충 예제\n\n```run-kotlin\n{code}```\n"
    if failure == "unclassified":
        body += "\n```run-kotlin\nfun main() = println(4)\n```\n"
    digest = _sha(body.encode())
    source_id = f"source://curated-wiki/{owner}/{digest[:24]}"
    claim_id = f"claim://curated-wiki/{owner}/{digest[:24]}-supplement"
    source = {
        "source_id": source_id,
        "kind": "curated-wiki",
        "locator": "source://supplement/example",
        "original_sha256": digest,
        "normalized_sha256": digest,
        "body": body,
        "privacy": "local-only",
        "lifecycle": "compiled",
        "purpose": "검토한 보충 설명이다.",
    }
    claim = {
        "claim_id": claim_id,
        "kind": "curated-document",
        "status": "accepted",
        "statement": "별도로 검토한 실행 예제다.",
        "source_ids": [source_id],
        "markdown": body,
    }
    page["source_ids"].append(source_id)
    page["claim_ids"].append(claim_id)
    page["render"]["supplemental_claim_ids"] = [claim_id]
    receipt_path = vault / "private/supplements/execution.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "key": "supplement",
                        "code_sha256": _sha(code.encode()),
                        "compile_exit": 1 if failure == "negative-execution" else 0,
                        "run_exit": 0,
                        "stdout": "3\n",
                        "stdout_sha256": _sha(b"3\n"),
                    }
                ]
            }
        )
    )
    wrapper_path = receipt_path.with_name("wrapper.json")
    wrapper_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "local",
                "external_transmission": False,
                "execution_receipt_relative_path": receipt_path.relative_to(vault).as_posix(),
                "execution_receipt_sha256": _sha(receipt_path.read_bytes()),
                "verification": "Existing local execution; no new run.",
            }
        )
    )
    replacement = copy.deepcopy(manifest)
    replacement["supplemental_runnables"] = [
        {
            "owner_id": owner,
            "source_id": source_id,
            "source_record_sha256": _record_sha(source),
            "claim_id": claim_id,
            "claim_record_sha256": _record_sha(claim),
            "run_language": "run-kotlin",
            "run_block_index": 1 if failure == "overlap" else 3,
            "code_sha256": _sha(code.encode()),
            "verification_case": "supplement",
            "verification_evidence": wrapper_path.relative_to(vault).as_posix(),
            "verification_sha256": _sha(wrapper_path.read_bytes()),
        }
    ]
    if failure == "source-pin":
        replacement["supplemental_runnables"][0]["source_record_sha256"] = "0" * 64
    if failure == "original-count":
        replacement["nodes"][0]["runnable"] = {"expected": 3, "verified": 3}
    update = BookCoverageManifestUpdate(
        relative_path=scope_path.relative_to(vault).as_posix(),
        expected_sha256=_sha(scope_path.read_bytes()),
        replacement=replacement,
        mode="merge-scope",
        base_relative_path=base_path.relative_to(vault).as_posix(),
        base_expected_sha256=_sha(base_path.read_bytes()),
        scope_root_id=owner,
    )
    transaction = CompiledWikiTransaction(
        expected_revisions={owner: service.get(owner).revision},
        sources_upsert=(source,),
        claims_upsert=(claim,),
        pages_upsert=(page,),
        curations_upsert=(curations[owner],),
        expected_page_spec_sha256={owner: _record_sha(pages[owner])},
        expected_catalog_revision=compiler.catalog_revision(),
        coverage_manifest=update,
    )
    return compiler, service, index, transaction, scope_path, base_path


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("", ""),
        ("index", "injected index failure"),
        ("overlap", "overlaps an original"),
        ("unclassified", "unassigned or missing"),
        ("negative-execution", "successful compile/run/stdout"),
        ("source-pin", "exact accepted, local-only provenance"),
        ("original-count", "preserve the translated scope and original counts"),
        ("stale-scope", "scoped book coverage manifest changed after review"),
    ],
)
def test_supplement_transaction_preserves_original_and_rolls_back_all_state(
    tmp_path: Path, failure: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler, service, index, transaction, scope_path, base_path = _fixture(
        tmp_path, failure=failure
    )
    if failure == "stale-scope":
        scope_path.write_bytes(scope_path.read_bytes() + b"\n")
    before_inputs = compiler.snapshot_inputs(extra_paths=(scope_path,))
    before_outputs = compiler.snapshot_outputs()
    before_base = base_path.read_bytes()
    source_id = transaction.sources_upsert[0]["source_id"]
    before_sources = compiler._load_inputs()[0]
    original_page = compiler._load_inputs()[2]["books/kotlin/chapter-01"]
    original_source_id = original_page["render"]["source_id"]
    if failure == "index":
        index.fail_next = True
    if failure:
        with pytest.raises((WoonError, RuntimeError), match=message):
            service.apply_compiled_wiki_transaction(transaction)
        assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == before_inputs
        assert compiler.snapshot_outputs() == before_outputs
        assert source_id not in compiler._load_inputs()[0]
    else:
        update = transaction.coverage_manifest
        assert update is not None
        payload = {
            "apply": True,
            **{
                key: getattr(transaction, key)
                for key in (
                    "expected_revisions",
                    "sources_upsert",
                    "claims_upsert",
                    "pages_upsert",
                    "curations_upsert",
                    "expected_page_spec_sha256",
                    "expected_catalog_revision",
                )
            },
            "coverage_manifest": {
                key: getattr(update, key)
                for key in (
                    "mode",
                    "relative_path",
                    "expected_sha256",
                    "replacement",
                    "base_relative_path",
                    "base_expected_sha256",
                    "scope_root_id",
                )
            },
        }
        input_path = tmp_path / "transaction.json"
        input_path.write_text(json.dumps(payload))
        monkeypatch.setattr(cli, "build_knowledge_service", lambda vault: (None, service))
        output = StringIO()
        cli.run(
            [
                "knowledge",
                "apply-compiled-transaction",
                "--input",
                str(input_path),
                "--vault",
                str(tmp_path),
            ],
            output,
        )
        assert json.loads(output.getvalue())["pages_upserted"] == 1
        scope = json.loads(scope_path.read_bytes())
        assert scope["nodes"][0]["runnable"] == {"expected": 2, "verified": 2}
        assert compiler._load_inputs()[0][original_source_id] == before_sources[original_source_id]
        assert compiler._load_inputs()[2]["books/kotlin/chapter-01"]["render"]["source_id"] == (
            original_source_id
        )
        rendered = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
        assert rendered.count("```run-kotlin") == 3
        assert rendered.count("### 검토한 보충 예제") == 1
        assert compiler.audit().complete
        scoped = audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix())
        assert scoped.complete
        # A stale replay cannot duplicate the appended body or advance receipts.
        accepted_inputs = compiler.snapshot_inputs(extra_paths=(scope_path,))
        with pytest.raises(WoonError, match="changed after it was read"):
            service.apply_compiled_wiki_transaction(transaction)
        assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == accepted_inputs
    assert base_path.read_bytes() == before_base


def test_supplement_renderer_cannot_be_applied_without_coverage(tmp_path: Path) -> None:
    compiler, service, _, transaction, scope_path, _ = _fixture(tmp_path)
    before = compiler.snapshot_inputs(extra_paths=(scope_path,))
    with pytest.raises(WoonError, match="require a scoped coverage transaction"):
        service.apply_compiled_wiki_transaction(replace(transaction, coverage_manifest=None))
    assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == before


def test_prose_footnote_preserves_absent_supplemental_runnable_partition(tmp_path: Path) -> None:
    compiler, service, _, transaction, scope_path, base_path = _fixture(tmp_path)
    original_scope = scope_path.read_bytes()
    original_base = base_path.read_bytes()
    scope = json.loads(original_scope)
    assert "supplemental_runnables" not in scope
    owner = "books/kotlin/chapter-01"
    body = "### 검토한 설명\n\n원문의 순서와 코드 예제를 그대로 보존한다.\n"
    body_hash = _sha(body.encode())
    source_id = f"source://curated-wiki/{owner}/{body_hash[:24]}"
    claim_id = f"claim://curated-wiki/{owner}/{body_hash[:24]}-note"
    source = {
        **transaction.sources_upsert[0],
        "source_id": source_id,
        "original_sha256": body_hash,
        "normalized_sha256": body_hash,
        "body": body,
    }
    claim = {
        **transaction.claims_upsert[0],
        "claim_id": claim_id,
        "source_ids": [source_id],
        "statement": "검토한 보충 설명이다.",
        "markdown": body,
    }
    original_page = compiler._load_inputs()[2][owner]
    page = copy.deepcopy(original_page)
    page["source_ids"].append(source_id)
    page["claim_ids"].append(claim_id)
    page["render"]["supplemental_claim_ids"] = [claim_id]
    page["render"]["personal_footnotes"] = [
        {
            "id": "prose-only",
            "claim_id": claim_id,
            "anchor": "한국어",
        }
    ]
    note_transaction = replace(
        transaction,
        sources_upsert=(source,),
        claims_upsert=(claim,),
        pages_upsert=(page,),
        coverage_manifest=replace(transaction.coverage_manifest, replacement=scope),
    )
    before = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
    service.apply_compiled_wiki_transaction(note_transaction)
    after = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
    assert "한국어[^wn-personal-prose-only]" in after
    assert before.count("\n```run-kotlin\n") == after.count("\n```run-kotlin\n") == 2
    assert json.loads(scope_path.read_bytes()) == json.loads(original_scope)
    assert base_path.read_bytes() == original_base
    assert "supplemental_runnables" not in json.loads(scope_path.read_bytes())
    assert audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix()).complete

    # Editing an existing note is explicit and cannot overwrite its evidence.
    old_sources, old_claims, current_pages, _, _ = compiler._load_inputs()
    current = current_pages[owner]
    revised_body = "각 원소의 값을 확인하고 같은 조건에서 결과를 비교한다.\n"
    digest = _sha(revised_body.encode())
    new_sid = f"source://curated-wiki/{owner}/{digest[:24]}"
    new_cid = f"claim://curated-wiki/{owner}/{digest[:24]}-note"
    successor_source = {
        **source,
        "source_id": new_sid,
        "original_sha256": digest,
        "normalized_sha256": digest,
        "body": revised_body,
    }
    successor_claim = {
        **claim,
        "claim_id": new_cid,
        "source_ids": [new_sid],
        "markdown": revised_body,
    }
    revised_page = copy.deepcopy(current)
    revised_page["source_ids"].append(new_sid)
    revised_page["claim_ids"].append(new_cid)
    revised_page["render"]["supplemental_claim_ids"] = [new_cid]
    revised_page["render"]["personal_footnotes"][0]["claim_id"] = new_cid
    revision = replace(
        note_transaction,
        sources_upsert=(successor_source,),
        claims_upsert=(successor_claim,),
        pages_upsert=(revised_page,),
        expected_catalog_revision=compiler.catalog_revision(),
        expected_revisions={owner: service.get(owner).revision},
        expected_page_spec_sha256={owner: _record_sha(current)},
        coverage_manifest=replace(
            note_transaction.coverage_manifest, expected_sha256=_sha(scope_path.read_bytes())
        ),
        personal_footnote_revision_ids={owner: ("prose-only",)},
    )
    with pytest.raises(WoonError, match="preserve rendered claim order"):
        service.apply_compiled_wiki_transaction(
            replace(revision, personal_footnote_revision_ids={})
        )
    moved_anchor = copy.deepcopy(revised_page)
    moved_anchor["render"]["personal_footnotes"][0]["anchor"] = "other"
    with pytest.raises(WoonError, match="same anchor"):
        service.apply_compiled_wiki_transaction(replace(revision, pages_upsert=(moved_anchor,)))
    for code in (
        "```run-kotlin\nfun main() = println(1)\n```",
        "> ```kotlin\nx\n> ```",
        "    println(1)",
    ):
        code_claim = {**successor_claim, "markdown": code}
        with pytest.raises(WoonError, match="cannot change or remove code"):
            service.apply_compiled_wiki_transaction(replace(revision, claims_upsert=(code_claim,)))
    service.apply_compiled_wiki_transaction(revision)
    rendered = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
    assert revised_body.strip() in rendered and "한국어[^wn-personal-prose-only]" in rendered
    assert rendered.count("\n```run-kotlin\n") == 2
    new_sources, new_claims, current_pages, _, _ = compiler._load_inputs()
    assert all(new_sources[key] == value for key, value in old_sources.items())
    assert all(new_claims[key] == value for key, value in old_claims.items())
    stripped_page = copy.deepcopy(current_pages[owner])
    stripped_page["render"].pop("personal_footnotes")
    stripped_page["render"].pop("supplemental_claim_ids")
    removal = replace(
        revision,
        sources_upsert=(),
        claims_upsert=(),
        pages_upsert=(stripped_page,),
        expected_catalog_revision=compiler.catalog_revision(),
        expected_revisions={owner: service.get(owner).revision},
        expected_page_spec_sha256={owner: _record_sha(current_pages[owner])},
        coverage_manifest=replace(
            revision.coverage_manifest, expected_sha256=_sha(scope_path.read_bytes())
        ),
    )
    with pytest.raises(WoonError, match="preserve rendered claim order"):
        service.apply_compiled_wiki_transaction(replace(removal, personal_footnote_revision_ids={}))
    service.apply_compiled_wiki_transaction(removal)
    rendered = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
    assert "wn-personal-prose-only" not in rendered
    assert rendered.count("\n```run-kotlin\n") == 2
    assert json.loads(scope_path.read_bytes()) == json.loads(original_scope)
    assert base_path.read_bytes() == original_base
    assert audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix()).complete

    # Older translated readers embedded an editorial callout in a prose span.
    # Removing that callout must not authorize any other translation/code edit.
    from woon_core.knowledge.compiled_wiki import (
        CuratedRevision,
        VerifiedBookPage,
        _remove_editorial_line,
    )

    sources_now, claims_now, pages_now, curations_now, _ = compiler._load_inputs()
    current_page = pages_now[owner]
    current_page["frontmatter"].update(reader_language="ko", node_kind="detail")
    compiler._write_inputs(sources_now, claims_now, pages_now, curations_now)
    compiler.compile(page_ids=(owner,))
    plain_body = sources_now[current_page["render"]["source_id"]]["body"]
    prose = "자연스러운 한국어 학습 본문이다."
    callout = "> **원문 정정:** 예제의 프로퍼티 이름을 확인했다."
    plain_body += "\n같은 문자열 " + callout + " 을 본문에서 설명한다.\n"
    editorial_body = plain_body.replace(prose, prose + "\n\n" + callout)
    assert editorial_body != plain_body
    compiler.curate_revisions((CuratedRevision(owner, editorial_body, "예제의 본문이다."),))
    editorial_scope = json.loads(scope_path.read_bytes())
    assignment = next(
        row
        for row in editorial_scope["source_element_assignments"]
        if row.get("delivery_span") == prose
    )
    assignment.update(
        delivery_span=prose + "\n\n" + callout,
        delivery_span_sha256=_sha((prose + "\n\n" + callout).encode()),
    )
    scope_path.write_text(json.dumps(editorial_scope))
    sources_now, _, pages_now, _, _ = compiler._load_inputs()
    current_page = pages_now[owner]
    source_now = sources_now[current_page["render"]["source_id"]]
    cleanup_scope = copy.deepcopy(editorial_scope)
    assignment = next(
        row
        for row in cleanup_scope["source_element_assignments"]
        if row.get("delivery_span") == prose + "\n\n" + callout
    )
    assignment.update(delivery_span=prose, delivery_span_sha256=_sha(prose.encode()))
    cleanup = replace(
        note_transaction.coverage_manifest,
        expected_sha256=_sha(scope_path.read_bytes()),
        replacement=cleanup_scope,
        editorial_line_removals={
            owner: {
                "source_id": source_now["source_id"],
                "source_record_sha256": _record_sha(source_now),
                "lines": [callout],
                "body_sha256": _sha(plain_body.encode()),
            }
        },
    )
    import re

    inline = "같은 문자열 " + callout + " 을 보존한다.\n\n"
    eof_body = inline + callout
    match = next(re.finditer(r"^" + re.escape(callout) + r"$", eof_body, re.M))
    assert _remove_editorial_line(eof_body, match) == inline
    whitespace_scope = copy.deepcopy(editorial_scope)
    whitespace_scope["source_element_assignments"][-1]["delivery_span"] = "  별도 문장  "
    whitespace_replacement = copy.deepcopy(cleanup_scope)
    whitespace_replacement["source_element_assignments"][-1]["delivery_span"] = "  별도 문장  "
    compiler._current_with_editorial_removals(
        whitespace_scope, replace(cleanup, replacement=whitespace_replacement)
    )
    from unittest.mock import patch

    live_inputs = compiler._load_inputs()
    for prefix in ("", "> "):
        fenced_body = editorial_body.replace(
            "\n" + callout + "\n",
            "\n" + prefix + "```markdown\n" + callout + "\n" + prefix + "```\n",
            1,
        )
        fake_inputs = copy.deepcopy(live_inputs)
        fake_source = fake_inputs[0][source_now["source_id"]]
        fake_source["body"] = fenced_body
        fenced_pins = copy.deepcopy(cleanup.editorial_line_removals)
        fenced_pins[owner]["source_record_sha256"] = _record_sha(fake_source)
        with (
            patch.object(compiler, "_load_inputs", return_value=fake_inputs),
            pytest.raises(WoonError, match="code fence"),
        ):
            compiler._editorial_removed_bodies(
                replace(cleanup, editorial_line_removals=fenced_pins)
            )
    with pytest.raises(WoonError, match="cannot decrease or be regenerated"):
        compiler.validate_book_coverage_manifest_update(
            replace(cleanup, editorial_line_removals=None)
        )
    changed_code = copy.deepcopy(cleanup_scope)
    changed_code["source_element_assignments"][-1]["owner_id"] = "other"
    with pytest.raises(WoonError, match="unrelated delivery or code bindings"):
        compiler.validate_book_coverage_manifest_update(replace(cleanup, replacement=changed_code))
    bad_pins = copy.deepcopy(cleanup.editorial_line_removals)
    bad_pins[owner]["lines"] = [prose]
    with pytest.raises(WoonError, match="exact editorial callout"):
        compiler.validate_book_coverage_manifest_update(
            replace(cleanup, editorial_line_removals=bad_pins)
        )
    record = VerifiedBookPage(
        owner,
        current_page["title"],
        plain_body,
        "예제의 본문이다.",
        "예제를 읽는다.",
        "source://kotlin/chapter-01",
        "a" * 64,
        copy.deepcopy(current_page["frontmatter"]),
        service.get(owner).revision,
    )
    with pytest.raises(WoonError, match="exact remaining reader body"):
        compiler.apply_verified_book_update(
            (replace(record, body=plain_body + "추가 변경"),), {}, {}, cleanup
        )
    payload = dict(
        pages=(record,),
        replacements={},
        retirement_expected_revisions={},
        retirement_body_sha256={},
        coverage_manifest=cleanup,
    )
    service.preflight_verified_book_update(**payload)
    service.apply_verified_book_update(**payload)
    restored = (tmp_path / "wiki/books/kotlin/chapter-01.md").read_text()
    assert "\n" + callout + "\n" not in restored
    assert restored.count(callout) == 1 and restored.count("\n```run-kotlin\n") == 2
    assert compiler._load_inputs()[0][source_now["source_id"]]["body"] == editorial_body
    assert base_path.read_bytes() == original_base
    assert audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix()).complete


def _personal_footnote_transaction(compiler, service, transaction, scope_path):
    """Migrate an already accepted supplement, as existing reader pages do."""
    service.apply_compiled_wiki_transaction(transaction)
    _, _, pages, curations, _ = compiler._load_inputs()
    owner = "books/kotlin/chapter-01"
    page = copy.deepcopy(pages[owner])
    page["render"]["personal_footnotes"] = [
        {
            "id": "reviewed-example",
            "claim_id": page["render"]["supplemental_claim_ids"][0],
            "anchor": "한국어",
        }
    ]
    return CompiledWikiTransaction(
        expected_revisions={owner: service.get(owner).revision},
        sources_upsert=(),
        claims_upsert=(),
        pages_upsert=(page,),
        curations_upsert=(curations[owner],),
        expected_page_spec_sha256={owner: _record_sha(pages[owner])},
        expected_catalog_revision=compiler.catalog_revision(),
        coverage_manifest=replace(
            transaction.coverage_manifest,
            expected_sha256=_sha(scope_path.read_bytes()),
        ),
    )


@pytest.mark.parametrize("index_failure", [False, True])
def test_personal_footnotes_preserve_source_runs_and_atomic_recovery(
    tmp_path: Path, index_failure: bool
) -> None:
    compiler, service, index, transaction, scope_path, base_path = _fixture(tmp_path)
    transaction = _personal_footnote_transaction(compiler, service, transaction, scope_path)
    before_inputs = compiler.snapshot_inputs(extra_paths=(scope_path,))
    before_outputs = compiler.snapshot_outputs()
    before_base = base_path.read_bytes()
    before_scope = scope_path.read_bytes()
    sources, claims, _, _, _ = compiler._load_inputs()
    with pytest.raises(WoonError, match="require a scoped coverage transaction"):
        service.apply_compiled_wiki_transaction(replace(transaction, coverage_manifest=None))
    if index_failure:
        index.fail_next = True
        with pytest.raises(RuntimeError, match="injected index failure"):
            service.apply_compiled_wiki_transaction(transaction)
        assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == before_inputs
        assert compiler.snapshot_outputs() == before_outputs
        return
    service.apply_compiled_wiki_transaction(transaction)
    assert compiler._load_inputs()[:2] == (sources, claims)
    assert scope_path.read_bytes() == before_scope
    assert base_path.read_bytes() == before_base
    output = tmp_path / "wiki/books/kotlin/chapter-01.md"
    rendered = output.read_text()
    assert "한국어[^wn-personal-reviewed-example]" in rendered
    assert "\n\n---\n\n[^wn-personal-reviewed-example]: **검토한 보충 예제**" in rendered
    assert "\n### 검토한 보충 예제" not in rendered
    assert "    ```run-kotlin\n    fun main() = println(3)\n    ```" in rendered
    assert rendered.count("\n```run-kotlin\n") == 2
    assert compiler.audit().complete
    assert audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix()).complete
    # Reject changed source prose, original code and supplemental code rather
    # than stripping arbitrary footnote syntax to make coverage pass.
    for old, new in (
        ("자연스러운", "바꾼"),
        ("println(1)", "println(99)"),
        ("println(3)", "println(99)"),
    ):
        header, reader = rendered.split("\n# ", 1)
        assert old in reader
        output.write_text(header + "\n# " + reader.replace(old, new, 1))
        audit = audit_book_coverage_scope(tmp_path, scope_path.relative_to(tmp_path).as_posix())
        assert any("presentation differs from its exact inputs" in error for error in audit.errors)
    output.write_text(rendered)
    accepted = compiler.snapshot_inputs(extra_paths=(scope_path,))
    with pytest.raises(WoonError, match="changed after it was read"):
        service.apply_compiled_wiki_transaction(transaction)
    assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == accepted


def test_personal_footnotes_reject_ambiguous_anchors_and_identifier_collisions() -> None:
    from woon_core.knowledge.book_reader_presentation import render_personal_footnotes

    claim = {
        "claim_id": "claim://note",
        "kind": "curated-document",
        "status": "accepted",
        "markdown": "## 개인 질문\n\n확인한 이해.\n",
    }
    render = {
        "kind": "source-body",
        "supplemental_claim_ids": [claim["claim_id"]],
        "personal_footnotes": [{"id": "question", "claim_id": claim["claim_id"], "anchor": "word"}],
    }
    cases = (
        ("word word", "word", "exactly once"),
        ("본문", "word", "exactly once"),
        ("```kotlin\nword\n```\n", "word", "code fence"),
        ("~~~kotlin\nword\n~~~\n", "word", "code fence"),
        ("본문 `word`.", "word", "inline code"),
        ("[word](https://example.org)", "word", "link or URL"),
        ("https://example.org/word/path", "word", "link or URL"),
        ("word[^wn-personal-question]", "word", "collides with the source"),
    )
    for body, anchor, message in cases:
        candidate = copy.deepcopy(render)
        candidate["personal_footnotes"][0]["anchor"] = anchor
        with pytest.raises(WoonError, match=message):
            render_personal_footnotes(body, candidate, [claim])
    # An entire inline-code expression can be annotated after its closing tick.
    candidate = copy.deepcopy(render)
    candidate["personal_footnotes"][0]["anchor"] = "`word`"
    assert "`word`[^wn-personal-question]" in render_personal_footnotes(
        "본문 `word`.",
        candidate,
        [claim],
    )
    duplicate = copy.deepcopy(render)
    duplicate["personal_footnotes"].append(copy.deepcopy(duplicate["personal_footnotes"][0]))
    with pytest.raises(WoonError, match="unique stable slugs"):
        render_personal_footnotes("word", duplicate, [claim])
    empty = copy.deepcopy(claim)
    empty["markdown"] = ""
    with pytest.raises(WoonError, match="non-empty accepted"):
        render_personal_footnotes("word", render, [empty])
