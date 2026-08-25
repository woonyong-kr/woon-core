"""Build local-only person histories from explicit private source ledgers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, exclusive_file_lock
from woon_core.people.service import PersonService

NOVEL_PERSON_LEDGER_RELATIVE_PATH = "work/people/person-link-ledger.yaml"
RETIRED_NOVEL_PERSON_DASHBOARD_RELATIVE_DIRECTORY = "work/people/dashboards"
NOVEL_WORK_CATALOG_RELATIVE_PATH = "work/work-catalog.yaml"
RETIRED_NOVEL_WORK_DASHBOARD_RELATIVE_DIRECTORY = "work/dashboards"
RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY = "inbox/private-person-history"
VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH = "inbox/review/private-person-history-review.md"
_NOVEL_DASHBOARD_MARKER = "woon_projection: novel-private-person-history\n"
_NOVEL_WORK_DASHBOARD_MARKER = "woon_projection: novel-private-work\n"
_VAULT_DASHBOARD_MARKER = "woon_projection: vault-private-person-history\n"
_VAULT_REVIEW_MARKER = "woon_projection: private-person-history-review\n"
_CARD_HISTORY_START = "<!-- woon-private-person-history:start -->"
_CARD_HISTORY_END = "<!-- woon-private-person-history:end -->"
_READONLY_FILE_MODE = 0o400
_WRITABLE_DIRECTORY_MODE = 0o700
_READONLY_DIRECTORY_MODE = 0o500
_WORK_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class PrivateWorkNavigation:
    """One local navigation target owned by a private creative work."""

    path: str
    role: str


@dataclass(frozen=True, slots=True)
class PrivateWork:
    """One first-class local-only creative work."""

    work_id: str
    work_number: int
    entity_type: str
    category: str
    title: str
    status: str
    canonical_entry: str
    navigation: tuple[PrivateWorkNavigation, ...]


@dataclass(frozen=True, slots=True)
class PrivateWorkCatalog:
    """Validated local-only work entities used by private projections."""

    works: tuple[PrivateWork, ...]


@dataclass(frozen=True, slots=True)
class PrivateHistoryLink:
    """One explicit relationship between a person card and a private source file."""

    work_id: str
    path: str
    role: str
    basis: str
    evidence: str


@dataclass(frozen=True, slots=True)
class PrivateHistoryCandidate:
    """A named source candidate that must not be promoted without a user decision."""

    work_id: str
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
    works: int
    people: int
    links: int
    candidates: int
    novel_ledger_path: str
    novel_work_catalog_path: str
    vault_dashboard_directory: str


class PrivatePersonHistoryService:
    """Validate private ledgers without creating a second human navigation tree."""

    def __init__(self, vault: Path, novel_root: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._novel_root = novel_root.expanduser().resolve()
        self._ledger_path = self._novel_root / NOVEL_PERSON_LEDGER_RELATIVE_PATH
        self._work_catalog_path = self._novel_root / NOVEL_WORK_CATALOG_RELATIVE_PATH
        self._novel_work_dashboard_directory = (
            self._novel_root / RETIRED_NOVEL_WORK_DASHBOARD_RELATIVE_DIRECTORY
        )
        self._novel_dashboard_directory = (
            self._novel_root / RETIRED_NOVEL_PERSON_DASHBOARD_RELATIVE_DIRECTORY
        )
        self._vault_dashboard_directory = (
            self._vault / RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY
        )
        self._vault_review_path = self._vault / VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH
        self._people = PersonService(self._vault)
        self._state_lock = self._vault / ".local/woon-knowledge/private-person-history.lock"

    def sync(self) -> PrivateHistorySyncResult:
        """Validate ledgers, retire duplicate dashboards, and keep Review only when needed."""

        catalog = self._load_work_catalog()
        ledger = self._load_ledger(catalog)
        for entry in ledger.entries:
            self._people.private_history_card(entry.person_id)
        with exclusive_file_lock(self._state_lock):
            changed = _retire_owned_directory(
                self._novel_work_dashboard_directory, (_NOVEL_WORK_DASHBOARD_MARKER,)
            )
            changed = (
                _retire_owned_directory(self._novel_dashboard_directory, (_NOVEL_DASHBOARD_MARKER,))
                or changed
            )
            changed = (
                _retire_owned_directory(self._vault_dashboard_directory, (_VAULT_DASHBOARD_MARKER,))
                or changed
            )
            changed = self._retire_card_history_links() or changed
            changed = self._refresh_vault_review(ledger) or changed
        return PrivateHistorySyncResult(
            changed=changed,
            works=len(catalog.works),
            people=len(ledger.entries),
            links=sum(len(entry.links) for entry in ledger.entries),
            candidates=len(ledger.candidates),
            novel_ledger_path=NOVEL_PERSON_LEDGER_RELATIVE_PATH,
            novel_work_catalog_path=NOVEL_WORK_CATALOG_RELATIVE_PATH,
            vault_dashboard_directory="wiki/private/novel/인물",
        )

    def _validate_local_root(self) -> None:
        if not self._novel_root.is_dir():
            raise WoonError("private history Novel root does not exist")
        if (self._novel_root / ".git").exists():
            raise WoonError("Novel private history root must not be a Git worktree")

    def _load_work_catalog(self) -> PrivateWorkCatalog:
        self._validate_local_root()
        if not self._work_catalog_path.is_file():
            raise WoonError("private Novel work catalog is missing")
        try:
            raw = yaml.safe_load(self._work_catalog_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise WoonError("private Novel work catalog YAML is invalid") from error
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise WoonError("private Novel work catalog must declare version: 1")
        if raw.get("access") != "local-only" or raw.get("publish") is not False:
            raise WoonError("private Novel work catalog must stay local-only and unpublished")
        works = _parse_works(raw.get("works"), self._novel_root)
        return PrivateWorkCatalog(works=works)

    def _load_ledger(self, catalog: PrivateWorkCatalog) -> PrivateHistoryLedger:
        self._validate_local_root()
        if not self._ledger_path.is_file():
            raise WoonError(
                "private person history ledger is missing; "
                "create it through the approved local workflow"
            )
        try:
            raw = yaml.safe_load(self._ledger_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise WoonError("private person history ledger YAML is invalid") from error
        if not isinstance(raw, dict) or raw.get("version") != 2:
            raise WoonError("private person history ledger must declare version: 2")
        if raw.get("access") != "local-only" or raw.get("source_kind") != "novel":
            raise WoonError("private person history ledger must stay local-only Novel metadata")
        if raw.get("work_catalog") != NOVEL_WORK_CATALOG_RELATIVE_PATH:
            raise WoonError(
                "private person history ledger must reference the canonical work catalog"
            )
        work_ids = {work.work_id for work in catalog.works}
        entries = _parse_entries(raw.get("people"), self._novel_root, work_ids)
        candidates = _parse_candidates(raw.get("review_candidates"), self._novel_root, work_ids)
        known_ids = {entry.person_id for entry in entries}
        if len(known_ids) != len(entries):
            raise WoonError("private person history ledger has duplicate person IDs")
        return PrivateHistoryLedger(entries=entries, candidates=candidates)

    def _retire_card_history_links(self) -> bool:
        changed = False
        for card in self._people.private_history_cards():
            path = self._vault / card.relative_path
            original = path.read_text(encoding="utf-8")
            updated = _remove_owned_block(original, _CARD_HISTORY_START, _CARD_HISTORY_END)
            if updated != original:
                atomic_write(path, updated.encode("utf-8"))
                changed = True
        return changed

    def _refresh_vault_review(self, ledger: PrivateHistoryLedger) -> bool:
        """Keep a visible local review note only while explicit candidates remain."""

        if (
            self._vault_review_path.exists()
            and _VAULT_REVIEW_MARKER not in self._vault_review_path.read_text(encoding="utf-8")
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
        return _write_owned_file(self._vault_review_path, _render_vault_review(ledger.candidates))


def _parse_works(value: object, novel_root: Path) -> tuple[PrivateWork, ...]:
    if not isinstance(value, list) or not value:
        raise WoonError("private Novel work catalog needs at least one work")
    works: list[PrivateWork] = []
    for item in value:
        if not isinstance(item, dict):
            raise WoonError("private Novel work entries must be mappings")
        work_id = _required_string(item.get("work_id"), "work_id")
        if not _WORK_ID_RE.fullmatch(work_id):
            raise WoonError("private Novel work_id must be a kebab-case identifier")
        work_number = item.get("work_number")
        if not isinstance(work_number, int) or isinstance(work_number, bool) or work_number < 1:
            raise WoonError("private Novel work_number must be a positive integer")
        entity_type = _required_string(item.get("entity_type"), "work entity_type")
        category = _required_string(item.get("category"), "work category")
        if entity_type != "creative-work" or category != "novel":
            raise WoonError("private Novel works must be creative-work entities in novel category")
        status = _required_string(item.get("status"), "work status")
        if status not in {"draft", "active", "complete", "archived"}:
            raise WoonError("private Novel work status is invalid")
        canonical_entry = _safe_novel_path(
            _required_string(item.get("canonical_entry"), "work canonical_entry"), novel_root
        )
        navigation_value = item.get("navigation")
        if not isinstance(navigation_value, list):
            raise WoonError("private Novel work navigation must be a list")
        navigation: list[PrivateWorkNavigation] = []
        for navigation_item in navigation_value:
            if not isinstance(navigation_item, dict):
                raise WoonError("private Novel work navigation entries must be mappings")
            navigation.append(
                PrivateWorkNavigation(
                    path=_safe_novel_path(
                        _required_string(navigation_item.get("path"), "work navigation path"),
                        novel_root,
                    ),
                    role=_required_string(navigation_item.get("role"), "work navigation role"),
                )
            )
        if len({entry.path for entry in navigation}) != len(navigation):
            raise WoonError(f"private Novel work has duplicate navigation paths: {work_id}")
        works.append(
            PrivateWork(
                work_id=work_id,
                work_number=work_number,
                entity_type=entity_type,
                category=category,
                title=_required_string(item.get("title"), "work title"),
                status=status,
                canonical_entry=canonical_entry,
                navigation=tuple(navigation),
            )
        )
    if len({work.work_id for work in works}) != len(works):
        raise WoonError("private Novel work catalog has duplicate work IDs")
    if len({work.work_number for work in works}) != len(works):
        raise WoonError("private Novel work catalog has duplicate work numbers")
    return tuple(works)


def _parse_entries(
    value: object, novel_root: Path, work_ids: set[str]
) -> tuple[PrivateHistoryEntry, ...]:
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
            work_id = _required_string(link_value.get("work_id"), "private history work_id")
            if work_id not in work_ids:
                raise WoonError(f"private history link references unknown work: {work_id}")
            relative_path = _safe_novel_path(
                _required_string(link_value.get("path"), "private history link path"), novel_root
            )
            links.append(
                PrivateHistoryLink(
                    work_id=work_id,
                    path=relative_path,
                    role=_required_string(link_value.get("role"), "private history link role"),
                    basis=_required_string(link_value.get("basis"), "private history link basis"),
                    evidence=_required_string(
                        link_value.get("evidence"), "private history link evidence"
                    ),
                )
            )
        if len({(link.work_id, link.path) for link in links}) != len(links):
            raise WoonError(f"private history has duplicate source paths for {person_id}")
        entries.append(PrivateHistoryEntry(person_id=person_id, links=tuple(links)))
    return tuple(entries)


def _parse_candidates(
    value: object, novel_root: Path, work_ids: set[str]
) -> tuple[PrivateHistoryCandidate, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise WoonError("private person history review_candidates must be a list")
    candidates: list[PrivateHistoryCandidate] = []
    for item in value:
        if not isinstance(item, dict):
            raise WoonError("private person history candidate must be a mapping")
        work_id = _required_string(item.get("work_id"), "private history candidate work_id")
        if work_id not in work_ids:
            raise WoonError(f"private history candidate references unknown work: {work_id}")
        candidates.append(
            PrivateHistoryCandidate(
                work_id=work_id,
                name=_required_string(item.get("name"), "private history candidate name"),
                path=_safe_novel_path(
                    _required_string(item.get("path"), "private history candidate path"), novel_root
                ),
                evidence=_required_string(
                    item.get("evidence"), "private history candidate evidence"
                ),
            )
        )
    if len(
        {(candidate.work_id, candidate.name, candidate.path) for candidate in candidates}
    ) != len(candidates):
        raise WoonError("private person history review candidates must be unique")
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


def _retire_owned_directory(directory: Path, markers: tuple[str, ...]) -> bool:
    """Remove only deterministic dashboard files that duplicate the Wiki tree."""

    if not directory.exists():
        return False
    directory.chmod(_WRITABLE_DIRECTORY_MODE)
    files = tuple(sorted(directory.glob("*.md")))
    unmanaged = [
        path.name
        for path in files
        if not any(marker in path.read_text(encoding="utf-8") for marker in markers)
    ]
    if unmanaged:
        directory.chmod(_READONLY_DIRECTORY_MODE)
        raise WoonError(
            "private person history retirement found unmanaged files: " + ", ".join(unmanaged)
        )
    for path in files:
        path.chmod(0o600)
        path.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()
    else:
        directory.chmod(_READONLY_DIRECTORY_MODE)
    return bool(files)


def _write_owned_file(path: Path, content: str) -> bool:
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if previous != content:
        atomic_write(path, content.encode("utf-8"), mode=_READONLY_FILE_MODE)
        return True
    if path.stat().st_mode & 0o777 != _READONLY_FILE_MODE:
        path.chmod(_READONLY_FILE_MODE)
        return True
    return False


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
                f"  - 작품: `{candidate.work_id}`",
                f"  - 확인 근거: {candidate.evidence}",
                "  - 원본 경로는 Novel local-only ledger에서만 확인한다.",
            )
        )
    return "\n".join(lines) + "\n"


def _remove_owned_block(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 and end < 0:
        return text
    if start < 0 or end < start:
        raise WoonError("private person history card markers are incomplete")
    return (text[:start].rstrip() + "\n" + text[end + len(end_marker) :].lstrip()).rstrip() + "\n"
