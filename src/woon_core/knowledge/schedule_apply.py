"""Policy-authorized local entry point for the macOS schedule bridge."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from woon_core.errors import WoonError
from woon_core.knowledge.macos_schedule_adapters import MacOSCalendarPort
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.schedule_bridge import (
    ScheduleBridge,
    ScheduleCandidate,
    ScheduleReceipt,
)


def apply_policy_authorized_schedule_candidate(
    vault: Path, candidate_path: Path
) -> ScheduleReceipt:
    """Apply one strict, allowlisted-mail schedule candidate exactly once."""

    settings = load_orchestrator_settings(vault)
    contract = next(
        (item for item in settings.automations if item.automation_id == "policy-schedule-apply"),
        None,
    )
    if contract is None or contract.mode != "policy-authorized" or contract.status != "local-only":
        raise WoonError("schedule apply policy must remain locally policy-authorized")
    candidate = _load_candidate(settings.vault, candidate_path)
    if not candidate.source_id.startswith("gmail-thread:"):
        raise WoonError("automatic schedule apply accepts allowlisted Gmail candidates only")
    if candidate.authorized_at is None:
        raise WoonError("schedule candidate has no policy authorization timestamp")
    state_path = settings.receipt_directory.parent / "schedule-bridge-state.json"
    bridge = ScheduleBridge(MacOSCalendarPort(), state_path=state_path)
    return bridge.apply(candidate)


def receipt_record(receipt: ScheduleReceipt) -> dict[str, object]:
    """Render only the local result fields needed for immediate user review."""

    return asdict(receipt)


def _load_candidate(vault: Path, candidate_path: Path) -> ScheduleCandidate:
    resolved_vault = vault.expanduser().resolve()
    resolved_path = candidate_path.expanduser().resolve()
    # Apply payloads contain opaque bridge identifiers. They are runtime input,
    # not an Obsidian review document, so they must never live under brain/.
    allowed_root = (resolved_vault / ".local/woon-knowledge/schedule-apply").resolve()
    try:
        resolved_path.relative_to(allowed_root)
    except ValueError as error:
        raise WoonError(
            "schedule candidate must be under .local/woon-knowledge/schedule-apply"
        ) from error
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("schedule candidate JSON is unreadable") from error
    if not isinstance(raw, dict):
        raise WoonError("schedule candidate JSON must be an object")
    lifecycle = _required(raw, "lifecycle")
    if lifecycle not in {"create", "update", "cancel"}:
        raise WoonError("schedule candidate lifecycle is unsupported")
    return ScheduleCandidate(
        candidate_id=_required(raw, "candidate_id"),
        source_id=_required(raw, "source_id"),
        activity_id=_required(raw, "activity_id"),
        intent=_required(raw, "intent"),
        timezone=_required(raw, "timezone"),
        start_at=_required_datetime(raw.get("start_at"), "start_at"),
        end_at=_required_datetime(raw.get("end_at"), "end_at"),
        authorized_at=_optional_datetime(raw.get("authorized_at")),
        lifecycle=cast(Literal["create", "update", "cancel"], lifecycle),
        idempotency_key=_required(raw, "idempotency_key"),
        category_id=_required(raw, "category_id"),
        bridge_revision=_positive_int(raw.get("bridge_revision"), "bridge_revision"),
    )


def _required(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"schedule candidate {field} must be a non-empty string")
    return value.strip()


def _required_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise WoonError(f"schedule candidate {field} must be an ISO8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WoonError(f"schedule candidate {field} must be ISO8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WoonError(f"schedule candidate {field} must include a timezone")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _required_datetime(value, "authorized_at")


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WoonError(f"schedule candidate {field} must be a positive integer")
    return value
