"""Human-readable, bounded learning checkpoint records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from woon_core.errors import WoonError

LEARNING_CHECKPOINT_START = "<!-- woon-learning-checkpoint:start -->"
LEARNING_CHECKPOINT_END = "<!-- woon-learning-checkpoint:end -->"

type LearningCheckpointStatus = Literal["confirmed", "partial", "retry"]

_STATUS_LABELS: dict[LearningCheckpointStatus, str] = {
    "confirmed": "확인됨",
    "partial": "부분 이해",
    "retry": "다시 연습",
}

_LEARNING_CHECKPOINT_PATTERN = re.compile(
    rf"(?ms)^## 학습 체크포인트\s*\n\s*{re.escape(LEARNING_CHECKPOINT_START)}.*?"
    rf"{re.escape(LEARNING_CHECKPOINT_END)}\s*(?=^## |\Z)"
)


@dataclass(frozen=True, slots=True)
class LearningCheckpoint:
    """The smallest durable state needed to resume one guided study target."""

    canonical_id: str
    unit: str
    status: LearningCheckpointStatus
    evidence: tuple[str, ...]
    unstable: tuple[str, ...]
    next_question: str
    recorded_on: date


@dataclass(frozen=True, slots=True)
class LearningCheckpointReport:
    """Observable result of one optimistic checkpoint update."""

    canonical_id: str
    relative_path: str
    revision: str
    changed: bool
    compiler_owned: bool


def validate_learning_checkpoint(checkpoint: LearningCheckpoint) -> LearningCheckpoint:
    """Reject chat logs, unbounded prose, and unsupported status values."""

    if checkpoint.status not in _STATUS_LABELS:
        raise WoonError("learning checkpoint status must be confirmed, partial, or retry")
    _bounded_line(checkpoint.unit, "unit", 160)
    _bounded_line(checkpoint.next_question, "next_question", 320)
    if len(checkpoint.evidence) > 8 or len(set(checkpoint.evidence)) != len(checkpoint.evidence):
        raise WoonError("learning checkpoint evidence must be unique and contain at most 8 items")
    if len(checkpoint.unstable) > 8 or len(set(checkpoint.unstable)) != len(checkpoint.unstable):
        raise WoonError("learning checkpoint unstable items must be unique and contain at most 8")
    if checkpoint.status in {"confirmed", "partial"} and not checkpoint.evidence:
        raise WoonError("confirmed or partial learning checkpoint requires execution evidence")
    if checkpoint.status in {"partial", "retry"} and not checkpoint.unstable:
        raise WoonError("partial or retry learning checkpoint requires an unstable item")
    for value in (*checkpoint.evidence, *checkpoint.unstable):
        _bounded_line(value, "item", 360)
    return checkpoint


def upsert_learning_checkpoint(body: str, checkpoint: LearningCheckpoint) -> str:
    """Replace one Core-owned checkpoint block without retaining session chatter."""

    checkpoint = validate_learning_checkpoint(checkpoint)
    block = render_learning_checkpoint(checkpoint)
    if _LEARNING_CHECKPOINT_PATTERN.search(body):
        return (
            _LEARNING_CHECKPOINT_PATTERN.sub(block.rstrip() + "\n\n", body, count=1).rstrip() + "\n"
        )
    return body.rstrip() + "\n\n" + block


def strip_learning_checkpoint(body: str) -> str:
    """Remove the Core-owned resume block before validating page-specific prose."""

    return _LEARNING_CHECKPOINT_PATTERN.sub("", body, count=1)


def render_learning_checkpoint(checkpoint: LearningCheckpoint) -> str:
    """Render a compact resume point that remains useful in Obsidian."""

    label = _STATUS_LABELS[checkpoint.status]
    lines = [
        "## 학습 체크포인트",
        LEARNING_CHECKPOINT_START,
        f"- 범위: {checkpoint.unit.strip()}",
        f"- 상태: {label}",
        f"- 기록일: {checkpoint.recorded_on.isoformat()}",
        "- 실행 증거:",
    ]
    lines.extend(f"  - {value.strip()}" for value in checkpoint.evidence)
    if not checkpoint.evidence:
        lines.append("  - 아직 확인된 실행 증거 없음")
    lines.append("- 아직 불안정함:")
    lines.extend(f"  - {value.strip()}" for value in checkpoint.unstable)
    if not checkpoint.unstable:
        lines.append("  - 없음")
    lines.extend(
        (
            f"- 다음 인출 질문: {checkpoint.next_question.strip()}",
            LEARNING_CHECKPOINT_END,
            "",
        )
    )
    return "\n".join(lines)


def _bounded_line(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value or "\x00" in value:
        raise WoonError(f"learning checkpoint {field} must be one bounded visible line")
