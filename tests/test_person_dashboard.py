from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.people.dashboard import (
    PERSON_DASHBOARD_BASE_RELATIVE_PATH,
    PersonDashboardProjection,
    is_core_person_dashboard_base,
)


def test_refresh_creates_a_date_aware_person_dashboard_idempotently(tmp_path: Path) -> None:
    service = PersonDashboardProjection(tmp_path)

    first = service.refresh()
    second = service.refresh()
    path = tmp_path / PERSON_DASHBOARD_BASE_RELATIVE_PATH
    content = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)

    assert first.changed is True
    assert second.changed is False
    assert is_core_person_dashboard_base(path) is True
    assert [view["name"] for view in parsed["views"]] == [
        "최근 색인 문서",
        "다가오는 일정",
        "프로젝트·학습·자료",
        "지난 일정",
    ]
    assert "people.contains(this)" in content
    assert "Date >= today()" in content
    assert "Date < today()" in content
    assert 'displayName: "시간"' in content
    assert path.stat().st_mode & 0o777 == 0o400


def test_refresh_migrates_only_the_known_legacy_base(tmp_path: Path) -> None:
    path = tmp_path / PERSON_DASHBOARD_BASE_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        """filters:
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
""",
        encoding="utf-8",
    )

    result = PersonDashboardProjection(tmp_path).refresh()

    assert result.changed is True
    assert is_core_person_dashboard_base(path) is True


def test_refresh_refuses_to_overwrite_an_unknown_base(tmp_path: Path) -> None:
    path = tmp_path / PERSON_DASHBOARD_BASE_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text("views: []\n# personal query\n", encoding="utf-8")

    with pytest.raises(WoonError, match="not the known legacy"):
        PersonDashboardProjection(tmp_path).refresh()

    assert path.read_text(encoding="utf-8") == "views: []\n# personal query\n"
