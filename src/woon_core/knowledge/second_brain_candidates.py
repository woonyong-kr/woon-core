"""Privacy-minimizing projections for second-brain review candidates.

The input objects are ephemeral connector values.  Only explicitly permitted
metadata and a bounded human-readable summary cross into a review file. Raw
mail bodies, system prompts, tool output, and reasoning are never returned or
persisted by this module.
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
_PERSON_NAME_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣 .'-]{0,47}")
_DISPLAY_FILE_STEM_RE = re.compile(r"[^0-9A-Za-z가-힣_-]+")


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
class PersonMemoryInput:
    """A reviewed, explicit person mention extracted from opted-in Codex messages.

    This is deliberately not an identity profile.  It carries only a display
    name as written, one to three explicit non-sensitive facts, and an
    optional next action.  A caller must not construct it from a bare name,
    an inferred relationship, Novel content, or a private original.
    """

    display_name: str
    explicit_facts: tuple[str, ...]
    next_action: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """Safe, local-only review record; not an instruction or an external write."""

    candidate_id: str
    kind: Literal["mail-schedule", "codex-history", "person-memory"]
    source_locator: str
    summary: str
    occurred_at: datetime
    time_precision: Literal["none", "date-only", "date-time"]
    scheduled_for: datetime | date | None
    calendar_candidate: bool
    person_name: str | None = None
    explicit_facts: tuple[str, ...] = ()
    next_action: str | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "source_locator": self.source_locator,
            "summary": self.summary,
            "occurred_at": self.occurred_at.isoformat(),
            "time_precision": self.time_precision,
            "scheduled_for": _iso(self.scheduled_for),
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
        calendar_candidate=calendar_candidate,
    )


def candidate_from_codex_messages(
    items: tuple[CodexResponseItem, ...], *, opt_in: bool, summary: str
) -> ReviewCandidate | None:
    """Project opted-in user/assistant messages into one raw-content-free candidate."""

    if not opt_in:
        return None
    selected = _codex_message_items(items)
    if not selected:
        return None
    _summary(summary)
    locator, content_digest = _codex_locator_and_digest(selected)
    candidate_id = _candidate_id("codex", locator, content_digest, summary)
    return ReviewCandidate(
        candidate_id=candidate_id,
        kind="codex-history",
        source_locator=locator,
        summary=summary.strip(),
        occurred_at=datetime.fromtimestamp(0, tz=UTC),
        time_precision="none",
        scheduled_for=None,
        calendar_candidate=False,
    )


def candidate_from_codex_person_memory(
    items: tuple[CodexResponseItem, ...], *, opt_in: bool, person: PersonMemoryInput
) -> ReviewCandidate | None:
    """Create a review-only person-memory candidate without resolving identity.

    The function intentionally does no name detection or matching.  It only
    accepts a prior bounded extraction of explicit facts and leaves any
    decision to create or link a person card to the user and the ``people``
    workflow.
    """

    if not opt_in:
        return None
    selected = _codex_message_items(items)
    if not selected:
        return None
    _person_memory(person)
    locator, content_digest = _codex_locator_and_digest(selected)
    facts = tuple(fact.strip() for fact in person.explicit_facts)
    title = _person_memory_title(person.display_name, facts[0])
    candidate_id = _candidate_id(
        "person-memory",
        locator,
        content_digest,
        person.display_name,
        *facts,
        person.next_action or "",
    )
    return ReviewCandidate(
        candidate_id=candidate_id,
        kind="person-memory",
        source_locator=locator,
        summary=title,
        occurred_at=datetime.fromtimestamp(0, tz=UTC),
        time_precision="none",
        scheduled_for=None,
        calendar_candidate=False,
        person_name=person.display_name.strip(),
        explicit_facts=facts,
        next_action=person.next_action.strip() if person.next_action else None,
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
        path = root / _candidate_filename(candidate)
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
    title = candidate.summary.strip()
    frontmatter = {
        "type": "Candidate",
        "title": title,
        "publish": False,
        "access": "local-only",
        "status": "Review",
        "summary": candidate.summary.strip(),
        "scheduled_for": _iso(candidate.scheduled_for),
    }
    if candidate.kind == "person-memory":
        frontmatter["review_kind"] = "인물 정리"
    # Codex projections deliberately have no persisted message timestamp.
    # Do not render the epoch placeholder as a misleading 1970 date.
    if candidate.occurred_at != datetime.fromtimestamp(0, tz=UTC):
        frontmatter["occurred_at"] = candidate.occurred_at.isoformat()
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
        ]
    )
    if candidate.kind == "person-memory":
        assert candidate.person_name is not None
        lines.extend(
            [
                "## 이름 표기",
                "",
                candidate.person_name,
                "",
                "## 확인된 사실",
                "",
                *(f"- {fact}" for fact in candidate.explicit_facts),
                "",
                "## 다음 행동",
                "",
                f"- {candidate.next_action or '없음'}",
                "",
                "## 반영 경계",
                "",
                "- 같은 이름의 기존 인물과 자동으로 연결하지 않는다.",
                "- 인물 카드·관계·연락처·신상은 만들거나 추정하지 않는다.",
                "- 원문 대화와 private·Novel 자료는 이 파일에 복사하지 않는다.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "## 반영 경계",
                "",
                "- 메일 자동화는 이 후보를 Apple Calendar에 바로 반영하지 않는다. "
                "시간 약속은 별도 local policy 경로에서 사용자가 확인한 뒤에만 처리한다.",
                "- 날짜만 있거나 모호·변경·취소 요청이면 실제 반영하지 않고 검토 대상으로 남긴다.",
                "- 원문 메일·대화·system/tool/reasoning은 이 파일에 복사하지 않는다.",
                "",
            ]
        )
    return "\n".join(lines)


def _candidate_filename(candidate: ReviewCandidate) -> str:
    """Use a readable Obsidian filename; opaque IDs stay in runtime receipts."""

    stem = _DISPLAY_FILE_STEM_RE.sub("-", candidate.summary.strip()).strip("-_")
    if not stem:
        stem = "검토-후보"
    return f"{stem[:80]}.md"


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


def _codex_message_items(items: tuple[CodexResponseItem, ...]) -> tuple[CodexResponseItem, ...]:
    selected = tuple(
        item for item in items if item.item_type == "message" and item.role in {"user", "assistant"}
    )
    if not selected:
        return ()
    thread_id = selected[0].thread_id
    if not thread_id or any(item.thread_id != thread_id for item in selected):
        raise WoonError("Codex candidate messages must belong to one thread")
    if any(item.sequence < 0 for item in selected):
        raise WoonError("Codex candidate sequence must be non-negative")
    return selected


def _codex_locator_and_digest(items: tuple[CodexResponseItem, ...]) -> tuple[str, str]:
    start = min(item.sequence for item in items)
    end = max(item.sequence for item in items)
    locator = f"codex-thread:{items[0].thread_id}#{start}-{end}"
    _locator(locator, "Codex source_locator")
    digest = hashlib.sha256("\0".join(item.content for item in items).encode("utf-8")).hexdigest()
    return locator, digest


def _person_memory(person: PersonMemoryInput) -> None:
    if not isinstance(person.display_name, str) or not _PERSON_NAME_RE.fullmatch(
        person.display_name.strip()
    ):
        raise WoonError("person-memory candidate requires a short explicit display name")
    if not 1 <= len(person.explicit_facts) <= 3:
        raise WoonError("person-memory candidate requires one to three explicit facts")
    for fact in person.explicit_facts:
        _person_memory_line(fact, "person-memory fact")
    if person.next_action is not None:
        _person_memory_line(person.next_action, "person-memory next action")


def _person_memory_line(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 120
        or "\n" in value
        or "\r" in value
        or "```" in value
        or "@" in value
    ):
        raise WoonError(f"{field} must be a short contact-free explicit statement")


def _person_memory_title(display_name: str, first_fact: str) -> str:
    prefix = f"{display_name.strip()}: "
    return (prefix + first_fact.strip())[:48]


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
