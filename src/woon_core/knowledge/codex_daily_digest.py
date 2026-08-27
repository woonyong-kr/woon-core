"""Project canonical Wiki changes into one daily navigation note.

The semantic ledger keeps conclusions and canonical document links. A separate
local-only archive preserves the conversation evidence needed to regenerate
them. The daily note renders only what changed in the Wiki; questions, answers,
timestamps and attachment inventories never become a second archive.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.codex_source_archive import load_codex_source_bundles
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunOutcome,
    RunRequest,
    snapshot_owned_paths,
)

_ENTRY_KINDS = {
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
    "완료",
    "프로젝트",
}
_INPUT_STATES = {
    "processed",
    "no-meaningful",
    "partial",
    "pending",
    "unavailable",
    "source-only",
}
_DIGEST_RENDER_REVISION = "26"
_DAILY_ENTRY_LIMIT = 256
_VISIBLE_LIMIT = 900
_TITLE_LIMIT = 80
_ATTACHMENT_LIMIT = 220
_SENSITIVE_RE = re.compile(
    r"(?:\b(?:api[_-]?key|token|secret|password|bearer)\b|sk-[A-Za-z0-9_-]{12,})",
    flags=re.IGNORECASE,
)
_RELATED_ROOTS = ("wiki/",)
_DAILY_GROUP_ENTITY_KINDS = {
    "project",
    "person",
    "book",
    "career",
    "application",
    "career-application",
}
_KEYWORD_CANDIDATES = (
    "Obsidian",
    "Wiki",
    "Codex",
    "Claude",
    "Herdr",
    "Apple Calendar",
    "Link Calendar",
    "Linked Graph",
    "AICE",
    "KRAFTON",
    "Codility",
    "PintOS",
    "Kyro",
    "Kubernetes",
    "Discord",
    "AWS",
    "1Password",
    "Cupertino",
    "Woon Voice",
    "STT",
    "TTS",
    "Node.js",
    "Next.js",
    "React",
    "LangGraph",
    "이력서",
    "면접",
    "일정",
    "자동화",
    "지식화",
    "데님",
    "커리어",
)


@dataclass(frozen=True, slots=True)
class CodexDailyDigestEntry:
    """One human-readable, source-minimized daily conclusion."""

    kind: str
    title: str
    summary: str
    intent: str | None = None
    exchanges: tuple[CodexDailyExchange, ...] = ()
    related_documents: tuple[str, ...] = ()
    people: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexDailyExchange:
    """One readable exchange; the raw transcript and locators stay outside Markdown."""

    question: str
    answer: str
    understanding: str | None = None
    outcome: str | None = None
    attachments: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexDailyDigestResult:
    """Only non-sensitive operation facts returned to the caller."""

    day: str
    entry_count: int
    receipt_id: str
    replayed: bool
    relative_path: str


@dataclass(frozen=True, slots=True)
class LegacyDailyDigestMigrationResult:
    """A one-time, reversible-by-Git migration of generated daily fragments."""

    migrated_days: tuple[str, ...]


def record_codex_daily_digest(
    vault: Path,
    *,
    day: date,
    entries: tuple[CodexDailyDigestEntry, ...],
    input_state: str = "processed",
    replace_generated_digest: bool = False,
) -> CodexDailyDigestResult:
    """Write the owned conversation block into one KST daily record.

    The daily record is the only user-facing history for a date.  A prior
    ``DailyDigest`` file is deliberately not read or replaced here: one-time
    migration owns legacy-file removal, while normal automation owns only the
    marker block in ``inbox/daily``.
    """

    settings = load_orchestrator_settings(vault)
    contract = next(
        (
            item
            for item in settings.automations
            if item.automation_id == "daily-record-materialization"
        ),
        None,
    )
    if contract is None or contract.mode != "materialize" or contract.status != "enabled":
        raise WoonError("daily record materialization automation is not enabled")
    if set(contract.owned_paths) != {"inbox/daily", "inbox/calendar", "brain/review/activity"}:
        raise WoonError("daily record materialization has an unsafe write boundary")
    _validate_entries(settings.vault, entries, input_state=input_state)
    canonical_entries = tuple(entry for entry in entries if entry.related_documents)
    # A processed legacy ledger can contain operational one-off records that
    # were explicitly marked ``wiki_update=false``. Those records remain in
    # local evidence, but their existence must not turn the human Daily into a
    # permanent promotion queue. ``source-only`` is reserved for an explicit
    # incomplete-promotion state written by the ingestion lane.
    rendered_input_state = input_state
    source_bundles = load_codex_source_bundles(settings.vault, day=day)
    if input_state == "source-only" and not source_bundles:
        raise WoonError("source-only daily record requires local conversation evidence")
    payload = {
        "render_revision": _DIGEST_RENDER_REVISION,
        "day": day.isoformat(),
        "input_state": input_state,
        "entries": [asdict(item) for item in entries],
        "source_bundles": source_bundles,
    }
    serialized_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    serialized = serialized_text.encode("utf-8")
    input_sha256 = hashlib.sha256(serialized).hexdigest()
    token = f"codex-daily-v{_DIGEST_RENDER_REVISION}-{day.strftime('%Y%m%d')}-{input_sha256[:12]}"
    request = RunRequest(
        source_range=token,
        input_sha256=input_sha256,
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=token,
    )
    destination = settings.vault / "inbox" / "daily" / f"{day.isoformat()}.md"
    content = _render_daily_block(
        settings.vault,
        canonical_entries,
        input_state=rendered_input_state,
        source_bundles=source_bundles,
    )

    def produce() -> RunOutcome:
        _ensure_daily_note(settings.vault, day)
        existing = destination.read_text(encoding="utf-8")
        updated = _replace_daily_block(
            existing,
            block=content,
            day=day,
            allow_legacy_embed=replace_generated_digest,
        )
        updated = _normalize_daily_shell(updated)
        updated = _update_daily_metadata(
            updated,
            canonical_entries,
            input_state=rendered_input_state,
        )
        if updated != existing:
            atomic_write(destination, updated.encode("utf-8"), mode=0o600)
        return RunOutcome(
            candidate_ids=tuple(_entry_id(day, entry) for entry in canonical_entries),
            output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    result = AutomationRunStore(settings).run("daily-record-materialization", request, produce)
    return CodexDailyDigestResult(
        day=day.isoformat(),
        entry_count=len(canonical_entries),
        receipt_id=result.receipt_id,
        replayed=result.replayed,
        relative_path=destination.relative_to(settings.vault).as_posix(),
    )


def entries_from_records(records: list[dict[str, object]]) -> tuple[CodexDailyDigestEntry, ...]:
    """Parse a narrow tool payload and reject raw conversation-shaped fields."""

    if len(records) > _DAILY_ENTRY_LIMIT:
        raise WoonError("Codex daily digest exceeds its bounded daily entry limit")
    entries: list[CodexDailyDigestEntry] = []
    for raw in records:
        allowed = {
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
        required = {"kind", "title", "summary"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex daily digest entry has unsupported fields")
        related = raw.get("related_documents", [])
        people = raw.get("people", [])
        intent = raw.get("intent")
        exchanges = raw.get("exchanges", [])
        if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
            raise WoonError("Codex daily digest related_documents must be a string list")
        if not isinstance(people, list) or not all(isinstance(item, dict) for item in people):
            raise WoonError("Codex daily digest people must be a list of mappings")
        kind, title, summary = raw["kind"], raw["title"], raw["summary"]
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(summary, str):
            raise WoonError("Codex daily digest entry text fields must be strings")
        if intent is not None and not isinstance(intent, str):
            raise WoonError("Codex daily digest intent must be a string or null")
        entries.append(
            CodexDailyDigestEntry(
                kind=kind,
                title=title,
                summary=summary,
                intent=intent,
                exchanges=_daily_exchanges(exchanges),
                related_documents=tuple(related),
                people=_person_names(people),
            )
        )
    return tuple(entries)


def record_daily_digest_from_codex_ledger(vault: Path, *, day: date) -> CodexDailyDigestResult:
    """Materialize the daily view from already-sanitized conversation entries.

    The daily lane does not reread a Codex transcript.  It consumes the local
    ledger written by the four-hourly conversation lane, so one conversation
    is classified exactly once.
    """

    from woon_core.knowledge.codex_knowledge import load_daily_entries, load_daily_input_status

    records = list(load_daily_entries(vault, day=day))
    recorded_input_state = load_daily_input_status(vault, day=day)
    digest_input_state: str = recorded_input_state or (
        "processed"
        if records
        else "source-only"
        if load_codex_source_bundles(vault, day=day)
        else "unavailable"
    )
    return record_codex_daily_digest(
        vault,
        day=day,
        entries=entries_from_records(records),
        input_state=digest_input_state,
        replace_generated_digest=True,
    )


def migrate_legacy_daily_digests(vault: Path) -> LegacyDailyDigestMigrationResult:
    """Move only generated ``DailyDigest`` files into their daily records.

    Validation completes for every candidate before the first write.  A file
    with human-authored or malformed content aborts the migration rather than
    being silently removed.  The caller can recover the exact files through
    Git because this migration operates only on Vault-tracked Markdown.
    """

    root = vault.expanduser().resolve()
    legacy_root = root / "inbox" / "daily-digests"
    if not legacy_root.exists():
        return LegacyDailyDigestMigrationResult(migrated_days=())
    if not legacy_root.is_dir() or legacy_root.is_symlink():
        raise WoonError("legacy daily digest root is unsafe")

    plans: list[tuple[date, Path, Path, str]] = []
    for legacy in sorted(legacy_root.glob("????-??-??.md")):
        if not legacy.is_file() or legacy.is_symlink():
            raise WoonError("legacy daily digest path is unsafe")
        try:
            day = date.fromisoformat(legacy.stem)
        except ValueError as error:
            raise WoonError("legacy daily digest filename is invalid") from error
        block = _legacy_digest_block(legacy.read_text(encoding="utf-8"), day=day)
        daily = root / "inbox" / "daily" / f"{day.isoformat()}.md"
        if not daily.is_file() or daily.is_symlink():
            raise WoonError("legacy daily digest has no safe daily record target")
        updated = _replace_daily_block(
            daily.read_text(encoding="utf-8"),
            block=block,
            day=day,
            allow_legacy_embed=True,
        )
        plans.append((day, legacy, daily, updated))

    for _, _, daily, updated in plans:
        atomic_write(daily, updated.encode("utf-8"), mode=0o600)
    for _, legacy, _, _ in plans:
        legacy.unlink()
    with suppress(OSError):
        legacy_root.rmdir()
    return LegacyDailyDigestMigrationResult(
        migrated_days=tuple(day.isoformat() for day, _, _, _ in plans)
    )


def _validate_entries(
    vault: Path, entries: tuple[CodexDailyDigestEntry, ...], *, input_state: str
) -> None:
    if len(entries) > _DAILY_ENTRY_LIMIT:
        raise WoonError("Codex daily digest exceeds its bounded daily entry limit")
    if input_state not in _INPUT_STATES:
        raise WoonError("Codex daily digest input state is invalid")
    if input_state in {"pending", "unavailable"} and entries:
        raise WoonError("pending or unavailable Codex input must not create a daily digest entry")
    for entry in entries:
        if entry.kind not in _ENTRY_KINDS:
            raise WoonError("Codex daily digest entry kind is invalid")
        _visible_text(entry.title, "title", _TITLE_LIMIT)
        _visible_text(entry.summary, "summary", _VISIBLE_LIMIT)
        if entry.intent is not None:
            _visible_text(entry.intent, "intent", _VISIBLE_LIMIT)
        if len(entry.exchanges) > 8:
            raise WoonError("Codex daily digest entry may contain at most eight exchanges")
        for exchange in entry.exchanges:
            _visible_text(exchange.question, "exchange question", _VISIBLE_LIMIT)
            _visible_text(exchange.answer, "exchange answer", _VISIBLE_LIMIT)
            if exchange.understanding is not None:
                _visible_text(exchange.understanding, "exchange understanding", _VISIBLE_LIMIT)
            if exchange.outcome is not None:
                _visible_text(exchange.outcome, "exchange outcome", _VISIBLE_LIMIT)
            if len(exchange.attachments) > 6 or len(set(exchange.attachments)) != len(
                exchange.attachments
            ):
                raise WoonError("Codex daily digest attachments must be unique and bounded")
            for attachment in exchange.attachments:
                _visible_text(attachment, "attachment", _ATTACHMENT_LIMIT)
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
                        f"Codex daily digest exchange {field} values must be unique and bounded"
                    )
                for value in values:
                    _visible_text(value, f"exchange {field}", _VISIBLE_LIMIT)
        if len(set(entry.related_documents)) != len(entry.related_documents):
            raise WoonError("Codex daily digest related documents must be unique")
        for relative_path in entry.related_documents:
            _related_document(vault, relative_path)
        if len(set(entry.people)) != len(entry.people):
            raise WoonError("Codex daily digest people must be unique")
        for name in entry.people:
            _visible_text(name, "person name", 48)


def _visible_text(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value or _SENSITIVE_RE.search(value):
        raise WoonError(f"Codex daily digest {field} is not safe visible text")


def _related_document(vault: Path, relative_path: str) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path.endswith(".md")
        or not relative_path.startswith(_RELATED_ROOTS)
    ):
        raise WoonError("Codex daily digest related document path is not allowed")
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WoonError("Codex daily digest related document path is not allowed")
    path = (vault / candidate).resolve()
    try:
        path.relative_to(vault.resolve())
    except ValueError as error:
        raise WoonError("Codex daily digest related document escapes vault") from error
    if not path.is_file() or path.is_symlink():
        raise WoonError("Codex daily digest related document is missing")
    return path


def _entry_id(day: date, entry: CodexDailyDigestEntry) -> str:
    stable = "\0".join(
        (
            day.isoformat(),
            entry.kind,
            entry.title,
            entry.summary,
            entry.intent or "",
            *(
                "\0".join(
                    (
                        exchange.question,
                        exchange.answer,
                        exchange.understanding or "",
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
            *entry.related_documents,
            *entry.people,
        )
    )
    return f"digest-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _render_daily_block(
    vault: Path,
    entries: tuple[CodexDailyDigestEntry, ...],
    *,
    input_state: str,
    source_bundles: tuple[dict[str, object], ...] = (),
) -> str:
    status_title, status_message = _daily_status(input_state=input_state, entry_count=len(entries))
    lines = [
        "<!-- woon-codex-digest:start -->",
        "",
        f"**{status_title}** — {status_message}",
    ]
    if not entries and not source_bundles:
        lines.extend(
            [
                "",
                "<!-- woon-codex-digest:end -->",
                "",
            ]
        )
        return "\n".join(lines)
    if entries:
        lines.extend(["", "## 정본 변경", ""])
        subjects = tuple(
            _combine_subject_entries(vault, same_subject)
            for same_subject in _group_section_entries(vault, list(entries))
        )
        primary_documents = tuple(
            path
            for entry in subjects
            if (path := _primary_related_document(vault, entry)) is not None
        )
        entry_linked_documents: list[str] = []
        for entry in subjects:
            primary_document, supporting_documents = _entry_document_links(
                vault,
                entry,
                primary_documents=primary_documents,
                already_linked=tuple(entry_linked_documents),
            )
            lines.extend(
                [
                    _render_entry(
                        vault,
                        entry,
                        primary_document=primary_document,
                        supporting_documents=supporting_documents,
                    ),
                    "",
                ]
            )
            entry_linked_documents.extend(
                path
                for path in (primary_document, *supporting_documents)
                if path is not None and path not in entry_linked_documents
            )
    else:
        entry_linked_documents = []
    lines.extend(["", "<!-- woon-codex-digest:end -->", ""])
    return "\n".join(lines)


def _replace_daily_block(
    note: str,
    *,
    block: str,
    day: date,
    allow_legacy_embed: bool,
) -> str:
    """Replace only the Core-owned block without touching personal writing."""

    start = "<!-- woon-codex-digest:start -->"
    end = "<!-- woon-codex-digest:end -->"
    start_index = note.find(start)
    end_index = note.find(end)
    if start_index >= 0 or end_index >= 0:
        if start_index < 0 or end_index < start_index or note.find(start, start_index + 1) >= 0:
            raise WoonError("daily Codex block ownership marker is invalid")
        tail = end_index + len(end)
        if tail < len(note) and note[tail] == "\n":
            tail += 1
        return f"{note[:start_index]}{block}{note[tail:]}"

    legacy_embed = f"## Codex 하루 정리\n\n![[../daily-digests/{day.isoformat()}]]\n"
    if legacy_embed in note:
        if not allow_legacy_embed:
            raise WoonError("daily note still uses the retired Codex digest embed")
        return note.replace(legacy_embed, block, 1)

    anchor = "## 포착\n"
    if anchor in note:
        return note.replace(anchor, f"{block}\n{anchor}", 1)
    separator = "" if note.endswith("\n\n") else "\n" if note.endswith("\n") else "\n\n"
    return f"{note}{separator}{block}"


def _normalize_daily_shell(note: str) -> str:
    """Remove only retired empty boilerplate while preserving personal text."""

    normalized = note.replace("\n\ud558\ub8e8 \uc870\uac01 \uc815\ub9ac \ub300\uae30.\n", "\n")
    for heading in ("오늘의 초점", "포착", "질문", "만든 문서"):
        pattern = rf"\n## {re.escape(heading)}\n(?P<body>.*?)(?=\n## |\n<!-- |\Z)"

        def remove_if_empty(match: re.Match[str]) -> str:
            return "\n" if match.group("body").strip() in {"", "-"} else match.group(0)

        normalized = re.sub(pattern, remove_if_empty, normalized, flags=re.DOTALL)
    normalized = re.sub(
        r"\n## 사실 이력\n\s*- 시간·행동·결정·외부 원본 위치만 짧게 기록\s*"
        r"(?=\n## |\n<!-- |\Z)",
        "\n",
        normalized,
    )
    normalized = re.sub(
        r"\n## Woon 처리 안내\n\s*"
        r"여기에 쓴 자유 메모는 자동으로 다른 폴더로 옮기지 않는다\..*?"
        r"실제 결정이 필요할 때만 확인한다\.\s*",
        "\n",
        normalized,
        flags=re.DOTALL,
    )
    if "<!-- woon-tasks:start -->" not in normalized:
        heading_end = re.search(r"^# .+?\n", normalized, flags=re.MULTILINE)
        if heading_end is None:
            raise WoonError("daily note heading is missing")
        task_block = (
            "\n## \uc624\ub298\uc758 \ud560 \uc77c\n\n"
            "<!-- woon-tasks:start -->\n"
            "<!-- woon-tasks:end -->\n"
        )
        normalized = normalized[: heading_end.end()] + task_block + normalized[heading_end.end() :]
    if "## \uc790\uc720 \uba54\ubaa8" not in normalized:
        normalized = normalized.rstrip() + "\n\n## \uc790\uc720 \uba54\ubaa8\n"
    return re.sub(r"\n{4,}", "\n\n\n", normalized).rstrip() + "\n"


def _update_daily_metadata(
    note: str,
    entries: tuple[CodexDailyDigestEntry, ...],
    *,
    input_state: str,
) -> str:
    """Keep the native Base columns useful without owning personal body text."""

    if not note.startswith("---\n"):
        return note
    boundary = note.find("\n---\n", 4)
    if boundary < 0:
        raise WoonError("daily note frontmatter is invalid")
    frontmatter = note[4:boundary].splitlines()
    cleaned: list[str] = []
    skipping_keywords = False
    for line in frontmatter:
        if re.match(r"^(summary|digest_status):", line):
            continue
        if line.startswith("keywords:"):
            skipping_keywords = True
            continue
        if skipping_keywords and re.match(r"^\s+-\s+", line):
            continue
        skipping_keywords = False
        cleaned.append(line)

    insert_at = next(
        (index + 1 for index, line in enumerate(cleaned) if line.startswith("title:")),
        len(cleaned),
    )
    metadata = [
        "summary: "
        + json.dumps(_daily_metadata_summary(entries, input_state=input_state), ensure_ascii=False),
        "digest_status: "
        + json.dumps(_daily_status_label(entries, input_state=input_state), ensure_ascii=False),
        "keywords:",
    ]
    metadata.extend(
        f"  - {json.dumps(item, ensure_ascii=False)}" for item in _daily_keywords(entries)
    )
    cleaned[insert_at:insert_at] = metadata
    return "---\n" + "\n".join(cleaned) + note[boundary:]


def _legacy_digest_block(content: str, *, day: date) -> str:
    """Convert the narrow retired generated shape into one owned block."""

    required = (
        "---\n"
        "type: DailyDigest\n"
        f'title: "{day.isoformat()} Codex 하루 정리"\n'
        "publish: false\n"
        "access: local-only\n"
        "status: Active\n"
    )
    if not content.startswith(required) or f"date: {day.isoformat()}\n" not in content:
        raise WoonError("legacy daily digest is not a generated Woon record")
    marker_start = "<!-- woon-codex-digest:start -->"
    marker_end = "<!-- woon-codex-digest:end -->"
    start = content.find(marker_start)
    end = content.find(marker_end)
    if start >= 0 or end >= 0:
        if start < 0 or end < start or content.find(marker_start, start + 1) >= 0:
            raise WoonError("legacy daily digest ownership marker is invalid")
        return content[start : end + len(marker_end)].strip() + "\n"

    prefix = f'---\ntype: DailyDigest\ntitle: "{day.isoformat()} Codex 하루 정리"\n'
    frontmatter_end = content.find("\n---\n\n")
    heading = f"# {day.isoformat()} Codex 하루 정리\n\n"
    if frontmatter_end < 0 or not content.startswith(prefix):
        raise WoonError("legacy daily digest frontmatter is invalid")
    body_start = frontmatter_end + len("\n---\n\n")
    if not content[body_start:].startswith(heading):
        raise WoonError("legacy daily digest heading is invalid")
    body = content[body_start + len(heading) :].strip()
    if not body.startswith("## 대화에서 남긴 것\n"):
        raise WoonError("legacy daily digest body is not generated")
    return f"{marker_start}\n\n{body}\n\n{marker_end}\n"


def _daily_status(*, input_state: str, entry_count: int) -> tuple[str, str]:
    """Render a human-readable state without making an empty note look done."""

    if input_state == "partial":
        return (
            "현재까지 정리됨",
            "오늘은 아직 진행 중이다. 지금까지 확인된 정본 변경만 먼저 연결했다.",
        )
    if input_state == "source-only":
        return (
            "정본 반영 필요",
            "원문은 로컬 증거로 보존했지만 Wiki에 반영할 내용을 아직 확정하지 못했다.",
        )
    if entry_count:
        return "정본 반영 완료", "이날 갱신된 내용을 주제별 정본 문서에 연결했다."
    messages = {
        "processed": (
            "남길 항목 없음",
            "대화를 읽었지만 오늘의 이력·학습·일정·인물·자료로 남길 최소 항목은 없었습니다.",
        ),
        "no-meaningful": (
            "남길 항목 없음",
            "대화를 읽었지만 재사용하거나 하루 이력으로 남길 항목은 없었습니다.",
        ),
        "pending": (
            "다음 실행 대기",
            "오늘의 Codex 대화가 아직 안전하게 읽을 수 있는 저장 상태가 아니어서 "
            "다음 실행에서 다시 확인합니다.",
        ),
        "unavailable": (
            "세션 원본을 찾지 못해 대기",
            "이 날짜의 Codex 세션 원본을 현재 기기에서 찾지 못해 자동 대화 정리를 "
            "만들지 못했습니다.",
        ),
        "source-only": (
            "정본 반영 필요",
            "원문은 보존했지만 Wiki 정본 반영은 아직 완료되지 않았습니다.",
        ),
    }
    return messages[input_state]


def _person_names(records: list[dict[str, object]]) -> tuple[str, ...]:
    names: list[str] = []
    for record in records:
        allowed = {"display_name", "explicit_facts", "next_action"}
        name = record.get("display_name")
        if set(record).difference(allowed) or not isinstance(name, str):
            raise WoonError("Codex daily digest person mention is invalid")
        names.append(name.strip())
    return tuple(names)


def _daily_exchanges(records: object) -> tuple[CodexDailyExchange, ...]:
    if not isinstance(records, list):
        raise WoonError("Codex daily digest exchanges must be a list")
    exchanges: list[CodexDailyExchange] = []
    for raw in records:
        if not isinstance(raw, dict):
            raise WoonError("Codex daily digest exchange must be a mapping")
        detail_fields = {
            "facts",
            "criteria",
            "alternatives",
            "evidence",
            "changes",
            "unresolved",
        }
        allowed = {
            "question",
            "answer",
            "understanding",
            "outcome",
            "attachments",
            *detail_fields,
        }
        if set(raw).difference(allowed) or not {"question", "answer"}.issubset(raw):
            raise WoonError("Codex daily digest exchange has unsupported fields")
        question, answer = raw["question"], raw["answer"]
        understanding = raw.get("understanding")
        outcome = raw.get("outcome")
        attachments = raw.get("attachments", [])
        if not isinstance(question, str) or not isinstance(answer, str):
            raise WoonError("Codex daily digest exchange question and answer must be strings")
        if understanding is not None and not isinstance(understanding, str):
            raise WoonError("Codex daily digest exchange understanding must be a string or null")
        if outcome is not None and not isinstance(outcome, str):
            raise WoonError("Codex daily digest exchange outcome must be a string or null")
        if not isinstance(attachments, list) or not all(
            isinstance(item, str) for item in attachments
        ):
            raise WoonError("Codex daily digest exchange attachments must be a string list")
        details = {
            field: _daily_string_list(raw.get(field, []), f"exchange {field}")
            for field in detail_fields
        }
        exchanges.append(
            CodexDailyExchange(
                question=question,
                answer=answer,
                understanding=understanding,
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


def _daily_string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"Codex daily digest {field} must be a string list")
    return tuple(value)


def _render_entry(
    vault: Path,
    entry: CodexDailyDigestEntry,
    *,
    primary_document: str | None = None,
    supporting_documents: tuple[str, ...] = (),
) -> str:
    display_title = entry.title.replace("Woon Wiki", "Wiki")
    heading = (
        _related_document_link(vault, primary_document, display=display_title)
        if primary_document is not None
        else display_title
    )
    lines = [f"### {heading}"]
    lines.extend(["", entry.summary])
    if supporting_documents:
        links = tuple(_related_document_link(vault, path) for path in supporting_documents)
        lines.extend(["", "**변경 문서**"])
        for offset in range(0, len(links), 6):
            lines.append("- " + " · ".join(links[offset : offset + 6]))
    if entry.people:
        lines.extend(["", f"**관련 인물** {', '.join(entry.people)}"])
    return "\n".join(lines)


def _group_section_entries(
    vault: Path,
    entries: list[CodexDailyDigestEntry],
) -> tuple[tuple[CodexDailyDigestEntry, ...], ...]:
    """Keep one visible row per canonical subject or owning entity.

    A project, person, book, career item, or application may update many focused
    children in one day. The ledger preserves every conclusion, while the Daily
    shows one owning entity summary and a compact list of changed Wiki links.
    """

    grouped: dict[str, list[CodexDailyDigestEntry]] = {}
    for entry in entries:
        primary = _primary_related_document(vault, entry)
        key = _daily_anchor_document(vault, primary) if primary is not None else entry.title.strip()
        grouped.setdefault(key, []).append(entry)
    return tuple(tuple(items) for items in grouped.values())


def _combine_subject_entries(
    vault: Path, entries: tuple[CodexDailyDigestEntry, ...]
) -> CodexDailyDigestEntry:
    if len(entries) == 1:
        return entries[0]
    first_primary = _primary_related_document(vault, entries[0])
    anchor = _daily_anchor_document(vault, first_primary) if first_primary is not None else None
    anchor_entries = tuple(
        entry for entry in entries if _primary_related_document(vault, entry) == anchor
    )
    representative = anchor_entries[-1] if anchor_entries else entries[-1]
    title = _related_document_title(vault, anchor) if anchor is not None else representative.title
    summary = (
        representative.summary
        if anchor_entries or anchor is None
        else _related_document_summary(vault, anchor) or representative.summary
    )
    return CodexDailyDigestEntry(
        kind="·".join(dict.fromkeys(entry.kind for entry in entries)),
        title=title,
        summary=summary,
        intent=" ".join(
            dict.fromkeys(entry.intent for entry in entries if entry.intent is not None)
        )
        or None,
        exchanges=tuple(exchange for entry in entries for exchange in entry.exchanges),
        related_documents=tuple(
            dict.fromkeys(
                path
                for path in ((anchor,) if anchor is not None else ())
                + tuple(path for entry in entries for path in entry.related_documents)
                if path is not None
            )
        ),
        people=tuple(dict.fromkeys(person for entry in entries for person in entry.people)),
    )


def _daily_anchor_document(vault: Path, relative_path: str) -> str:
    """Return the nearest time-owning entity without inventing a second tree."""

    current = relative_path
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        metadata = _related_document_metadata(vault, current)
        if (
            metadata.get("node_kind") == "entity"
            and str(metadata.get("entity_kind", "")) in _DAILY_GROUP_ENTITY_KINDS
        ):
            return current
        parent = metadata.get("parent")
        if not isinstance(parent, str):
            break
        match = re.fullmatch(r"\[\[(?P<target>[^]|#]+)(?:#[^]|]+)?(?:\|[^]]+)?]]", parent)
        if match is None:
            break
        target = match.group("target")
        current = target if target.endswith(".md") else f"{target}.md"
        if not current.startswith("wiki/") or not (vault / current).is_file():
            break
    return relative_path


def _related_document_metadata(vault: Path, relative_path: str) -> dict[str, object]:
    text = _related_document(vault, relative_path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        return {}
    payload = yaml.safe_load(text[4:boundary]) or {}
    return payload if isinstance(payload, dict) else {}


def _related_document_summary(vault: Path, relative_path: str) -> str:
    summary = _related_document_metadata(vault, relative_path).get("summary")
    return str(summary).strip() if summary is not None else ""


def _primary_related_document(vault: Path, entry: CodexDailyDigestEntry) -> str | None:
    for path in entry.related_documents:
        document_title = _related_document_title(vault, path).strip().casefold()
        if document_title == entry.title.strip().casefold():
            return path
    return entry.related_documents[0] if entry.related_documents else None


def _entry_document_links(
    vault: Path,
    entry: CodexDailyDigestEntry,
    *,
    primary_documents: tuple[str, ...],
    already_linked: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...]]:
    primary_document = _primary_related_document(vault, entry)
    supporting_documents = tuple(
        path
        for path in entry.related_documents
        if path != primary_document and path not in primary_documents and path not in already_linked
    )
    return primary_document, supporting_documents


def _daily_metadata_summary(entries: tuple[CodexDailyDigestEntry, ...], *, input_state: str) -> str:
    if entries:
        titles = list(dict.fromkeys(entry.summary.rstrip(". ") for entry in entries))
        selected: list[str] = []
        for title in titles:
            candidate = " · ".join((*selected, title))
            if selected and len(candidate) > 180:
                break
            selected.append(title)
        return " · ".join(selected)
    return {
        "processed": "확인한 대화에서 남길 기록이 없었다.",
        "no-meaningful": "확인한 대화에서 남길 기록이 없었다.",
        "partial": "오늘 대화를 정리하는 중이다.",
        "pending": "다음 자동 정리를 기다리는 중이다.",
        "unavailable": "이 날짜의 Codex 세션 원본을 찾지 못했다.",
        "source-only": "원문은 보존했지만 Wiki 정본 반영은 아직 완료되지 않았다.",
    }[input_state]


def _daily_status_label(entries: tuple[CodexDailyDigestEntry, ...], *, input_state: str) -> str:
    if input_state == "partial":
        return "진행 중"
    if entries:
        return "정본 반영 완료"
    return {
        "processed": "남길 항목 없음",
        "no-meaningful": "남길 항목 없음",
        "pending": "다음 실행 대기",
        "unavailable": "원본 확인 필요",
        "source-only": "정본 반영 필요",
    }[input_state]


def _daily_keywords(entries: tuple[CodexDailyDigestEntry, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for entry in entries:
        for keyword in _entry_keywords(entry):
            if keyword not in values:
                values.append(keyword)
    return tuple(values[:10])


def _entry_keywords(entry: CodexDailyDigestEntry) -> tuple[str, ...]:
    haystack = " ".join((entry.title, entry.summary, entry.intent or ""))
    values = [entry.kind]
    values.extend(keyword for keyword in _KEYWORD_CANDIDATES if keyword in haystack)
    return tuple(dict.fromkeys(values))[:6]


def _ensure_daily_note(vault: Path, day: date) -> None:
    """Create only a missing daily shell from the canonical template."""

    destination = vault / "inbox" / "daily" / f"{day.isoformat()}.md"
    if destination.exists():
        if not destination.is_file() or destination.is_symlink():
            raise WoonError("daily note path is unsafe")
        return
    template = vault / "templates" / "daily-note.md"
    if not template.is_file() or template.is_symlink():
        raise WoonError("daily note template is missing")
    content = template.read_text(encoding="utf-8").replace("{{date}}", day.isoformat())
    if "{{date}}" in content or f"# {day.isoformat()}" not in content:
        raise WoonError("daily note template cannot materialize the requested date")
    atomic_write(destination, content.encode("utf-8"), mode=0o600)


def _related_document_title(vault: Path, relative_path: str) -> str:
    """Use the document title rather than a machine-oriented filename in Obsidian."""

    path = _related_document(vault, relative_path)
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip().strip("\"'")
    h1 = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return h1.group(1).strip() if h1 else path.stem


def _related_document_link(
    vault: Path,
    relative_path: str,
    *,
    display: str | None = None,
) -> str:
    """Render one human-readable link to the only canonical Wiki document."""

    title = display or _related_document_title(vault, relative_path)
    return f"[[../../{relative_path[:-3]}|{title}]]"
