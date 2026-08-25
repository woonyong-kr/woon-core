from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


def _load_backfill_module():
    script = Path(__file__).parents[1] / "scripts" / "backfill_codex_sources.py"
    spec = importlib.util.spec_from_file_location("backfill_codex_sources", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rows_keep_readable_candidates_and_expose_user_event_state(tmp_path: Path) -> None:
    state_db = tmp_path / "state.sqlite"
    connection = sqlite3.connect(state_db)
    connection.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            rollout_path TEXT NOT NULL,
            source TEXT NOT NULL,
            has_user_event INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    rows = (
        ("user-thread", "사용자 작업", "/tmp/user.jsonl", "vscode", 1, 1),
        ("system-thread", "자동화 작업", "/tmp/missing.jsonl", "vscode", 0, 2),
        ("cli-thread", "CLI 작업", "/tmp/cli.jsonl", "cli", 1, 3),
    )
    connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()

    module = _load_backfill_module()

    selected = list(module._rows(state_db))

    assert [row["id"] for row in selected] == ["user-thread", "system-thread"]
    assert [row["has_user_event"] for row in selected] == [1, 0]
