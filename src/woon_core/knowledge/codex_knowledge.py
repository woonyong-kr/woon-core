"""One-pass semantic projection of Codex conclusions into the single Wiki.

Exact allowed user/assistant text belongs to the separate local source archive.
This module receives detailed Korean semantics—facts, criteria, alternatives,
evidence, changes, and unresolved questions—then updates the one canonical
subject and its private execution ledger.  Daily, Calendar, people, and project
views remain projections rather than parallel knowledge stores.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_candidates import (
    ReviewCandidate,
    prepare_review_candidates,
)
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunOutcome,
    RunRequest,
    snapshot_owned_paths,
)
from woon_core.knowledge.wiki_tree import prepare_wiki_tree_refresh
from woon_core.knowledge.woon_wiki import (
    InterviewAnswerRevision,
    WikiDelta,
    prepare_wiki_pages,
    resolve_wiki_path,
    wiki_relative_path,
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
    "프로젝트",
}
_INPUT_STATES = {"processed", "no-meaningful", "partial", "pending", "unavailable"}
_TITLE_LIMIT = 72
_SUMMARY_LIMIT = 360
_QUESTION_LIMIT = 240
_INTENT_LIMIT = 240
_EXCHANGE_QUESTION_LIMIT = 420
_EXCHANGE_ANSWER_LIMIT = 900
_EXCHANGE_OUTCOME_LIMIT = 420
_ATTACHMENT_SUMMARY_LIMIT = 220
_CALENDAR_TITLE_LIMIT = 120
_OBJECTIVE_LIMIT = 280
_VISIBLE_SECRET_RE = re.compile(
    r"(?:\b(?:api[_-]?key|token|secret|password|bearer)\b|sk-[A-Za-z0-9_-]{12,})",
    flags=re.IGNORECASE,
)
_RELATED_ROOTS = ("wiki/",)
_CODEX_OWNED_PATHS = {
    "wiki",
    "brain/review/codex",
    ".local/woon-knowledge/codex-knowledge",
    "wiki/private/_sources/codex",
}
_CALENDAR_LINK_REASONS = {"준비", "작업", "결정", "결과", "참고"}
_PERSON_NAME_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣 .'-]{0,47}")
_CONTENT_KINDS = {
    "book",
    "film",
    "series",
    "lecture",
    "course",
    "podcast",
    "game",
    "article",
    "exhibition",
    "learning-material-bundle",
}
_PROJECT_STATUSES = {"Planned", "Active", "Paused", "Completed", "Cancelled"}
_LIFECYCLE_STATES = {
    "idea",
    "planned",
    "active",
    "paused",
    "completed",
    "cancelled",
    "archived",
}
_PROJECT_LIFECYCLE = {
    "Planned": "planned",
    "Active": "active",
    "Paused": "paused",
    "Completed": "completed",
    "Cancelled": "cancelled",
}
_CODEX_RANGE_RE = re.compile(
    r"^codex-kst-(\d{4}-\d{2}-\d{2})-(?:scan-)?through-(\d+)(?:-[a-z0-9-]+)?-v\d+$"
)

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
    "프로젝트",
]
type CodexInputState = Literal["processed", "no-meaningful", "partial", "pending", "unavailable"]
type CodexDisposition = Literal["organized", "review"]


@dataclass(frozen=True, slots=True)
class CalendarContext:
    """One explicit link between a conversation outcome and a calendar event."""

    event_day: date
    event_title: str
    related_documents: tuple[str, ...]
    reason: str
    include_wiki_subject: bool = False


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
class ContentMention:
    """One explicitly named work or material bundle, never its raw body."""

    title: str
    content_kind: str
    genre: str | None = None
    resource_keyword: str | None = None
    creators: tuple[str, ...] = ()
    official_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectMention:
    """One explicit outcome-bound project derived from the user's wording."""

    title: str
    objective: str
    status: str = "Active"
    materials: tuple[str, ...] = ()
    lifecycle_status: str = "active"
    started_on: date | None = None
    ended_on: date | None = None
    occurred_on: date | None = None


@dataclass(frozen=True, slots=True)
class InterviewAnswerMention:
    """A minimized revision tied to one existing semantic parent Wiki."""

    question: str
    answer: str | None
    parent_wiki_path: str
    interview_tracks: tuple[str, ...]
    question_topic: str
    context: str | None = None
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    job_variants: tuple[str, ...] = ()
    change_reason: str = "답변을 새로 정리했다."
    quality_assessment: str | None = None
    source_label: str | None = None
    promote_current: bool = True


@dataclass(frozen=True, slots=True)
class ConversationExchange:
    """One readable exchange plus the details needed to recover its reasoning."""

    question: str
    answer: str
    outcome: str | None = None
    attachments: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexKnowledgeEntry:
    """A minimized conclusion selected from an opted-in user/assistant exchange."""

    day: date
    kind: CodexKnowledgeKind
    title: str
    summary: str
    lifecycle_status: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    occurred_on: date | None = None
    wiki_update: bool = False
    wiki_subject_path: str | None = None
    new_wiki_reason: str | None = None
    parent: str | None = None
    keywords: tuple[str, ...] = ()
    central_question: str | None = None
    intent: str | None = None
    next_question: str | None = None
    exchanges: tuple[ConversationExchange, ...] = ()
    related_documents: tuple[str, ...] = ()
    calendar_contexts: tuple[CalendarContext, ...] = ()
    people: tuple[PersonMention, ...] = ()
    contents: tuple[ContentMention, ...] = ()
    projects: tuple[ProjectMention, ...] = ()
    interview_answer: InterviewAnswerMention | None = None
    disposition: CodexDisposition = "organized"
    review_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CodexKnowledgeResult:
    """Non-sensitive outcome for one idempotent conversation projection."""

    entry_count: int
    wiki_page_count: int
    receipt_id: str
    replayed: bool
    day: str
    input_state: CodexInputState


@dataclass(frozen=True, slots=True)
class DailyHistoryRewriteResult:
    """Result of one validated historical ledger replacement."""

    day: str
    entry_count: int
    replaced_entry_count: int


def entries_from_records(records: list[dict[str, object]]) -> tuple[CodexKnowledgeEntry, ...]:
    """Validate a narrow tool payload rather than accepting raw conversation data."""

    if len(records) > 24:
        raise WoonError("Codex knowledge entries may contain at most twenty-four records")
    entries: list[CodexKnowledgeEntry] = []
    for raw in records:
        disposition = raw.get("disposition", "organized")
        if disposition == "excluded":
            # Excluded material is deliberately not validated, hashed, logged,
            # or returned.  The caller may pass a classifier explanation, raw
            # body, or opaque locator; none of it crosses the persistence
            # boundary.
            continue
        if disposition not in {"organized", "review"}:
            raise WoonError("Codex knowledge disposition is invalid")
        allowed = {
            "day",
            "kind",
            "title",
            "summary",
            "lifecycle_status",
            "started_on",
            "ended_on",
            "occurred_on",
            "wiki_update",
            "wiki_subject_path",
            "new_wiki_reason",
            "parent",
            "keywords",
            "central_question",
            "intent",
            "next_question",
            "exchanges",
            "related_documents",
            "calendar_contexts",
            "people",
            "contents",
            "projects",
            "interview_answer",
            "disposition",
            "review_reason",
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
        lifecycle_status = raw.get("lifecycle_status")
        started_on = _record_date(raw.get("started_on"), "started_on")
        ended_on = _record_date(raw.get("ended_on"), "ended_on")
        occurred_on = _record_date(raw.get("occurred_on"), "occurred_on")
        wiki_update = raw.get("wiki_update", False)
        wiki_subject_path = raw.get("wiki_subject_path")
        new_wiki_reason = raw.get("new_wiki_reason")
        parent = raw.get("parent")
        keywords = raw.get("keywords", [])
        central_question = raw.get("central_question")
        related = raw.get("related_documents", [])
        exchanges = raw.get("exchanges", [])
        calendar_contexts = raw.get("calendar_contexts", [])
        people = raw.get("people", [])
        contents = raw.get("contents", [])
        projects = raw.get("projects", [])
        interview_answer = raw.get("interview_answer")
        review_reason = raw.get("review_reason")
        if not isinstance(raw_day, str):
            raise WoonError("Codex knowledge day must be a string")
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(summary, str):
            raise WoonError("Codex knowledge entry text fields must be strings")
        if next_question is not None and not isinstance(next_question, str):
            raise WoonError("Codex knowledge next_question must be a string or null")
        if intent is not None and not isinstance(intent, str):
            raise WoonError("Codex knowledge intent must be a string or null")
        if lifecycle_status is not None and not isinstance(lifecycle_status, str):
            raise WoonError("Codex knowledge lifecycle_status must be a string or null")
        if not isinstance(wiki_update, bool):
            raise WoonError("Codex knowledge wiki_update must be a boolean")
        if wiki_subject_path is not None and not isinstance(wiki_subject_path, str):
            raise WoonError("Codex knowledge wiki_subject_path must be a string or null")
        if new_wiki_reason is not None and not isinstance(new_wiki_reason, str):
            raise WoonError("Codex knowledge new_wiki_reason must be a string or null")
        if parent is not None and not isinstance(parent, str):
            raise WoonError("Codex knowledge parent must be a string or null")
        if not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords):
            raise WoonError("Codex knowledge keywords must be a string list")
        if central_question is not None and not isinstance(central_question, str):
            raise WoonError("Codex knowledge central_question must be a string or null")
        if review_reason is not None and not isinstance(review_reason, str):
            raise WoonError("Codex knowledge review_reason must be a string or null")
        if disposition == "review" and review_reason is None:
            raise WoonError("review disposition requires review_reason")
        if disposition == "organized" and review_reason is not None:
            raise WoonError("organized disposition must not have review_reason")
        if not isinstance(related, list) or not all(isinstance(value, str) for value in related):
            raise WoonError("Codex knowledge related_documents must be a string list")
        if not isinstance(calendar_contexts, list):
            raise WoonError("Codex knowledge calendar_contexts must be a list")
        if not isinstance(people, list):
            raise WoonError("Codex knowledge people must be a list")
        if not isinstance(contents, list):
            raise WoonError("Codex knowledge contents must be a list")
        if not isinstance(projects, list):
            raise WoonError("Codex knowledge projects must be a list")
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
        if review_reason is not None:
            _visible(review_reason, "review_reason", _INTENT_LIMIT)
        if new_wiki_reason is not None:
            _visible(new_wiki_reason, "new_wiki_reason", _INTENT_LIMIT)
        if parent is not None:
            _visible(parent, "parent", 240)
        for keyword in keywords:
            _visible(keyword, "keyword", 120)
        if central_question is not None:
            _visible(central_question, "central_question", _QUESTION_LIMIT)
        entries.append(
            CodexKnowledgeEntry(
                day=entry_day,
                kind=cast(CodexKnowledgeKind, kind),
                title=title,
                summary=summary,
                lifecycle_status=lifecycle_status,
                started_on=started_on,
                ended_on=ended_on,
                occurred_on=occurred_on,
                wiki_update=wiki_update,
                wiki_subject_path=wiki_subject_path,
                new_wiki_reason=new_wiki_reason,
                parent=parent,
                keywords=tuple(keywords),
                central_question=central_question,
                intent=intent,
                next_question=next_question,
                exchanges=_exchanges_from_records(exchanges),
                related_documents=tuple(related),
                calendar_contexts=_calendar_contexts_from_records(calendar_contexts),
                people=_people_from_records(people),
                contents=_contents_from_records(contents),
                projects=_projects_from_records(projects),
                interview_answer=_interview_answer_from_record(interview_answer),
                disposition=cast(CodexDisposition, disposition),
                review_reason=review_reason,
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
    _validate_monotonic_source_range(settings.checkpoint_path, source_range)
    serialized = _encode_entries(entries, day=day, input_state=input_state)
    request = RunRequest(
        source_range=source_range,
        input_sha256=hashlib.sha256(serialized).hexdigest(),
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=source_range,
    )
    wiki_page_count = _declared_wiki_page_count(entries)

    def produce() -> RunOutcome:
        wiki_deltas = _wiki_deltas(settings.vault, entries)
        wiki_pages = prepare_wiki_pages(settings.vault, wiki_deltas)
        resource_pages = _prepare_resource_index_pages(settings.vault, entries)
        overlap = set(wiki_pages).intersection(resource_pages)
        if overlap:
            raise WoonError("Codex resource index conflicts with a Wiki subject update")
        candidates = _review_candidates(entries)
        review_pages = dict(
            prepare_review_candidates(settings.vault, "brain/review/codex", candidates)
        )
        runtime_pages = dict(
            [_prepare_input_status(settings.vault, day=day, input_state=input_state)]
            + [_prepare_ledger_entry(settings.vault, entry) for entry in entries]
        )
        prepared = {**wiki_pages, **resource_pages, **review_pages, **runtime_pages}
        _apply_codex_pipeline_batch(settings.vault, prepared)
        return RunOutcome(
            candidate_ids=tuple(_entry_id(entry) for entry in entries)
            + tuple(candidate.candidate_id for candidate in candidates),
            output_sha256=hashlib.sha256(
                b"\0".join(prepared[path] for path in sorted(prepared))
            ).hexdigest(),
        )

    result = AutomationRunStore(settings).run(
        "codex-conversation-ingest",
        request,
        produce,
        validate=lambda: _validate_entries(
            settings.vault,
            day=day,
            entries=entries,
            input_state=input_state,
        ),
    )
    return CodexKnowledgeResult(
        entry_count=len(entries),
        wiki_page_count=wiki_page_count,
        receipt_id=result.receipt_id,
        replayed=result.replayed,
        day=day.isoformat(),
        input_state=input_state,
    )


def _declared_wiki_page_count(entries: tuple[CodexKnowledgeEntry, ...]) -> int:
    """Count declared Wiki identities without touching current Vault state.

    Receipt replay must be decidable before resolving a `new_wiki_reason`
    against the page created by the original run. This structural count keeps
    the result stable while all state-dependent validation stays inside the
    receipt-guarded producer.
    """

    identities: set[str] = set()
    for entry in entries:
        if entry.wiki_update:
            identities.add(entry.wiki_subject_path or f"subject:{entry.title.casefold()}")
        identities.update(f"content:{item.title.casefold()}" for item in entry.contents)
        identities.update(f"project:{item.title.casefold()}" for item in entry.projects)
    return len(identities)


def _validate_monotonic_source_range(checkpoint_path: Path, source_range: str) -> None:
    """Reject an older completed-turn boundary before any projection is prepared."""

    candidate = _codex_range_boundary(source_range)
    if candidate is None or not checkpoint_path.is_file():
        return
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        current = checkpoint.get("lanes", {}).get("codex-conversation-ingest", {}).get("cursor")
    except (OSError, json.JSONDecodeError, AttributeError) as error:
        raise WoonError("Codex knowledge checkpoint is unreadable") from error
    if not isinstance(current, str):
        return
    previous = _codex_range_boundary(current)
    if previous is not None and candidate < previous:
        raise WoonError("Codex knowledge source range must not move backward")


def _codex_range_boundary(value: str) -> tuple[date, int] | None:
    match = _CODEX_RANGE_RE.fullmatch(value)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1)), int(match.group(2))
    except ValueError as error:
        raise WoonError("Codex knowledge source range is invalid") from error


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


def replace_daily_history_entries(
    vault: Path,
    *,
    day: date,
    entries: tuple[CodexKnowledgeEntry, ...],
    input_state: CodexInputState,
) -> DailyHistoryRewriteResult:
    """Replace one historical minimized ledger without replaying side effects.

    This repair path owns only the local ledger for one exact date.  It cannot
    create or update Wiki pages, review candidates, Calendar links, content or
    project projections, or interview answers.  The old directory is retained
    until the validated staging directory has been swapped into place.
    """

    root = vault.expanduser().resolve()
    settings = load_orchestrator_settings(root)
    _validate_entries(settings.vault, day=day, entries=entries, input_state=input_state)
    for entry in entries:
        if entry.day != day:
            raise WoonError("historical Codex entry day does not match the replacement day")
        if (
            entry.wiki_update
            or entry.wiki_subject_path is not None
            or entry.new_wiki_reason is not None
            or entry.parent is not None
            or entry.keywords
            or entry.central_question is not None
            or entry.calendar_contexts
            or entry.contents
            or entry.projects
            or entry.interview_answer is not None
            or entry.disposition != "organized"
            or entry.review_reason is not None
        ):
            raise WoonError("historical Codex rewrite may update only the minimized daily ledger")

    ledger_parent = root / ".local/woon-knowledge/codex-knowledge"
    _ensure_private_runtime_parent(root, ledger_parent)
    destination = ledger_parent / day.isoformat()
    previous_count = (
        len([path for path in destination.glob("*.json") if path.name != "_input-status.json"])
        if destination.is_dir()
        else 0
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{day.isoformat()}-rewrite-", dir=ledger_parent))
    backup = ledger_parent / f".{day.isoformat()}-previous"
    if backup.exists():
        shutil.rmtree(staging)
        raise WoonError("historical Codex rewrite found an unfinished previous transaction")
    try:
        for entry in entries:
            _, serialized = _prepare_ledger_entry(root, entry)
            atomic_write(staging / f"{_entry_id(entry)}.json", serialized, mode=0o600)
        _, status = _prepare_input_status(root, day=day, input_state=input_state)
        atomic_write(staging / "_input-status.json", status, mode=0o600)
        staging.chmod(0o700)
        if destination.exists():
            destination.rename(backup)
        staging.rename(destination)
    except Exception:
        if destination.exists() and backup.exists():
            shutil.rmtree(destination)
        if backup.exists():
            backup.rename(destination)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    return DailyHistoryRewriteResult(
        day=day.isoformat(),
        entry_count=len(entries),
        replaced_entry_count=previous_count,
    )


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
        subject_path = record.get("wiki_subject_path")
        if subject_path is not None and not isinstance(subject_path, str):
            raise WoonError("Codex knowledge calendar context Wiki path is unreadable")
        parsed_contexts = _calendar_contexts_from_records(contexts)
        _validate_calendar_contexts(
            vault,
            parsed_contexts,
            wiki_subject_path=subject_path,
        )
        for context in parsed_contexts:
            if (
                context.event_day != day
                or _normalized_calendar_title(context.event_title) != normalized_title
            ):
                continue
            for relative_path in _calendar_context_documents(
                context,
                wiki_subject_path=subject_path,
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
        _validate_exchanges(entry.exchanges)
        if entry.review_reason is not None:
            _visible(entry.review_reason, "review_reason", _INTENT_LIMIT)
        if entry.new_wiki_reason is not None:
            _visible(entry.new_wiki_reason, "new_wiki_reason", _INTENT_LIMIT)
        if entry.parent is not None:
            _visible(entry.parent, "parent", 240)
        if len(set(value.casefold() for value in entry.keywords)) != len(entry.keywords):
            raise WoonError("Codex knowledge keywords must be unique")
        for keyword in entry.keywords:
            _visible(keyword, "keyword", 120)
        if entry.central_question is not None:
            _visible(entry.central_question, "central_question", _QUESTION_LIMIT)
        identity_fields = int(entry.wiki_subject_path is not None) + int(
            entry.new_wiki_reason is not None
        )
        if entry.wiki_update and identity_fields != 1:
            raise WoonError(
                "wiki_update requires exactly one existing wiki_subject_path or new_wiki_reason"
            )
        if not entry.wiki_update and identity_fields:
            raise WoonError("non-Wiki Codex knowledge must not declare a Wiki identity")
        if entry.new_wiki_reason is not None:
            if entry.parent is None or not entry.keywords or entry.central_question is None:
                raise WoonError(
                    "new Wiki knowledge requires parent, keywords, and central_question"
                )
            _validate_new_wiki_parent(vault, entry.parent)
        elif entry.parent is not None or entry.keywords or entry.central_question is not None:
            raise WoonError(
                "existing Wiki updates inherit tree identity; "
                "structure fields are only for new Wiki"
            )
        if entry.disposition == "review" and (
            entry.calendar_contexts
            or entry.people
            or entry.contents
            or entry.projects
            or entry.interview_answer
            or entry.related_documents
            or entry.wiki_subject_path
            or entry.new_wiki_reason
            or entry.parent
            or entry.keywords
            or entry.central_question
            or entry.lifecycle_status
            or entry.started_on
            or entry.ended_on
            or entry.occurred_on
        ):
            raise WoonError(
                "review-only Codex knowledge must not create links or entity side effects"
            )
        if len(set(entry.related_documents)) != len(entry.related_documents):
            raise WoonError("Codex knowledge related documents must be unique")
        for relative_path in entry.related_documents:
            _related_document(vault, relative_path)
        wiki_identity = _wiki_identity(vault, entry)
        wiki_subject_path = wiki_identity[0] if wiki_identity is not None else None
        _validate_calendar_contexts(
            vault,
            entry.calendar_contexts,
            wiki_subject_path=wiki_subject_path,
        )
        _validate_people(entry.people)
        _validate_contents(entry.contents)
        _validate_lifecycle(
            entry.lifecycle_status,
            entry.started_on,
            entry.ended_on,
            entry.occurred_on,
            label="entry",
        )
        _validate_projects(entry.projects)
        _validate_interview_answer_mention(vault, entry)
        if entry.projects and any(
            content.content_kind == "learning-material-bundle" for content in entry.contents
        ):
            raise WoonError(
                "Project-exclusive learning materials must be listed under the project hub"
            )
        entity_paths = tuple(
            [
                _entity_wiki_relative_path(vault, item.title, "wiki/books")
                for item in entry.contents
                if item.content_kind == "book"
            ]
            + [
                _entity_wiki_relative_path(vault, item.title, "wiki/personal/projects")
                for item in entry.projects
            ]
        )
        if len(entity_paths) != len(set(entity_paths)):
            raise WoonError("Codex knowledge entity documents must be unique")
        entry_id = _entry_id(entry)
        if entry_id in seen:
            raise WoonError("Codex knowledge entries must be unique")
        seen.add(entry_id)


def _visible(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value or _VISIBLE_SECRET_RE.search(value):
        raise WoonError(f"Codex knowledge {field} is not safe visible text")


def _validate_exchanges(exchanges: tuple[ConversationExchange, ...]) -> None:
    if len(exchanges) > 8:
        raise WoonError("Codex knowledge entry may contain at most eight exchanges")
    seen: set[tuple[str, str]] = set()
    for exchange in exchanges:
        _visible(exchange.question, "exchange question", _EXCHANGE_QUESTION_LIMIT)
        _visible(exchange.answer, "exchange answer", _EXCHANGE_ANSWER_LIMIT)
        if exchange.outcome is not None:
            _visible(exchange.outcome, "exchange outcome", _EXCHANGE_OUTCOME_LIMIT)
        if len(exchange.attachments) > 6 or len(set(exchange.attachments)) != len(
            exchange.attachments
        ):
            raise WoonError("Codex knowledge exchange attachments must be unique and bounded")
        for attachment in exchange.attachments:
            _visible(attachment, "exchange attachment", _ATTACHMENT_SUMMARY_LIMIT)
        for field, values in (
            ("fact", exchange.facts),
            ("criterion", exchange.criteria),
            ("alternative", exchange.alternatives),
            ("evidence", exchange.evidence),
            ("change", exchange.changes),
            ("unresolved", exchange.unresolved),
        ):
            if len(values) > 12 or len(set(values)) != len(values):
                raise WoonError(
                    f"Codex knowledge exchange {field} values must be unique and bounded"
                )
            for value in values:
                _visible(value, f"exchange {field}", _EXCHANGE_OUTCOME_LIMIT)
        key = (exchange.question.strip(), exchange.answer.strip())
        if key in seen:
            raise WoonError("Codex knowledge exchanges must be unique")
        seen.add(key)


def _validate_contents(contents: tuple[ContentMention, ...]) -> None:
    if len(contents) > 3:
        raise WoonError("Codex knowledge contents may contain at most three records")
    for content in contents:
        _visible(content.title, "content title", _TITLE_LIMIT)
        if content.content_kind not in _CONTENT_KINDS:
            raise WoonError("Codex knowledge content kind is invalid")
        if content.content_kind == "book":
            if content.genre is None:
                raise WoonError("Codex knowledge book requires one genre keyword")
            _visible(content.genre, "book genre", 72)
            if content.resource_keyword is not None:
                raise WoonError("Codex knowledge book must not use a resource keyword")
        else:
            if content.genre is not None:
                raise WoonError("Codex knowledge genre is only valid for books")
            if content.resource_keyword is None:
                raise WoonError("Codex knowledge non-book material requires one resource keyword")
            _visible(content.resource_keyword, "resource keyword", 72)
            if content.official_url is None:
                raise WoonError("Codex knowledge non-book material requires one official HTTPS URL")
        if len(content.creators) > 8 or len(set(content.creators)) != len(content.creators):
            raise WoonError("Codex knowledge content creators must be unique and bounded")
        for creator in content.creators:
            _visible(creator, "content creator", 72)
        if content.official_url is not None and (
            len(content.official_url) > 240
            or not content.official_url.startswith("https://")
            or any(char.isspace() for char in content.official_url)
        ):
            raise WoonError("Codex knowledge content official_url must be a safe HTTPS URL")


def _validate_projects(projects: tuple[ProjectMention, ...]) -> None:
    if len(projects) > 2:
        raise WoonError("Codex knowledge projects may contain at most two records")
    for project in projects:
        _visible(project.title, "project title", _TITLE_LIMIT)
        _visible(project.objective, "project objective", _OBJECTIVE_LIMIT)
        if project.status not in _PROJECT_STATUSES:
            raise WoonError("Codex knowledge project status is invalid")
        if project.lifecycle_status != _PROJECT_LIFECYCLE[project.status]:
            raise WoonError("Codex knowledge project status and lifecycle_status conflict")
        _validate_lifecycle(
            project.lifecycle_status,
            project.started_on,
            project.ended_on,
            project.occurred_on,
            label="project",
        )
        if len(project.materials) > 12 or len(set(project.materials)) != len(project.materials):
            raise WoonError("Codex knowledge project materials must be unique and bounded")
        for material in project.materials:
            _visible(material, "project material", 120)


def _validate_lifecycle(
    lifecycle_status: str | None,
    started_on: date | None,
    ended_on: date | None,
    occurred_on: date | None,
    *,
    label: str,
) -> None:
    dates = (started_on, ended_on, occurred_on)
    if lifecycle_status is None:
        if any(value is not None for value in dates):
            raise WoonError(f"Codex knowledge {label} dates require lifecycle_status")
        return
    if lifecycle_status not in _LIFECYCLE_STATES:
        raise WoonError(f"Codex knowledge {label} lifecycle_status is invalid")
    if occurred_on is not None and any(value is not None for value in (started_on, ended_on)):
        raise WoonError(f"Codex knowledge {label} occurred_on cannot be combined with a date range")
    if started_on is not None and ended_on is not None and ended_on < started_on:
        raise WoonError(f"Codex knowledge {label} ended_on cannot precede started_on")
    if lifecycle_status in {"completed", "cancelled", "archived"} and not (ended_on or occurred_on):
        raise WoonError(
            f"Codex knowledge {label} closed lifecycle requires ended_on or occurred_on"
        )
    if lifecycle_status in {"idea", "planned", "active", "paused"} and ended_on is not None:
        raise WoonError(f"Codex knowledge {label} open lifecycle cannot have ended_on")


def _record_date(value: object, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WoonError(f"Codex knowledge {field} must be YYYY-MM-DD or null")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise WoonError(f"Codex knowledge {field} must be YYYY-MM-DD or null") from error


def _validate_interview_answer_mention(vault: Path, entry: CodexKnowledgeEntry) -> None:
    mention = entry.interview_answer
    if mention is None:
        return
    if not entry.wiki_update or entry.kind not in {"질문", "커리어", "학습"}:
        raise WoonError(
            "Codex interview answers require a question, career, or learning Wiki update"
        )
    identity = _wiki_identity(vault, entry)
    if identity is None or mention.question.strip() != identity[1].strip():
        raise WoonError("Codex interview question must match the stable Wiki identity")
    if not mention.parent_wiki_path.startswith("wiki/"):
        raise WoonError("Codex interview parent_wiki_path must target the Wiki")
    parent = _related_document(vault, mention.parent_wiki_path)
    parent_text = parent.read_text(encoding="utf-8")
    if _frontmatter_value(parent_text, "type") != "Wiki":
        raise WoonError("Codex interview parent_wiki_path must target a Wiki document")
    if re.fullmatch(r"wiki/personal/interview/[^/]+/README\.md", mention.parent_wiki_path):
        raise WoonError("Codex interview questions must not use a job track as their parent")
    if identity is not None:
        subject = vault / identity[0]
        expected_parent = Path(mention.parent_wiki_path).with_suffix("").as_posix()
        if subject.is_file():
            declared_parent = _frontmatter_value(subject.read_text(encoding="utf-8"), "parent")
            if expected_parent not in declared_parent:
                raise WoonError("Codex interview parent must match the subject's canonical parent")
        elif entry.parent is not None and expected_parent not in entry.parent:
            raise WoonError("new interview parent must match the declared canonical parent")
    if not mention.interview_tracks or len(set(mention.interview_tracks)) != len(
        mention.interview_tracks
    ):
        raise WoonError("Codex interview interview_tracks must be a unique non-empty list")
    for track in mention.interview_tracks:
        _visible(track, "interview track", 120)
    _visible(mention.question_topic, "interview question_topic", 120)
    if _markdown_title(parent_text).strip() != mention.question_topic.strip():
        raise WoonError("Codex interview question_topic must match the semantic parent Wiki title")
    InterviewAnswerRevision(
        question=mention.question,
        answer=mention.answer,
        context=mention.context,
        evidence=mention.evidence,
        limitations=mention.limitations,
        job_variants=mention.job_variants,
        change_reason=mention.change_reason,
        quality_assessment=mention.quality_assessment,
        source_label=mention.source_label,
        promote_current=mention.promote_current,
    )


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


def _wiki_identity(vault: Path, entry: CodexKnowledgeEntry) -> tuple[str, str] | None:
    """Resolve one explicit subject identity before any Wiki or receipt write."""

    if not entry.wiki_update:
        return None
    if entry.wiki_subject_path is not None:
        relative_path = entry.wiki_subject_path.strip()
        if not relative_path.startswith("wiki/"):
            raise WoonError("Codex knowledge wiki_subject_path must target wiki/**/*.md")
        path = _related_document(vault, relative_path)
        text = path.read_text(encoding="utf-8")
        title = _markdown_title(text)
        if not title:
            raise WoonError("Codex knowledge Wiki subject has no human-readable title")
        state = _frontmatter_value(text, "knowledge_state")
        if state == "폐기됨":
            raise WoonError("Codex knowledge must not update a retired Wiki subject")
        return relative_path, title

    path = resolve_wiki_path(vault, entry.title)
    if path.is_file():
        raise WoonError("new_wiki_reason cannot replace an existing subject; use wiki_subject_path")
    return path.relative_to(vault.resolve()).as_posix(), entry.title.strip()


def _validate_new_wiki_parent(vault: Path, parent: str) -> None:
    match = re.fullmatch(
        r"\[\[(?P<target>wiki/[^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]",
        parent.strip(),
    )
    if match is None:
        raise WoonError("new Wiki parent must be one exact Wiki Wikilink")
    target = match.group("target")
    relative = f"{target}.md" if not target.endswith(".md") else target
    if relative in {"wiki/README.md", "wiki/personal/README.md"}:
        raise WoonError("new Wiki parent must be a meaningful keyword below the generic root")
    _related_document(vault, relative)


def _markdown_title(text: str) -> str:
    value = _frontmatter_value(text, "title")
    if value:
        return value
    match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf'^\s*{re.escape(key)}:\s*["\']?(.+?)["\']?\s*$', text, flags=re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def _frontmatter_list(text: str, key: str) -> tuple[str, ...]:
    match = re.search(rf"^\s*{re.escape(key)}:\s*(\[.*\])\s*$", text, flags=re.MULTILINE)
    if match is not None:
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise WoonError(f"Codex knowledge Wiki {key} is invalid") from error
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WoonError(f"Codex knowledge Wiki {key} must be a string list")
        return tuple(value)
    block = re.search(
        rf"(?ms)^\s*{re.escape(key)}:\s*\n(?P<body>(?:\s{{2,}}-\s*[^\n]+\n?)+)",
        text,
    )
    if block is None:
        return ()
    return tuple(
        line.split("-", 1)[1].strip().strip("'\"")
        for line in block.group("body").splitlines()
        if line.lstrip().startswith("-")
    )


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
            "include_wiki_subject",
        }
        required = {"event_day", "event_title", "related_documents", "reason"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge calendar context has unsupported fields")
        event_day = raw["event_day"]
        event_title = raw["event_title"]
        related_documents = raw["related_documents"]
        reason = raw["reason"]
        include_wiki_subject = raw.get("include_wiki_subject", False)
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
        if not isinstance(include_wiki_subject, bool):
            raise WoonError(
                "Codex knowledge calendar context include_wiki_subject must be a boolean"
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
                include_wiki_subject=include_wiki_subject,
            )
        )
    return tuple(contexts)


def _exchanges_from_records(records: object) -> tuple[ConversationExchange, ...]:
    if not isinstance(records, list):
        raise WoonError("Codex knowledge exchanges must be a list")
    if len(records) > 8:
        raise WoonError("Codex knowledge entry may contain at most eight exchanges")
    exchanges: list[ConversationExchange] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex knowledge exchange must be a mapping")
        detail_fields = {
            "facts",
            "criteria",
            "alternatives",
            "evidence",
            "changes",
            "unresolved",
        }
        allowed = {"question", "answer", "outcome", "attachments", *detail_fields}
        required = {"question", "answer"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge exchange has unsupported fields")
        question = raw["question"]
        answer = raw["answer"]
        outcome = raw.get("outcome")
        attachments = raw.get("attachments", [])
        if not isinstance(question, str) or not isinstance(answer, str):
            raise WoonError("Codex knowledge exchange question and answer must be text")
        if outcome is not None and not isinstance(outcome, str):
            raise WoonError("Codex knowledge exchange outcome must be text or null")
        if not isinstance(attachments, list) or not all(
            isinstance(item, str) for item in attachments
        ):
            raise WoonError("Codex knowledge exchange attachments must be a string list")
        details = {
            field: _string_list(raw.get(field, []), f"exchange {field}") for field in detail_fields
        }
        exchanges.append(
            ConversationExchange(
                question=question,
                answer=answer,
                outcome=outcome,
                attachments=tuple(attachments),
                facts=details["facts"],
                criteria=details["criteria"],
                alternatives=details["alternatives"],
                evidence=details["evidence"],
                changes=details["changes"],
                unresolved=details["unresolved"],
            )
        )
    return tuple(exchanges)


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"Codex knowledge {field} must be a string list")
    return tuple(value)


def _validate_calendar_contexts(
    vault: Path,
    contexts: tuple[CalendarContext, ...],
    *,
    wiki_subject_path: str | None,
) -> None:
    seen: set[tuple[date, str, tuple[str, ...], str, bool]] = set()
    for context in contexts:
        _visible(context.event_title, "calendar context event_title", _CALENDAR_TITLE_LIMIT)
        if context.reason not in _CALENDAR_LINK_REASONS:
            raise WoonError("Codex knowledge calendar context reason is invalid")
        document_paths = _calendar_context_documents(
            context,
            wiki_subject_path=wiki_subject_path,
        )
        if not document_paths or len(set(document_paths)) != len(document_paths):
            raise WoonError("Codex knowledge calendar context related documents must be unique")
        for relative_path in document_paths:
            if relative_path == wiki_subject_path:
                continue
            _related_document(vault, relative_path)
        key = (
            context.event_day,
            _normalized_calendar_title(context.event_title),
            document_paths,
            context.reason,
            context.include_wiki_subject,
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


def _contents_from_records(records: list[object]) -> tuple[ContentMention, ...]:
    if len(records) > 3:
        raise WoonError("Codex knowledge contents may contain at most three records")
    contents: list[ContentMention] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex knowledge content mention must be a mapping")
        allowed = {
            "title",
            "content_kind",
            "genre",
            "resource_keyword",
            "creators",
            "official_url",
        }
        required = {"title", "content_kind"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge content mention has unsupported fields")
        title = raw["title"]
        content_kind = raw["content_kind"]
        genre = raw.get("genre")
        resource_keyword = raw.get("resource_keyword")
        creators = raw.get("creators", [])
        official_url = raw.get("official_url")
        if not isinstance(title, str) or not isinstance(content_kind, str):
            raise WoonError("Codex knowledge content mention text fields must be strings")
        if genre is not None and not isinstance(genre, str):
            raise WoonError("Codex knowledge content genre must be a string or null")
        if resource_keyword is not None and not isinstance(resource_keyword, str):
            raise WoonError("Codex knowledge resource keyword must be a string or null")
        if not isinstance(creators, list) or not all(isinstance(item, str) for item in creators):
            raise WoonError("Codex knowledge content creators must be a string list")
        if official_url is not None and not isinstance(official_url, str):
            raise WoonError("Codex knowledge content official_url must be a string or null")
        contents.append(
            ContentMention(
                title=title,
                content_kind=content_kind,
                genre=genre,
                resource_keyword=resource_keyword,
                creators=tuple(creators),
                official_url=official_url,
            )
        )
    return tuple(contents)


def _projects_from_records(records: list[object]) -> tuple[ProjectMention, ...]:
    if len(records) > 2:
        raise WoonError("Codex knowledge projects may contain at most two records")
    projects: list[ProjectMention] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex knowledge project mention must be a mapping")
        allowed = {
            "title",
            "objective",
            "status",
            "materials",
            "lifecycle_status",
            "started_on",
            "ended_on",
            "occurred_on",
        }
        required = {"title", "objective"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex knowledge project mention has unsupported fields")
        title = raw["title"]
        objective = raw["objective"]
        status = raw.get("status", "Active")
        lifecycle_status = raw.get(
            "lifecycle_status",
            _PROJECT_LIFECYCLE.get(status) if isinstance(status, str) else None,
        )
        started_on = _record_date(raw.get("started_on"), "project started_on")
        ended_on = _record_date(raw.get("ended_on"), "project ended_on")
        occurred_on = _record_date(raw.get("occurred_on"), "project occurred_on")
        materials = raw.get("materials", [])
        if (
            not isinstance(title, str)
            or not isinstance(objective, str)
            or not isinstance(status, str)
            or not isinstance(lifecycle_status, str)
        ):
            raise WoonError("Codex knowledge project mention text fields must be strings")
        if not isinstance(materials, list) or not all(isinstance(item, str) for item in materials):
            raise WoonError("Codex knowledge project materials must be a string list")
        projects.append(
            ProjectMention(
                title=title,
                objective=objective,
                status=status,
                materials=tuple(materials),
                lifecycle_status=lifecycle_status,
                started_on=started_on,
                ended_on=ended_on,
                occurred_on=occurred_on,
            )
        )
    return tuple(projects)


def _interview_answer_from_record(raw: object) -> InterviewAnswerMention | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WoonError("Codex knowledge interview_answer must be a mapping")
    allowed = {
        "question",
        "answer",
        "parent_wiki_path",
        "interview_tracks",
        "question_topic",
        "context",
        "evidence",
        "limitations",
        "job_variants",
        "change_reason",
        "quality_assessment",
        "source_label",
        "promote_current",
    }
    required = {"question", "answer", "parent_wiki_path", "interview_tracks", "question_topic"}
    if set(raw).difference(allowed) or not required.issubset(raw):
        raise WoonError("Codex knowledge interview_answer has unsupported fields")
    question = raw["question"]
    answer = raw["answer"]
    parent_wiki_path = raw["parent_wiki_path"]
    interview_tracks = raw["interview_tracks"]
    question_topic = raw["question_topic"]
    context = raw.get("context")
    evidence = raw.get("evidence", [])
    limitations = raw.get("limitations", [])
    job_variants = raw.get("job_variants", [])
    change_reason = raw.get("change_reason", "답변을 새로 정리했다.")
    quality_assessment = raw.get("quality_assessment")
    source_label = raw.get("source_label")
    promote_current = raw.get("promote_current", True)
    if (
        not isinstance(question, str)
        or not isinstance(parent_wiki_path, str)
        or not isinstance(question_topic, str)
    ):
        raise WoonError("Codex knowledge interview question and parent_wiki_path must be text")
    if not isinstance(interview_tracks, list) or not all(
        isinstance(item, str) for item in interview_tracks
    ):
        raise WoonError("Codex knowledge interview_tracks must be a string list")
    if answer is not None and not isinstance(answer, str):
        raise WoonError("Codex knowledge interview answer must be text or null")
    for value, field in (
        (context, "context"),
        (quality_assessment, "quality_assessment"),
        (source_label, "source_label"),
    ):
        if value is not None and not isinstance(value, str):
            raise WoonError(f"Codex knowledge interview {field} must be text or null")
    if not isinstance(change_reason, str) or not isinstance(promote_current, bool):
        raise WoonError("Codex knowledge interview change metadata is invalid")
    for values, field in (
        (evidence, "evidence"),
        (limitations, "limitations"),
        (job_variants, "job_variants"),
    ):
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise WoonError(f"Codex knowledge interview {field} must be a string list")
    return InterviewAnswerMention(
        question=question,
        answer=answer,
        parent_wiki_path=parent_wiki_path,
        interview_tracks=tuple(interview_tracks),
        question_topic=question_topic,
        context=context,
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        job_variants=tuple(job_variants),
        change_reason=change_reason,
        quality_assessment=quality_assessment,
        source_label=source_label,
        promote_current=promote_current,
    )


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
    context: CalendarContext, *, wiki_subject_path: str | None
) -> tuple[str, ...]:
    if not context.include_wiki_subject:
        return context.related_documents
    if wiki_subject_path is None:
        raise WoonError("Codex knowledge calendar context has no Wiki subject")
    return (*context.related_documents, wiki_subject_path)


def _entry_id(entry: CodexKnowledgeEntry) -> str:
    stable = "\0".join(
        (
            entry.day.isoformat(),
            entry.kind,
            entry.title.strip(),
            entry.summary.strip(),
            entry.intent or "",
            entry.next_question or "",
            *(
                "\0".join(
                    (
                        exchange.question,
                        exchange.answer,
                        exchange.outcome or "",
                        *exchange.attachments,
                        *exchange.facts,
                        *exchange.criteria,
                        *exchange.alternatives,
                        *exchange.evidence,
                        *exchange.changes,
                        *exchange.unresolved,
                    )
                )
                for exchange in entry.exchanges
            ),
            entry.disposition,
            entry.review_reason or "",
            str(entry.wiki_update),
            entry.wiki_subject_path or "",
            entry.new_wiki_reason or "",
            entry.parent or "",
            *entry.keywords,
            entry.central_question or "",
            entry.lifecycle_status or "",
            entry.started_on.isoformat() if entry.started_on else "",
            entry.ended_on.isoformat() if entry.ended_on else "",
            entry.occurred_on.isoformat() if entry.occurred_on else "",
            *entry.related_documents,
            *(
                "\0".join(
                    (
                        context.event_day.isoformat(),
                        context.event_title.strip(),
                        context.reason,
                        *context.related_documents,
                        str(context.include_wiki_subject),
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
            *(
                "\0".join(
                    (
                        content.title.strip(),
                        content.content_kind,
                        content.genre or "",
                        content.resource_keyword or "",
                        *content.creators,
                        content.official_url or "",
                    )
                )
                for content in entry.contents
            ),
            *(
                "\0".join(
                    (
                        project.title.strip(),
                        project.objective.strip(),
                        project.status,
                        project.lifecycle_status,
                        project.started_on.isoformat() if project.started_on else "",
                        project.ended_on.isoformat() if project.ended_on else "",
                        project.occurred_on.isoformat() if project.occurred_on else "",
                        *project.materials,
                    )
                )
                for project in entry.projects
            ),
            *(
                (
                    "\0".join(
                        (
                            entry.interview_answer.question,
                            entry.interview_answer.answer or "",
                            entry.interview_answer.parent_wiki_path,
                            *entry.interview_answer.interview_tracks,
                            entry.interview_answer.question_topic,
                            entry.interview_answer.context or "",
                            *entry.interview_answer.evidence,
                            *entry.interview_answer.limitations,
                            *entry.interview_answer.job_variants,
                            entry.interview_answer.change_reason,
                            entry.interview_answer.quality_assessment or "",
                            entry.interview_answer.source_label or "",
                            str(entry.interview_answer.promote_current),
                        )
                    ),
                )
                if entry.interview_answer is not None
                else ()
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
                    "lifecycle_status": entry.lifecycle_status,
                    "started_on": entry.started_on.isoformat() if entry.started_on else None,
                    "ended_on": entry.ended_on.isoformat() if entry.ended_on else None,
                    "occurred_on": entry.occurred_on.isoformat() if entry.occurred_on else None,
                    "wiki_update": entry.wiki_update,
                    "wiki_subject_path": entry.wiki_subject_path,
                    "new_wiki_reason": entry.new_wiki_reason,
                    "parent": entry.parent,
                    "keywords": list(entry.keywords),
                    "central_question": entry.central_question,
                    "intent": entry.intent,
                    "next_question": entry.next_question,
                    "exchanges": [
                        {
                            "question": exchange.question,
                            "answer": exchange.answer,
                            "outcome": exchange.outcome,
                            "attachments": list(exchange.attachments),
                        }
                        for exchange in entry.exchanges
                    ],
                    "related_documents": list(entry.related_documents),
                    "calendar_contexts": [
                        {
                            "event_day": context.event_day.isoformat(),
                            "event_title": context.event_title,
                            "related_documents": list(context.related_documents),
                            "reason": context.reason,
                            "include_wiki_subject": context.include_wiki_subject,
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
                    "contents": [
                        {
                            "title": content.title,
                            "content_kind": content.content_kind,
                            "genre": content.genre,
                            "resource_keyword": content.resource_keyword,
                            "creators": list(content.creators),
                            "official_url": content.official_url,
                        }
                        for content in entry.contents
                    ],
                    "projects": [
                        {
                            "title": project.title,
                            "objective": project.objective,
                            "status": project.status,
                            "lifecycle_status": project.lifecycle_status,
                            "started_on": (
                                project.started_on.isoformat() if project.started_on else None
                            ),
                            "ended_on": (
                                project.ended_on.isoformat() if project.ended_on else None
                            ),
                            "occurred_on": (
                                project.occurred_on.isoformat() if project.occurred_on else None
                            ),
                            "materials": list(project.materials),
                        }
                        for project in entry.projects
                    ],
                    "interview_answer": _interview_answer_record(entry.interview_answer),
                    "disposition": entry.disposition,
                    "review_reason": entry.review_reason,
                }
                for entry in entries
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _interview_answer_record(
    mention: InterviewAnswerMention | None,
) -> dict[str, object] | None:
    if mention is None:
        return None
    return {
        "question": mention.question,
        "answer": mention.answer,
        "parent_wiki_path": mention.parent_wiki_path,
        "interview_tracks": list(mention.interview_tracks),
        "question_topic": mention.question_topic,
        "context": mention.context,
        "evidence": list(mention.evidence),
        "limitations": list(mention.limitations),
        "job_variants": list(mention.job_variants),
        "change_reason": mention.change_reason,
        "quality_assessment": mention.quality_assessment,
        "source_label": mention.source_label,
        "promote_current": mention.promote_current,
    }


def _ledger_path(vault: Path, entry: CodexKnowledgeEntry) -> Path:
    return (
        vault.resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / entry.day.isoformat()
        / f"{_entry_id(entry)}.json"
    )


def _wiki_related_documents(vault: Path, entry: CodexKnowledgeEntry) -> tuple[str, ...]:
    if entry.disposition == "review":
        return entry.related_documents
    identity = _wiki_identity(vault, entry)
    subject_paths = (identity[0],) if identity is not None else ()
    return tuple(
        dict.fromkeys(
            (
                *subject_paths,
                *entry.related_documents,
                *((entry.interview_answer.parent_wiki_path,) if entry.interview_answer else ()),
                *(_content_wiki_relative_path(vault, item) for item in entry.contents),
                *(
                    _entity_wiki_relative_path(vault, item.title, "wiki/personal/projects")
                    for item in entry.projects
                ),
            )
        )
    )


def _prepare_ledger_entry(vault: Path, entry: CodexKnowledgeEntry) -> tuple[Path, bytes]:
    path = _ledger_path(vault, entry)
    identity = _wiki_identity(vault, entry)
    value = {
        "kind": entry.kind,
        "title": entry.title.strip(),
        "summary": entry.summary.strip(),
        "lifecycle_status": entry.lifecycle_status,
        "started_on": entry.started_on.isoformat() if entry.started_on else None,
        "ended_on": entry.ended_on.isoformat() if entry.ended_on else None,
        "occurred_on": entry.occurred_on.isoformat() if entry.occurred_on else None,
        "wiki_update": entry.wiki_update,
        "wiki_subject_path": identity[0] if identity is not None else None,
        "new_wiki_reason": entry.new_wiki_reason,
        "parent": entry.parent,
        "keywords": list(entry.keywords),
        "central_question": entry.central_question,
        "disposition": entry.disposition,
        "review_reason": entry.review_reason,
        "intent": entry.intent.strip() if entry.intent else None,
        "exchanges": [
            {
                "question": exchange.question.strip(),
                "answer": exchange.answer.strip(),
                "outcome": exchange.outcome.strip() if exchange.outcome else None,
                "attachments": [item.strip() for item in exchange.attachments],
                "facts": [item.strip() for item in exchange.facts],
                "criteria": [item.strip() for item in exchange.criteria],
                "alternatives": [item.strip() for item in exchange.alternatives],
                "evidence": [item.strip() for item in exchange.evidence],
                "changes": [item.strip() for item in exchange.changes],
                "unresolved": [item.strip() for item in exchange.unresolved],
            }
            for exchange in entry.exchanges
        ],
        "related_documents": list(_wiki_related_documents(vault, entry)),
        "calendar_contexts": [
            {
                "event_day": context.event_day.isoformat(),
                "event_title": context.event_title.strip(),
                "related_documents": list(context.related_documents),
                "reason": context.reason,
                "include_wiki_subject": context.include_wiki_subject,
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
        "contents": [
            {
                "title": content.title.strip(),
                "content_kind": content.content_kind,
                "genre": content.genre,
                "resource_keyword": content.resource_keyword,
                "creators": list(content.creators),
                "official_url": content.official_url,
            }
            for content in entry.contents
        ],
        "projects": [
            {
                "title": project.title.strip(),
                "objective": project.objective.strip(),
                "status": project.status,
                "lifecycle_status": project.lifecycle_status,
                "started_on": project.started_on.isoformat() if project.started_on else None,
                "ended_on": project.ended_on.isoformat() if project.ended_on else None,
                "occurred_on": project.occurred_on.isoformat() if project.occurred_on else None,
                "materials": list(project.materials),
            }
            for project in entry.projects
        ],
        "interview_answer": _interview_answer_record(entry.interview_answer),
    }
    serialized = encode_json(value)
    if path.exists() and path.read_bytes() != serialized:
        raise WoonError("Codex knowledge ledger entry conflicts with an existing record")
    return path, serialized


def _write_ledger_entry(vault: Path, entry: CodexKnowledgeEntry) -> None:
    path, serialized = _prepare_ledger_entry(vault, entry)


def _prepare_input_status(vault: Path, *, day: date, input_state: str) -> tuple[Path, bytes]:
    """Persist only the availability state needed to explain a blank daily view."""

    path = (
        vault.resolve()
        / ".local/woon-knowledge/codex-knowledge"
        / day.isoformat()
        / "_input-status.json"
    )
    serialized = encode_json({"input_state": input_state})
    return path, serialized


def _write_input_status(vault: Path, *, day: date, input_state: str) -> None:
    path, serialized = _prepare_input_status(vault, day=day, input_state=input_state)
    if not path.exists() or path.read_bytes() != serialized:
        atomic_write(path, serialized, mode=0o600)


def _apply_codex_pipeline_batch(vault: Path, prepared: dict[Path, bytes]) -> None:
    """Write one Codex input batch as a rollback-capable local transaction."""

    root = vault.resolve()
    allowed_roots = tuple(
        (root / relative).resolve()
        for relative in (
            "wiki",
            "brain/review/codex",
            ".local/woon-knowledge/codex-knowledge",
        )
    )
    snapshots: list[tuple[Path, bytes | None, int]] = []
    writes: list[tuple[Path, bytes, int]] = []
    for path, content in sorted(prepared.items(), key=lambda item: item[0].as_posix()):
        resolved = path.resolve()
        if not any(resolved.is_relative_to(allowed) for allowed in allowed_roots):
            raise WoonError("Codex knowledge pipeline attempted an unowned write")
        previous = resolved.read_bytes() if resolved.is_file() else None
        mode = (resolved.stat().st_mode & 0o777) if resolved.exists() else 0o600
        snapshots.append((resolved, previous, mode))
        if previous != content:
            writes.append((resolved, content, mode))
    try:
        for path, content, mode in writes:
            _ensure_private_runtime_parent(root, path.parent)
            atomic_write(path, content, mode=mode)
        if any(path.resolve().is_relative_to(root / "wiki") for path in prepared):
            tree_report = prepare_wiki_tree_refresh(root)
            if tree_report.issues:
                raise WoonError(
                    "Codex Wiki update would break the canonical tree: "
                    + "; ".join(tree_report.issues[:8])
                )
            for path, content in sorted(
                tree_report.pages.items(), key=lambda item: item[0].as_posix()
            ):
                previous = path.read_bytes() if path.is_file() else None
                mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
                snapshots.append((path, previous, mode))
                if previous != content:
                    atomic_write(path, content, mode=mode)
    except Exception:
        for path, previous, mode in reversed(snapshots):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous, mode=mode)
        raise


def _ensure_private_runtime_parent(vault: Path, parent: Path) -> None:
    """Create every Codex-ledger directory with user-only permissions."""

    runtime_root = (vault / ".local/woon-knowledge").resolve()
    resolved_parent = parent.resolve()
    if not resolved_parent.is_relative_to(runtime_root):
        return
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    current = runtime_root
    for part in resolved_parent.relative_to(runtime_root).parts:
        current /= part
        current.mkdir(exist_ok=True)
        current.chmod(0o700)


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
    conclusions become a readable review card, never a direct external task,
    Calendar, person-card, or source mutation.
    """

    candidates: list[ReviewCandidate] = []
    for entry in entries:
        if entry.disposition == "review":
            stable = "\0".join(
                (_entry_id(entry), entry.title, entry.summary, entry.review_reason or "")
            )
            candidates.append(
                ReviewCandidate(
                    candidate_id=(
                        f"codex-ambiguous-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"
                    ),
                    kind="codex-projection",
                    source_locator=f"codex:{_entry_id(entry)}",
                    summary=entry.summary.strip(),
                    display_title=f"확인 필요: {entry.title.strip()}"[:72],
                    review_kind="분류 확인",
                    occurred_at=datetime.fromtimestamp(0, tz=UTC),
                    time_precision="none",
                    scheduled_for=None,
                    calendar_candidate=False,
                )
            )
            continue
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


def _entity_wiki_relative_path(vault: Path, title: str, root: str) -> str:
    resolved = resolve_wiki_path(vault, title)
    if resolved.is_file():
        return resolved.relative_to(vault.resolve()).as_posix()
    return f"{root}/{Path(wiki_relative_path(title)).name}"


def _book_genre_parent(vault: Path, genre: str | None) -> str:
    if genre is None or not genre.strip():
        raise WoonError("book genre keyword is required before creating a book Wiki")
    wanted = genre.strip().casefold()
    matches: list[tuple[str, str]] = []
    for path in sorted((vault / "wiki/books").glob("*.md")):
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        if _frontmatter_value(text, "node_kind") != "hub":
            continue
        title = _frontmatter_value(text, "title")
        keywords = _frontmatter_list(text, "keywords")
        if wanted not in {title.casefold(), *(item.casefold() for item in keywords)}:
            continue
        matches.append((path.relative_to(vault).with_suffix("").as_posix(), title))
    if len(matches) != 1:
        raise WoonError("book genre must match exactly one existing genre hub before Wiki creation")
    relative, title = matches[0]
    return f"[[{relative}|{title}]]"


def _resource_keyword_topic(vault: Path, keyword: str | None) -> tuple[str, str]:
    if keyword is None or not keyword.strip():
        raise WoonError("resource keyword is required before indexing a resource")
    wanted = keyword.strip().casefold()
    root = (vault / "wiki/resources").resolve()
    matches: list[tuple[str, str]] = []
    for path in sorted(root.glob("*.md")) if root.is_dir() else ():
        if path.name == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        if _frontmatter_value(text, "node_kind") != "topic":
            continue
        title = _frontmatter_value(text, "title")
        keywords = _frontmatter_list(text, "keywords")
        resource_keyword = _frontmatter_value(text, "resource_keyword")
        if wanted not in {
            title.casefold(),
            resource_keyword.casefold(),
            *(item.casefold() for item in keywords),
        }:
            continue
        matches.append((path.relative_to(vault).as_posix(), title))
    if len(matches) != 1:
        raise WoonError(
            "resource keyword must match exactly one existing resource topic before indexing"
        )
    return matches[0]


def _content_wiki_relative_path(vault: Path, content: ContentMention) -> str:
    if content.content_kind == "book":
        return _entity_wiki_relative_path(vault, content.title, "wiki/books")
    return _resource_keyword_topic(vault, content.resource_keyword)[0]


def _prepare_resource_index_pages(
    vault: Path, entries: tuple[CodexKnowledgeEntry, ...]
) -> dict[Path, bytes]:
    """Add non-book source hyperlinks to one existing keyword topic only."""

    additions: dict[Path, set[str]] = {}
    for entry in entries:
        if entry.disposition == "review":
            continue
        for content in entry.contents:
            if content.content_kind == "book":
                continue
            relative, _ = _resource_keyword_topic(vault, content.resource_keyword)
            assert content.official_url is not None
            label = content.title.strip().replace("[", "(").replace("]", ")")
            additions.setdefault(vault / relative, set()).add(
                f"- [{label}]({content.official_url})"
            )
    pages: dict[Path, bytes] = {}
    for path, links in additions.items():
        text = path.read_text(encoding="utf-8")
        existing_urls = set(re.findall(r"(?m)^- \[[^\]]+\]\((https://[^)]+)\)\s*$", text))
        new_links = []
        for link in sorted(links, key=str.casefold):
            match = re.search(r"\((https://[^)]+)\)$", link)
            if match is not None and match.group(1) not in existing_urls:
                new_links.append(link)
        if new_links:
            text = text.rstrip() + "\n" + "\n".join(new_links) + "\n"
        pages[path] = text.encode("utf-8")
    return pages


def _existing_tree_fields(
    vault: Path, relative: str
) -> tuple[str, tuple[str, ...], str | None, str, str]:
    text = (vault / relative).read_text(encoding="utf-8")
    parent = _frontmatter_value(text, "parent")
    keywords = _frontmatter_list(text, "keywords")
    node_kind = _frontmatter_value(text, "node_kind")
    view_mode = _frontmatter_value(text, "view_mode")
    central_question = _frontmatter_value(text, "central_question") or None
    if not parent or not keywords or not node_kind or not view_mode:
        raise WoonError(f"existing Wiki subject has incomplete tree metadata: {relative}")
    return parent, keywords, central_question, node_kind, view_mode


def _tree_fields_for_entry(
    vault: Path, entry: CodexKnowledgeEntry, subject_path: str
) -> tuple[str, tuple[str, ...], str | None, str, str]:
    if (vault / subject_path).is_file():
        return _existing_tree_fields(vault, subject_path)
    assert entry.parent is not None and entry.keywords and entry.central_question is not None
    node_kind = "decision" if entry.kind == "결정" else "topic"
    view_mode = "article" if entry.kind in {"질문", "결정", "회고"} else "tree"
    return entry.parent, entry.keywords, entry.central_question, node_kind, view_mode


def _wiki_deltas(vault: Path, entries: tuple[CodexKnowledgeEntry, ...]) -> tuple[WikiDelta, ...]:
    """Convert one sanitized batch directly into canonical Wiki subject deltas."""

    deltas: list[WikiDelta] = []
    for entry in entries:
        if entry.disposition == "review":
            continue
        identity = _wiki_identity(vault, entry)
        subject_path = identity[0] if identity is not None else None
        subject_title = identity[1] if identity is not None else entry.title
        tree_fields = (
            _tree_fields_for_entry(vault, entry, subject_path) if subject_path is not None else None
        )
        entity_paths = tuple(
            dict.fromkeys(
                [_content_wiki_relative_path(vault, content) for content in entry.contents]
                + [
                    _entity_wiki_relative_path(vault, project.title, "wiki/personal/projects")
                    for project in entry.projects
                ]
            )
        )
        main_relations = tuple(
            path
            for path in dict.fromkeys((*entry.related_documents, *entity_paths))
            if path != subject_path
        )
        if entry.wiki_update:
            assert subject_path is not None and tree_fields is not None
            tree_parent, tree_keywords, central_question, node_kind, view_mode = tree_fields
            interview = entry.interview_answer
            deltas.append(
                WikiDelta(
                    title=subject_title,
                    summary=entry.summary,
                    facets=(
                        ("커리어", "학습")
                        if interview is not None
                        else _facets_for_kind(entry.kind)
                    ),
                    knowledge_state=_state_for_kind(entry.kind),
                    day=entry.day,
                    event_kind=_event_kind_for_entry(entry.kind),
                    intent=entry.intent,
                    next_question=entry.next_question,
                    related_documents=main_relations,
                    wiki_subject_path=subject_path,
                    parent=tree_parent,
                    keywords=tree_keywords,
                    central_question=central_question,
                    node_kind=node_kind,
                    view_mode=view_mode,
                    lifecycle_status=entry.lifecycle_status,
                    started_on=entry.started_on,
                    ended_on=entry.ended_on,
                    occurred_on=entry.occurred_on,
                    interview_tracks=(interview.interview_tracks if interview is not None else ()),
                    question_topic=(interview.question_topic if interview is not None else None),
                    interview_answer=(
                        InterviewAnswerRevision(
                            question=interview.question,
                            answer=interview.answer,
                            context=interview.context,
                            evidence=interview.evidence,
                            limitations=interview.limitations,
                            job_variants=interview.job_variants,
                            change_reason=interview.change_reason,
                            quality_assessment=interview.quality_assessment,
                            source_label=interview.source_label,
                            promote_current=interview.promote_current,
                        )
                        if interview is not None
                        else None
                    ),
                )
            )
        for content in entry.contents:
            if content.content_kind != "book":
                continue
            content_path = _entity_wiki_relative_path(
                vault,
                content.title,
                "wiki/books",
            )
            content_exists = (vault / content_path).is_file()
            if content_exists:
                content_parent, content_keywords, content_question, content_node, content_view = (
                    _existing_tree_fields(vault, content_path)
                )
            else:
                content_parent = _book_genre_parent(vault, content.genre)
                content_keywords = (content.title.strip(),)
                content_question = f"{content.title.strip()}에서 무엇을 이해하고 다시 찾을 것인가?"
                content_node = "entity"
                content_view = "linear"
            relations = (
                (subject_path,) if subject_path is not None and subject_path != content_path else ()
            )
            content_delta = WikiDelta(
                title=content.title,
                summary=f"{content.title.strip()}을 책으로 관리한다.",
                facets=("학습",),
                knowledge_state="확인 필요",
                day=entry.day,
                event_kind="산출물",
                related_documents=relations,
                wiki_subject_path=content_path,
                parent=content_parent,
                keywords=content_keywords,
                central_question=content_question,
                node_kind=content_node,
                view_mode=content_view,
                entity_kind="book",
                content_kind=content.content_kind,
                creators=content.creators,
                official_url=content.official_url,
            )
            deltas.append(content_delta)
        for project in entry.projects:
            project_path = _entity_wiki_relative_path(
                vault, project.title, "wiki/personal/projects"
            )
            project_exists = (vault / project_path).is_file()
            if project_exists:
                project_parent, project_keywords, project_question, project_node, project_view = (
                    _existing_tree_fields(vault, project_path)
                )
            else:
                project_parent = "[[wiki/personal/projects/README|프로젝트]]"
                project_keywords = (project.title.strip(),)
                project_question = project.objective.strip()
                project_node = "entity"
                project_view = "project"
            relations = (
                (subject_path,) if subject_path is not None and subject_path != project_path else ()
            )
            project_delta = WikiDelta(
                title=project.title,
                summary=project.objective,
                facets=("프로젝트",),
                knowledge_state="생각 중",
                day=entry.day,
                event_kind="산출물",
                related_documents=relations,
                wiki_subject_path=project_path,
                parent=project_parent,
                keywords=project_keywords,
                central_question=project_question,
                node_kind=project_node,
                view_mode=project_view,
                entity_kind="project",
                lifecycle_status=project.lifecycle_status,
                started_on=project.started_on,
                ended_on=project.ended_on,
                occurred_on=project.occurred_on,
                project_status=project.status,
                objective=project.objective,
                materials=project.materials,
            )
            deltas.append(project_delta)
    return tuple(deltas)


def _facets_for_kind(kind: str) -> tuple[str, ...]:
    mapping: dict[str, tuple[str, ...]] = {
        "학습": ("개념", "학습"),
        "개념": ("개념", "학습"),
        "질문": ("개념", "학습"),
        "결정": ("개념", "생활"),
        "회고": ("개념", "생활"),
        "커리어": ("커리어",),
        "자료": ("리소스", "학습"),
        "프로젝트": ("프로젝트",),
        "인물": ("인물",),
        "관계": ("인물", "생활"),
        "창작": ("프로젝트",),
        "건강": ("생활",),
    }
    return mapping.get(kind, ("생활",))


def _state_for_kind(kind: str) -> str:
    if kind in {"일정", "인물", "관계", "커리어", "재정·행정", "자료"}:
        return "확인 필요"
    return "생각 중"


def _event_kind_for_entry(kind: str) -> str:
    if kind == "일정":
        return "예정"
    if kind == "결정":
        return "변경"
    if kind in {"자료", "프로젝트", "창작"}:
        return "산출물"
    return "실행"


def _related_document_title(vault: Path, relative_path: str) -> str:
    """Use a person-readable Markdown title in generated Obsidian links."""

    path = _related_document(vault, relative_path)
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip().strip("\"'")
    h1 = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return h1.group(1).strip() if h1 else path.stem
