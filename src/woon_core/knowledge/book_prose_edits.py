"""Exact Korean prose revisions; immutable book evidence is never rewritten."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.book_reader_presentation import (
    _anchor_end,
    _fenced_lines,
    render_personal_footnotes,
)
from woon_core.knowledge.wiki_tree import split_markdown, strip_generated_wiki_views


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _record_sha(record: Any) -> str:
    return _sha(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _protected(text: str) -> list[str]:
    # Preserve complete inline code, links, math, quoted speech and numbers.
    # This is deliberately conservative: complex formatting requires a separate review.
    pattern = (
        r"`+[^`\n]*`+|!?\[\[[^\n]*?\]\]|!?\[[^\n]*?\]\([^\n]*?\)"
        r"|\[\^[^\]\n]+\]|<[^>\n]+>|https?://\S+"
        r"|\$[^\n]*?\$|\\\([^\n]*?\\\)|\\\[[^\n]*?\\\]"
        r"|[\"“‘'][^\n]*?[\"”’']|\d+(?:[.,]\d+)*"
    )
    return re.findall(pattern, text)


def _replace_sentence(body: str, old: str, new: str) -> str:
    if (
        not isinstance(old, str)
        or not isinstance(new, str)
        or not old.strip()
        or not new.strip()
        or old == new
        or any(c in old + new for c in "\r\n")
        or not re.search(r"[가-힣]", old)
        or not re.search(r"[가-힣]", new)
        or body.count(old) != 1
    ):
        raise WoonError("Korean prose edit requires one exact nonempty Korean sentence")
    start = body.index(old)
    end = start + len(old)
    line_start = body.rfind("\n", 0, start) + 1
    line_end = body.find("\n", end)
    line = body[line_start : line_end if line_end >= 0 else None]
    new_line = line[: start - line_start] + new + line[end - line_start :]
    math_spans = [m.span() for m in re.finditer(r"\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]", body)]
    prefix = r"^[ \t]*(?:(?:[-+*]|\d+[.)]) +|#{1,6} +|> *)?"
    old_prefix, new_prefix = re.match(prefix, line), re.match(prefix, new_line)
    assert old_prefix is not None and new_prefix is not None
    if (
        any(start < stop and begin < end for begin, stop in _fenced_lines(body))
        or any(start < stop and begin < end for begin, stop in math_spans)
        or re.match(r"(?:[ \t]*[>#|]| {4}|\t|[ \t]*\[.*?\]:)", line)
        or re.match(r"(?:[ \t]*[>#|]| {4}|\t|[ \t]*\[.*?\]:)", new_line)
        or re.search(r"`{3,}|~{3,}|<!--|-->|\$\$", old + new)
        or re.search(r"(?m)^[ \t]*>(?:[ \t]*>)*[ \t]*(?:`{3,}|~{3,})", body)
        or _protected(old) != _protected(new)
        or old_prefix[0] != new_prefix[0]
        or re.search(r"(?m)^#{1,6}\s+(?:내 이해와 보충|개인 보충|내 메모)", body[:start])
    ):
        raise WoonError("Korean prose edit would change protected code, math, quote or structure")
    # Edits cannot begin/end inside a protected inline construct.
    for token in _protected(line):
        for match in re.finditer(re.escape(token), line):
            a, b = line_start + match.start(), line_start + match.end()
            if (a < start < b) or (a < end < b):
                raise WoonError("Korean prose edit must not split an inline construct")
    return body[:start] + new + body[end:]


def prepare_korean_prose_edits(
    vault: Path,
    current: dict[str, Any] | None,
    replacement: dict[str, Any],
    proofs: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    claims: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate pinned sentences and derive only their reader-span revisions.

    Returns exact new reader bodies and the expected manifest. No writes occur.
    The existing verified-book service supplies locking, compile, scope audit,
    receipts, index rollback and immutable successor source/claim creation.
    """
    if (
        not isinstance(current, dict)
        or current.get("schema_version") != 3
        or current.get("workflow_phase") != "translated"
        or replacement.get("workflow_phase") != "translated"
        or not isinstance(proofs, dict)
        or not proofs
    ):
        raise WoonError("Korean prose edit requires a pinned translated scope")
    expected = copy.deepcopy(current)
    assignments = expected.get("source_element_assignments", [])
    elements = {row["element_id"]: row for row in current.get("source_elements", [])}
    bodies: dict[str, str] = {}
    for owner, proof in proofs.items():
        fields = {
            "source_id",
            "source_record_sha256",
            "page_spec_sha256",
            "reader_sha256",
            "before_body_sha256",
            "after_body_sha256",
            "edits",
        }
        if not isinstance(proof, dict) or set(proof) != fields:
            raise WoonError("Korean prose edit requires exact source, page, body and reader pins")
        page = pages.get(owner, {})
        render = page.get("render", {})
        has_notes = "personal_footnotes" in render
        source = sources.get(render.get("source_id"), {})
        metadata = page.get("frontmatter", {})
        relative = Path(str(page.get("output_path", "")))
        output = vault / "wiki" / relative
        if (
            page.get("page_id") != owner
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != owner + ".md"
            or not output.is_file()
            or any(p.is_symlink() for p in (output, *output.parents))
            or render.get("kind") != "source-body"
            or render.get("source_id") != proof["source_id"]
            or set(render)
            != (
                {"kind", "source_id", "personal_footnotes", "supplemental_claim_ids"}
                if has_notes
                else {"kind", "source_id"}
            )
            or source.get("privacy") != "local-only"
            or metadata.get("reader_language") != "ko"
            or metadata.get("access") != "local-only"
            or metadata.get("publish") is not False
            or proof["source_record_sha256"] != _record_sha(source)
            or proof["page_spec_sha256"] != _record_sha(page)
            or proof["reader_sha256"] != hashlib.sha256(output.read_bytes()).hexdigest()
        ):
            raise WoonError("Korean prose edit source/page changed or contains unsupported notes")
        body = source.get("body")
        edits = proof["edits"]
        if (
            not isinstance(body, str)
            or _sha(body) != proof["before_body_sha256"]
            or not isinstance(edits, list)
            or not edits
            or (not has_notes and body.strip() not in output.read_text(encoding="utf-8"))
        ):
            raise WoonError("Korean prose edit requires an exact existing reader body")
        if has_notes:
            attached = [
                claims[cid]
                for cid in page.get("claim_ids", [])
                if claims is not None and cid in claims
            ]
            displayed = render_personal_footnotes(body, render, attached)
            _, reader = split_markdown(
                strip_generated_wiki_views(output.read_text(encoding="utf-8"))
            )
            if reader.strip() != (f"# {page['title']}\n\n" + displayed).strip():
                raise WoonError("Korean prose personal footnotes differ from the pinned reader")
        seen: set[str] = set()
        original = body
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"before", "after"}:
                raise WoonError("Korean prose edit requires explicit before/after sentences")
            old, new = edit["before"], edit["after"]
            if not isinstance(old, str) or old in seen or original.count(old) != 1:
                raise WoonError("Korean prose edits must be distinct existing sentences")
            seen.add(old)
            if has_notes:
                start, end = body.index(old), body.index(old) + len(old)
                if any(
                    start < _anchor_end(body, note["anchor"]) and body.index(note["anchor"]) < end
                    for note in render["personal_footnotes"]
                ):
                    raise WoonError("Korean prose edit cannot change a personal footnote anchor")
            previous_body = body
            body = _replace_sentence(body, old, new)
            matches = [
                row
                for row in assignments
                if row.get("owner_id") == owner
                and isinstance(row.get("delivery_span"), str)
                and old in row["delivery_span"]
            ]
            if len(matches) != 1:
                raise WoonError("Korean prose sentence needs exactly one owned delivery span")
            row = matches[0]
            element = elements.get(row["element_id"], {})
            span = row["delivery_span"]
            if (
                row.get("delivery") != "reader-span"
                or (element.get("kind"), element.get("semantic_unit"))
                not in {("claim", "paragraph"), ("claim", "list"), ("caution", "caution")}
                or row.get("delivery_span_sha256") != _sha(span)
                or span.count(old) != 1
            ):
                raise WoonError("Korean prose edit cannot change non-prose source evidence")
            span = span.replace(old, new)
            if span not in body:
                raise WoonError("Korean prose delivery span is not in its exact reader body")
            row.update(delivery_span=span, delivery_span_sha256=_sha(span))
            # Wrapper spans pin current delivery, not the immutable archived source.
            for wrapper in expected.get("retired_source_section_wrappers", []):
                if wrapper.get("first_leaf_id") != owner:
                    continue
                relocated = wrapper.get("relocated_delivery_span")
                if not isinstance(relocated, str) or old not in relocated:
                    continue
                if (
                    wrapper.get("relocated_delivery_span_sha256") != _sha(relocated)
                    or previous_body.count(relocated) != 1
                    or relocated.count(old) != 1
                ):
                    raise WoonError("Korean prose edit requires an exact pinned wrapper delivery")
                relocated = relocated.replace(old, new)
                if body.count(relocated) != 1:
                    raise WoonError("Korean prose wrapper delivery must remain unique in its leaf")
                wrapper.update(
                    relocated_delivery_span=relocated,
                    relocated_delivery_span_sha256=_sha(relocated),
                )
        if _sha(body) != proof["after_body_sha256"]:
            raise WoonError("Korean prose edit contains unreviewed body changes")
        if has_notes:
            render_personal_footnotes(body, render, attached)
        bodies[owner] = body
    if replacement != expected:
        raise WoonError("Korean prose edit changed source, owner, phase or unreviewed coverage")
    return bodies, expected
