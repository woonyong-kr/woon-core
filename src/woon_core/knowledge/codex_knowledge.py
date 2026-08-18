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
from datetime import date
from pathlib import Path
from typing import Literal, cast

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json
from woon_core.knowledge.orchestration import load_orchestrator_settings
from woon_core.knowledge.second_brain_runtime import (
    AutomationRunStore,
    RunOutcome,
    RunRequest,
    snapshot_owned_paths,
)

_KINDS = {"학습", "결정", "질문", "다음 행동"}
_GROWTH_KINDS = {"학습", "결정"}
_TITLE_LIMIT = 72
_SUMMARY_LIMIT = 360
_QUESTION_LIMIT = 240
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


@dataclass(frozen=True, slots=True)
class CodexKnowledgeEntry:
    """A minimized conclusion selected from an opted-in user/assistant exchange."""

    day: date
    kind: Literal["학습", "결정", "질문", "다음 행동"]
    title: str
    summary: str
    next_question: str | None = None
    related_documents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexKnowledgeResult:
    """Non-sensitive outcome for one idempotent conversation projection."""

    entry_count: int
    growth_page_count: int
    receipt_id: str
    replayed: bool


def entries_from_records(records: list[dict[str, object]]) -> tuple[CodexKnowledgeEntry, ...]:
    """Validate a narrow tool payload rather than accepting raw conversation data."""

    if not records or len(records) > 8:
        raise WoonError("Codex knowledge entries must contain one to eight records")
    entries: list[CodexKnowledgeEntry] = []
    for raw in records:
        allowed = {"day", "kind", "title", "summary", "next_question", "related_documents"}
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
        related = raw.get("related_documents", [])
        if not isinstance(raw_day, str):
            raise WoonError("Codex knowledge day must be a string")
        if not isinstance(kind, str) or not isinstance(title, str) or not isinstance(summary, str):
            raise WoonError("Codex knowledge entry text fields must be strings")
        if next_question is not None and not isinstance(next_question, str):
            raise WoonError("Codex knowledge next_question must be a string or null")
        if not isinstance(related, list) or not all(isinstance(value, str) for value in related):
            raise WoonError("Codex knowledge related_documents must be a string list")
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
        entries.append(
            CodexKnowledgeEntry(
                day=entry_day,
                kind=cast(Literal["학습", "결정", "질문", "다음 행동"], kind),
                title=title,
                summary=summary,
                next_question=next_question,
                related_documents=tuple(related),
            )
        )
    return tuple(entries)


def record_codex_knowledge_entries(
    vault: Path, *, source_range: str, entries: tuple[CodexKnowledgeEntry, ...]
) -> CodexKnowledgeResult:
    """Persist one sanitized conversation batch without retaining its transcript."""

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
    _validate_entries(settings.vault, entries)
    serialized = _encode_entries(entries)
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
        for entry in entries:
            _write_ledger_entry(settings.vault, entry)
        for entry in entries:
            if entry.kind in _GROWTH_KINDS:
                _write_growth_page(settings.vault, entry)
        return RunOutcome(
            candidate_ids=tuple(_entry_id(entry) for entry in entries),
            output_sha256=hashlib.sha256(_output_bytes(settings.vault, entries)).hexdigest(),
        )

    result = AutomationRunStore(settings).run("codex-conversation-ingest", request, produce)
    return CodexKnowledgeResult(
        entry_count=len(entries),
        growth_page_count=len(growth_paths),
        receipt_id=result.receipt_id,
        replayed=result.replayed,
    )


def load_daily_entries(vault: Path, *, day: date) -> tuple[dict[str, object], ...]:
    """Read only the minimized ledger view needed by the daily-record lane."""

    root = vault.expanduser().resolve() / ".local/woon-knowledge/codex-knowledge" / day.isoformat()
    if not root.is_dir():
        return ()
    entries: list[dict[str, object]] = []
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError("Codex knowledge ledger is unreadable") from error
        if not isinstance(value, dict):
            raise WoonError("Codex knowledge ledger entry must be a mapping")
        entries.append(value)
    return tuple(entries)


def _validate_entries(vault: Path, entries: tuple[CodexKnowledgeEntry, ...]) -> None:
    if not entries or len(entries) > 8:
        raise WoonError("Codex knowledge entries must contain one to eight records")
    seen: set[str] = set()
    for entry in entries:
        if entry.kind not in _KINDS:
            raise WoonError("Codex knowledge kind is invalid")
        _visible(entry.title, "title", _TITLE_LIMIT)
        _visible(entry.summary, "summary", _SUMMARY_LIMIT)
        if entry.next_question is not None:
            _visible(entry.next_question, "next_question", _QUESTION_LIMIT)
        if len(set(entry.related_documents)) != len(entry.related_documents):
            raise WoonError("Codex knowledge related documents must be unique")
        for relative_path in entry.related_documents:
            _related_document(vault, relative_path)
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


def _entry_id(entry: CodexKnowledgeEntry) -> str:
    stable = "\0".join(
        (
            entry.day.isoformat(),
            entry.kind,
            entry.title.strip(),
            entry.summary.strip(),
            entry.next_question or "",
            *entry.related_documents,
        )
    )
    return f"codex-knowledge-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:24]}"


def _encode_entries(entries: tuple[CodexKnowledgeEntry, ...]) -> bytes:
    return json.dumps(
        [
            {
                "day": entry.day.isoformat(),
                "kind": entry.kind,
                "title": entry.title,
                "summary": entry.summary,
                "next_question": entry.next_question,
                "related_documents": list(entry.related_documents),
            }
            for entry in entries
        ],
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
        "related_documents": list(entry.related_documents),
    }
    serialized = encode_json(value)
    if path.exists() and path.read_bytes() != serialized:
        raise WoonError("Codex knowledge ledger entry conflicts with an existing record")
    if not path.exists():
        atomic_write(path, serialized, mode=0o600)


def _growth_path(vault: Path, entry: CodexKnowledgeEntry) -> Path:
    stem = _FILE_STEM_RE.sub("-", entry.title.strip()).strip("-_").lower()
    if not stem:
        raise WoonError("Codex knowledge title cannot form a growth Wiki filename")
    return vault.resolve() / "brain/wiki" / f"{stem}.md"


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


def _output_bytes(vault: Path, entries: tuple[CodexKnowledgeEntry, ...]) -> bytes:
    paths = [_ledger_path(vault, entry) for entry in entries]
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
