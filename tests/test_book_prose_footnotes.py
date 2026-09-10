import copy
import json
from dataclasses import replace

import pytest
from test_book_supplements import _fixture, _personal_footnote_transaction, _record_sha, _sha

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import audit_book_coverage_scope
from woon_core.knowledge.book_reader_presentation import render_personal_footnotes
from woon_core.knowledge.compiled_wiki import VerifiedBookPage
from woon_core.knowledge.wiki_tree import split_markdown, strip_generated_wiki_views


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "stale-reader",
        "stale-note",
        "anchor",
        "code",
        "index",
        "missing-evidence",
        "tampered-evidence",
    ],
)
def test_prose_edit_preserves_personal_note_position_claims_code_and_rollback(tmp_path, failure):
    compiler, service, index, tx, scope_path, base_path = _fixture(tmp_path)
    note_tx = _personal_footnote_transaction(compiler, service, tx, scope_path)
    service.apply_compiled_wiki_transaction(note_tx)
    owner = "books/kotlin/chapter-01"
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page = pages[owner]
    page["frontmatter"].update(
        reader_language="ko", access="local-only", publish=False, node_kind="detail"
    )
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(page_ids=(owner,))
    service.reindex()
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page = pages[owner]
    source = sources[page["render"]["source_id"]]
    old, new = ("한국어", "한글") if failure == "anchor" else ("자연스러운", "자연스럽게 쓴")
    before = source["body"]
    after = before.replace(old, new)
    current = json.loads(scope_path.read_bytes())
    replacement = copy.deepcopy(current)
    (row,) = [
        r for r in replacement["source_element_assignments"] if old in r.get("delivery_span", "")
    ]
    row["delivery_span"] = row["delivery_span"].replace(old, new)
    row["delivery_span_sha256"] = _sha(row["delivery_span"].encode())
    output = tmp_path / "wiki" / page["output_path"]
    proof = dict(
        source_id=source["source_id"],
        source_record_sha256=_record_sha(source),
        page_spec_sha256=_record_sha(page),
        reader_sha256=_sha(output.read_bytes()),
        before_body_sha256=_sha(before.encode()),
        after_body_sha256=_sha(after.encode()),
        edits=[{"before": old, "after": new}],
    )
    update = replace(
        note_tx.coverage_manifest,
        replacement=replacement,
        expected_sha256=_sha(scope_path.read_bytes()),
        korean_prose_edits={owner: proof},
    )
    (claim,) = [
        claims[c] for c in page["claim_ids"] if claims[c].get("source_ids") == [source["source_id"]]
    ]
    record = VerifiedBookPage(
        owner,
        page["title"],
        after,
        claim["statement"],
        curations[owner]["current_use"],
        source["locator"],
        source["original_sha256"],
        copy.deepcopy(page["frontmatter"]),
        service.get(owner).revision,
    )
    if failure == "stale-reader":
        proof["reader_sha256"] = "0" * 64
    if failure == "stale-note":
        note_id = page["render"]["supplemental_claim_ids"][0]
        claims[note_id]["markdown"] += "\n검토하지 않은 개인각주다.\n"
        compiler._write_inputs(sources, claims, pages, curations)
    if failure == "code":
        record = replace(record, body=after.replace("println(1)", "println(9)"))
    if failure == "index":
        index.fail_next = True
    if failure in {"missing-evidence", "tampered-evidence"}:
        evidence = tmp_path / current["supplemental_runnables"][0]["verification_evidence"]
        if failure == "missing-evidence":
            evidence.unlink()
        else:
            evidence.write_text("{}")
    inputs = compiler.snapshot_inputs(extra_paths=(scope_path, base_path))
    outputs = compiler.snapshot_outputs()
    payload = dict(
        pages=(record,),
        replacements={},
        retirement_expected_revisions={},
        retirement_body_sha256={},
        coverage_manifest=update,
    )
    if failure:
        message = {
            "index": "injected index failure",
            "stale-note": "personal footnotes differ",
            "anchor": "cannot change a personal footnote anchor",
            "missing-evidence": "evidence is unreadable",
            "tampered-evidence": "evidence hash mismatch",
        }.get(failure)
        with pytest.raises((WoonError, RuntimeError), match=message):
            if failure in {"missing-evidence", "tampered-evidence"}:
                service.preflight_verified_book_update(**payload)
            else:
                service.apply_verified_book_update(**payload)
        assert compiler.snapshot_inputs(extra_paths=(scope_path, base_path)) == inputs
        assert compiler.snapshot_outputs() == outputs
        return
    service.preflight_verified_book_update(**payload)
    service.apply_verified_book_update(**payload)
    actual_sources, actual_claims, actual_pages, _, _ = compiler._load_inputs()
    current_page = actual_pages[owner]
    assert {k: v for k, v in current_page["render"].items() if k != "source_id"} == {
        k: v for k, v in page["render"].items() if k != "source_id"
    }
    assert current_page["render"]["source_id"] != page["render"]["source_id"]
    for cid in page["render"]["supplemental_claim_ids"]:
        assert actual_claims[cid] == claims[cid]
        assert cid in current_page["claim_ids"]
    for sid, original in sources.items():
        assert actual_sources[sid]["body"] == original["body"]
        assert actual_sources[sid]["original_sha256"] == original["original_sha256"]
    for cid, original in claims.items():
        assert actual_claims[cid]["markdown"] == original["markdown"]
        assert actual_claims[cid]["statement"] == original["statement"]
    reader = split_markdown(strip_generated_wiki_views(output.read_text()))[1].strip()
    expected = (
        (
            "# "
            + page["title"]
            + "\n\n"
            + render_personal_footnotes(before, page["render"], list(claims.values()))
        )
        .replace(old, new)
        .strip()
    )
    assert reader == expected
    assert "자연스럽게 쓴 한국어[^wn-personal-reviewed-example]" in reader
    assert "    fun main() = println(3)" in reader
    assert audit_book_coverage_scope(tmp_path, str(scope_path.relative_to(tmp_path))).complete
    assert base_path.read_bytes() == inputs[base_path]
