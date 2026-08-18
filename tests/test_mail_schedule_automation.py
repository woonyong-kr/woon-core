from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from test_orchestration import write_policy

from woon_core.knowledge.mail_schedule_automation import (
    MailScheduleSubmission,
    record_mail_schedule_candidates,
)
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import record_governance_preflight


def test_empty_mail_window_writes_a_receipt_without_a_review_file(tmp_path: Path) -> None:
    settings = _runnable_settings(tmp_path)

    first = record_mail_schedule_candidates(
        tmp_path, run_token="mail-kst-20260817-0600", submissions=()
    )
    second = record_mail_schedule_candidates(
        tmp_path, run_token="mail-kst-20260817-0600", submissions=()
    )

    receipt_path = (
        settings.receipt_directory / "mail-schedule-candidates" / f"{first.receipt_id}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert first.candidate_count == 0
    assert first.replayed is False
    assert second.replayed is True
    assert receipt["candidate_ids"] == []
    assert not (tmp_path / "brain/review/mail").exists()


def test_allowlisted_mail_candidate_is_recorded_without_calendar_access(tmp_path: Path) -> None:
    settings = _runnable_settings(tmp_path)

    result = record_mail_schedule_candidates(
        tmp_path,
        run_token="mail-kst-20260817-1200",
        submissions=(
            MailScheduleSubmission(
                source_locator="gmail-thread:opaque-krafton-001",
                summary="면접 시간 확인이 필요하다.",
                occurred_at=datetime(2026, 8, 17, 3, 0, tzinfo=UTC),
                scheduled_for=datetime(2026, 8, 21, 16, 30, tzinfo=UTC),
            ),
        ),
    )

    assert result.candidate_count == 1
    assert (tmp_path / "brain/review/mail/면접-시간-확인이-필요하다.md").is_file()
    assert not (tmp_path / ".local/woon-knowledge/schedule-apply").exists()
    receipt_path = (
        settings.receipt_directory / "mail-schedule-candidates" / f"{result.receipt_id}.json"
    )
    assert receipt_path.is_file()


def _runnable_settings(vault: Path):
    write_policy(
        vault,
        status="enabled",
        thread_id="fixture-mail-thread",
        codex_automation_id="fixture-mail-automation",
        rrule="FREQ=DAILY;BYHOUR=9",
        notification_policy="failed_runs_only",
        prompt_sha256="a" * 64,
    )
    policy_path = vault / "config/second-brain-orchestrator.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
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
        ),
        encoding="utf-8",
    )
    settings = load_orchestrator_settings(vault)
    record_governance_preflight(settings, input_sha256="a" * 64, output_sha256="b" * 64)
    return settings
