"""One safe projection of Codex conclusions into growth and daily knowledge.

The caller may inspect opted-in Codex conversations, but this module never
accepts or stores a transcript.  It receives only short, Korean conclusions
and writes two derived views from the same validated payload:

* a small ``brain/wiki`` page for reusable learning or decisions;
* a local daily ledger consumed later by the daily-record lane.

This removes the former double interpretation of the same conversation while
keeping the daily note and the growth Wiki under their separate owners.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_candidates import ReviewCandidate, persist_review_candidates
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunOutcome,
    RunRequest,
    snapshot_owned_paths,
)

_KINDS = {
    "활동",
    "일정",
    "할 일",
    "인물",
    "관계",
    "학습",
    "개념",
    "결정",
    "질문",
    "다음 행동",
    "커리어",
    "창작",
    "자료",
    "건강",
    "재정·행정",
    "생활",
    "여행·구매",
    "회고",
}
_GROWTH_KINDS = {"학습", "개념", "결정"}
_INPUT_STATES = {"processed", "no-meaningful", "pending", "unavailable"}
_TITLE_LIMIT = 72
_SUMMARY_LIMIT = 360
_QUESTION_LIMIT = 240
_INTENT_LIMIT = 240
_CALENDAR_TITLE_LIMIT = 120
_VISIBLE_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|token|secret|password|bearer)\b|sk-[A-Za-z0-9_-]{12,})",
    flags=re.IGNORECASE,
)
_FILE_STEM_RE = re.compile(r"[^0-9A-Za-z가-힣_-]+")
_RELATED_ROOTS = ("brain/wiki/", "wiki/", "maps/")
_CODEX_OWNED_PATHS = {
    "brain/wiki",
    "brain/review/codex",
    ".local/woon-knowledge/codex-knowledge",
}
_CALENDAR_LINK_REASONS = {"준비", "작업", "결정", "결과", "참고"}
_PERSON_NAME_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣 .'-]{0,47}")

type CodexKnowledgeKind = Literal[
    "활동",
    "일정",
    "할 일",
    "인물",
    "관계",
    "학습",
    "개념",
    "결정",
    "질문",
    "다음 행동",
    "커리어",
    "창작",
    "자료",
    "건강",
    "재정·행정",
    "생활",
    "여행·구매",
    "회고",
]
type CodexInputState = Literal["processed", "no-meaningful", "pending", "unavailable"]


@dataclass(frozen=True, slots=True)
class CalendarContext:
    """One explicit link between a conversation outcome and a calendar event."""

    event_day: date
    event_title: str
    related_documents: tuple[str, ...]
    reason: str
    include_generated_growth_page: bool = False


@dataclass(frozen=True, slots=True)
class CalendarDocumentLink:
    """A resolved document link rendered by the read-only Calendar projection."""

    relative_path: str
    title: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonMention:
    """An explicit, review-only person mention; never an identity resolution."""

    display_name: str
    explicit_facts: tuple[str, ...]
    next_action: str | None = None


@dataclass(frozen=True, slots=True)
class CodexKnowledgeEntry:
    """A minimized conclusion selected from an opted-in user/assistant exchange."""

    day: date
    kind: CodexKnowledgeKind
    title: str
    summary: str
    intent: str | None = None
    next_question: str | None = None
    related_documents: tuple[str, ...] = ()
    calendar_contexts: tuple[CalendarContext, ...] = ()
    people: tuple[PersonMention, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexKnowledgeResult:
    """Non-sensitive outcome for one idempotent conversation projection."""

    entry_count: int
    growth_page_count: int
    receipt_id: str
    replayed: bool
    day: str
    input_state: CodexInputState


def entries_from_records(records: list[dict[str, object]]) -> tuple[CodexKnowledgeEntry, ...]:
    """Validate a narrow tool payload rather than accepting raw conversation data."""

    if len(records) > 24:
        raise WoonError("Codex knowledge entries may contain at most twenty-four records")
    entries: list[CodexKnowledgeEntry] = []
    for raw in records:
        allowed = {
            "day",
            "kind",
            "title",
            "summary",
            "intent",
            "next_question",
            "related_documents",
            "calendar_contexts",
            "people",
        }
        required = {"day", "kind", "title", "summary"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge entry has unsupported fields")
        raw_day, kind, title, summary = (
            raw["day"],
            raw["kind"],
            raw["title"],
            raw["summary"],
        )
        next_question = raw.get("next_question")
        intent = raw.get("intent")
        related = raw.get("related_documents", [])
        calendar_contexts = raw.get("calendar_contexts", [])
        people = raw.get("people", [])
        if not isinstance(raw_day, str):
            raise WoonError("Codex knowledge day must be a string")
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(summary, str):
            raise WoonError("Codex knowledge entry text fields must be strings")
        if next_question is not None and not isinstance(next_question, str):
            raise WoonError("Codex knowledge next_question must be a string or null")
        if intent is not None and not isinstance(intent, str):
            raise WoonError("Codex knowledge intent must be a string or null")
        if not isinstance(related, list) or not all(isinstance(value, str) for value in related):
            raise WoonError("Codex knowledge related_documents must be a string list")
        if not isinstance(calendar_contexts, list):
            raise WoonError("Codex knowledge calendar_contexts must be a list")
        if not isinstance(people, list):
            raise WoonError("Codex knowledge people must be a list")
        try:
            entry_day = date.fromisoformat(raw_day)
        except ValueError as error:
            raise WoonError("Codex knowledge day must be YYYY-MM-DD") from error
        if kind not in _KINDS:
            raise WoonError("Codex knowledge kind is invalid")
        _visible(title, "title", _TITLE_LIMIT)
        _visible(summary, "summary", _SUMMARY_LIMIT)
        if next_question is not None:
            _visible(next_question, "next_question", _QUESTION_LIMIT)
        if intent is not None:
            _visible(intent, "intent", _INTENT_LIMIT)
        entries.append(
            CodexKnowledgeEntry(
                day=entry_day,
                kind=cast(CodexKnowledgeKind, kind),
                title=title,
                summary=summary,
                intent=intent,
                next_question=next_question,
                related_documents=tuple(related),
                calendar_contexts=_calendar_contexts_from_records(calendar_contexts),
                people=_people_from_records(people),
            )
        )
    return tuple(entries)


def record_codex_knowledge_entries(
    vault: Path,
    *,
    source_range: str,
    day: date | None = None,
    entries: tuple[CodexKnowledgeEntry, ...],
    input_state: CodexInputState = "processed",
) -> CodexKnowledgeResult:
    """Persist one sanitized conversation batch without retaining its transcript."""

    day = _resolve_batch_day(day=day, source_range=source_range, entries=entries)

    settings = load_orchestrator_settings(vault)
    contract = next(
        (
            item
            for item in settings.automations
            if item.automation_id == "codex-conversation-ingest"
        ),
        None,
    )
    if contract is None or contract.mode != "materialize" or contract.status != "enabled":
        raise WoonError(
            "Codex conversation knowledge automation is not enabled for materialization"
        )
    if set(contract.owned_paths) != _CODEX_OWNED_PATHS:
        raise WoonError("Codex conversation knowledge automation has an unsafe write boundary")
    _validate_entries(settings.vault, day=day, entries=entries, input_state=input_state)
    serialized = _encode_entries(entries, day=day, input_state=input_state)
    request = RunRequest(
        source_range=source_range,
        input_sha256=hashlib.sha256(serialized).hexdigest(),
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=source_range,
    )
    growth_paths = tuple(
        _growth_path(settings.vault, entry) for entry in entries if entry.kind in _GROWTH_KINDS
    )

    def produce() -> RunOutcome:
        _reject_conflicting_growth_pages(settings.vault, growth_paths, entries)
        _write_input_status(settings.vault, day=day, input_state=input_state)
        for entry in entries:
            _write_ledger_entry(settings.vault, entry)
        for entry in entries:
            if entry.kind in _GROWTH_KINDS:
                _write_growth_page(settings.vault, entry)
        candidates = _review_candidates(entries)
        if candidates:
            persist_review_candidates(settings.vault, "brain/review/codex", candidates)
        return RunOutcome(
            candidate_ids=tuple(_entry_id(entry) for entry in entries)
            + tuple(candidate.candidate_id for candidate in candidates),
            output_sha256=hashlib.sha256(_output_bytes(settings.vault, day, entries)).hexdigest(),
        )

    result = AutomationRunStore(settings).run("codex-conversation-ingest", request, produce)
    return CodexKnowledgeResult(
        entry_count=len(entries),
        growth_page_count=len(growth_paths),
        receipt_id=result.receipt_id,
        replayed=result.replayed,
        day=day.isoformat(),
        input_state=input_state,
    )


def load_daily_entries(vault: Path, *, day: date) -> tuple[dict[str, object], ...]:
    """Read only the minimized ledger view needed by the daily-record lane."""

    root = vault.expanduser().resolve() / ".local/woon-knowledge/codex-knowledge" / day.isoformat()
    if not root.is_dir():
        return ()
    entries: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        if path.name == "_input-status.json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError("Codex knowledge ledger is unreadable") from error
        if not isinstance(value, dict):
            raise WoonError("Codex knowledge ledger entry must be a mapping")
        entries.append(value)
    return tuple(entries)


def _resolve_batch_day(
    *, day: date | None, source_range: str, entries: tuple[CodexKnowledgeEntry, ...]
) -> date:
    if day is not None:
        return day
    if entries:
        return entries[0].day
    match = re.search(r"20\d{2}[01]\d[0-3]\d", source_range)
    if match is None:
        raise WoonError("empty Codex knowledge batch requires an explicit day")
    try:
        return datetime.strptime(match.group(0), "%Y%m%d").date()
    except ValueError as error:
        raise WoonError("Codex knowledge source range has an invalid date") from error


def load_daily_input_status(vault: Path, *, day: date) -> CodexInputState | None:
    """Return why a day has no entries without ever exposing source contents."""

    path = (
        vault.expanduser().resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / day.isoformat()
        / "_input-status.json"
    )
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("Codex knowledge input status is unreadable") from error
    if not isinstance(value, dict) or value.get("input_state") not in _INPUT_STATES:
        raise WoonError("Codex knowledge input status is invalid")
    return cast(CodexInputState, value["input_state"])


def calendar_document_links_for_event(
    vault: Path, *, day: date, event_title: str
) -> tuple[CalendarDocumentLink, ...]:
    """Resolve only explicit, same-day conversation links for one Calendar event.

    A title must match after whitespace normalization. This deliberately does
    not use semantic similarity or file timestamps, which would attach
    unrelated work from the same day to a schedule entry.
    """

    normalized_title = _normalized_calendar_title(event_title)
    selected: dict[str, set[str]] = {}
    for record in load_daily_entries(vault, day=day):
        contexts = record.get("calendar_contexts", [])
        if contexts == []:
            continue
        if not isinstance(contexts, list):
            raise WoonError("Codex knowledge calendar context ledger is unreadable")
        kind = record.get("kind")
        title = record.get("title")
        if not isinstance(kind, str) or not isinstance(title, str):
            raise WoonError("Codex knowledge calendar context ledger is unreadable")
        generated_growth_path = _growth_relative_path(title) if kind in _GROWTH_KINDS else None
        parsed_contexts = _calendar_contexts_from_records(contexts)
        _validate_calendar_contexts(
            vault,
            parsed_contexts,
            generated_growth_path=generated_growth_path,
        )
        for context in parsed_contexts:
            if (
                context.event_day != day
                or _normalized_calendar_title(context.event_title) != normalized_title
            ):
                continue
            for relative_path in _calendar_context_documents(
                context,
                generated_growth_path=generated_growth_path,
            ):
                selected.setdefault(relative_path, set()).add(context.reason)
    return tuple(
        CalendarDocumentLink(
            relative_path=relative_path,
            title=_related_document_title(vault, relative_path),
            reasons=tuple(sorted(reasons)),
        )
        for relative_path, reasons in sorted(selected.items())
    )


def _validate_entries(
    vault: Path,
    *,
    day: date,
    entries: tuple[CodexKnowledgeEntry, ...],
    input_state: str,
) -> None:
    if len(entries) > 24:
        raise WoonError("Codex knowledge entries may contain at most twenty-four records")
    if input_state not in _INPUT_STATES:
        raise WoonError("Codex knowledge input state is invalid")
    if input_state in {"pending", "unavailable"} and entries:
        raise WoonError("pending or unavailable Codex input must not create knowledge entries")
    seen: set[str] = set()
    for entry in entries:
        if entry.day != day:
            raise WoonError("Codex knowledge batch must contain exactly one KST day")
        if entry.kind not in _KINDS:
            raise WoonError("Codex knowledge kind is invalid")
        _visible(entry.title, "title", _TITLE_LIMIT)
        _visible(entry.summary, "summary", _SUMMARY_LIMIT)
        if entry.intent is not None:
            _visible(entry.intent, "intent", _INTENT_LIMIT)
        if entry.next_question is not None:
            _visible(entry.next_question, "next_question", _QUESTION_LIMIT)
        if len(set(entry.related_documents)) != len(entry.related_documents):
            raise WoonError("Codex knowledge related documents must be unique")
        for relative_path in entry.related_documents:
            _related_document(vault, relative_path)
        generated_growth_path = (
            _growth_relative_path(entry.title) if entry.kind in _GROWTH_KINDS else None
        )
        _validate_calendar_contexts(
            vault,
            entry.calendar_contexts,
            generated_growth_path=generated_growth_path,
        )
        _validate_people(entry.people)
        entry_id = _entry_id(entry)
        if entry_id in seen:
            raise WoonError("Codex knowledge entries must be unique")
        seen.add(entry_id)


def _visible(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value or _VISIBLE_SECRET_RE.search(value):
        raise WoonError(f"Codex knowledge {field} is not safe visible text")


def _related_document(vault: Path, relative_path: str) -> Path:
    if not relative_path.endswith(".md") or not relative_path.startswith(_RELATED_ROOTS):
        raise WoonError("Codex knowledge related document path is not allowed")
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WoonError("Codex knowledge related document path is not allowed")
    path = (vault.resolve() / candidate).resolve()
    try:
        path.relative_to(vault.resolve())
    except ValueError as error:
        raise WoonError("Codex knowledge related document escapes vault") from error
    if not path.is_file() or path.is_symlink():
        raise WoonError("Codex knowledge related document is missing")
    return path


def _calendar_contexts_from_records(records: list[object]) -> tuple[CalendarContext, ...]:
    contexts: list[CalendarContext] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex knowledge calendar context must be a mapping")
        allowed = {
            "event_day",
            "event_title",
            "related_documents",
            "reason",
            "include_generated_growth_page",
        }
        required = {"event_day", "event_title", "related_documents", "reason"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge calendar context has unsupported fields")
        event_day = raw["event_day"]
        event_title = raw["event_title"]
        related_documents = raw["related_documents"]
        reason = raw["reason"]
        include_generated_growth_page = raw.get("include_generated_growth_page", False)
        if (
            not isinstance(event_day, str)
            or not isinstance(event_title, str)
            or not isinstance(reason, str)
        ):
            raise WoonError("Codex knowledge calendar context text fields must be strings")
        if not isinstance(related_documents, list) or not all(
            isinstance(value, str) for value in related_documents
        ):
            raise WoonError(
                "Codex knowledge calendar context related_documents must be a string list"
            )
        if not isinstance(include_generated_growth_page, bool):
            raise WoonError(
                "Codex knowledge calendar context include_generated_growth_page must be a boolean"
            )
        try:
            parsed_day = date.fromisoformat(event_day)
        except ValueError as error:
            raise WoonError(
                "Codex knowledge calendar context event_day must be YYYY-MM-DD"
            ) from error
        contexts.append(
            CalendarContext(
                event_day=parsed_day,
                event_title=event_title,
                related_documents=tuple(related_documents),
                reason=reason,
                include_generated_growth_page=include_generated_growth_page,
            )
        )
    return tuple(contexts)


def _validate_calendar_contexts(
    vault: Path,
    contexts: tuple[CalendarContext, ...],
    *,
    generated_growth_path: str | None,
) -> None:
    seen: set[tuple[date, str, tuple[str, ...], str, bool]] = set()
    for context in contexts:
        _visible(context.event_title, "calendar context event_title", _CALENDAR_TITLE_LIMIT)
        if context.reason not in _CALENDAR_LINK_REASONS:
            raise WoonError("Codex knowledge calendar context reason is invalid")
        document_paths = _calendar_context_documents(
            context,
            generated_growth_path=generated_growth_path,
        )
        if not document_paths or len(set(document_paths)) != len(document_paths):
            raise WoonError("Codex knowledge calendar context related documents must be unique")
        for relative_path in document_paths:
            if relative_path == generated_growth_path:
                continue
            _related_document(vault, relative_path)
        key = (
            context.event_day,
            _normalized_calendar_title(context.event_title),
            document_paths,
            context.reason,
            context.include_generated_growth_page,
        )
        if key in seen:
            raise WoonError("Codex knowledge calendar contexts must be unique")
        seen.add(key)


def _normalized_calendar_title(value: str) -> str:
    return " ".join(value.split())


def _people_from_records(records: list[object]) -> tuple[PersonMention, ...]:
    people: list[PersonMention] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex knowledge person mention must be a mapping")
        allowed = {"display_name", "explicit_facts", "next_action"}
        required = {"display_name", "explicit_facts"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge person mention has unsupported fields")
        display_name = raw["display_name"]
        facts = raw["explicit_facts"]
        next_action = raw.get("next_action")
        if (
            not isinstance(display_name, str)
            or not isinstance(facts, list)
            or not all(isinstance(item, str) for item in facts)
        ):
            raise WoonError("Codex knowledge person mention fields must be text")
        if next_action is not None and not isinstance(next_action, str):
            raise WoonError("Codex knowledge person mention next_action must be text or null")
        people.append(
            PersonMention(
                display_name=display_name,
                explicit_facts=tuple(facts),
                next_action=next_action,
            )
        )
    return tuple(people)


def _validate_people(people: tuple[PersonMention, ...]) -> None:
    if len(people) > 3:
        raise WoonError("Codex knowledge entry may contain at most three person mentions")
    seen: set[str] = set()
    for person in people:
        name = person.display_name.strip()
        if not _PERSON_NAME_RE.fullmatch(name) or name in seen:
            raise WoonError("Codex knowledge person mention name is invalid")
        seen.add(name)
        if not 1 <= len(person.explicit_facts) <= 3:
            raise WoonError("Codex knowledge person mention needs one to three explicit facts")
        for fact in person.explicit_facts:
            _visible_person_line(fact, "person fact")
        if person.next_action is not None:
            _visible_person_line(person.next_action, "person next_action")


def _visible_person_line(value: str, field: str) -> None:
    if (
        not value.strip()
        or len(value) > 120
        or "\n" in value
        or "@" in value
        or _VISIBLE_SECRET_RE.search(value)
    ):
        raise WoonError(f"Codex knowledge {field} is not safe visible text")


def _calendar_context_documents(
    context: CalendarContext, *, generated_growth_path: str | None
) -> tuple[str, ...]:
    if not context.include_generated_growth_page:
        return context.related_documents
    if generated_growth_path is None:
        raise WoonError(
            "Codex knowledge calendar context may include a generated page only for 학습 or 결정"
        )
    return (*context.related_documents, generated_growth_path)


def _entry_id(entry: CodexKnowledgeEntry) -> str:
    stable = "\0".join(
        (
            entry.day.isoformat(),
            entry.kind,
            entry.title.strip(),
            entry.summary.strip(),
            entry.intent or "",
            entry.next_question or "",
            *entry.related_documents,
            *(
                "\0".join(
                    (
                        context.event_day.isoformat(),
                        context.event_title.strip(),
                        context.reason,
                        *context.related_documents,
                        str(context.include_generated_growth_page),
                    )
                )
                for context in entry.calendar_contexts
            ),
            *(
                "\0".join(
                    (person.display_name.strip(), *person.explicit_facts, person.next_action or "")
                )
                for person in entry.people
            ),
        )
    )
    return f"codex-knowledge-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _encode_entries(
    entries: tuple[CodexKnowledgeEntry, ...], *, day: date, input_state: str
) -> bytes:
    return json.dumps(
        {
            "day": day.isoformat(),
            "input_state": input_state,
            "entries": [
                {
                    "day": entry.day.isoformat(),
                    "kind": entry.kind,
                    "title": entry.title,
                    "summary": entry.summary,
                    "intent": entry.intent,
                    "next_question": entry.next_question,
                    "related_documents": list(entry.related_documents),
                    "calendar_contexts": [
                        {
                            "event_day": context.event_day.isoformat(),
                            "event_title": context.event_title,
                            "related_documents": list(context.related_documents),
                            "reason": context.reason,
                            "include_generated_growth_page": context.include_generated_growth_page,
                        }
                        for context in entry.calendar_contexts
                    ],
                    "people": [
                        {
                            "display_name": person.display_name,
                            "explicit_facts": list(person.explicit_facts),
                            "next_action": person.next_action,
                        }
                        for person in entry.people
                    ],
                }
                for entry in entries
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _ledger_path(vault: Path, entry: CodexKnowledgeEntry) -> Path:
    return (
        vault.resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / entry.day.isoformat()
        / f"{_entry_id(entry)}.json"
    )


def _write_ledger_entry(vault: Path, entry: CodexKnowledgeEntry) -> None:
    path = _ledger_path(vault, entry)
    value = {
        "kind": entry.kind,
        "title": entry.title.strip(),
        "summary": entry.summary.strip(),
        "intent": entry.intent.strip() if entry.intent else None,
        "related_documents": list(entry.related_documents),
        "calendar_contexts": [
            {
                "event_day": context.event_day.isoformat(),
                "event_title": context.event_title.strip(),
                "related_documents": list(context.related_documents),
                "reason": context.reason,
                "include_generated_growth_page": context.include_generated_growth_page,
            }
            for context in entry.calendar_contexts
        ],
        "people": [
            {
                "display_name": person.display_name.strip(),
                "explicit_facts": list(person.explicit_facts),
                "next_action": person.next_action,
            }
            for person in entry.people
        ],
    }
    serialized = encode_json(value)
    if path.exists() and path.read_bytes() != serialized:
        raise WoonError("Codex knowledge ledger entry conflicts with an existing record")
    if not path.exists():
        atomic_write(path, serialized, mode=0o600)


def _write_input_status(vault: Path, *, day: date, input_state: str) -> None:
    """Persist only the availability state needed to explain a blank daily view."""

    path = (
        vault.resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / day.isoformat()
        / "_input-status.json"
    )
    serialized = encode_json({"input_state": input_state})
    if not path.exists() or path.read_bytes() != serialized:
        atomic_write(path, serialized, mode=0o600)


_ENTRY_REVIEW_KINDS = {
    "일정": "일정 검토",
    "할 일": "할 일 검토",
    "커리어": "커리어 근거 검토",
    "창작": "창작 연결 검토",
    "자료": "자료 보관 검토",
    "재정·행정": "행정 확인",
}


def _review_candidates(entries: tuple[CodexKnowledgeEntry, ...]) -> tuple[ReviewCandidate, ...]:
    """Project actionable conclusions to human review without external side effects.

    A person mention is intentionally narrower than an ordinary projection: it
    stores only explicit facts and remains unlinked.  All other action-shaped
    conclusions become a readable review card, never a direct Things,
    Calendar, person-card, or source mutation.
    """

    candidates: list[ReviewCandidate] = []
    for entry in entries:
        review_kind = _ENTRY_REVIEW_KINDS.get(entry.kind)
        if review_kind is not None:
            stable = "\0".join((_entry_id(entry), review_kind, entry.title, entry.summary))
            candidates.append(
                ReviewCandidate(
                    candidate_id=(
                        "codex-projection-"
                        f"{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"
                    ),
                    kind="codex-projection",
                    source_locator=f"codex:{_entry_id(entry)}",
                    summary=entry.summary.strip(),
                    display_title=f"{review_kind}: {entry.title.strip()}"[:72],
                    review_kind=review_kind,
                    occurred_at=datetime.fromtimestamp(0, tz=UTC),
                    time_precision="none",
                    scheduled_for=None,
                    calendar_candidate=False,
                )
            )
        for person in entry.people:
            title = f"{person.display_name.strip()}: {person.explicit_facts[0].strip()}"[:48]
            stable = "\0".join((_entry_id(entry), person.display_name, *person.explicit_facts))
            candidate_id = (
                f"person-memory-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"
            )
            candidates.append(
                ReviewCandidate(
                    candidate_id=candidate_id,
                    kind="person-memory",
                    source_locator=f"codex:{_entry_id(entry)}",
                    summary=title,
                    occurred_at=datetime.fromtimestamp(0, tz=UTC),
                    time_precision="none",
                    scheduled_for=None,
                    calendar_candidate=False,
                    person_name=person.display_name.strip(),
                    explicit_facts=person.explicit_facts,
                    next_action=person.next_action.strip() if person.next_action else None,
                )
            )
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise WoonError("Codex knowledge review candidates must be unique")
    return tuple(candidates)


def _growth_path(vault: Path, entry: CodexKnowledgeEntry) -> Path:
    return vault.resolve() / _growth_relative_path(entry.title)


def _growth_relative_path(title: str) -> str:
    stem = _FILE_STEM_RE.sub("-", title.strip()).strip("-_").lower()
    if not stem:
        raise WoonError("Codex knowledge title cannot form a growth Wiki filename")
    return f"brain/wiki/{stem}.md"


def _render_growth_page(vault: Path, entry: CodexKnowledgeEntry) -> bytes:
    lines = [
        "---",
        "type: Wiki",
        f'title: "{entry.title.strip()}"',
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Active",
        f'summary: "{entry.summary.strip()}"',
        "---",
        "",
        f"# {entry.title.strip()}",
        "",
        "## 현재 이해",
        "",
        entry.summary.strip(),
    ]
    if entry.intent:
        lines.extend(["", "## 남긴 의도", "", f"추정 의도: {entry.intent.strip()}"])
    if entry.related_documents:
        lines.extend(["", "## 연결"])
        for relative_path in entry.related_documents:
            lines.append(
                f"- [[../../{relative_path[:-3]}|{_related_document_title(vault, relative_path)}]]"
            )
    if entry.next_question:
        lines.extend(["", "## 다음 질문", "", entry.next_question.strip()])
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _reject_conflicting_growth_pages(
    vault: Path, paths: tuple[Path, ...], entries: tuple[CodexKnowledgeEntry, ...]
) -> None:
    expected = (
        {
            _growth_path(vault, entry): _render_growth_page(vault, entry)
            for entry in entries
            if entry.kind in _GROWTH_KINDS
        }
        if paths
        else {}
    )
    if len(expected) != len(paths):
        raise WoonError("Codex knowledge growth Wiki titles must be unique within one run")
    for path, content in expected.items():
        if path.exists() and path.read_bytes() != content:
            raise WoonError("Codex knowledge growth Wiki update needs curation review")


def _write_growth_page(vault: Path, entry: CodexKnowledgeEntry) -> None:
    path = _growth_path(vault, entry)
    content = _render_growth_page(vault, entry)
    if not path.exists():
        atomic_write(path, content, mode=0o600)


def _output_bytes(vault: Path, day: date, entries: tuple[CodexKnowledgeEntry, ...]) -> bytes:
    paths = [_ledger_path(vault, entry) for entry in entries]
    paths.append(
        vault.resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / day.isoformat()
        / "_input-status.json"
    )
    paths.extend(_growth_path(vault, entry) for entry in entries if entry.kind in _GROWTH_KINDS)
    return b"\0".join(path.read_bytes() for path in sorted(paths))


def _related_document_title(vault: Path, relative_path: str) -> str:
    """Use a person-readable Markdown title in generated Obsidian links."""

    path = _related_document(vault, relative_path)
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip().strip("\"'")
    h1 = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return h1.group(1).strip() if h1 else path.stem
