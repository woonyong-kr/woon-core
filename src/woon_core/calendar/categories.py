"""Stable, user-visible categories for Woon-owned Calendar events."""

from __future__ import annotations

from typing import Final

CALENDAR_CATEGORY_TITLES: Final[dict[str, str]] = {
    "career": "커리어",
    "learning": "학습",
    "creative": "창작",
    "life": "생활",
    "relationship": "관계",
    "health": "건강",
    "admin": "행정",
}
CALENDAR_CATEGORY_IDS: Final[frozenset[str]] = frozenset(CALENDAR_CATEGORY_TITLES)
UNCATEGORIZED_CALENDAR_CATEGORY_ID: Final = "other"
UNCATEGORIZED_CALENDAR_CATEGORY_TITLE: Final = "기타"


def calendar_category_title(category_id: str | None) -> str:
    """Return the Korean display name without inferring a category from an event title."""

    if category_id is None:
        return UNCATEGORIZED_CALENDAR_CATEGORY_TITLE
    return CALENDAR_CATEGORY_TITLES.get(category_id, UNCATEGORIZED_CALENDAR_CATEGORY_TITLE)
