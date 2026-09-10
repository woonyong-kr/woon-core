from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_book_supplements import _fixture, _record_sha, _sha

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import audit_book_coverage_scope
from woon_core.knowledge.book_prose_edits import _replace_sentence
from woon_core.knowledge.compiled_wiki import VerifiedBookPage


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "index",
        "stale-reader",
        "stale-source",
        "owner",
        "phase",
        "code",
        "unreviewed",
        "statement",
        "purpose",
        "wrapper-unreviewed",
        "wrapper-pin",
    ],
)
def test_pinned_prose_service_preserves_source_and_rolls_back(tmp_path: Path, failure: str):
    compiler, service, index, tx, scope_path, base_path = _fixture(tmp_path)
    sources, claims, pages, curations, _ = compiler._load_inputs()
    owner = "books/kotlin/chapter-01"
    page = pages[owner]
    page["frontmatter"].update(
        reader_language="ko", publish=False, access="local-only", node_kind="detail"
    )
    compiler._write_inputs(sources, claims, pages, curations)
    compiler.compile(page_ids=(owner,))
    service.reindex()
    sources, claims, pages, curations, _ = compiler._load_inputs()
    page = pages[owner]
    source = sources[page["render"]["source_id"]]
    old, new = "자연스러운 한국어 학습 본문이다.", "자연스럽게 쓴 한국어 학습 본문이다."
    before = source["body"]
    assert before.count(old) == 1
    after = before.replace(old, new)
    manifest = json.loads(scope_path.read_bytes())
    wrapper = {
        "wrapper_id": "books/kotlin/retired-part",
        "map_id": "books/kotlin",
        "group_label": "퇴역 도입 문단",
        "first_leaf_id": owner,
        "source_locator": "source://kotlin#retired-part",
        "relocated_delivery_span": old,
        "relocated_delivery_span_sha256": _sha(old.encode()),
    }
    other = dict(
        wrapper,
        wrapper_id="books/kotlin/other-part",
        relocated_delivery_span="## 설명",
        relocated_delivery_span_sha256=_sha("## 설명".encode()),
    )
    manifest["retired_source_section_wrappers"] = [wrapper, other]
    if failure == "wrapper-pin":
        wrapper["relocated_delivery_span_sha256"] = "0" * 64
    scope_path.write_text(json.dumps(manifest))
    replacement = copy.deepcopy(manifest)
    replacement["retired_source_section_wrappers"][0].update(
        relocated_delivery_span=new,
        relocated_delivery_span_sha256=_sha(new.encode()),
    )
    if failure == "wrapper-unreviewed":
        replacement["retired_source_section_wrappers"][1]["group_label"] = "변경"

    row = next(
        r for r in replacement["source_element_assignments"] if r.get("delivery_span") == old
    )
    row.update(delivery_span=new, delivery_span_sha256=_sha(new.encode()))
    proof = {
        "source_id": source["source_id"],
        "source_record_sha256": _record_sha(source),
        "page_spec_sha256": _record_sha(page),
        "reader_sha256": _sha((tmp_path / "wiki" / page["output_path"]).read_bytes()),
        "before_body_sha256": _sha(before.encode()),
        "after_body_sha256": _sha(after.encode()),
        "edits": [{"before": old, "after": new}],
    }
    update = replace(
        tx.coverage_manifest,
        replacement=replacement,
        korean_prose_edits={owner: proof},
        expected_sha256=_sha(scope_path.read_bytes()),
    )
    record = VerifiedBookPage(
        owner,
        page["title"],
        after,
        next(
            claims[cid]["statement"]
            for cid in page["claim_ids"]
            if claims[cid].get("source_ids") == [source["source_id"]]
        ),
        curations[owner]["current_use"],
        source["locator"],
        source["original_sha256"],
        copy.deepcopy(page["frontmatter"]),
        service.get(owner).revision,
    )
    if failure == "stale-reader":
        proof["reader_sha256"] = "0" * 64
    if failure == "stale-source":
        proof["source_record_sha256"] = "0" * 64
    if failure == "owner":
        row["owner_id"] = "other"
    if failure == "phase":
        replacement["phase_evidence"]["translated"]["unexpected"] = True
    if failure == "code":
        record = replace(record, body=after.replace("println", "print", 1))
    if failure == "unreviewed":
        record = replace(record, body=after + "추가 문장\n")
    if failure == "statement":
        record = replace(record, statement="검토하지 않은 주장이다.")
    if failure == "purpose":
        record = replace(record, current_use="검토하지 않은 새 용도다.")
    before_inputs = compiler.snapshot_inputs(extra_paths=(scope_path,))
    before_outputs = compiler.snapshot_outputs()
    base_bytes = base_path.read_bytes()
    if failure == "index":
        index.fail_next = True
    payload = {
        "pages": (record,),
        "replacements": {},
        "retirement_expected_revisions": {},
        "retirement_body_sha256": {},
        "coverage_manifest": update,
    }
    if failure:
        with pytest.raises(
            (WoonError, RuntimeError),
            match="injected index failure" if failure == "index" else None,
        ):
            service.apply_verified_book_update(**payload)
        assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == before_inputs
        assert compiler.snapshot_outputs() == before_outputs
    else:
        service.preflight_verified_book_update(**payload)
        service.apply_verified_book_update(**payload)
        rendered = (tmp_path / "wiki" / page["output_path"]).read_text()
        assert after.strip() in rendered
        resulting = json.loads(scope_path.read_bytes())["retired_source_section_wrappers"]
        assert resulting[0]["relocated_delivery_span"] == new
        assert resulting[1] == other

        after_sources, after_claims, _, _, _ = compiler._load_inputs()
        assert all(after_sources[k]["body"] == v["body"] for k, v in sources.items() if "body" in v)
        assert all(
            after_sources[k].get("original_sha256") == v.get("original_sha256")
            for k, v in sources.items()
        )
        assert all(after_claims[k].get("markdown") == v.get("markdown") for k, v in claims.items())
        assert all(
            after_claims[k].get("statement") == v.get("statement") for k, v in claims.items()
        )
        assert audit_book_coverage_scope(
            tmp_path, scope_path.relative_to(tmp_path).as_posix()
        ).complete
        with pytest.raises(WoonError):
            compiler.validate_book_coverage_manifest_update(
                replace(update, korean_prose_edits=None)
            )
    assert base_path.read_bytes() == base_bytes


@pytest.mark.parametrize(
    ("body", "old", "new"),
    [
        ("```kotlin\n한국어 코드\n```", "한국어 코드", "새 코드"),
        ("~~~text\n한국어 출력\n~~~", "한국어 출력", "새 출력"),
        ("    한국어 코드", "한국어 코드", "새 코드"),
        ("> 한국어 인용", "한국어 인용", "새 인용"),
        ("한국어는 `val`이다.", "한국어는 `val`이다.", "한국어는 `var`이다."),
        ("한국어 [링크](url)", "한국어 [링크](url)", "한글 [링크](other)"),
        ("한국어 ![그림](image.png)", "한국어 ![그림](image.png)", "한글 ![그림](other.png)"),
        ("한국어 $x$ 수식", "한국어 $x$ 수식", "한글 $y$ 수식"),
        ("$$\n한국어 수식\n$$", "한국어 수식", "다른 수식"),
        ("한국어는 0이다.", "한국어는 0이다.", "한국어는 1이다."),
        ("‘한국어 인용’이다.", "‘한국어 인용’이다.", "‘새 인용’이다."),
        ("한국어 문장이다.", "한국어 문장이다.", "# 한국어 제목"),
        ("한국어 문장 한국어 문장", "한국어 문장", "다른 문장"),
        ("## 내 이해와 보충\n한국어 보충", "한국어 보충", "다른 보충"),
    ],
)
def test_prose_edits_do_not_enter_protected_content(body, old, new):
    with pytest.raises(WoonError):
        _replace_sentence(body, old, new)


def test_materialized_wrapper_delivery_replaces_identity_without_touching_neighbors(tmp_path):
    from woon_core.knowledge.compiled_wiki import _materialize_book_coverage_scopes

    _, _, _, _, scope_path, base_path = _fixture(tmp_path)
    base, scope = json.loads(base_path.read_bytes()), json.loads(scope_path.read_bytes())
    owner = "books/kotlin/chapter-01"
    old = {
        "wrapper_id": "books/kotlin/part",
        "map_id": "books/kotlin",
        "group_label": "도입",
        "first_leaf_id": owner,
        "source_locator": "source://kotlin#part",
        "relocated_delivery_span": "기존 문장이다.",
        "relocated_delivery_span_sha256": _sha("기존 문장이다.".encode()),
    }
    neighbor = dict(
        old, wrapper_id="books/kotlin/neighbor", first_leaf_id="books/kotlin/chapter-02"
    )
    revised = dict(
        old,
        relocated_delivery_span="교정한 문장이다.",
        relocated_delivery_span_sha256=_sha("교정한 문장이다.".encode()),
    )
    base["retired_source_section_wrappers"] = [old, neighbor]
    scope["retired_source_section_wrappers"] = [revised]
    original = copy.deepcopy(base)
    result = _materialize_book_coverage_scopes(base, (scope,), (owner,))
    assert result["retired_source_section_wrappers"] == [revised, neighbor]
    assert base == original
    for invalid, message in (
        ([dict(revised, source_locator="source://changed")], "identity"),
        ([dict(neighbor, relocated_delivery_span="변경")], "outside"),
        ([revised, revised], "unique wrapper_id"),
    ):
        scope["retired_source_section_wrappers"] = invalid
        with pytest.raises(WoonError, match=message):
            _materialize_book_coverage_scopes(base, (scope,), (owner,))
