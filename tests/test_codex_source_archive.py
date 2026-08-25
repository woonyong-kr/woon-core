from __future__ import annotations

import json
import stat
from datetime import date
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.codex_source_archive import (
    CodexSourceBundle,
    CodexSourceMessage,
    bundle_from_record,
    load_codex_source_bundles,
    record_codex_source_bundle,
    redact_secrets,
)


def test_records_allowed_messages_redacts_secrets_and_replays(tmp_path: Path) -> None:
    bundle = CodexSourceBundle(
        day=date(2026, 8, 25),
        source_locator="thread-1:2026-08-25",
        title="로컬 대화 증거를 보존한다",
        messages=(
            CodexSourceMessage(
                role="user",
                text="KvmNxlFWT_aNZ1Zp9mhNEQ 이게 띵3 토큰이네요",
                created_at="2026-08-25T00:00:00+09:00",
            ),
            CodexSourceMessage(
                role="assistant",
                text="토큰은 화면이나 Git에 노출하지 않습니다.",
                created_at="2026-08-25T00:01:00+09:00",
            ),
        ),
    )

    first = record_codex_source_bundle(tmp_path, bundle)
    second = record_codex_source_bundle(tmp_path, bundle)

    path = next((tmp_path / "wiki/private/_sources/codex/2026-08-25").glob("*.json"))
    value = json.loads(path.read_text(encoding="utf-8"))
    assert first.replayed is False
    assert second.replayed is True
    assert "KvmNxlFWT_aNZ1Zp9mhNEQ" not in path.read_text(encoding="utf-8")
    assert "[민감정보 숨김]" in value["messages"][0]["text"]
    assert value.get("source_locator") is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_allows_only_append_and_rejects_other_roles(tmp_path: Path) -> None:
    first = CodexSourceBundle(
        day=date(2026, 8, 25),
        source_locator="thread-2:2026-08-25",
        title="추가 메시지만 허용한다",
        messages=(
            CodexSourceMessage(role="user", text="첫 질문", created_at="2026-08-25T01:00:00Z"),
        ),
    )
    record_codex_source_bundle(tmp_path, first)
    appended = CodexSourceBundle(
        day=first.day,
        source_locator=first.source_locator,
        title=first.title,
        messages=first.messages
        + (
            CodexSourceMessage(role="assistant", text="첫 답변", created_at="2026-08-25T01:01:00Z"),
        ),
    )
    record_codex_source_bundle(tmp_path, appended)
    rewritten = CodexSourceBundle(
        day=first.day,
        source_locator=first.source_locator,
        title=first.title,
        messages=(
            CodexSourceMessage(role="user", text="바뀐 질문", created_at="2026-08-25T01:00:00Z"),
        ),
    )
    with pytest.raises(WoonError, match="append without rewriting"):
        record_codex_source_bundle(tmp_path, rewritten)
    with pytest.raises(WoonError, match="role or text"):
        bundle_from_record(
            {
                "day": "2026-08-25",
                "source_locator": "thread-3",
                "title": "도구 출력 금지",
                "messages": [
                    {
                        "role": "tool",
                        "text": "금지",
                        "created_at": "2026-08-25T01:00:00Z",
                    }
                ],
            }
        )
    loaded = load_codex_source_bundles(tmp_path, day=date(2026, 8, 25))
    assert loaded[0]["messages"][-1]["text"] == "첫 답변"


def test_redaction_preserves_program_tokens() -> None:
    value = 'token = strtok_r(fn_copy, " ", &save_point);\npassword: actual-secret'

    redacted = redact_secrets(value)

    assert 'token = strtok_r(fn_copy, " ", &save_point);' in redacted
    assert "actual-secret" not in redacted
