"""Shared validation for stable Wiki canonical identities."""

from __future__ import annotations

import re

from woon_core.errors import WoonError


def validate_canonical_id(value: str) -> str:
    """Validate one path-like identity without rewriting its Unicode or case."""

    canonical_id = value.strip()
    if not canonical_id or canonical_id != value or len(canonical_id) > 160:
        raise _invalid_canonical_id()
    segments = canonical_id.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise _invalid_canonical_id()
    for segment in segments:
        if not segment[0].isalnum() or not segment[-1].isalnum():
            raise _invalid_canonical_id()
        if any(not (character.isalnum() or character == "-") for character in segment):
            raise _invalid_canonical_id()
    return canonical_id


def is_book_scoped_canonical_id(value: str) -> bool:
    """Return whether a page identity is scoped below one book chapter or appendix."""

    return any(
        re.fullmatch(r"chapter-\d{2}|appendix-[a-z]", segment) is not None
        for segment in value.split("/")
    )


def _invalid_canonical_id() -> WoonError:
    return WoonError(
        "canonical_id must be a slash-separated stable path using letters, "
        "numbers, and internal hyphens, such as personal/자격-준비"
    )
