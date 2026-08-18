"""Privacy-bounded daily projections of opted-in Codex conversation summaries.

This module never reads a Codex transcript.  A scheduled Codex task provides
only short Korean conclusions that were already selected from opted-in
user/assistant messages.  The digest is a local-only fragment embedded by the
daily note; raw chats, tool output, system text, and opaque locators never
enter the visible Markdown.
"""

from __future__ import annotations

import hashlib
import json
import re
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

_ENTRY_KINDS = {"결정", "완료", "학습", "질문", "다음 행동"}
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
    related_documents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexDailyDigestResult:
    """Only non-sensitive operation facts returned to the caller."""

    day: str
    entry_count: int
    receipt_id: str
    replayed: bool
    relative_path: str


def record_codex_daily_digest(
    vault: Path,
    *,
    day: date,
    entries: tuple[CodexDailyDigestEntry, ...],
    replace_empty_digest: bool = False,
) -> CodexDailyDigestResult:
    """Write exactly one digest fragment for a KST day through a receipt-first lane."""

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
    required_paths = {"inbox/daily", "inbox/daily-digests"}
    if not required_paths.issubset(contract.owned_paths):
        raise WoonError("daily record materialization must own the daily note and digest")
    _validate_entries(settings.vault, entries)
    payload = {
        "day": day.isoformat(),
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
    destination = settings.vault / "inbox" / "daily-digests" / f"{day.isoformat()}.md"
    content = _render_digest(settings.vault, day, entries)

    def produce() -> RunOutcome:
        _ensure_daily_note(settings.vault, day)
        existing = destination.read_text(encoding="utf-8") if destination.exists() else None
        if existing is not None and existing != content:
            if not replace_empty_digest or not _is_empty_digest(existing):
                raise WoonError("Codex daily digest already exists with different content")
            atomic_write(destination, content.encode("utf-8"), mode=0o600)
        if not destination.exists():
            atomic_write(destination, content.encode("utf-8"), mode=0o600)
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

    if len(records) > 8:
        raise WoonError("Codex daily digest may contain at most eight entries")
    entries: list[CodexDailyDigestEntry] = []
    for raw in records:
        allowed = {"kind", "title", "summary", "related_documents"}
        required = {"kind", "title", "summary"}
        if set(raw).difference(allowed) or not required.issubset(raw):
            raise WoonError("Codex daily digest entry has unsupported fields")
        related = raw.get("related_documents", [])
        if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
            raise WoonError("Codex daily digest related_documents must be a string list")
        kind, title, summary = raw["kind"], raw["title"], raw["summary"]
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(summary, str):
            raise WoonError("Codex daily digest entry text fields must be strings")
        entries.append(
            CodexDailyDigestEntry(
                kind=kind,
                title=title,
                summary=summary,
                related_documents=tuple(related),
            )
        )
    return tuple(entries)


def record_daily_digest_from_codex_ledger(
    vault: Path, *, day: date, replace_empty_digest: bool = False
) -> CodexDailyDigestResult:
    """Materialize the daily view from already-sanitized conversation entries.

    The daily lane does not reread a Codex transcript.  It consumes the local
    ledger written by the four-hourly conversation lane, so one conversation
    is classified exactly once.
    """

    from woon_core.knowledge.codex_knowledge import load_daily_entries

    records = list(load_daily_entries(vault, day=day))
    return record_codex_daily_digest(
        vault,
        day=day,
        entries=entries_from_records(records),
        replace_empty_digest=replace_empty_digest,
    )


def _validate_entries(vault: Path, entries: tuple[CodexDailyDigestEntry, ...]) -> None:
    if len(entries) > 8:
        raise WoonError("Codex daily digest may contain at most eight entries")
    for entry in entries:
        if entry.kind not in _ENTRY_KINDS:
            raise WoonError("Codex daily digest entry kind is invalid")
        _visible_text(entry.title, "title", _TITLE_LIMIT)
        _visible_text(entry.summary, "summary", _VISIBLE_LIMIT)
        if len(set(entry.related_documents)) != len(entry.related_documents):
            raise WoonError("Codex daily digest related documents must be unique")
        for relative_path in entry.related_documents:
            _related_document(vault, relative_path)


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


def _render_digest(vault: Path, day: date, entries: tuple[CodexDailyDigestEntry, ...]) -> str:
    lines = [
        "---",
        "type: DailyDigest",
        f'title: "{day.isoformat()} Codex 하루 정리"',
        "publish: false",
        "access: local-only",
        "status: Active",
        f"date: {day.isoformat()}",
        "record_owner: choi-woonyoung",
        "---",
        "",
        f"# {day.isoformat()} Codex 하루 정리",
        "",
        "## 대화에서 남긴 것",
        "",
    ]
    if not entries:
        lines.extend(["- 이 날에는 보관 조건을 충족한 Codex 대화 요약이 없습니다.", ""])
        return "\n".join(lines)
    for entry in entries:
        lines.append(f"- **{entry.kind} · {entry.title}** — {entry.summary}")
    related = tuple(dict.fromkeys(path for entry in entries for path in entry.related_documents))
    if related:
        lines.extend(["", "## 연결 문서", ""])
        for path in related:
            display = _related_document_title(vault, path)
            lines.append(f"- [[../../{path[:-3]}|{display}]]")
    lines.append("")
    return "\n".join(lines)


def _is_empty_digest(content: str) -> bool:
    return content.endswith("- 이 날에는 보관 조건을 충족한 Codex 대화 요약이 없습니다.\n")


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
