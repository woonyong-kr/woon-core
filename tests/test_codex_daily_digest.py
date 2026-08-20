from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from test_orchestration import write_policy

from woon_core.errors import WoonError
from woon_core.knowledge.codex_daily_digest import (
    CodexDailyDigestEntry,
    entries_from_records,
    record_codex_daily_digest,
    record_daily_digest_from_codex_ledger,
)
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import record_governance_preflight


def test_records_transcript_free_daily_digest_once(tmp_path: Path) -> None:
    settings = _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")
    (tmp_path / "brain/wiki").mkdir(parents=True)
    (tmp_path / "brain/wiki/herdr.md").write_text("# Herdr\n", encoding="utf-8")
    entries = (
        CodexDailyDigestEntry(
            kind="결정",
            title="일일 대화 요약 자동화 추가",
            summary="원문 대신 결정과 다음 행동만 하루 노트에 남기도록 정했다.",
            related_documents=("brain/wiki/herdr.md",),
        ),
    )

    first = record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=entries)
    second = record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=entries)

    path = tmp_path / first.relative_path
    receipt = (
        settings.receipt_directory / "daily-record-materialization" / f"{first.receipt_id}.json"
    )
    assert first.entry_count == 1
    assert second.replayed is True
    assert path.is_file()
    assert "원문 대신 결정과 다음 행동만" in path.read_text(encoding="utf-8")
    assert "[[../../brain/wiki/herdr|Herdr]]" in path.read_text(encoding="utf-8")
    assert receipt.is_file()
    assert json.loads(receipt.read_text(encoding="utf-8"))["candidate_ids"]


def test_rejects_sensitive_or_unrelated_digest_input_without_writing(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")

    with pytest.raises(WoonError, match="safe visible text"):
        record_codex_daily_digest(
            tmp_path,
            day=date(2026, 8, 17),
            entries=(
                CodexDailyDigestEntry(
                    kind="학습",
                    title="비밀값 점검",
                    summary="token=sk-this-must-never-be-persisted",
                ),
            ),
        )
    with pytest.raises(WoonError, match="unsupported fields"):
        entries_from_records(
            [{"kind": "결정", "title": "제목", "summary": "요약", "raw_transcript": "금지"}]
        )
    assert not (tmp_path / "inbox/daily-digests").exists()


def test_creates_a_missing_daily_note_from_the_canonical_template(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    template = tmp_path / "templates" / "daily-note.md"
    template.parent.mkdir(parents=True)
    template.write_text("# {{date}}\n\n![[../daily-digests/{{date}}]]\n", encoding="utf-8")

    record_codex_daily_digest(tmp_path, day=date(2026, 8, 17), entries=())

    assert (
        (tmp_path / "inbox/daily/2026-08-17.md")
        .read_text(encoding="utf-8")
        .startswith("# 2026-08-17")
    )


def test_replaces_only_a_markerless_legacy_generated_digest(tmp_path: Path) -> None:
    _digest_settings(tmp_path)
    (tmp_path / "inbox/daily").mkdir(parents=True)
    (tmp_path / "inbox/daily/2026-08-17.md").write_text("# 2026-08-17\n", encoding="utf-8")
    root = tmp_path / ".local/woon-knowledge/codex-knowledge/2026-08-17"
    root.mkdir(parents=True)
    (root / "entry.json").write_text(
        json.dumps(
            {
                "kind": "활동",
                "title": "면접 준비",
                "summary": "면접 준비 내용을 정리했다.",
                "intent": None,
                "related_documents": [],
                "calendar_contexts": [],
                "people": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "_input-status.json").write_text('{"input_state":"processed"}', encoding="utf-8")
    legacy = tmp_path / "inbox/daily-digests/2026-08-17.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        '---\ntype: DailyDigest\ntitle: "2026-08-17 Codex 하루 정리"\n'
        "publish: false\naccess: local-only\nstatus: Active\ndate: 2026-08-17\n"
        "record_owner: choi-woonyoung\n---\n\n# 2026-08-17 Codex 하루 정리\n\n"
        "## 대화에서 남긴 것\n\n- 이전 자동 요약\n",
        encoding="utf-8",
    )

    result = record_daily_digest_from_codex_ledger(tmp_path, day=date(2026, 8, 17))

    assert result.entry_count == 1
    content = legacy.read_text(encoding="utf-8")
    assert "면접 준비" in content
    assert "<!-- woon-codex-digest:start -->" in content


def _digest_settings(vault: Path):
    write_policy(vault)
    policy = vault / "config/second-brain-orchestrator.yaml"
    original = policy.read_text(encoding="utf-8")
    digest_lane = """
  - id: daily-record-materialization
    owner: daily-record-task
    cadence: daily
    inputs: [codex-opted-in-summaries]
    output: [daily-codex-digest]
    checkpoint_key: daily-record-materialization
    required_signals: [kst-day, privacy-classification]
    prohibited: [raw-transcript-ingest, system-prompt-ingest, tool-output-ingest, reasoning-ingest,
      person-profile-inference, unresolved-identity-link]
    execution:
      mode: materialize
      status: enabled
      task_thread_id: fixture-daily-thread
      codex_automation_id: fixture-daily-automation
      rrule: FREQ=DAILY;BYHOUR=23;BYMINUTE=55;BYSECOND=0
      notification_policy: failed_runs_only
      prompt_sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
      owned_paths: [inbox/daily, inbox/daily-digests]
"""
    runnable = original.replace(
        """mode: proposal-only
      status: planned
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null""",
        """mode: proposal-only
      status: enabled
      task_thread_id: fixture-governance-thread
      codex_automation_id: fixture-governance-automation
      rrule: FREQ=DAILY;BYHOUR=8
      notification_policy: failed_runs_only
      prompt_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb""",
    )
    policy.write_text(
        runnable.replace("cursor_contract:\n", digest_lane + "cursor_contract:\n"),
        encoding="utf-8",
    )
    settings = load_orchestrator_settings(vault)
    record_governance_preflight(settings, input_sha256="a" * 64, output_sha256="b" * 64)
    return settings
