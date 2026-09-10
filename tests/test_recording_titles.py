import json

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.recording_titles import (
    validate_recording_catalog_change,
    validate_recording_title_change,
)


def test_recording_title_change_preserves_dates_relations_and_source_text() -> None:
    before = (
        "---\ntitle: 옛 제목\nrecording_id: record-one\nrecord_kind: recording\n"
        "access: local-only\npublication_state: private\npublish: false\n"
        "recorded_at: 2026-05-16\ncandidate_person_ids: [unconfirmed]\n"
        "audio_verified: false\n---\n\n# 옛 제목\n\n보존할 전사 원문\n"
    ).encode()
    after = before.replace("옛 제목".encode(), "새 제목".encode())
    assert validate_recording_title_change(before, after, "record-one") == "새 제목"
    for changed in (
        after.replace(b"2026-05-16", b"2026-09-11"),
        after.replace(b"[unconfirmed]", b"[confirmed-person]"),
        after.replace("보존할 전사 원문".encode(), "교정한 전사 원문".encode()),
        after.replace(b"audio_verified: false", b"audio_verified: true"),
    ):
        with pytest.raises(WoonError):
            validate_recording_title_change(before, changed, "record-one")


def test_recording_catalog_preserves_identity_history_and_provider_state() -> None:
    original = {
        "access": "local-only",
        "publication_state": "private",
        "records": [
            {
                "recording_id": "record-one",
                "reader_path": "private/old.md",
                "title": "옛 제목",
                "previous_reader_path": "private/original.md",
                "original_title": "원자료 이름",
                "audio_sha256": "original-audio-hash",
                "remote_title_state": "not-verified",
            }
        ],
    }
    before = json.dumps(original).encode()
    revised = json.loads(before)
    revised["records"][0].update(reader_path="private/new.md", title="새 제목")
    renames = {"record-one": ("private/old.md", "private/new.md", "새 제목")}
    validate_recording_catalog_change(before, json.dumps(revised).encode(), renames)
    for field in ("previous_reader_path", "original_title", "audio_sha256", "remote_title_state"):
        changed = json.loads(json.dumps(revised))
        changed["records"][0][field] = "changed"
        with pytest.raises(WoonError):
            validate_recording_catalog_change(before, json.dumps(changed).encode(), renames)
