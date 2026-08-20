"""Tool-facing boundary for receipt-first mail schedule candidate runs.

Gmail classification stays in the connector task. This module only accepts
already-minimized allowlisted candidates, so an empty run can advance safely
without inspecting a Calendar payload or retaining any mail content.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_candidates import (
    MailScheduleInput,
    ReviewCandidate,
    candidate_from_allowlisted_mail,
)
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunRequest,
    snapshot_owned_paths,
)

_RUN_TOKEN = re.compile(r"mail-kst-[0-9]{8}-[0-9]{4}")


@dataclass(frozen=True, slots=True)
class MailScheduleSubmission:
    """Minimal, raw-content-free input for one already-allowlisted mail candidate."""

    source_locator: str
    summary: str
    occurred_at: datetime
    scheduled_for: datetime | date | None


@dataclass(frozen=True, slots=True)
class MailScheduleRecordResult:
    """Receipt result for one deterministic mail polling window."""

    candidate_count: int
    receipt_id: str
    replayed: bool


def record_mail_schedule_candidates(
    vault: Path,
    *,
    run_token: str,
    submissions: tuple[MailScheduleSubmission, ...],
) -> MailScheduleRecordResult:
    """Persist zero or more already-allowlisted candidates without Calendar access.

    A run token identifies one KST polling window. The runtime stores only
    hashes in its receipt; human review files receive a bounded summary only
    when a candidate exists. Calendar application is a separate local policy
    action and is intentionally not reachable from this candidate-only path.
    """

    if not _RUN_TOKEN.fullmatch(run_token):
        raise WoonError("mail run_token must use mail-kst-YYYYMMDD-HHMM")
    settings = load_orchestrator_settings(vault)
    contract = next(
        (item for item in settings.automations if item.automation_id == "mail-schedule-candidates"),
        None,
    )
    if contract is None or contract.mode != "candidate-only" or contract.status != "enabled":
        raise WoonError("mail schedule candidate automation is not enabled")

    candidates = tuple(_candidate_from_submission(item) for item in submissions)
    payload = {
        "run_token": run_token,
        "candidates": [item.as_record() for item in candidates],
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    request = RunRequest(
        source_range=run_token,
        input_sha256=digest,
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=run_token,
    )
    result = AutomationRunStore(settings).run_review_candidates(
        "mail-schedule-candidates", request, candidates
    )
    return MailScheduleRecordResult(
        candidate_count=len(candidates), receipt_id=result.receipt_id, replayed=result.replayed
    )


def submissions_from_records(
    records: list[dict[str, object]],
) -> tuple[MailScheduleSubmission, ...]:
    """Parse the bounded MCP/CLI payload without accepting mail body fields."""

    submissions: list[MailScheduleSubmission] = []
    for raw in records:
        allowed = {"source_locator", "summary", "occurred_at", "scheduled_for"}
        required = {"source_locator", "summary", "occurred_at"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError(
                "mail candidate requires source_locator, summary, occurred_at, "
                "and optional scheduled_for"
            )
        source_locator = raw["source_locator"]
        summary = raw["summary"]
        if not isinstance(source_locator, str) or not isinstance(summary, str):
            raise WoonError("mail source_locator and summary must be strings")
        submissions.append(
            MailScheduleSubmission(
                source_locator=source_locator,
                summary=summary,
                occurred_at=_parse_aware_datetime(raw["occurred_at"], "occurred_at"),
                scheduled_for=_parse_scheduled_for(raw.get("scheduled_for")),
            )
        )
    return tuple(submissions)


def _candidate_from_submission(submission: MailScheduleSubmission) -> ReviewCandidate:
    candidate = candidate_from_allowlisted_mail(
        MailScheduleInput(
            source_locator=submission.source_locator,
            classification="allowlisted",
            actionable=True,
            summary=submission.summary,
            occurred_at=submission.occurred_at,
            scheduled_for=submission.scheduled_for,
        )
    )
    if candidate is None:  # Defensive: the fixed allowlisted inputs above must create a candidate.
        raise WoonError("allowlisted mail candidate could not be created")
    return candidate


def _parse_aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise WoonError(f"{field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WoonError(f"{field} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WoonError(f"{field} must include a timezone")
    return parsed


def _parse_scheduled_for(value: object) -> datetime | date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WoonError("scheduled_for must be an ISO date, ISO datetime, or null")
    if "T" in value:
        return _parse_aware_datetime(value, "scheduled_for")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise WoonError("scheduled_for must be an ISO date, ISO datetime, or null") from error
