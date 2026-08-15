from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_orchestration import write_policy

from woon_core.errors import WoonError
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_candidates import (
    MailScheduleInput,
    candidate_from_allowlisted_mail,
)
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunOutcome,
    RunRequest,
    snapshot_owned_paths,
)


def _request(settings: object, vault: Path, *, cursor: str = "cursor-2") -> RunRequest:
    return RunRequest(
        source_range="fixture-range-001",
        input_sha256=hashlib.sha256(b"safe fixture input").hexdigest(),
        expected_owned_revision=snapshot_owned_paths(vault, ("brain/review/mail",)),
        cursor_after=cursor,
    )


def _write_runnable_policy(vault: Path) -> None:
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


def test_planned_lane_cannot_create_runtime_state(tmp_path: Path) -> None:
    write_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)

    with pytest.raises(WoonError, match="not enabled"):
        AutomationRunStore(settings).run(
            "mail-schedule-candidates",
            _request(settings, tmp_path),
            lambda: RunOutcome(candidate_ids=(), output_sha256="a" * 64),
        )

    assert not settings.checkpoint_path.exists()


def test_commits_receipt_before_checkpoint_and_replays_same_operation(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    store = AutomationRunStore(settings)
    request = _request(settings, tmp_path)
    calls = 0

    def produce() -> RunOutcome:
        nonlocal calls
        calls += 1
        return RunOutcome(candidate_ids=("candidate-001",), output_sha256="a" * 64)

    first = store.run("mail-schedule-candidates", request, produce)
    second = store.run("mail-schedule-candidates", request, produce)

    assert calls == 1
    assert first.replayed is False
    assert second.replayed is True
    checkpoint = json.loads(settings.checkpoint_path.read_text(encoding="utf-8"))
    lane = checkpoint["lanes"]["mail-schedule-candidates"]
    assert lane["cursor"] == "cursor-2"
    assert lane["receipt_id"] == first.receipt_id
    receipt_path = (
        settings.receipt_directory / "mail-schedule-candidates" / f"{first.receipt_id}.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt == {
        "automation_id": "mail-schedule-candidates",
        "candidate_ids": ["candidate-001"],
        "cursor_after": "cursor-2",
        "input_sha256": request.input_sha256,
        "operation_id": first.receipt_id,
        "output_sha256": "a" * 64,
        "policy_sha256": settings.policy_sha256,
        "source_range": "fixture-range-001",
        "version": 1,
    }


def test_failure_keeps_checkpoint_immutable_and_cleans_the_run_lock(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    store = AutomationRunStore(settings)
    request = _request(settings, tmp_path)

    with pytest.raises(WoonError, match="candidate producer failed"):
        store.run(
            "mail-schedule-candidates",
            request,
            lambda: (_ for _ in ()).throw(RuntimeError("connection reset")),
        )

    assert not settings.checkpoint_path.exists()
    assert not (settings.receipt_directory / "mail-schedule-candidates").exists()

    result = store.run(
        "mail-schedule-candidates",
        request,
        lambda: RunOutcome(candidate_ids=(), output_sha256="b" * 64),
    )
    assert result.replayed is False


def test_rejects_changed_owned_path_before_candidate_producer_runs(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    request = _request(settings, tmp_path)
    changed = tmp_path / "brain/review/mail/manual-review.md"
    changed.parent.mkdir(parents=True)
    changed.write_text("user edit\n", encoding="utf-8")
    store = AutomationRunStore(settings)
    calls = 0

    def produce() -> RunOutcome:
        nonlocal calls
        calls += 1
        return RunOutcome(candidate_ids=(), output_sha256="c" * 64)

    with pytest.raises(WoonError, match="owned paths changed"):
        store.run("mail-schedule-candidates", request, produce)

    assert calls == 0
    assert not settings.checkpoint_path.exists()


def test_rejects_malformed_checkpoint_without_overwriting_it(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    settings.checkpoint_path.parent.mkdir(parents=True)
    settings.checkpoint_path.write_text("[]\n", encoding="utf-8")
    original = settings.checkpoint_path.read_bytes()

    with pytest.raises(WoonError, match="checkpoint must be a mapping"):
        AutomationRunStore(settings).run(
            "mail-schedule-candidates",
            _request(settings, tmp_path),
            lambda: RunOutcome(candidate_ids=(), output_sha256="d" * 64),
        )

    assert settings.checkpoint_path.read_bytes() == original


def test_candidate_run_writes_only_the_lane_owned_review_path(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    candidate = candidate_from_allowlisted_mail(
        MailScheduleInput(
            source_locator="gmail-thread:opaque-krafton-001",
            classification="allowlisted",
            actionable=True,
            summary="크래프톤 면접 일정 확인이 필요하다.",
            occurred_at=datetime(2026, 8, 15, tzinfo=UTC),
            scheduled_for=None,
        )
    )
    assert candidate is not None
    request = _request(settings, tmp_path)

    result = AutomationRunStore(settings).run_review_candidates(
        "mail-schedule-candidates", request, (candidate,)
    )

    assert (tmp_path / "brain/review/mail" / f"{candidate.candidate_id}.md").is_file()
    assert not (tmp_path / "brain/review/codex" / f"{candidate.candidate_id}.md").exists()
    assert result.replayed is False


def test_rejects_proposal_lane_from_using_candidate_writer(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    policy_path = tmp_path / "config/second-brain-orchestrator.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8").replace(
            "mode: candidate-only", "mode: proposal-only", 1
        ),
        encoding="utf-8",
    )
    settings = load_orchestrator_settings(tmp_path)
    request = RunRequest(
        source_range="fixture-range-governance",
        input_sha256=hashlib.sha256(b"proposal input").hexdigest(),
        expected_owned_revision=snapshot_owned_paths(tmp_path, ("brain/review/mail",)),
        cursor_after="proposal-cursor-1",
    )

    with pytest.raises(WoonError, match="requires candidate-only lane"):
        AutomationRunStore(settings).run_review_candidates("mail-schedule-candidates", request, ())


def test_policy_change_blocks_existing_lane_until_governance_preflight(tmp_path: Path) -> None:
    _write_runnable_policy(tmp_path)
    initial = load_orchestrator_settings(tmp_path)
    initial_request = _request(initial, tmp_path, cursor="cursor-before-policy-change")
    AutomationRunStore(initial).run(
        "mail-schedule-candidates",
        initial_request,
        lambda: RunOutcome(candidate_ids=(), output_sha256="e" * 64),
    )

    policy_path = tmp_path / "config/second-brain-orchestrator.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8") + "\n# policy revision for fixture\n",
        encoding="utf-8",
    )
    revised = load_orchestrator_settings(tmp_path)
    mail_request = RunRequest(
        source_range="fixture-range-after-policy-change",
        input_sha256=hashlib.sha256(b"new safe fixture input").hexdigest(),
        expected_owned_revision=snapshot_owned_paths(tmp_path, ("brain/review/mail",)),
        cursor_after="cursor-after-policy-change",
    )
    calls = 0

    def produce_mail() -> RunOutcome:
        nonlocal calls
        calls += 1
        return RunOutcome(candidate_ids=(), output_sha256="f" * 64)

    with pytest.raises(WoonError, match="requires governance preflight"):
        AutomationRunStore(revised).run("mail-schedule-candidates", mail_request, produce_mail)
    assert calls == 0

    governance_request = RunRequest(
        source_range="governance-policy-revision",
        input_sha256=hashlib.sha256(b"current policy inventory").hexdigest(),
        expected_owned_revision=snapshot_owned_paths(tmp_path, ("brain/review/governance",)),
        cursor_after="governance-policy-revision",
    )
    AutomationRunStore(revised).run(
        "governance-audit",
        governance_request,
        lambda: RunOutcome(candidate_ids=(), output_sha256="a" * 64),
    )

    result = AutomationRunStore(revised).run("mail-schedule-candidates", mail_request, produce_mail)
    assert result.replayed is False
    assert calls == 1
