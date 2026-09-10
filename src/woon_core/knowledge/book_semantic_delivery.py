"""Locate reviewed book source units without changing their inventory identity."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, cast

from woon_core.errors import WoonError


def digest(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def _private_bytes(vault: Path, relative: object, expected: object) -> bytes:
    if (
        not isinstance(relative, str)
        or not relative.startswith("private/")
        or "\\" in relative
        or Path(relative).as_posix() != relative
        or ".." in Path(relative).parts
    ):
        raise WoonError("source mapping requires a private relative evidence path")
    parts = Path(relative).parts
    if any((vault / Path(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1)):
        raise WoonError("source mapping evidence must not traverse symlinks")
    try:
        raw = (vault / relative).read_bytes()
    except OSError as error:
        raise WoonError("source mapping evidence file is missing") from error
    if digest(raw) != expected:
        raise WoonError("source mapping evidence hash differs")
    return raw


class _SourceNodes(HTMLParser):
    """Retain source offsets and direct DOM order; never choose a title by text."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer("\n", source))
        self.nodes: list[dict[str, Any]] = []
        self.stack: list[int] = []
        self.feed(source)
        self.close()

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self.source_offset()
        row = {
            "tag": tag,
            "attrs": dict(attrs),
            "start": start,
            "end": start + len(cast(str, self.get_starttag_text())),
            "parent": self.stack[-1] if self.stack else None,
            "text": "",
        }
        self.nodes.append(row)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(len(self.nodes) - 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        old = len(self.stack)
        self.handle_starttag(tag, attrs)
        del self.stack[old:]

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.nodes[self.stack[-1]]["tag"] != tag:
            raise WoonError("source mapping requires balanced source HTML")
        index = self.stack.pop()
        self.nodes[index]["end"] = self.source.index(">", self.source_offset()) + 1

    def handle_data(self, data: str) -> None:
        for index in self.stack:
            self.nodes[index]["text"] += data

    def block(self, index: object, subindex: object) -> tuple[dict[str, Any], int]:
        bodies = [i for i, n in enumerate(self.nodes) if n["tag"] == "body"]
        if len(bodies) != 1:
            raise WoonError("source mapping requires exactly one HTML body")
        children = [i for i, n in enumerate(self.nodes) if n["parent"] == bodies[0]]
        if type(index) is not int or not 0 <= index < len(children):
            raise WoonError("source mapping DOM block index is invalid")
        chosen = children[index]
        if subindex is not None:
            children = [i for i, n in enumerate(self.nodes) if n["parent"] == chosen]
            if type(subindex) is not int or not 0 <= subindex < len(children):
                raise WoonError("source mapping DOM child index is invalid")
            chosen = children[subindex]
        return self.nodes[chosen], chosen


def _code_lines(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _prose(text: str) -> str:
    text = re.sub(r"^[#*\s]+", "", text)
    text = re.sub(r"[❶-❿①-⑳]", "", text)
    return "".join(text.replace("`", "").replace("*", "").split())


class _PrintedCode(HTMLParser):
    """Remove publisher-marked callouts and join only explicit continuation arrows."""

    def __init__(self, excerpt: str) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.ignored = False
        self.feed(excerpt)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == "span" and "fm-code-continuation-arrow" in classes:
            if re.search(r"\r?\n[ \t]*$", self.text) is None:
                raise WoonError("source continuation arrow has no preceding printed line break")
            self.text = re.sub(r"\r?\n[ \t]*$", "", self.text)
            self.ignored = True
        elif tag == "span" and "fm-combinumeral" in classes:
            self.ignored = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self.ignored = False

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.text += data


def source_mapping(
    vault: Path,
    proof: object,
    element: dict[str, Any],
    owner: str,
) -> dict[str, Any]:
    """Verify legacy Markdown identity against one pinned original HTML node."""
    if not isinstance(proof, dict) or set(proof) != {"relative_path", "sha256"}:
        raise WoonError("source mapping evidence fields are invalid")
    try:
        data = json.loads(_private_bytes(vault, proof["relative_path"], proof["sha256"]))
    except (ValueError, UnicodeError) as error:
        raise WoonError("source mapping evidence must be JSON") from error
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise WoonError("source mapping evidence records are missing")
    hits = [
        r
        for r in data["records"]
        if isinstance(r, dict) and r.get("element_id") == element.get("element_id")
    ]
    if len(hits) != 1:
        raise WoonError("source mapping element must occur exactly once")
    row = hits[0]
    legacy = row.get("legacy_source_markdown")
    if (
        row.get("owner_id") != owner
        or row.get("source_locator") != element.get("source_locator")
        or not isinstance(legacy, str)
        or digest(legacy) != element.get("source_sha256")
    ):
        raise WoonError("source mapping changed source identity or owner")
    locator = data.get("document_locator")
    if (
        not isinstance(locator, str)
        or not locator
        or re.fullmatch(re.escape(locator) + r":(?:block|code)-[0-9]+", row["source_locator"])
        is None
    ):
        raise WoonError("source mapping locator is not in the pinned document")
    source = _private_bytes(vault, data.get("document_path"), data.get("document_sha256")).decode()
    parser = _SourceNodes(source)
    node, node_index = parser.block(row.get("dom_child_index"), row.get("subindex"))
    excerpt = source[node["start"] : node["end"]]
    if digest(excerpt) != row.get("source_excerpt_sha256"):
        raise WoonError("source mapping DOM excerpt hash differs")
    if element.get("kind") == "code":
        if node["tag"] != "pre":
            raise WoonError("source code mapping must select a pre node")
        text = node["text"].replace("\r\n", "\n")
        legacy_clean = re.sub(r"[ \t]*//[ \t]*[①-⑳][ \t]*(?=\n|$)", "", legacy)
        clean = re.sub(r"[❶-❿①-⑳]", "", text)
        if _code_lines(legacy_clean.replace("➥", "")) != _code_lines(clean.replace("➥", "")):
            raise WoonError("source mapping legacy code differs from the original pre payload")
    elif _prose(legacy) != _prose(node["text"]):
        raise WoonError("source mapping legacy prose differs from original HTML text")
    return {
        **row,
        "source_tag": node["tag"],
        "source_text": node["text"],
        "source_code_payload": _PrintedCode(excerpt).text if node["tag"] == "pre" else None,
        "source_node_index": node_index,
        "source_document_sha256": data["document_sha256"],
    }


def static_parts_source_mapping(
    vault: Path, proof: object, element: dict[str, Any], owner: str, payload: str
) -> None:
    """Preserve one original pre across language-specific static fences."""
    mapped = source_mapping(vault, proof, element, owner)
    if mapped["source_tag"] != "pre" or element.get("kind") != "code":
        raise WoonError("static_parts mapping requires one original code pre")
    original = mapped["source_code_payload"].replace("\r\n", "\n")
    if original.removesuffix("\n") != payload.removesuffix("\n"):
        raise WoonError("static_parts payload differs from its mapped original pre")


def _rendered_heading_lines(body: str) -> list[str]:
    """Exclude fenced examples and comments from actual Markdown heading positions."""
    headings = []
    fence: tuple[str, int] | None = None
    comment = False
    for original in body.splitlines():
        if fence is not None:
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", original
            ):
                fence = None
            continue
        line = original
        visible = ""
        while line:
            marker = "-->" if comment else "<!--"
            before, found, after = line.partition(marker)
            if not comment:
                visible += before
            if not found:
                break
            comment = not comment
            line = after
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", visible)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = opening[1][0], len(opening[1])
        elif visible == original and re.fullmatch(r"#{1,2} .+", visible):
            headings.append(visible)
    return headings


def heading_delivery(
    assignment: dict[str, Any],
    element: dict[str, Any],
    manifest: dict[str, Any],
    pages: dict[str, tuple[Path, dict[str, Any], str]],
    vault: Path,
) -> tuple[str, str]:
    """Verify a heading in its own page or its actual navigation ancestor."""
    if set(assignment) != {
        "element_id",
        "owner_id",
        "delivery",
        "heading_page_id",
        "heading",
        "heading_sha256",
        "source_mapping_evidence",
    }:
        raise WoonError("heading delivery fields are invalid")
    owner, target, heading = (
        assignment["owner_id"],
        assignment["heading_page_id"],
        assignment["heading"],
    )
    source = source_mapping(vault, assignment["source_mapping_evidence"], element, owner)
    if not re.fullmatch(r"h[1-6]", source["source_tag"]):
        raise WoonError("heading delivery cannot replace a source paragraph")
    if (
        not isinstance(heading, str)
        or re.fullmatch(r"#{1,2} [^\n]+", heading) is None
        or digest(heading) != assignment["heading_sha256"]
    ):
        raise WoonError("heading delivery requires an exact H1 or H2 and its hash")
    nodes = {n["canonical_id"]: n for n in manifest["nodes"]}
    cursor, chain = owner, set()
    while cursor in nodes and cursor not in chain:
        chain.add(cursor)
        cursor = nodes[cursor].get("parent_id")
    if target not in chain or target not in pages:
        raise WoonError("heading delivery target must be the owner or its real ancestor")
    _, metadata, body = pages[target]
    # Full compiled body is intentional: the ordinary reader view strips H1/navigation.
    if _rendered_heading_lines(body).count(heading) != 1:
        raise WoonError("heading delivery must appear exactly once in its target page")
    if heading.startswith("# "):
        if heading != "# " + str(metadata.get("title", "")):
            raise WoonError("heading delivery H1 differs from the canonical page title")
        source_number = re.match(r"\s*(\d+(?:\.\d+)*)\b", source["source_text"])
        target_number = re.match(r"# (\d+(?:\.\d+)*)", heading)
        if source_number and (not target_number or source_number[1] != target_number[1]):
            raise WoonError("heading delivery H1 changed the original section number")
    else:
        groups = metadata.get("navigation_groups", [])
        hits = [g for g in groups if g.get("label") == heading[3:]]
        if len(hits) != 1 or owner not in hits[0].get("children", []):
            raise WoonError("heading delivery H2 must be the owner's navigation group")
        source_number = re.match(r"\s*(\d+(?:\.\d+)*)\b", source["source_text"])
        target_number = re.match(r"## (\d+(?:\.\d+)*)\b", heading)
        if not source_number or not target_number or source_number[1] != target_number[1]:
            raise WoonError("heading delivery changed the original section number")
    return target, digest(heading)


def repeated_span_delivery(
    assignment: dict[str, Any],
    element: dict[str, Any],
    manifest: dict[str, Any],
    reader_body: str,
    vault: Path,
) -> str:
    """Match every repeated source callout to one ordered reader occurrence."""
    if set(assignment) != {
        "element_id",
        "owner_id",
        "delivery",
        "delivery_span",
        "delivery_span_sha256",
        "span_occurrence",
        "source_mapping_evidence",
    }:
        raise WoonError("repeated reader span fields are invalid")
    span, occurrence = assignment["delivery_span"], assignment["span_occurrence"]
    if (
        not isinstance(span, str)
        or not span.strip()
        or digest(span) != assignment["delivery_span_sha256"]
    ):
        raise WoonError("repeated reader span payload or hash differs")
    owner = assignment["owner_id"]
    assignments = {a["element_id"]: a for a in manifest["source_element_assignments"]}
    group = [
        e
        for e in manifest["source_elements"]
        if e.get("kind") == element.get("kind")
        and e.get("source_sha256") == element.get("source_sha256")
        and assignments.get(e["element_id"], {}).get("owner_id") == owner
    ]
    if len(group) < 2 or reader_body.count(span) != len(group):
        raise WoonError("repeated span requires the complete source group and equal reader count")
    if type(occurrence) is not int or not 1 <= occurrence <= len(group):
        raise WoonError("repeated span occurrence is invalid")
    positions = []
    for index, source in enumerate(group, 1):
        candidate = assignments[source["element_id"]]
        if (
            candidate.get("delivery") != "reader-span-occurrence"
            or candidate.get("delivery_span") != span
            or candidate.get("delivery_span_sha256") != digest(span)
            or candidate.get("span_occurrence") != index
        ):
            raise WoonError(
                "repeated source group must allocate all occurrences exactly once in order"
            )
        mapping = source_mapping(vault, candidate.get("source_mapping_evidence"), source, owner)
        if mapping["source_tag"] != "p":
            raise WoonError("repeated reader span is limited to source paragraphs/callouts")
        positions.append(mapping["source_node_index"])
    if (
        positions != sorted(set(positions))
        or group[occurrence - 1]["element_id"] != element["element_id"]
    ):
        raise WoonError("repeated source span order or source node identity differs")
    return f"{digest(span)}:occurrence-{occurrence}"


def runnable_upgrade(
    vault: Path,
    element: dict[str, Any],
    item: dict[str, Any],
    result: dict[str, Any],
    assignment: dict[str, Any],
    evidence_sha256: str,
) -> None:
    """Accept a classification upgrade only for the exact successful original program."""
    owner = item["owner_id"]
    if (
        result.get("source_element_id") != element.get("element_id")
        or result.get("status") != "runnable-verified"
        or result.get("execution_kind") != "source-standalone"
        or result.get("expected_runtime_error") is True
        or type(result.get("compile_exit_code")) is not int
        or type(result.get("run_exit_code")) is not int
        or result["compile_exit_code"] != 0
        or result["run_exit_code"] != 0
        or result.get("reason") != item["reason"]
        or not owner.endswith("/" + str(result.get("owner", "")))
        or assignment.get("owner_id") != owner
        or assignment.get("delivery") != "run-block"
        or assignment.get("execution_kind") != "source-standalone"
        or assignment.get("verification_sha256") != evidence_sha256
        or assignment.get("run_language") != "run-" + str(result.get("language", ""))
    ):
        raise WoonError("runnable upgrade requires successful original standalone execution")
    pinned = _run_evidence(vault, assignment)
    if not isinstance(pinned, dict) or not isinstance(pinned.get("results"), list):
        raise WoonError("runnable upgrade requires its pinned correction evidence")
    bound = [
        r
        for r in pinned["results"]
        if isinstance(r, dict) and r.get("source_element_id") == element.get("element_id")
    ]
    if bound != [result]:
        raise WoonError("runnable upgrade is not bound to its correction evidence")
    mapping = source_mapping(vault, result.get("source_mapping_evidence"), element, owner)
    source = _private_bytes(
        vault,
        result.get("executed_source_path"),
        result.get("executed_source_sha256"),
    ).decode()
    if digest(source.rstrip("\n")) != result.get("code_sha256"):
        raise WoonError("runnable upgrade executed file does not match the original code hash")
    original = mapping["source_code_payload"]
    if _code_lines(original) != _code_lines(source):
        raise WoonError("runnable upgrade executed file contains a wrapper or changed source")
    try:
        recorded = json.loads(
            _private_bytes(
                vault,
                result.get("original_result_path"),
                result.get("original_result_sha256"),
            )
        )
    except (ValueError, UnicodeError) as error:
        raise WoonError("runnable upgrade original execution record is invalid") from error
    if not isinstance(recorded, list):
        raise WoonError("runnable upgrade original execution result list is missing")
    hits = [r for r in recorded if isinstance(r, dict) and r.get("block_id") == item["block_id"]]
    if len(hits) != 1 or any(
        result.get(key) != hits[0].get(key)
        for key in (
            "status",
            "owner",
            "language",
            "code_sha256",
            "compile_exit_code",
            "run_exit_code",
            "stdout",
            "stderr",
        )
    ):
        raise WoonError("runnable upgrade differs from the pinned original execution result")
    dependencies = result.get("runtime_dependencies")
    if not isinstance(dependencies, list):
        raise WoonError("runnable upgrade must inventory its execution dependencies")
    required = []
    for path_key, hash_key in (
        ("reflection_jar", "reflection_jar_sha256"),
        ("dependency_path", "dependency_sha256"),
    ):
        record = hits[0]
        if path_key in record or hash_key in record:
            if not isinstance(record.get(hash_key), str):
                raise WoonError("original execution dependency is missing its hash")
            required.append((record.get(path_key), record[hash_key]))
    if len(dependencies) != len(required):
        raise WoonError("runnable upgrade must preserve every original execution dependency")
    remaining = list(required)
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"path", "sha256"}:
            raise WoonError("runnable upgrade dependency record is invalid")
        path = dependency["path"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise WoonError("runnable upgrade dependency must identify its local file")
        matches = [
            r for r in remaining if r[1] == dependency["sha256"] and (r[0] is None or r[0] == path)
        ]
        if len(matches) != 1:
            raise WoonError("runnable upgrade dependency differs from original execution")
        remaining.remove(matches[0])
        try:
            actual = digest(Path(path).read_bytes())
        except OSError as error:
            raise WoonError("runnable upgrade execution dependency is unavailable") from error
        if actual != dependency["sha256"]:
            raise WoonError("runnable upgrade execution dependency changed")


def _run_evidence(vault: Path, assignment: dict[str, Any]) -> Any:
    locator = assignment.get("verification_evidence")
    if not isinstance(locator, str) or not locator.startswith(vault.name + "/private/"):
        raise WoonError("run payload requires a pinned private evidence locator")
    relative = locator[len(vault.name) + 1 :]
    try:
        evidence = json.loads(
            _private_bytes(vault, relative, assignment.get("verification_sha256"))
        )
    except (ValueError, UnicodeError) as error:
        raise WoonError("run payload evidence must be JSON") from error
    return evidence


def upgraded_run_payload(
    vault: Path,
    assignment: dict[str, Any],
    element: dict[str, Any],
    payload: str,
) -> None:
    """Bind upgraded run delivery to the executed file, including after promotion."""
    required = assignment.get("execution_kind") == "source-standalone"
    locator = assignment.get("verification_evidence")
    if not required and (
        not isinstance(locator, str) or not locator.startswith(vault.name + "/private/")
    ):
        return  # Existing non-upgrade evidence retains its separate legacy validator.
    evidence = _run_evidence(vault, assignment)
    if not isinstance(evidence, dict) or not isinstance(evidence.get("results"), list):
        if required:
            raise WoonError("upgraded run evidence is missing its result list")
        return
    upgrades = [
        r
        for r in evidence["results"]
        if isinstance(r, dict) and r.get("execution_kind") == "source-standalone"
    ]
    if not upgrades:
        if required:
            raise WoonError("upgraded run evidence has no standalone execution")
        return
    if any(not isinstance(r.get("source_element_id"), str) for r in upgrades):
        raise WoonError("upgraded execution evidence is missing its source element identity")
    rows = [
        r
        for r in evidence["results"]
        if isinstance(r, dict)
        and r.get("source_element_id") == element.get("element_id")
        and r.get("execution_kind") == "source-standalone"
    ]
    if not rows:
        # Other unchanged source blocks may share this evidence file, but their
        # identities must also be explicit; an upgrade must never fall through.
        legacy = [
            r
            for r in evidence["results"]
            if isinstance(r, dict) and r.get("source_element_id") == element.get("element_id")
        ]
        if not required and len(legacy) == 1 and "execution_kind" not in legacy[0]:
            return
        raise WoonError("upgraded run evidence has no matching source element")
    if not required:
        raise WoonError("upgraded run assignment is missing its execution kind")
    if len(rows) != 1 or digest(payload.rstrip("\n")) != rows[0].get("code_sha256"):
        raise WoonError("upgraded run block differs from its original executed payload")
