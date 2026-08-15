from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.schedule_apply import _load_candidate


def _write_candidate(vault: Path, **changes: object) -> Path:
    values: dict[str, object] = {
        "candidate_id": "candidate-001",
        "source_id": "gmail-thread:opaque-001",
        "activity_id": "activity-001",
        "intent": "직무 면접",
        "timezone": "Asia/Seoul",
        "start_at": "2026-08-21T16:30:00+00:00",
        "end_at": "2026-08-21T17:00:00+00:00",
        "authorized_at": "2026-08-15T12:00:00+00:00",
        "lifecycle": "create",
        "idempotency_key": "schedule-001",
        "area_id": "career",
        "things_tags": ["컴퓨터", "일정"],
        "bridge_revision": 1,
    }
    values.update(changes)
    path = vault / ".local/woon-knowledge/schedule-apply/candidate-001.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return path


def test_loads_only_local_policy_authorized_schedule_candidate(tmp_path: Path) -> None:
    path = _write_candidate(tmp_path)

    candidate = _load_candidate(tmp_path, path)

    assert candidate.area_id == "career"
    assert candidate.things_tags == ("컴퓨터", "일정")
    assert candidate.bridge_revision == 1
    assert candidate.authorized_at == datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def test_rejects_schedule_candidate_outside_policy_apply_root(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(WoonError, match="under .local/woon-knowledge/schedule-apply"):
        _load_candidate(tmp_path, path)
