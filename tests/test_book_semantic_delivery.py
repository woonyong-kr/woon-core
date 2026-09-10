"""Regressions for real source headings, repeated callouts and runnable restoration."""

import copy
import json
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.book_coverage import (
    static_parts_payload,
    validate_static_parts_source_evidence,
)
from woon_core.knowledge.book_semantic_delivery import (
    _SourceNodes,
    digest,
    heading_delivery,
    repeated_span_delivery,
    runnable_upgrade,
    source_mapping,
    upgraded_run_payload,
)


def evidence(vault, html, source_rows):
    private = vault / "private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "source.html").write_text(html)
    parser = _SourceNodes(html)
    records = []
    elements = []
    for index, (kind, legacy, owner) in enumerate(source_rows):
        element = {
            "element_id": f"{kind}:{index}",
            "kind": kind,
            "source_locator": f"source://edition/chapter.htm:block-{index:03}",
            "source_sha256": digest(legacy),
        }
        node, _ = parser.block(index, None)
        records.append(
            {
                **element,
                "owner_id": owner,
                "legacy_source_markdown": legacy,
                "dom_child_index": index,
                "subindex": None,
                "source_excerpt_sha256": digest(html[node["start"] : node["end"]]),
            }
        )
        elements.append(element)
    payload = {
        "document_locator": "source://edition/chapter.htm",
        "document_path": "private/source.html",
        "document_sha256": digest((private / "source.html").read_bytes()),
        "records": records,
    }
    path = private / "mapping.json"
    path.write_text(json.dumps(payload))
    return elements, {"relative_path": "private/mapping.json", "sha256": digest(path.read_bytes())}


def test_heading_requires_original_heading_real_ancestor_and_correct_group(tmp_path):
    vault = tmp_path / "vault"
    owner, parent = "book/chapter/1-1-1", "book/chapter"
    elements, proof = evidence(
        vault,
        "<html><body><h2>1.1 Operations</h2><p>Details</p></body></html>",
        [("claim", "## 1.1 Operations", owner), ("claim", "Details", owner)],
    )
    heading = "## 1.1 연산"
    assignment = {
        "element_id": elements[0]["element_id"],
        "owner_id": owner,
        "delivery": "reader-heading",
        "heading_page_id": parent,
        "heading": heading,
        "heading_sha256": digest(heading),
        "source_mapping_evidence": proof,
    }
    manifest = {
        "nodes": [
            {"canonical_id": owner, "parent_id": parent},
            {"canonical_id": parent, "parent_id": "book"},
        ]
    }
    metadata = {"title": "1장", "navigation_groups": [{"label": "1.1 연산", "children": [owner]}]}
    pages = {parent: (Path("chapter.md"), metadata, "# 1장\n\n" + heading + "\n")}
    assert heading_delivery(assignment, elements[0], manifest, pages, vault) == (
        parent,
        digest(heading),
    )
    for case in (
        "wrong-parent",
        "wrong-group",
        "duplicate",
        "paragraph",
        "fenced",
        "tilde-fenced",
        "comment",
        "inline-comment",
    ):
        bad = copy.deepcopy(assignment)
        pp = copy.deepcopy(pages)
        element = elements[0]
        if case == "wrong-parent":
            bad["heading_page_id"] = "book/other"
        elif case == "wrong-group":
            pp[parent][1]["navigation_groups"][0]["children"] = ["other"]
        elif case == "duplicate":
            pp[parent] = (*pp[parent][:2], pp[parent][2] + heading + "\n")
        elif case in ("fenced", "tilde-fenced", "comment", "inline-comment"):
            body = {
                "fenced": "# 1장\n```text\n" + heading + "\n```\n",
                "tilde-fenced": "# 1장\n~~~~text\n" + heading + "\n~~~~\n",
                "comment": "# 1장\n<!--\n" + heading + "\n-->\n",
                "inline-comment": "# 1장\n<!-- " + heading + " -->\n",
            }[case]
            pp[parent] = (*pp[parent][:2], body)
        else:
            element = elements[1]
            bad["element_id"] = element["element_id"]
        with pytest.raises(WoonError):
            heading_delivery(bad, element, manifest, pp, vault)


def test_repeated_callouts_require_complete_source_group_and_distinct_order(tmp_path):
    vault = tmp_path / "vault"
    owner = "book/leaf"
    elements, proof = evidence(
        vault,
        "<html><body><p>Returns a function</p><p>Returns a function</p></body></html>",
        [("claim", "Returns a function", owner)] * 2,
    )
    span = "- 함수를 반환한다."
    assignments = [
        {
            "element_id": e["element_id"],
            "owner_id": owner,
            "delivery": "reader-span-occurrence",
            "delivery_span": span,
            "delivery_span_sha256": digest(span),
            "span_occurrence": index,
            "source_mapping_evidence": proof,
        }
        for index, e in enumerate(elements, 1)
    ]
    manifest = {"source_elements": elements, "source_element_assignments": assignments}
    body = span + "\n\nOther example\n\n" + span
    signatures = [
        repeated_span_delivery(a, e, manifest, body, vault)
        for a, e in zip(assignments, elements, strict=True)
    ]
    assert len(set(signatures)) == 2
    bad = copy.deepcopy(manifest)
    bad["source_element_assignments"][1]["span_occurrence"] = 1
    with pytest.raises(WoonError, match="all occurrences"):
        repeated_span_delivery(assignments[0], elements[0], bad, body, vault)
    with pytest.raises(WoonError, match="equal reader count"):
        repeated_span_delivery(assignments[0], elements[0], manifest, body + span, vault)
    altered = copy.deepcopy(proof)
    altered["relative_path"] = "private/../mapping.json"
    with pytest.raises(WoonError, match="private relative"):
        source_mapping(vault, altered, elements[0], owner)


def test_legacy_multipart_preserves_original_pre_and_distinct_fence_order(tmp_path):
    vault = tmp_path / "vault"
    code = "val x = 1\nERROR: example diagnostics"
    elements, proof = evidence(
        vault,
        "<html><body><pre>" + code.replace("\n", "\r\n") + "</pre></body></html>",
        [("code", code, "book/leaf")],
    )
    payloads = ["val x = 1\n", "ERROR: example diagnostics\n"]
    assignment = {
        "owner_id": "book/leaf",
        "static_parts": [
            {"language": lang, "block_index": 1, "body_sha256": digest(body)}
            for lang, body in zip(("kotlin", "text"), payloads, strict=True)
        ],
        "static_body_sha256": digest("".join(payloads)),
        "source_payload_evidence": {"source_mapping_evidence": proof},
    }
    reader = "```kotlin\n" + payloads[0] + "```\n\n```text\n" + payloads[1] + "```\n"
    payload, signatures = static_parts_payload(assignment, reader)
    assert len(signatures) == 2
    validate_static_parts_source_evidence(vault, elements[0], assignment, payload)
    with pytest.raises(WoonError, match="mapped original pre"):
        validate_static_parts_source_evidence(
            vault, elements[0], assignment, payload.replace("x = 1", "x = 9")
        )
    with pytest.raises(WoonError, match="identity or owner"):
        validate_static_parts_source_evidence(
            vault, elements[0], {**assignment, "owner_id": "book/other"}, payload
        )
    with pytest.raises(WoonError, match="ordered and unique"):
        static_parts_payload(
            {**assignment, "static_parts": assignment["static_parts"][::-1]}, reader
        )
    with pytest.raises(WoonError, match="missing reader fence"):
        static_parts_payload(assignment, "```kotlin\n" + payloads[0] + "```\n")


def test_runnable_restoration_rejects_failure_wrapper_and_wrong_reader_payload(tmp_path):
    vault = tmp_path / "vault"
    owner = "book/leaf"
    code = 'fun main() { println("one") }'
    elements, proof = evidence(
        vault, f"<html><body><pre>{code}</pre></body></html>", [("code", code, owner)]
    )
    element = elements[0]
    (vault / "private/code.kt").write_text(code + "\n")
    original = {
        "block_id": "b0",
        "owner": "leaf",
        "status": "runnable-verified",
        "language": "kotlin",
        "code_sha256": digest(code),
        "compile_exit_code": 0,
        "run_exit_code": 0,
        "stdout": "one\n",
        "stderr": "",
    }
    path = vault / "private/original.json"
    path.write_text(json.dumps([original]))
    result = {
        **original,
        "source_element_id": element["element_id"],
        "reason": "Original source",
        "execution_kind": "source-standalone",
        "source_mapping_evidence": proof,
        "executed_source_path": "private/code.kt",
        "executed_source_sha256": digest(code + "\n"),
        "original_result_path": "private/original.json",
        "original_result_sha256": digest(path.read_bytes()),
        "runtime_dependencies": [],
    }
    path = vault / "private/execution.json"
    path.write_text(json.dumps({"results": [result]}))
    eh = digest(path.read_bytes())
    item = {"owner_id": owner, "reason": result["reason"], "block_id": "b0"}
    assignment = {
        "owner_id": owner,
        "delivery": "run-block",
        "execution_kind": "source-standalone",
        "verification_sha256": eh,
        "run_language": "run-kotlin",
        "verification_evidence": "vault/private/execution.json",
    }
    runnable_upgrade(vault, element, item, result, assignment, eh)
    upgraded_run_payload(vault, assignment, element, code + "\n")
    for change in (
        {"source_element_id": None},
        {"source_element_id": "code:other"},
        {"run_exit_code": 1},
        {"execution_kind": "contextual-wrapper"},
        {"expected_runtime_error": True},
        {"compile_exit_code": False},
    ):
        with pytest.raises(WoonError, match="successful original standalone"):
            runnable_upgrade(vault, element, item, {**result, **change}, assignment, eh)
    with pytest.raises(WoonError, match="executed payload"):
        upgraded_run_payload(vault, assignment, element, code + "\nprintln(999)\n")
    unmarked = {k: v for k, v in assignment.items() if k != "execution_kind"}
    with pytest.raises(WoonError, match="execution kind"):
        upgraded_run_payload(vault, unmarked, element, code)
    for rows in (
        [],
        [{**result, "source_element_id": "code:other"}],
        [{**result, "execution_kind": "contextual-wrapper"}],
    ):
        path.write_text(json.dumps({"results": rows}))
        bad = {**assignment, "verification_sha256": digest(path.read_bytes())}
        with pytest.raises(WoonError):
            upgraded_run_payload(vault, bad, element, code)
    path.write_text(json.dumps({"results": [result]}))
    for locator in (None, "vault/other/execution.json", "vault/private/missing.json"):
        with pytest.raises(WoonError):
            upgraded_run_payload(
                vault, {**assignment, "verification_evidence": locator}, element, code
            )
    broken = copy.deepcopy(result)
    del broken["source_element_id"]
    path.write_text(json.dumps({"results": [broken]}))
    bad_assignment = {**assignment, "verification_sha256": digest(path.read_bytes())}
    with pytest.raises(WoonError, match="source element identity"):
        upgraded_run_payload(vault, bad_assignment, element, "unexecuted payload")
    with pytest.raises(WoonError, match="correction evidence"):
        runnable_upgrade(
            vault, element, item, result, bad_assignment, bad_assignment["verification_sha256"]
        )
    path.write_text(json.dumps({"results": [result]}))

    # Original execution declares a dependency; omitting it cannot erase that fact.
    jar = tmp_path / "reflection.jar"
    jar.write_bytes(b"fixture jar")
    original_with_jar = {
        **original,
        "reflection_jar": str(jar),
        "reflection_jar_sha256": digest(jar.read_bytes()),
    }
    original_path = vault / "private/original.json"
    original_path.write_text(json.dumps([original_with_jar]))
    dependent = {**result, "original_result_sha256": digest(original_path.read_bytes())}

    def check(candidate):
        path.write_text(json.dumps({"results": [candidate]}))
        candidate_hash = digest(path.read_bytes())
        a = {**assignment, "verification_sha256": candidate_hash}
        runnable_upgrade(vault, element, item, candidate, a, candidate_hash)

    with pytest.raises(WoonError, match="every original execution dependency"):
        check(dependent)
    dependent["runtime_dependencies"] = [{"path": str(jar), "sha256": digest(jar.read_bytes())}]
    check(dependent)
    substitute = tmp_path / "different.jar"
    substitute.write_bytes(jar.read_bytes())
    with pytest.raises(WoonError, match="differs from original execution"):
        check(
            {
                **dependent,
                "runtime_dependencies": [
                    {"path": str(substitute), "sha256": digest(substitute.read_bytes())}
                ],
            }
        )
    jar.unlink()
    with pytest.raises(WoonError, match="unavailable"):
        check(dependent)

    # A legacy hash-only record is kept hash-only and still requires the real bytes.
    original_path.write_text(
        json.dumps([{**original, "dependency_sha256": digest(b"fixture jar")}])
    )
    hash_only = {
        **result,
        "original_result_sha256": digest(original_path.read_bytes()),
        "runtime_dependencies": [{"path": str(substitute), "sha256": digest(b"fixture jar")}],
    }
    check(hash_only)
    original_path.write_text(json.dumps([original]))
    (vault / "private/code.kt").write_text(code + "\nprintln(999)\n")
    altered = {**result, "executed_source_sha256": digest((vault / "private/code.kt").read_bytes())}
    with pytest.raises(WoonError, match="original code hash"):
        check(altered)
