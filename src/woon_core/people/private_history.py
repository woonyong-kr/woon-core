"""Build local-only person histories from explicit private source ledgers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, exclusive_file_lock
from woon_core.people.service import PersonCard, PersonDocument, PersonService

NOVEL_PERSON_LEDGER_RELATIVE_PATH = "work/people/person-link-ledger.yaml"
NOVEL_PERSON_DASHBOARD_RELATIVE_DIRECTORY = "work/people/dashboards"
VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY = "inbox/private-person-history"
VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH = "inbox/review/private-person-history-review.md"
_NOVEL_DASHBOARD_MARKER = "woon_projection: novel-private-person-history\n"
_VAULT_DASHBOARD_MARKER = "woon_projection: vault-private-person-history\n"
_VAULT_REVIEW_MARKER = "woon_projection: private-person-history-review\n"
_CARD_HISTORY_START = "<!-- woon-private-person-history:start -->"
_CARD_HISTORY_END = "<!-- woon-private-person-history:end -->"
_READONLY_FILE_MODE = 0o400
_WRITABLE_DIRECTORY_MODE = 0o700
_READONLY_DIRECTORY_MODE = 0o500


@dataclass(frozen=True, slots=True)
class PrivateHistoryLink:
    """One explicit relationship between a person card and a private source file."""

    path: str
    role: str
    basis: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PrivateHistoryCandidate:
    """A named source candidate that must not be promoted without a user decision."""

    name: str
    path: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PrivateHistoryEntry:
    """All confirmed private source links for one existing person card."""

    person_id: str
    links: tuple[PrivateHistoryLink, ...]


@dataclass(frozen=True, slots=True)
class PrivateHistoryLedger:
    """Validated contents of the private source-of-truth ledger."""

    entries: tuple[PrivateHistoryEntry, ...]
    candidates: tuple[PrivateHistoryCandidate, ...]


@dataclass(frozen=True, slots=True)
class PrivateHistorySyncResult:
    """Paths and counts produced by one deterministic private-history sync."""

    changed: bool
    people: int
    links: int
    candidates: int
    novel_ledger_path: str
    vault_dashboard_directory: str


class PrivatePersonHistoryService:
    """Generate read-only private person dashboards without copying Novel source text."""

    def __init__(self, vault: Path, novel_root: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._novel_root = novel_root.expanduser().resolve()
        self._ledger_path = self._novel_root / NOVEL_PERSON_LEDGER_RELATIVE_PATH
        self._novel_dashboard_directory = (
            self._novel_root / NOVEL_PERSON_DASHBOARD_RELATIVE_DIRECTORY
        )
        self._vault_dashboard_directory = (
            self._vault / VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY
        )
        self._vault_review_path = self._vault / VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH
        self._people = PersonService(self._vault)
        self._state_lock = (
            self._vault / ".local/woon-knowledge/private-person-history.lock"
        )

    def sync(self) -> PrivateHistorySyncResult:
        """Regenerate Novel and Vault views from a user-confirmed private ledger."""

        ledger = self._load_ledger()
        cards = {
            entry.person_id: self._people.private_history_card(entry.person_id)
            for entry in ledger.entries
        }
        with exclusive_file_lock(self._state_lock):
            changed = self._refresh_novel_dashboards(ledger, cards)
            changed = self._refresh_vault_dashboards(ledger, cards) or changed
            changed = self._refresh_card_history_links(ledger, cards) or changed
            changed = self._refresh_vault_review(ledger) or changed
        return PrivateHistorySyncResult(
            changed=changed,
            people=len(ledger.entries),
            links=sum(len(entry.links) for entry in ledger.entries),
            candidates=len(ledger.candidates),
            novel_ledger_path=NOVEL_PERSON_LEDGER_RELATIVE_PATH,
            vault_dashboard_directory=VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY,
        )

    def _load_ledger(self) -> PrivateHistoryLedger:
        if not self._novel_root.is_dir():
            raise WoonError("private history Novel root does not exist")
        if (self._novel_root / ".git").exists():
            raise WoonError("Novel private history root must not be a Git worktree")
        if not self._ledger_path.is_file():
            raise WoonError(
                "private person history ledger is missing; "
                "create it through the approved local workflow"
            )
        try:
            raw = yaml.safe_load(self._ledger_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise WoonError("private person history ledger YAML is invalid") from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise WoonError("private person history ledger must declare version: 1")
        if raw.get("access") != "local-only" or raw.get("source_kind") != "novel":
            raise WoonError("private person history ledger must stay local-only Novel metadata")
        entries = _parse_entries(raw.get("people"), self._novel_root)
        candidates = _parse_candidates(raw.get("review_candidates"), self._novel_root)
        known_ids = {entry.person_id for entry in entries}
        if len(known_ids) != len(entries):
            raise WoonError("private person history ledger has duplicate person IDs")
        return PrivateHistoryLedger(entries=entries, candidates=candidates)

    def _refresh_novel_dashboards(
        self, ledger: PrivateHistoryLedger, cards: dict[str, PersonCard]
    ) -> bool:
        return _refresh_directory(
            self._novel_dashboard_directory,
            {
                f"{entry.person_id}.md": _render_novel_dashboard(
                    entry, cards[entry.person_id], self._novel_root
                )
                for entry in ledger.entries
            }
            | {"README.md": _render_novel_dashboard_index(ledger, cards)},
            _NOVEL_DASHBOARD_MARKER,
        )

    def _refresh_vault_dashboards(
        self, ledger: PrivateHistoryLedger, cards: dict[str, PersonCard]
    ) -> bool:
        return _refresh_directory(
            self._vault_dashboard_directory,
            {
                f"{entry.person_id}.md": _render_vault_dashboard(
                    entry,
                    cards[entry.person_id],
                    self._people.private_history_documents(entry.person_id),
                )
                for entry in ledger.entries
            }
            | {"README.md": _render_vault_dashboard_index(ledger, cards)},
            _VAULT_DASHBOARD_MARKER,
        )

    def _refresh_card_history_links(
        self, ledger: PrivateHistoryLedger, cards: dict[str, PersonCard]
    ) -> bool:
        changed = False
        entries = {entry.person_id: entry for entry in ledger.entries}
        for card in self._people.private_history_cards():
            path = self._vault / card.relative_path
            original = path.read_text(encoding="utf-8")
            if card.person_id not in entries:
                updated = _remove_owned_block(
                    original, _CARD_HISTORY_START, _CARD_HISTORY_END
                )
                if updated != original:
                    atomic_write(path, updated.encode("utf-8"))
                    changed = True
                continue
            entry = entries[card.person_id]
            target = f"[[{VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY}/{entry.person_id}|비공개 이력]]"
            block = (
                f"{_CARD_HISTORY_START}\n\n"
                "## 비공개 이력\n\n"
                f"- {target}\n"
                "- Novel 원문은 이 Vault에 복사하지 않으며, 위 대시보드는 Core가 검증한 "
                "local-only 연결만 보여 준다.\n\n"
                f"{_CARD_HISTORY_END}"
            )
            updated = _replace_owned_block(original, _CARD_HISTORY_START, _CARD_HISTORY_END, block)
            if updated != original:
                atomic_write(path, updated.encode("utf-8"))
                changed = True
        return changed

    def _refresh_vault_review(self, ledger: PrivateHistoryLedger) -> bool:
        """Keep a visible local review note only while explicit candidates remain."""

        if (
            self._vault_review_path.exists()
            and _VAULT_REVIEW_MARKER
            not in self._vault_review_path.read_text(encoding="utf-8")
        ):
            raise WoonError("private person history review path is not a Core-generated projection")
        if not ledger.candidates:
            if self._vault_review_path.exists():
                self._vault_review_path.unlink()
                return True
            return False
        self._vault_review_path.parent.mkdir(
            mode=_WRITABLE_DIRECTORY_MODE, parents=True, exist_ok=True
        )
        return _write_owned_file(
            self._vault_review_path, _render_vault_review(ledger.candidates)
        )


def _parse_entries(value: object, novel_root: Path) -> tuple[PrivateHistoryEntry, ...]:
    if not isinstance(value, list) or not value:
        raise WoonError("private person history ledger needs at least one person entry")
    entries: list[PrivateHistoryEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise WoonError("private person history people entries must be mappings")
        person_id = _required_string(item.get("person_id"), "person_id")
        links_value = item.get("links")
        if not isinstance(links_value, list) or not links_value:
            raise WoonError(f"private history links are required for {person_id}")
        links: list[PrivateHistoryLink] = []
        for link_value in links_value:
            if not isinstance(link_value, dict):
                raise WoonError(f"private history link must be a mapping for {person_id}")
            relative_path = _safe_novel_path(
                _required_string(link_value.get("path"), "private history link path"), novel_root
            )
            links.append(
                PrivateHistoryLink(
                    path=relative_path,
                    role=_required_string(link_value.get("role"), "private history link role"),
                    basis=_required_string(link_value.get("basis"), "private history link basis"),
                    evidence=_required_string(
                        link_value.get("evidence"), "private history link evidence"
                    ),
                )
            )
        if len({link.path for link in links}) != len(links):
            raise WoonError(f"private history has duplicate source paths for {person_id}")
        entries.append(PrivateHistoryEntry(person_id=person_id, links=tuple(links)))
    return tuple(entries)


def _parse_candidates(value: object, novel_root: Path) -> tuple[PrivateHistoryCandidate, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise WoonError("private person history review_candidates must be a list")
    candidates: list[PrivateHistoryCandidate] = []
    for item in value:
        if not isinstance(item, dict):
            raise WoonError("private person history candidate must be a mapping")
        candidates.append(
            PrivateHistoryCandidate(
                name=_required_string(item.get("name"), "private history candidate name"),
                path=_safe_novel_path(
                    _required_string(item.get("path"), "private history candidate path"), novel_root
                ),
                evidence=_required_string(
                    item.get("evidence"), "private history candidate evidence"
                ),
            )
        )
    return tuple(candidates)


def _safe_novel_path(value: str, novel_root: Path) -> str:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise WoonError("private history source paths must be safe Novel-relative paths")
    resolved = (novel_root / candidate).resolve()
    if not resolved.is_file() or novel_root not in resolved.parents:
        raise WoonError(f"private history source does not exist: {value}")
    return candidate.as_posix()


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        raise WoonError(f"{field} must be one non-empty line")
    return value.strip()


def _refresh_directory(directory: Path, expected: dict[str, str], marker: str) -> bool:
    directory.mkdir(mode=_WRITABLE_DIRECTORY_MODE, parents=True, exist_ok=True)
    directory.chmod(_WRITABLE_DIRECTORY_MODE)
    try:
        existing = {path.name: path for path in directory.glob("*.md")}
        unmanaged = [
            path.name
            for path in existing.values()
            if marker not in path.read_text(encoding="utf-8")
        ]
        if unmanaged:
            raise WoonError(
                "private person history directory contains unmanaged files: "
                + ", ".join(unmanaged)
            )
        changed = False
        for name, content in expected.items():
            path = directory / name
            if _write_owned_file(path, content):
                changed = True
        stale = [path for name, path in existing.items() if name not in expected]
        if stale:
            for path in stale:
                path.unlink()
            changed = True
        return changed
    finally:
        directory.chmod(_READONLY_DIRECTORY_MODE)


def _write_owned_file(path: Path, content: str) -> bool:
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if previous != content:
        atomic_write(path, content.encode("utf-8"), mode=_READONLY_FILE_MODE)
        return True
    if path.stat().st_mode & 0o777 != _READONLY_FILE_MODE:
        path.chmod(_READONLY_FILE_MODE)
        return True
    return False


def _render_novel_dashboard(
    entry: PrivateHistoryEntry, card: PersonCard, novel_root: Path
) -> str:
    lines = _document_header(
        title=f"{card.title} · Novel 비공개 이력",
        marker=_NOVEL_DASHBOARD_MARKER,
        person_id=card.person_id,
        person_link=None,
    )
    lines.extend(
        (
            "",
            f"# {card.title} · Novel 비공개 이력",
            "",
            "원문을 복사하지 않고, 확정된 관계와 원본 위치만 다시 찾기 위한 대시보드다.",
            "",
            "## 연결 자료",
            "",
        )
    )
    for link in entry.links:
        relative = os.path.relpath(
            novel_root / link.path,
            novel_root / NOVEL_PERSON_DASHBOARD_RELATIVE_DIRECTORY,
        )
        lines.extend(
            (
                f"- [{Path(link.path).name}]({relative}) · `{link.role}`",
                f"  - 근거: {link.evidence} ({link.basis})",
            )
        )
    return "\n".join(lines) + "\n"


def _render_novel_dashboard_index(
    ledger: PrivateHistoryLedger, cards: dict[str, PersonCard]
) -> str:
    lines = [
        "---",
        "type: private-person-history-index",
        "title: Novel 비공개 인물 이력",
        "access: local-only",
        "publish: false",
        _NOVEL_DASHBOARD_MARKER.rstrip(),
        "---",
        "",
        "# Novel 비공개 인물 이력",
        "",
        "확정된 인물 연결만 보여 주며, 원본의 사실·해석·허구를 재판정하지 않는다.",
        "",
    ]
    for entry in ledger.entries:
        lines.append(f"- [{cards[entry.person_id].title}]({entry.person_id}.md)")
    lines.extend(("", "## 확인 대기", ""))
    if not ledger.candidates:
        lines.append("- 현재 확인 대기 인물이 없습니다.")
    for candidate in ledger.candidates:
        lines.append(f"- `{candidate.name}` · `{candidate.path}` · {candidate.evidence}")
    return "\n".join(lines) + "\n"


def _render_vault_dashboard(
    entry: PrivateHistoryEntry, card: PersonCard, documents: tuple[PersonDocument, ...]
) -> str:
    link = f"[[{card.relative_path.removesuffix('.md')}|{card.title}]]"
    lines = _document_header(
        title=f"{card.title} · 비공개 이력",
        marker=_VAULT_DASHBOARD_MARKER,
        person_id=card.person_id,
        person_link=link,
    )
    lines.extend(
        (
            "",
            f"# {card.title} · 비공개 이력",
            "",
            "일정·Vault 연결과 별도 Novel workspace의 확정 관계를 함께 보는 "
            "local-only 요약이다.",
            "",
            "## Vault에서 연결된 기록",
            "",
        )
    )
    if not documents:
        lines.append("- 아직 연결된 Vault 기록이 없습니다.")
    for document in documents:
        roles = ", ".join(_history_role_label(role) for role in document.roles)
        if not roles:
            roles = "기록 소유" if document.record_owner == card.person_id else "연결됨"
        lines.append(
            f"- [[{document.relative_path.removesuffix('.md')}|{document.title}]] · `{roles}`"
        )
    lines.extend(
        (
            "",
            "## Novel 확정 연결",
            "",
            f"- 확정 자료: {len(entry.links)}개",
            "- 원문 내용과 파일 경로는 Novel local-only dashboard에서만 확인한다.",
            "",
        )
    )
    return "\n".join(lines)


def _history_role_label(role: str) -> str:
    """Keep a private dashboard readable without changing canonical role values."""

    return {
        "organizer": "일정 주관",
        "mentioned": "일정에 언급",
        "participant": "참석자",
        "speaker": "발화자",
        "subject": "대상",
        "author": "저자",
        "source-provider": "자료 제공",
        "interviewee": "인터뷰이",
        "collaborator": "협업자",
        "reviewer": "검토자",
    }.get(role, role)


def _render_vault_dashboard_index(
    ledger: PrivateHistoryLedger, cards: dict[str, PersonCard]
) -> str:
    lines = [
        "---",
        "type: private-person-history-index",
        'title: "비공개 인물 이력"',
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Generated",
        "source: private-person-history-ledger",
        _VAULT_DASHBOARD_MARKER.rstrip(),
        "people: []",
        "person_roles: []",
        "---",
        "",
        "# 비공개 인물 이력",
        "",
        "Novel 원문을 복사하지 않고, local-only 연결 상태와 Vault 기록만 보여 준다.",
        "",
    ]
    for entry in ledger.entries:
        lines.append(
            f"- [[{VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY}/{entry.person_id}|"
            f"{cards[entry.person_id].title}]]"
        )
    return "\n".join(lines) + "\n"


def _render_vault_review(candidates: tuple[PrivateHistoryCandidate, ...]) -> str:
    lines = [
        "---",
        "type: private-person-history-review",
        'title: "비공개 인물 연결 검토"',
        "record_owner: choi-woonyoung",
        "publish: false",
        "access: local-only",
        "status: Review",
        _VAULT_REVIEW_MARKER.rstrip(),
        "people: []",
        "person_roles: []",
        "---",
        "",
        "# 비공개 인물 연결 검토",
        "",
        "아래 후보는 Novel 원문에서 자동으로 사람을 만들거나 연결하지 않은 항목이다. "
        "사용자가 같은 사람을 확인하면 ledger에 확정 연결을 기록하고, 이 목록에서 "
        "해당 후보를 제거한 뒤 다시 생성한다.",
        "",
        "## 검토 대기 후보",
        "",
    ]
    for candidate in candidates:
        lines.extend(
            (
                f"- `{candidate.name}`",
                f"  - 출처: `{candidate.path}`",
                f"  - 확인 근거: {candidate.evidence}",
            )
        )
    return "\n".join(lines) + "\n"


def _document_header(
    title: str, marker: str, person_id: str, person_link: str | None
) -> list[str]:
    lines = [
        "---",
        "type: private-person-history",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        "publish: false",
        "access: local-only",
        "status: Generated",
        marker.rstrip(),
        f"person_id: {person_id}",
    ]
    if person_link is not None:
        lines.extend(
            (
                "people:",
                f"  - {json.dumps(person_link, ensure_ascii=False)}",
                "person_roles:",
                f"  - person: {json.dumps(person_link, ensure_ascii=False)}",
                '    role: "subject"',
                '    basis: "explicit-private-history-ledger"',
                '    evidence: "사용자가 확인한 local-only 인물 연결"',
            )
        )
    lines.append("---")
    return lines


def _replace_owned_block(text: str, start_marker: str, end_marker: str, block: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 and end >= 0 or start >= 0 and end < 0:
        raise WoonError("private person history card markers are incomplete")
    if start >= 0 and end >= start:
        end += len(end_marker)
        return text[:start] + block + text[end:]
    return text.rstrip() + "\n\n" + block + "\n"


def _remove_owned_block(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 and end < 0:
        return text
    if start < 0 or end < start:
        raise WoonError("private person history card markers are incomplete")
    return (text[:start].rstrip() + "\n" + text[end + len(end_marker) :].lstrip()).rstrip() + "\n"
