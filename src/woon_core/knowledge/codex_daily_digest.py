"""Privacy-bounded Codex-conversation blocks for one canonical daily note.

This module never reads a Codex transcript.  A scheduled Codex task provides
only short Korean conclusions that were already selected from opted-in
user/assistant messages.  The rendered block belongs directly to the daily
note, so one date has one human-facing historical record.  Raw chats, tool
output, system text, and opaque locators never enter the visible Markdown.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.io import atomic_write
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
}
_INPUT_STATES = {"processed", "no-meaningful", "pending", "unavailable"}
_SECTION_ORDER = (
    ("일정·할 일", {"일정", "할 일", "다음 행동", "완료"}),
    ("하루의 활동", {"활동", "생활", "건강", "여행·구매", "재정·행정", "회고"}),
    ("사람·관계", {"인물", "관계"}),
    ("성장·학습", {"학습", "개념", "결정", "질문"}),
    ("커리어·창작·자료", {"커리어", "창작", "자료"}),
)
_DIGEST_RENDER_REVISION = "5"
_VISIBLE_LIMIT = 280
_TITLE_LIMIT = 80
_SENSITIVE_RE = re.compile(
    r"(?:\b(?:api[_-]?key|token|secret|password|bearer)\b|sk-[A-Za-z0-9_-]{12,})",
    flags=re.IGNORECASE,
)
_RELATED_ROOTS = ("brain/wiki/", "wiki/", "maps/")


@dataclass(frozen=True, slots=True)
class CodexDailyDigestEntry:
    """One human-readable, source-minimized daily conclusion."""

    kind: str
    title: str
    summary: str
    intent: str | None = None
    related_documents: tuple[str, ...] = ()
    people: tuple[str, ...] = ()


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
    payload = {
        "render_revision": _DIGEST_RENDER_REVISION,
        "day": day.isoformat(),
        "input_state": input_state,
        "entries": [asdict(item) for item in entries],
    }
    serialized_text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    serialized = serialized_text.encode("utf-8")
    token = f"codex-daily-{day.strftime('%Y%m%d')}"
    request = RunRequest(
        source_range=token,
        input_sha256=hashlib.sha256(serialized).hexdigest(),
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=token,
    )
    destination = settings.vault / "inbox" / "daily" / f"{day.isoformat()}.md"
    content = _render_daily_block(settings.vault, entries, input_state=input_state)

    def produce() -> RunOutcome:
        _ensure_daily_note(settings.vault, day)
        existing = destination.read_text(encoding="utf-8")
        updated = _replace_daily_block(
            existing,
            block=content,
            day=day,
            allow_legacy_embed=replace_generated_digest,
        )
        if updated != existing:
            atomic_write(destination, updated.encode("utf-8"), mode=0o600)
        return RunOutcome(
            candidate_ids=tuple(_entry_id(day, entry) for entry in entries),
            output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    result = AutomationRunStore(settings).run("daily-record-materialization", request, produce)
    return CodexDailyDigestResult(
        day=day.isoformat(),
        entry_count=len(entries),
        receipt_id=result.receipt_id,
        replayed=result.replayed,
        relative_path=destination.relative_to(settings.vault).as_posix(),
    )


def entries_from_records(records: list[dict[str, object]]) -> tuple[CodexDailyDigestEntry, ...]:
    """Parse a narrow tool payload and reject raw conversation-shaped fields."""

    if len(records) > 24:
        raise WoonError("Codex daily digest may contain at most twenty-four entries")
    entries: list[CodexDailyDigestEntry] = []
    for raw in records:
        allowed = {
            "kind",
            "title",
            "summary",
            "intent",
            "related_documents",
            "calendar_contexts",
            "people",
        }
        required = {"kind", "title", "summary"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex daily digest entry has unsupported fields")
        related = raw.get("related_documents", [])
        people = raw.get("people", [])
        intent = raw.get("intent")
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

    from woon_core.knowledge.codex_knowledge import (
        growth_relative_path,
        load_daily_entries,
        load_daily_input_status,
    )

    records = list(load_daily_entries(vault, day=day))
    # Learning/decision records already have one canonical Growth Wiki page.
    # Derive its link locally only when that exact generated page exists.  A
    # same-day title, file timestamp, or semantic similarity is never enough
    # to attach an arbitrary note to the daily history.
    for record in records:
        kind = record.get("kind")
        title = record.get("title")
        if kind not in {"학습", "개념", "결정"} or not isinstance(title, str):
            continue
        relative_path = growth_relative_path(title)
        if not (vault.resolve() / relative_path).is_file():
            continue
        related = record.get("related_documents", [])
        if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
            raise WoonError("Codex knowledge ledger related_documents is unreadable")
        if relative_path not in related:
            record["related_documents"] = [*related, relative_path]
    input_state = load_daily_input_status(vault, day=day)
    if input_state is None:
        input_state = "processed" if records else "unavailable"
    return record_codex_daily_digest(
        vault,
        day=day,
        entries=entries_from_records(records),
        input_state=input_state,
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
    if len(entries) > 24:
        raise WoonError("Codex daily digest may contain at most twenty-four entries")
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
    stable = "\0".join((day.isoformat(), entry.kind, entry.title, entry.summary))
    return f"digest-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _render_daily_block(
    vault: Path,
    entries: tuple[CodexDailyDigestEntry, ...],
    *,
    input_state: str,
) -> str:
    lines = [
        "<!-- woon-codex-digest:start -->",
        "",
        "## 대화에서 남긴 것",
    ]
    if not entries:
        lines.extend(
            [
                f"- {_empty_message(input_state)}",
                "",
                "<!-- woon-codex-digest:end -->",
                "",
            ]
        )
        return "\n".join(lines)
    lines.append("")
    assigned: set[int] = set()
    for heading, kinds in _SECTION_ORDER:
        grouped = [entry for entry in entries if entry.kind in kinds]
        if not grouped:
            continue
        assigned.update(id(entry) for entry in grouped)
        lines.extend([f"## {heading}", ""])
        for entry in grouped:
            lines.append(_render_entry(entry))
        lines.append("")
    for entry in entries:
        if id(entry) not in assigned:
            lines.append(_render_entry(entry))
    related = tuple(dict.fromkeys(path for entry in entries for path in entry.related_documents))
    if related:
        lines.extend(["", "## 연결 문서", ""])
        for path in related:
            display = _related_document_title(vault, path)
            lines.append(f"- [[../../{path[:-3]}|{display}]]")
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

    prefix = (
        "---\n"
        "type: DailyDigest\n"
        f'title: "{day.isoformat()} Codex 하루 정리"\n'
    )
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


def _empty_message(input_state: str) -> str:
    messages = {
        "processed": "".join(
            (
                "대화를 확인했지만 오늘의 이력·학습·일정·인물·자료로 남길 최소 항목은 ",
                "없었습니다.",
            )
        ),
        "no-meaningful": "대화를 확인했지만 재사용하거나 하루 이력으로 남길 항목은 없었습니다.",
        "pending": (
            "오늘의 Codex 대화가 아직 안전하게 읽을 수 있는 저장 상태가 아니어서, "
            "대화 정리는 다음 실행에서 다시 확인합니다."
        ),
        "unavailable": (
            "이 날짜의 Codex 세션 원본을 현재 기기에서 찾지 못해 자동 대화 정리를 "
            "만들지 못했습니다."
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


def _render_entry(entry: CodexDailyDigestEntry) -> str:
    people = f" · 관련 인물: {', '.join(entry.people)}" if entry.people else ""
    intent = f" · 추정 의도: {entry.intent}" if entry.intent else ""
    return f"- **{entry.kind} · {entry.title}** — {entry.summary}{people}{intent}"


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
