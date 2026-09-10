import copy
import json

import pytest
from test_book_supplements import _fixture, _record_sha, _sha

from woon_core.errors import WoonError
from woon_core.knowledge import book_prose_edits as module


@pytest.mark.parametrize(
    "kind,semantic,allowed",
    [
        ("caution", "caution", True),
        ("claim", "paragraph", True),
        ("code", "code", False),
        ("caution", "code", False),
        ("claim", "caution", False),
    ],
)
def test_caution_prose_preserves_classification_and_evidence(tmp_path, kind, semantic, allowed):
    compiler, _, _, _, scope_path, _ = _fixture(tmp_path)
    sources, _, pages, curations, _ = compiler._load_inputs()
    owner = "books/kotlin/chapter-01"
    page = pages[owner]
    page["frontmatter"].update(reader_language="ko", publish=False, access="local-only")
    compiler._write_inputs(sources, compiler._load_inputs()[1], pages, curations)
    compiler.compile(page_ids=(owner,))
    sources, _, pages, _, _ = compiler._load_inputs()
    page = pages[owner]
    source = sources[page["render"]["source_id"]]
    old = "자연스러운 한국어 학습 본문이다."
    new = "자연스럽게 쓴 한국어 학습 본문이다."
    before = source["body"]
    after = before.replace(old, new)
    manifest = json.loads(scope_path.read_bytes())
    row = next(r for r in manifest["source_element_assignments"] if r.get("delivery_span") == old)
    element = next(e for e in manifest["source_elements"] if e["element_id"] == row["element_id"])
    element.update(kind=kind, semantic_unit=semantic)
    original = copy.deepcopy(manifest)
    replacement = copy.deepcopy(manifest)
    delivery = next(
        r for r in replacement["source_element_assignments"] if r.get("delivery_span") == old
    )
    delivery.update(delivery_span=new, delivery_span_sha256=_sha(new.encode()))
    proof = {
        "source_id": source["source_id"],
        "source_record_sha256": _record_sha(source),
        "page_spec_sha256": _record_sha(page),
        "reader_sha256": _sha((tmp_path / "wiki" / page["output_path"]).read_bytes()),
        "before_body_sha256": _sha(before.encode()),
        "after_body_sha256": _sha(after.encode()),
        "edits": [{"before": old, "after": new}],
    }
    snapshot = compiler.snapshot_inputs(extra_paths=(scope_path,))
    if allowed:
        bodies, expected = module.prepare_korean_prose_edits(
            tmp_path, manifest, replacement, {owner: proof}, sources, pages
        )
        assert bodies == {owner: after}
        assert expected == replacement
        assert expected["source_elements"] == manifest["source_elements"]
        tampered = copy.deepcopy(replacement)
        tampered["source_elements"][0]["semantic_unit"] = "changed"
        with pytest.raises(WoonError):
            module.prepare_korean_prose_edits(
                tmp_path, manifest, tampered, {owner: proof}, sources, pages
            )
    else:
        with pytest.raises(WoonError, match="non-prose source evidence"):
            module.prepare_korean_prose_edits(
                tmp_path, manifest, replacement, {owner: proof}, sources, pages
            )
    assert manifest == original
    assert compiler.snapshot_inputs(extra_paths=(scope_path,)) == snapshot
