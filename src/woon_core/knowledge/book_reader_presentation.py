"""Render personal book notes without editing source or supplement payloads."""

from __future__ import annotations

import re
from typing import Any

from woon_core.errors import WoonError

FOOTNOTE_PREFIX = "wn-personal-"
_NOTE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_FENCE_LINE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
_INLINE = re.compile(
    r"(?P<ticks>`+)(?!`)[^\n]*?(?P=ticks)(?!`)"
    r"|!?\[\[[^\n]*?\]\]"
    r"|!?\[[^\]\n]*\]\([^\n]*?\)"
    r"|!?\[[^\]\n]*\]\[[^\]\n]*\]"
    r"|<[^>\n]+>|https?://[^\s<>]+"
)


def _fenced_lines(text: str) -> list[tuple[int, int]]:
    """Return full fence spans, including an unfinished fence through EOF."""
    spans: list[tuple[int, int]] = []
    opening: tuple[str, int, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE_LINE.match(line.rstrip("\r\n"))
        if match:
            fence, tail = match.groups()
            if opening is None:
                opening = (fence[0], len(fence), offset)
            elif fence[0] == opening[0] and len(fence) >= opening[1] and not tail.strip():
                spans.append((opening[2], offset + len(line)))
                opening = None
        offset += len(line)
    if opening is not None:
        spans.append((opening[2], len(text)))
    return spans


def _anchor_end(source_body: str, anchor: str) -> int:
    if not anchor.strip() or "\n" in anchor or "\r" in anchor:
        raise WoonError("personal footnote anchor must be one non-empty source span")
    positions = [
        match.start() for match in re.finditer(r"(?=" + re.escape(anchor) + r")", source_body)
    ]
    if len(positions) != 1:
        raise WoonError("personal footnote anchor must occur exactly once in the source")
    start = positions[0]
    end = start + len(anchor)
    if any(start < stop and begin < end for begin, stop in _fenced_lines(source_body)):
        raise WoonError("personal footnote anchor must not enter a code fence")
    # Inserting after a complete inline code/link is valid; inserting inside
    # its delimiter, URL or label changes the source expression.
    for match in _INLINE.finditer(source_body):
        if (
            match.start() < end < match.end()
            or match.start() < start < match.end()
            or (
                match[0].startswith(("http://", "https://", "<http"))
                and start < match.end()
                and match.start() < end
            )
        ):
            raise WoonError("personal footnote anchor must not enter inline code, a link or URL")
    line_start = source_body.rfind("\n", 0, start) + 1
    line_end = source_body.find("\n", end)
    line = source_body[line_start : line_end if line_end >= 0 else None]
    if line.startswith(("    ", "\t")) or re.match(r"\s*\[\^.+?\]:", line):
        raise WoonError("personal footnote anchor must be in the source prose")
    return end


def _note_display(markdown: str) -> str:
    """Demote note headings while leaving code and the stored claim unchanged."""
    protected = _fenced_lines(markdown)
    lines: list[str] = []
    offset = 0
    for line in markdown.splitlines(keepends=True):
        original_length = len(line)
        if not any(begin <= offset < end for begin, end in protected):
            heading = re.match(r"^#{1,6} (.+?)(?:[ \t]+#+)?[ \t]*(\r?\n)?$", line)
            if heading:
                line = f"**{heading[1]}**" + (heading[2] or "")
        lines.append(line)
        # Offsets refer to the original claim, not its displayed headings.
        offset += original_length
    return "".join(lines).strip()


def render_personal_footnotes(
    source_body: str, render: dict[str, Any], claims: list[dict[str, Any]]
) -> str:
    """Validate exact anchors and render every declared supplement once.

    Definitions retain supplemental claim order, preserving runnable indices.
    This pure function never runs code or changes canonical input records.
    """
    notes = render.get("personal_footnotes")
    if render.get("kind") != "source-body" or not isinstance(notes, list) or not notes:
        raise WoonError("personal_footnotes requires a non-empty source-body note list")
    claim_ids = render.get("supplemental_claim_ids", [])
    if not isinstance(claim_ids, list) or not claim_ids:
        raise WoonError("personal footnotes require declared supplemental claims")
    by_claim = {claim["claim_id"]: claim for claim in claims}
    seen_ids: set[str] = set()
    seen_positions: set[int] = set()
    ordered_claim_ids: list[str] = []
    insertions: list[tuple[int, str]] = []
    definitions: list[str] = []
    if f"[^{FOOTNOTE_PREFIX}" in source_body:
        raise WoonError("personal footnote ID prefix collides with the source")
    for note in notes:
        if not isinstance(note, dict) or set(note) != {"id", "claim_id", "anchor"}:
            raise WoonError("personal footnotes require id, claim_id and exact anchor")
        note_id, claim_id, anchor = note["id"], note["claim_id"], note["anchor"]
        if (
            not isinstance(note_id, str)
            or _NOTE_ID.fullmatch(note_id) is None
            or note_id in seen_ids
        ):
            raise WoonError("personal footnote IDs must be unique stable slugs")
        if (
            not isinstance(claim_id, str)
            or claim_id not in claim_ids
            or not isinstance(anchor, str)
        ):
            raise WoonError("personal footnote must identify an attached supplement and anchor")
        claim = by_claim.get(claim_id, {})
        markdown = claim.get("markdown")
        if (
            claim.get("status") != "accepted"
            or claim.get("kind") != "curated-document"
            or not isinstance(markdown, str)
            or not markdown.strip()
        ):
            raise WoonError("personal footnote requires a non-empty accepted curated claim")
        if f"[^{FOOTNOTE_PREFIX}" in markdown:
            raise WoonError("personal footnote ID prefix collides with a supplement")
        position = _anchor_end(source_body, anchor)
        if position in seen_positions:
            raise WoonError("personal footnotes must not repeat the same anchor")
        seen_ids.add(note_id)
        seen_positions.add(position)
        ordered_claim_ids.append(claim_id)
        marker = f"[^{FOOTNOTE_PREFIX}{note_id}]"
        insertions.append((position, marker))
        display_lines = _note_display(markdown).splitlines()
        definitions.append(
            marker
            + ": "
            + display_lines[0]
            + "\n"
            + "\n".join("    " + line if line else "" for line in display_lines[1:])
        )
    if ordered_claim_ids != claim_ids or len(set(ordered_claim_ids)) != len(ordered_claim_ids):
        raise WoonError(
            "personal footnotes must cover supplemental claims once in their original order"
        )
    body = source_body
    for position, marker in sorted(insertions, reverse=True):
        body = body[:position] + marker + body[position:]
    return body.rstrip() + "\n\n---\n\n" + "\n\n".join(definitions).rstrip() + "\n"
