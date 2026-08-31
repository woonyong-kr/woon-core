"""Generate the shared Obsidian Base embedded by every person entity page."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.io import atomic_write

PERSON_DASHBOARD_BASE_RELATIVE_PATH = "inbox/person-indexed-docs.base"
_PROJECTION_MARKER = "# woon_projection: person-dashboard-base\n"
_READONLY_FILE_MODE = 0o400

_LEGACY_BASE = """filters:
  and:
    - file.ext == "md"
    - file.path != this.file.path
    - or:
        - people.contains(this)
        - record_owner == this.person_id
views:
  - type: table
    name: "최근 색인 문서"
    limit: 30
    order:
      - file.name
      - title
      - type
      - status
      - record_owner
      - person_roles
      - attributions
      - parent
      - file.ctime
      - file.mtime
    sort:
      - property: file.ctime
        direction: DESC
      - property: file.mtime
        direction: DESC
"""


@dataclass(frozen=True, slots=True)
class PersonDashboardProjectionResult:
    """One deterministic refresh of the shared person projection."""

    changed: bool
    relative_path: str


class PersonDashboardProjection:
    """Own a semantic, date-aware view over explicit person relationships."""

    def __init__(self, vault: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._path = self._vault / PERSON_DASHBOARD_BASE_RELATIVE_PATH

    def refresh(self) -> PersonDashboardProjectionResult:
        """Create or refresh the Base without overwriting an unknown user file."""

        content = _render_base()
        previous = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        if previous and _PROJECTION_MARKER not in previous and previous != _LEGACY_BASE:
            raise WoonError(
                "person dashboard Base is not the known legacy view or a Core projection"
            )
        changed = previous != content
        if changed:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, content.encode("utf-8"), mode=_READONLY_FILE_MODE)
        elif self._path.stat().st_mode & 0o777 != _READONLY_FILE_MODE:
            self._path.chmod(_READONLY_FILE_MODE)
            changed = True
        return PersonDashboardProjectionResult(
            changed=changed,
            relative_path=PERSON_DASHBOARD_BASE_RELATIVE_PATH,
        )


def is_core_person_dashboard_base(path: Path) -> bool:
    """Return whether a Base is the exact deterministic Core projection."""

    return path.is_file() and path.read_text(encoding="utf-8") == _render_base()


def _render_base() -> str:
    """Render one person-relative dashboard with explicit time semantics."""

    return """# woon_projection: person-dashboard-base
filters:
  and:
    - file.ext == "md"
    - file.path != this.file.path
    - or:
        - people.contains(this)
        - record_owner == this.person_id
properties:
  title:
    displayName: "문서"
  Date:
    displayName: "날짜"
  Time:
    displayName: "시간"
  Start Date:
    displayName: "시작"
  End Date:
    displayName: "종료"
  Category:
    displayName: "분류"
  type:
    displayName: "종류"
  status:
    displayName: "상태"
views:
  - type: table
    name: "최근 색인 문서"
    limit: 40
    order:
      - title
      - type
      - status
      - Date
      - Time
      - Category
      - parent
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
  - type: table
    name: "다가오는 일정"
    limit: 30
    filters:
      and:
        - type == "calendar-event"
        - Date >= today()
    order:
      - title
      - Date
      - Time
      - Category
    sort:
      - property: Date
        direction: ASC
      - property: Start Date
        direction: ASC
  - type: table
    name: "프로젝트·학습·자료"
    limit: 40
    filters:
      and:
        - type != "calendar-event"
    order:
      - title
      - type
      - status
      - parent
      - file.mtime
    sort:
      - property: file.mtime
        direction: DESC
  - type: table
    name: "지난 일정"
    limit: 40
    filters:
      and:
        - type == "calendar-event"
        - Date < today()
    order:
      - title
      - Date
      - Time
      - Category
    sort:
      - property: Date
        direction: DESC
      - property: Start Date
        direction: DESC
"""
