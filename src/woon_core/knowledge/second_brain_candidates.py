"""Privacy-minimizing projections for second-brain review candidates.

The input objects are ephemeral connector values.  Only explicitly permitted
metadata and a bounded human-readable summary cross into a review file.  Raw
mail bodies, chat transcripts, system prompts, tool output, and reasoning are
never returned or persisted by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.second_brain_runtime import RunOutcome

_LOCATOR_RE = re.compile(r"[a-z][a-z0-9-]{1,63}:[A-Za-z0-9._:#-]{1,192}")
_SUMMARY_LIMIT = 280


@dataclass(frozen=True, slots=True)
class MailScheduleInput:
    """Ephemeral, connector-normalized mail attributes for one thread."""

    source_locator: str
    classification: Literal["allowlisted", "advertising", "ambiguous", "other"]
    actionable: bool
    summary: str
    occurred_at: datetime
    scheduled_for: datetime | date | None


@dataclass(frozen=True, slots=True)
class CodexResponseItem:
    """Ephemeral subset of one Codex response item; content is never persisted."""

    item_type: str
    role: str
    content: str
    thread_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """Safe, local-only review record; not an instruction or an external write."""

    candidate_id: str
    kind: Literal["mail-schedule", "codex-history"]
    source_locator: str
    summary: str
    occurred_at: datetime
    time_precision: Literal["none", "date-only", "date-time"]
    scheduled_for: datetime | date | None
    things_candidate: bool
    calendar_candidate: bool

    def as_record(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "source_locator": self.source_locator,
            "summary": self.summary,
            "occurred_at": self.occurred_at.isoformat(),
            "time_precision": self.time_precision,
            "scheduled_for": _iso(self.scheduled_for),
            "things_candidate": self.things_candidate,
            "calendar_candidate": self.calendar_candidate,
        }


def candidate_from_allowlisted_mail(message: MailScheduleInput) -> ReviewCandidate | None:
    """Return a candidate only for a known, actionable, allowlisted thread."""

    if message.classification != "allowlisted" or not message.actionable:
        return None
    _locator(message.source_locator, "mail source_locator")
    _summary(message.summary)
    _aware_datetime(message.occurred_at, "mail occurred_at")
    precision, calendar_candidate = _schedule_precision(message.scheduled_for)
    candidate_id = _candidate_id(
        "mail", message.source_locator, message.occurred_at.isoformat(), message.summary
    )
    return ReviewCandidate(
        candidate_id=candidate_id,
        kind="mail-schedule",
        source_locator=message.source_locator,
        summary=message.summary.strip(),
        occurred_at=message.occurred_at,
        time_precision=precision,
        scheduled_for=message.scheduled_for,
        things_candidate=True,
        calendar_candidate=calendar_candidate,
    )


def candidate_from_codex_messages(
    items: tuple[CodexResponseItem, ...], *, opt_in: bool, summary: str
) -> ReviewCandidate | None:
    """Project opted-in user/assistant messages into one raw-content-free candidate."""

    if not opt_in:
        return None
    selected = tuple(
        item for item in items if item.item_type == "message" and item.role in {"user", "assistant"}
    )
    if not selected:
        return None
    thread_id = selected[0].thread_id
    if not thread_id or any(item.thread_id != thread_id for item in selected):
        raise WoonError("Codex candidate messages must belong to one thread")
    if any(item.sequence < 0 for item in selected):
        raise WoonError("Codex candidate sequence must be non-negative")
    _summary(summary)
    start = min(item.sequence for item in selected)
    end = max(item.sequence for item in selected)
    locator = f"codex-thread:{thread_id}#{start}-{end}"
    _locator(locator, "Codex source_locator")
    content_digest = hashlib.sha256(
        "\0".join(item.content for item in selected).encode("utf-8")
    ).hexdigest()
    candidate_id = _candidate_id("codex", locator, content_digest, summary)
    return ReviewCandidate(
        candidate_id=candidate_id,
        kind="codex-history",
        source_locator=locator,
        summary=summary.strip(),
        occurred_at=datetime.fromtimestamp(0, tz=UTC),
        time_precision="none",
        scheduled_for=None,
        things_candidate=False,
        calendar_candidate=False,
    )


def persist_review_candidates(
    vault: Path, owned_root: str, candidates: tuple[ReviewCandidate, ...]
) -> RunOutcome:
    """Write deterministic local review files without overwriting manual changes."""

    root = _review_root(vault, owned_root)
    seen: set[str] = set()
    serialized: list[bytes] = []
    for candidate in sorted(candidates, key=lambda item: item.candidate_id):
        if candidate.candidate_id in seen:
            raise WoonError("second-brain review candidate IDs must be unique")
        seen.add(candidate.candidate_id)
        data = _render_candidate(candidate).encode("utf-8")
        path = root / f"{candidate.candidate_id}.md"
        if path.exists():
            if path.read_bytes() != data:
                raise WoonError("candidate conflicts with an existing review file")
        else:
            atomic_write(path, data, mode=0o600)
        serialized.append(data)
    return RunOutcome(
        candidate_ids=tuple(sorted(seen)),
        output_sha256=hashlib.sha256(b"\0".join(serialized)).hexdigest(),
    )


def _candidate_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _schedule_precision(
    value: datetime | date | None,
) -> tuple[Literal["none", "date-only", "date-time"], bool]:
    if value is None:
        return "none", False
    if isinstance(value, datetime):
        _aware_datetime(value, "scheduled_for")
        return "date-time", True
    if isinstance(value, date):
        return "date-only", False
    raise WoonError("scheduled_for must be a date, datetime, or null")


def _review_root(vault: Path, owned_root: str) -> Path:
    resolved_vault = vault.expanduser().resolve()
    candidate = Path(owned_root)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or not owned_root.startswith("brain/review/")
    ):
        raise WoonError("review candidate root must stay under brain/review")
    root = (resolved_vault / candidate).resolve()
    try:
        root.relative_to(resolved_vault)
    except ValueError as error:
        raise WoonError("review candidate root escapes vault") from error
    return root


def _render_candidate(candidate: ReviewCandidate) -> str:
    record = candidate.as_record()
    title = f"검토 후보 — {candidate.candidate_id}"
    frontmatter = {
        "type": "Candidate",
        "title": title,
        "publish": False,
        "access": "local-only",
        "status": "Review",
        **record,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        encoded = _yaml_scalar(value)
        lines.append(f"{key}: {encoded}")
    lines.extend(
        [
            "---",
            "",
            f"# {title}",
            "",
            "## 요약",
            "",
            candidate.summary.strip(),
            "",
            "## 반영 경계",
            "",
            "- Things 3 또는 Apple Calendar에는 아직 반영하지 않는다.",
            "- 명시적 사용자 승인과 별도 bridge 검증 뒤에만 실제 반영 후보가 된다.",
            "- 원문 메일·대화·system/tool/reasoning은 이 파일에 복사하지 않는다.",
            "",
        ]
    )
    return "\n".join(lines)


def _locator(value: str, field: str) -> None:
    if not isinstance(value, str) or not _LOCATOR_RE.fullmatch(value):
        raise WoonError(f"{field} must be an opaque local locator")


def _summary(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _SUMMARY_LIMIT
        or "\n" in value
        or "\r" in value
        or "```" in value
    ):
        raise WoonError("candidate summary must be a bounded single-line summary")


def _aware_datetime(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WoonError(f"{field} must include a timezone")


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _yaml_scalar(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)
