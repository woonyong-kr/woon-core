"""Safe, local-only person cards and role-aware document links."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

_PERSON_ID = re.compile(r"^[a-z][a-z0-9-]{2,79}$")
_PERSON_KINDS = frozenset(
    {"vault-owner", "related-person", "public-author", "organization-representative"}
)
_CREATION_BASES = frozenset({"explicit-request", "repeated-evidence"})
_ROLES = frozenset(
    {
        "author",
        "source-provider",
        "speaker",
        "participant",
        "organizer",
        "interviewee",
        "collaborator",
        "reviewer",
        "subject",
        "mentioned",
    }
)
_FORBIDDEN_LINK_ROOTS = ("wiki/", "catalog/", "sources/private/", "projects/writing/")
_PRIVATE_HISTORY_ROOTS = (
    "inbox/calendar/events/",
    "inbox/daily/",
)
_PRIVATE_HISTORY_EXCLUDED_PATHS = frozenset(
    {
        "users/README.md",
        "maps/people-index.md",
        "maps/local-private-index.md",
        "inbox/daily/README.md",
    }
)
_DEFAULT_OWNER_ID = "choi-woonyoung"
_LEGACY_DEFAULT_OWNER_LINK = "[[users/choi-woonyoung/README|최우녕]]"
_KOREAN_NAME_PARTICLES = (
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "의",
    "에게",
    "께",
    "도",
    "만",
    "랑",
    "하고",
)


@dataclass(frozen=True, slots=True)
class PersonIdentityIdentifier:
    """A user-confirmed wording that may resolve one person in local Calendar titles."""

    value: str
    evidence: str
    context_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonIdentityIdentifierInput:
    """One requested identifier update after the user has resolved an identity."""

    value: str
    context_terms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PersonCard:
    """Minimal reusable identity card, not a biography."""

    person_id: str
    title: str
    person_kind: str
    relationship_to_owner: str
    person_scope: str
    identifiers: tuple[PersonIdentityIdentifier, ...]
    relative_path: str
    revision: str


@dataclass(frozen=True, slots=True)
class PersonDocument:
    """One document that intentionally refers to a person card."""

    relative_path: str
    title: str
    roles: tuple[str, ...]
    record_owner: str | None


@dataclass(frozen=True, slots=True)
class PersonWriteResult:
    created: bool
    changed: bool
    card: PersonCard


@dataclass(frozen=True, slots=True)
class PersonLinkResult:
    changed: bool
    document: PersonDocument


@dataclass(frozen=True, slots=True)
class CalendarPersonReference:
    """A known person card referenced only from a local Calendar projection."""

    person_id: str
    title: str
    link: str


@dataclass(frozen=True, slots=True)
class CalendarPersonMatch:
    """One unambiguous identifier match in an event title."""

    reference: CalendarPersonReference
    identifier: PersonIdentityIdentifier


@dataclass(frozen=True, slots=True)
class CalendarIdentityAmbiguity:
    """A title identifier shared by multiple possible person cards."""

    identifier: str
    candidates: tuple[CalendarPersonReference, ...]


@dataclass(frozen=True, slots=True)
class CalendarIdentityResolution:
    """Resolved Calendar people and identities that require a user decision."""

    matches: tuple[CalendarPersonMatch, ...]
    ambiguities: tuple[CalendarIdentityAmbiguity, ...]


@dataclass(frozen=True, slots=True)
class PersonIdentifierWriteResult:
    """Result of replacing the explicit Calendar identifiers on one known card."""

    changed: bool
    card: PersonCard


@dataclass(frozen=True, slots=True)
class OwnerMaterializationResult:
    """One controlled migration of the Vault owner's default onto editable records."""

    changed: int
    skipped: int


class PersonService:
    """Own person-card creation and explicit links outside compiled Wiki output."""

    def __init__(self, vault: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._users_root = self._vault / "users"
        self._state_path = self._vault / ".local/woon-knowledge/people-state.json"

    def find(self, query: str) -> tuple[PersonCard, ...]:
        """Find general-scope person cards by stable ID, title, or relationship."""

        needle = query.strip().casefold()
        if not needle:
            raise WoonError("person search query must not be empty")
        return tuple(
            card
            for card in self.list_cards()
            if needle in card.person_id.casefold()
            or needle in card.title.casefold()
            or needle in card.relationship_to_owner.casefold()
        )

    def list_cards(self) -> tuple[PersonCard, ...]:
        return tuple(card for card in self._all_cards() if card.person_scope == "general")

    def _all_cards(self) -> tuple[PersonCard, ...]:
        if not self._users_root.exists():
            return ()
        return tuple(
            _read_card(path, self._vault) for path in sorted(self._users_root.glob("*/README.md"))
        )

    def default_owner_reference(self) -> CalendarPersonReference | None:
        """Return the known Vault owner without creating a card or guessing identity."""

        for card in self.list_cards():
            if card.person_id == _DEFAULT_OWNER_ID:
                return _calendar_reference(card)
        return None

    def calendar_title_resolution(self, title: str) -> CalendarIdentityResolution:
        """Resolve user-approved title identifiers without guessing a person identity.

        General cards may use their card title until they declare identifiers.  A
        local-only card participates only after it declares explicit identifiers,
        so a private card cannot become discoverable through a random event title.
        """

        normalized = title.strip()
        if not normalized:
            return CalendarIdentityResolution((), ())

        candidates: list[tuple[int, int, PersonCard, PersonIdentityIdentifier]] = []
        for card in self._all_cards():
            for identifier in _calendar_identifiers(card):
                for start in _title_identifier_starts(normalized, identifier.value):
                    candidates.append((start, start + len(identifier.value), card, identifier))

        # A full identifier (for example, "이민정") wins over an overlapping
        # shorter nickname ("민정") before duplicate detection happens.
        retained: list[tuple[int, int, PersonCard, PersonIdentityIdentifier]] = []
        for candidate in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
            start, end, _, _ = candidate
            if any(
                start >= kept_start and end <= kept_end and (start, end) != (kept_start, kept_end)
                for kept_start, kept_end, *_ in retained
            ):
                continue
            retained.append(candidate)

        by_occurrence: dict[tuple[int, str], list[tuple[PersonCard, PersonIdentityIdentifier]]] = {}
        for start, _, card, identifier in retained:
            by_occurrence.setdefault((start, identifier.value), []).append((card, identifier))

        matches: dict[str, CalendarPersonMatch] = {}
        ambiguities: list[CalendarIdentityAmbiguity] = []
        for (_, identifier_value), matching_cards in sorted(by_occurrence.items()):
            unique_cards: dict[str, tuple[PersonCard, PersonIdentityIdentifier]] = {}
            for card, identifier in matching_cards:
                previous = unique_cards.get(card.person_id)
                if previous is None or len(identifier.context_terms) > len(
                    previous[1].context_terms
                ):
                    unique_cards[card.person_id] = (card, identifier)

            if len(unique_cards) == 1:
                card, identifier = next(iter(unique_cards.values()))
                matches[card.person_id] = CalendarPersonMatch(_calendar_reference(card), identifier)
                continue
            contextual = [
                entry
                for entry in unique_cards.values()
                if entry[1].context_terms
                and all(term in normalized for term in entry[1].context_terms)
            ]
            if len(contextual) == 1:
                card, identifier = contextual[0]
                matches[card.person_id] = CalendarPersonMatch(_calendar_reference(card), identifier)
                continue
            ambiguity_candidates = tuple(
                _calendar_reference(card)
                for card, _ in sorted(unique_cards.values(), key=lambda item: item[0].person_id)
            )
            ambiguities.append(CalendarIdentityAmbiguity(identifier_value, ambiguity_candidates))
        return CalendarIdentityResolution(
            matches=tuple(sorted(matches.values(), key=lambda item: item.reference.person_id)),
            ambiguities=tuple(ambiguities),
        )

    def set_identity_identifiers(
        self,
        *,
        person_id: str,
        identifiers: tuple[PersonIdentityIdentifierInput, ...],
        evidence: str,
        expected_revision: str | None = None,
    ) -> PersonIdentifierWriteResult:
        """Store only direct user-confirmed Calendar identifiers on an existing card."""

        _validate_identifier_inputs(identifiers, evidence)
        path = self._person_card_path(person_id)
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            original = path.read_text(encoding="utf-8")
            card = _read_card(path, self._vault)
            if expected_revision is not None and card.revision != expected_revision:
                raise WoonError(
                    "person card revision changed; read it again before updating identifiers"
                )
            metadata, body = _frontmatter(original, path)
            metadata["identifiers"] = [
                {
                    "value": identifier.value,
                    "basis": "user-confirmed",
                    "evidence": evidence.strip(),
                    "context_terms": list(identifier.context_terms),
                }
                for identifier in identifiers
            ]
            updated = _render_document(metadata, body)
            changed = updated != original
            if changed:
                atomic_write(path, updated.encode("utf-8"))
            card = _read_card(path, self._vault)
            _record_operation(
                self._state_path,
                operation=f"set-identifiers:{person_id}",
                payload={
                    "revision": card.revision,
                    "identifiers": [identifier.value for identifier in card.identifiers],
                },
            )
        return PersonIdentifierWriteResult(changed=changed, card=card)

    def documents_for(self, person_id: str) -> tuple[PersonDocument, ...]:
        """Return deliberate person links plus the Vault owner's implicit record ownership."""

        card = self._card(person_id)
        return self._documents_for(card, include_implicit_default_owner=True)

    def private_history_card(self, person_id: str) -> PersonCard:
        """Read one exact local card for the private-history adapter only."""

        return _read_card(self._person_card_path(person_id), self._vault)

    def private_history_cards(self) -> tuple[PersonCard, ...]:
        """List cards only for removing Core-owned private-history view blocks."""

        return self._all_cards()

    def private_history_documents(self, person_id: str) -> tuple[PersonDocument, ...]:
        """Read explicit record-owner and people links for a private history view only."""

        return self._documents_for(
            self.private_history_card(person_id),
            include_implicit_default_owner=False,
            private_history_only=True,
        )

    def _documents_for(
        self,
        card: PersonCard,
        *,
        include_implicit_default_owner: bool,
        private_history_only: bool = False,
    ) -> tuple[PersonDocument, ...]:
        """Return deliberate links after the caller has applied the correct scope boundary."""

        link = _person_link(card)
        documents: list[PersonDocument] = []
        for path in _markdown_files(self._vault):
            relative = _relative(path, self._vault)
            if relative == card.relative_path or relative.startswith(_FORBIDDEN_LINK_ROOTS):
                continue
            if relative.startswith("inbox/daily-digests/"):
                # A retired duplicate projection must never become person history.
                continue
            text = path.read_text(encoding="utf-8")
            try:
                metadata, _ = _frontmatter(text, path)
            except WoonError:
                # Legacy imports without parseable metadata cannot claim a deliberate person link.
                continue
            people = _link_list(metadata.get("people"), path, "people")
            explicit_owner = _record_owner(metadata.get("record_owner"), path)
            is_implicit_default_owner = (
                card.person_id == _DEFAULT_OWNER_ID and explicit_owner is None
            )
            is_record_owner = explicit_owner in {card.person_id, link} or (
                include_implicit_default_owner and is_implicit_default_owner
            )
            if link not in people and not is_record_owner:
                continue
            if private_history_only and not _is_private_history_document(
                relative=relative,
                card=card,
                people=people,
            ):
                continue
            try:
                title = _required_string(metadata.get("title"), "title", path)
            except WoonError:
                # Auxiliary maps may be valid Markdown without person-dashboard metadata.
                continue
            roles = (
                tuple(
                    role
                    for role in _roles_for(metadata.get("person_roles"), link, path)
                    if role in _ROLES
                )
                if link in people
                else ()
            )
            documents.append(
                PersonDocument(
                    relative_path=relative,
                    title=title,
                    roles=roles,
                    record_owner=explicit_owner or _DEFAULT_OWNER_ID,
                )
            )
        return tuple(
            sorted(documents, key=lambda item: (item.title.casefold(), item.relative_path))
        )

    def upsert_card(
        self,
        *,
        person_id: str,
        title: str,
        person_kind: str,
        relationship_to_owner: str,
        purpose: str,
        creation_basis: str,
        expected_revision: str | None = None,
    ) -> PersonWriteResult:
        """Create a small general-scope card only from explicit or repeated evidence."""

        _validate_card_input(
            person_id,
            title,
            person_kind,
            relationship_to_owner,
            purpose,
            creation_basis,
        )
        path = self._users_root / person_id / "README.md"
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            current = _read_card(path, self._vault) if path.exists() else None
            if expected_revision is not None and (
                current is None or current.revision != expected_revision
            ):
                raise WoonError("person card revision changed; read it again before updating")
            content = _render_card(
                person_id=person_id,
                title=title,
                person_kind=person_kind,
                relationship_to_owner=relationship_to_owner,
                purpose=purpose,
                creation_basis=creation_basis,
            )
            changed = not path.exists() or path.read_text(encoding="utf-8") != content
            if changed:
                atomic_write(path, content.encode("utf-8"))
            card = _read_card(path, self._vault)
            _record_operation(
                self._state_path,
                operation=f"upsert:{person_id}",
                payload={"revision": card.revision, "relative_path": card.relative_path},
            )
        return PersonWriteResult(created=current is None, changed=changed, card=card)

    def link_document(
        self,
        *,
        relative_path: str,
        person_id: str,
        roles: tuple[str, ...],
        evidence: str,
        expected_revision: str | None = None,
    ) -> PersonLinkResult:
        """Attach an existing general person card and explicit role to one editable document."""

        card = self._card(person_id)
        _validate_roles(roles)
        if not evidence.strip() or "\n" in evidence or len(evidence) > 280:
            raise WoonError("person link evidence must be one non-empty line up to 280 characters")
        path = _document_path(self._vault, relative_path)
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            original = path.read_text(encoding="utf-8")
            revision = _revision(original)
            if expected_revision is not None and expected_revision != revision:
                raise WoonError("document revision changed; read it again before linking a person")
            metadata, body = _frontmatter(original, path)
            link = _person_link(card)
            people = _link_list(metadata.get("people"), path, "people")
            if link not in people:
                people.append(link)
            metadata["people"] = people
            role_entries = _role_entries(metadata.get("person_roles"), path)
            known = {
                (entry.get("person"), entry.get("role"), entry.get("evidence"))
                for entry in role_entries
            }
            for role in roles:
                entry = {"person": link, "role": role, "basis": "explicit", "evidence": evidence}
                marker = (link, role, evidence)
                if marker not in known:
                    role_entries.append(entry)
                    known.add(marker)
            metadata["person_roles"] = role_entries
            updated = _render_document(metadata, body)
            changed = updated != original
            if changed:
                atomic_write(path, updated.encode("utf-8"))
            document = PersonDocument(
                relative_path=_relative(path, self._vault),
                title=_required_string(metadata.get("title"), "title", path),
                roles=tuple(roles),
                record_owner=(
                    _record_owner(metadata.get("record_owner"), path) or _DEFAULT_OWNER_ID
                ),
            )
            _record_operation(
                self._state_path,
                operation=f"link:{document.relative_path}:{person_id}",
                payload={"document_revision": _revision(updated), "roles": list(roles)},
            )
        return PersonLinkResult(changed=changed, document=document)

    def materialize_default_owner(self) -> OwnerMaterializationResult:
        """Write the default owner only to safe, editable records that omit it."""

        owner = self._card(_DEFAULT_OWNER_ID)
        default_owner = owner.person_id
        changed = 0
        skipped = 0
        with exclusive_file_lock(self._state_path.with_suffix(".lock")):
            for path in _markdown_files(self._vault):
                relative = _relative(path, self._vault)
                if relative.startswith(_FORBIDDEN_LINK_ROOTS):
                    skipped += 1
                    continue
                original = path.read_text(encoding="utf-8")
                try:
                    metadata, _ = _frontmatter(original, path)
                except WoonError:
                    skipped += 1
                    continue
                existing_owner = _record_owner(metadata.get("record_owner"), path)
                if metadata.get("entity_type") == "person" or _belongs_to_novel_local_person(
                    path, self._vault
                ):
                    skipped += 1
                    continue
                if existing_owner is not None and existing_owner != _LEGACY_DEFAULT_OWNER_LINK:
                    skipped += 1
                    continue
                updated = (
                    _replace_record_owner(original, default_owner)
                    if existing_owner == _LEGACY_DEFAULT_OWNER_LINK
                    else _insert_record_owner(original, default_owner)
                )
                atomic_write(path, updated.encode("utf-8"))
                changed += 1
            _record_operation(
                self._state_path,
                operation="materialize-default-owner",
                payload={"changed": changed, "skipped": skipped},
            )
        return OwnerMaterializationResult(changed=changed, skipped=skipped)

    def _card(self, person_id: str) -> PersonCard:
        card = _read_card(self._person_card_path(person_id), self._vault)
        if card.person_scope != "general":
            raise WoonError(
                "private or Novel person cards cannot be linked through the general person index"
            )
        return card

    def _person_card_path(self, person_id: str) -> Path:
        _validate_person_id(person_id)
        path = self._users_root / person_id / "README.md"
        if not path.is_file():
            raise WoonError(
                "person card does not exist; create it only with explicit or repeated evidence"
            )
        return path


def _read_card(path: Path, vault: Path) -> PersonCard:
    text = path.read_text(encoding="utf-8")
    metadata, _ = _frontmatter(text, path)
    if metadata.get("entity_type") != "person":
        raise WoonError(f"person card entity_type must be person: {path}")
    person_id = _required_string(metadata.get("person_id"), "person_id", path)
    _validate_person_id(person_id)
    person_kind = _required_string(metadata.get("person_kind"), "person_kind", path)
    if person_kind not in _PERSON_KINDS:
        raise WoonError(f"person card has unsupported person_kind: {path}")
    person_scope = _required_string(metadata.get("person_scope"), "person_scope", path)
    if person_scope not in {"general", "novel-local-only"}:
        raise WoonError(f"person card has unsupported person_scope: {path}")
    title = _required_string(metadata.get("title"), "title", path)
    return PersonCard(
        person_id=person_id,
        title=title,
        person_kind=person_kind,
        relationship_to_owner=_required_string(
            metadata.get("relationship_to_owner"), "relationship_to_owner", path
        ),
        person_scope=person_scope,
        identifiers=_read_identifiers(metadata.get("identifiers"), path),
        relative_path=_relative(path, vault),
        revision=_revision(text),
    )


def _frontmatter(text: str, path: Path) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise WoonError(f"document needs YAML frontmatter: {path}")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise WoonError(f"document frontmatter is incomplete: {path}")
    try:
        metadata = yaml.safe_load(text[4:end])
    except yaml.YAMLError as error:
        raise WoonError(f"document frontmatter is invalid: {path}") from error
    if not isinstance(metadata, dict):
        raise WoonError(f"document frontmatter must be a mapping: {path}")
    return metadata, text[end + len("\n---\n") :]


def _render_document(metadata: dict[str, object], body: str) -> str:
    frontmatter = yaml.safe_dump(
        metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{frontmatter}---\n{body}"


def _insert_record_owner(text: str, owner_id: str) -> str:
    """Add one frontmatter field without rewriting the rest of a user document."""

    end = text.find("\n---\n", 4)
    if end < 0:
        raise WoonError("document frontmatter is incomplete")
    frontmatter = text[4:end]
    lines = frontmatter.splitlines(keepends=True)
    insertion_index = next(
        (index + 1 for index, line in enumerate(lines) if line.startswith("title:")),
        len(lines),
    )
    lines.insert(insertion_index, f"record_owner: {owner_id}\n")
    return f"---\n{''.join(lines)}{text[end:]}"


def _replace_record_owner(text: str, owner_id: str) -> str:
    """Normalize the old default link to an ID without reformatting frontmatter."""

    end = text.find("\n---\n", 4)
    if end < 0:
        raise WoonError("document frontmatter is incomplete")
    frontmatter = text[4:end]
    lines = frontmatter.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("record_owner:"):
            lines[index] = f"record_owner: {owner_id}\n"
            return f"---\n{''.join(lines)}{text[end:]}"
    raise WoonError("document record_owner field is missing")


def _belongs_to_novel_local_person(path: Path, vault: Path) -> bool:
    """Keep a local-only person card and its workspace out of general migrations."""

    relative = _relative(path, vault)
    parts = Path(relative).parts
    if len(parts) < 3 or parts[0] != "users":
        return False
    card_path = vault / "users" / parts[1] / "README.md"
    if not card_path.is_file():
        return False
    try:
        metadata, _ = _frontmatter(card_path.read_text(encoding="utf-8"), card_path)
    except WoonError:
        return True
    return metadata.get("person_scope") == "novel-local-only"


def _document_path(vault: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix != ".md":
        raise WoonError("person link document must use a safe vault-relative Markdown path")
    normalized = candidate.as_posix()
    if normalized.startswith(_FORBIDDEN_LINK_ROOTS):
        raise WoonError("compiled Wiki, private originals, and Novel paths require their own owner")
    path = (vault / candidate).resolve()
    try:
        path.relative_to(vault)
    except ValueError as error:
        raise WoonError("person link document escapes the vault") from error
    if not path.is_file():
        raise WoonError("person link document does not exist")
    return path


def _markdown_files(vault: Path) -> tuple[Path, ...]:
    content_roots = ("brain", "inbox", "sources", "maps", "users")
    ignored = {".git", ".github", ".obsidian", ".local", "exports", "quartz", "templates", "types"}
    files: list[Path] = []
    for root in content_roots:
        directory = vault / root
        if not directory.exists():
            continue
        files.extend(
            path
            for path in directory.rglob("*.md")
            if path.name not in {"AGENTS.md", "CLAUDE.md"}
            and not ignored.intersection(path.relative_to(vault).parts)
        )
    return tuple(sorted(files))


def _required_string(value: object, field: str, path: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{field} is required: {path}")
    return value.strip()


def _record_owner(value: object, path: Path) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise WoonError(f"record_owner must be a person ID or person-card link: {path}")
    owner = value.strip()
    if _PERSON_ID.fullmatch(owner) or owner.startswith("[[users/"):
        return owner
    raise WoonError(f"record_owner must be a person ID or person-card link: {path}")


def _link_list(value: object, path: Path, field: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"{field} must be a list of person-card links: {path}")
    links = [item.strip() for item in value]
    if any(not item.startswith("[[users/") for item in links):
        raise WoonError(f"{field} must only contain person-card links: {path}")
    return links


def _role_entries(value: object, path: Path) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise WoonError(f"person_roles must be a list of mappings: {path}")
    entries: list[dict[str, str]] = []
    for item in value:
        person = item.get("person")
        role = item.get("role")
        evidence = item.get("evidence")
        basis = item.get("basis")
        if (
            not isinstance(person, str)
            or not isinstance(role, str)
            or not isinstance(evidence, str)
            or not isinstance(basis, str)
        ):
            raise WoonError(f"person_roles has an incomplete entry: {path}")
        entries.append({"person": person, "role": role, "basis": basis, "evidence": evidence})
    return entries


def _roles_for(value: object, link: str, path: Path) -> tuple[str, ...]:
    return tuple(entry["role"] for entry in _role_entries(value, path) if entry["person"] == link)


def _validate_card_input(
    person_id: str,
    title: str,
    person_kind: str,
    relationship_to_owner: str,
    purpose: str,
    creation_basis: str,
) -> None:
    _validate_person_id(person_id)
    for field, value, limit in (
        ("person title", title, 120),
        ("relationship_to_owner", relationship_to_owner, 120),
        ("person purpose", purpose, 280),
    ):
        if not value.strip() or "\n" in value or len(value) > limit:
            raise WoonError(f"{field} must be one non-empty line up to {limit} characters")
    if person_kind not in _PERSON_KINDS:
        raise WoonError(f"person_kind must be one of {sorted(_PERSON_KINDS)}")
    if creation_basis not in _CREATION_BASES:
        raise WoonError("person card creation requires explicit-request or repeated-evidence")


def _validate_person_id(person_id: str) -> None:
    if not _PERSON_ID.fullmatch(person_id):
        raise WoonError("person_id must be lowercase kebab-case and at least three characters")


def _validate_roles(roles: tuple[str, ...]) -> None:
    if not roles or len(set(roles)) != len(roles) or any(role not in _ROLES for role in roles):
        raise WoonError(f"person roles must be unique values from {sorted(_ROLES)}")


def _validate_identifier_inputs(
    identifiers: tuple[PersonIdentityIdentifierInput, ...], evidence: str
) -> None:
    if not identifiers:
        raise WoonError("at least one direct user-confirmed identifier is required")
    if not evidence.strip() or "\n" in evidence or len(evidence) > 280:
        raise WoonError("identifier evidence must be one non-empty line up to 280 characters")
    values: set[str] = set()
    for identifier in identifiers:
        value = identifier.value.strip()
        if not value or "\n" in value or len(value) > 80:
            raise WoonError("identifier value must be one non-empty line up to 80 characters")
        if value in values:
            raise WoonError("identifier values must be unique")
        values.add(value)
        if len(identifier.context_terms) != len(set(identifier.context_terms)):
            raise WoonError("identifier context terms must be unique")
        for term in identifier.context_terms:
            if not term.strip() or "\n" in term or len(term) > 80:
                raise WoonError(
                    "identifier context terms must be one non-empty line up to 80 characters"
                )


def _read_identifiers(value: object, path: Path) -> tuple[PersonIdentityIdentifier, ...]:
    """Read only directly confirmed identifiers; generic aliases remain non-resolving prose."""

    if value in (None, ""):
        return ()
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, dict) for item in value)
    ):
        raise WoonError(f"identifiers must be a non-empty list of mappings: {path}")
    identifiers: list[PersonIdentityIdentifier] = []
    for item in value:
        identifier = PersonIdentityIdentifierInput(
            value=_required_string(item.get("value"), "identifier value", path),
            context_terms=_identifier_context_terms(item.get("context_terms"), path),
        )
        basis = item.get("basis")
        evidence = item.get("evidence")
        if basis != "user-confirmed" or not isinstance(evidence, str):
            raise WoonError(f"identifier needs user-confirmed basis and evidence: {path}")
        _validate_identifier_inputs((identifier,), evidence)
        identifiers.append(
            PersonIdentityIdentifier(
                value=identifier.value.strip(),
                evidence=evidence.strip(),
                context_terms=tuple(term.strip() for term in identifier.context_terms),
            )
        )
    if len({identifier.value for identifier in identifiers}) != len(identifiers):
        raise WoonError(f"identifier values must be unique: {path}")
    return tuple(identifiers)


def _identifier_context_terms(value: object, path: Path) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WoonError(f"identifier context_terms must be a list of strings: {path}")
    return tuple(value)


def _calendar_identifiers(card: PersonCard) -> tuple[PersonIdentityIdentifier, ...]:
    """Use private cards only when their identifiers were deliberately confirmed."""

    identifiers = list(card.identifiers)
    if card.person_scope == "general":
        identifiers.extend(
            PersonIdentityIdentifier(
                value=value,
                evidence="general person card title",
                context_terms=(),
            )
            for value in _default_korean_name_identifiers(card.title)
        )
    unique: dict[str, PersonIdentityIdentifier] = {}
    for identifier in identifiers:
        unique.setdefault(identifier.value, identifier)
    return tuple(unique.values())


def _default_korean_name_identifiers(title: str) -> tuple[str, ...]:
    """Use a registered Korean full name and its surname-free form as safe defaults."""

    compact = "".join(title.split())
    values = [title]
    if len(compact) == 3 and all("가" <= character <= "힣" for character in compact):
        values.append(compact[1:])
    return tuple(values)


def _is_private_history_document(*, relative: str, card: PersonCard, people: list[str]) -> bool:
    """Keep private histories focused on time records and deliberate person links."""

    if relative.startswith("inbox/private-person-history/"):
        return False
    if relative in _PRIVATE_HISTORY_EXCLUDED_PATHS:
        return False
    link = _person_link(card)
    if link in people:
        return True
    return card.person_id == _DEFAULT_OWNER_ID and relative.startswith(_PRIVATE_HISTORY_ROOTS)


def _person_link(card: PersonCard) -> str:
    return f"[[{card.relative_path.removesuffix('.md')}|{card.title}]]"


def _calendar_reference(card: PersonCard) -> CalendarPersonReference:
    return CalendarPersonReference(
        person_id=card.person_id,
        title=card.title,
        link=_person_link(card),
    )


def _title_identifier_starts(title: str, identifier: str) -> tuple[int, ...]:
    """Match an approved Korean identifier as a whole name or with a common particle."""

    starts: list[int] = []
    start = title.find(identifier)
    while start >= 0:
        end = start + len(identifier)
        before = title[start - 1] if start else ""
        after = title[end:]
        if _name_boundary_before(before) and _name_boundary_after(after):
            starts.append(start)
        start = title.find(identifier, start + 1)
    return tuple(starts)


def _name_boundary_before(value: str) -> bool:
    return not value or value.isspace() or value in "·,./()[]{}<>-–—:：;!?！？"


def _name_boundary_after(value: str) -> bool:
    if not value or value[0].isspace() or value[0] in "·,./()[]{}<>-–—:：;!?！？":
        return True
    return value.startswith(_KOREAN_NAME_PARTICLES)


def _render_card(
    *,
    person_id: str,
    title: str,
    person_kind: str,
    relationship_to_owner: str,
    purpose: str,
    creation_basis: str,
) -> str:
    link = f"[[users/{person_id}/README|{title}]]"
    frontmatter = {
        "type": "Wiki",
        "title": title,
        "publish": False,
        "access": "local-only",
        "status": "Active",
        "lifecycle": "active",
        "entity_type": "person",
        "person_id": person_id,
        "person_kind": person_kind,
        "person_scope": "general",
        "relationship_to_owner": relationship_to_owner,
        "card_creation_basis": creation_basis,
        "role": "person-dashboard",
        "parent_moc": "[[people-index|인물 관계]]",
        "tags": ["domain:common", "topic:people"],
        "people": [link],
        "related_to": ["[[people-index|인물 관계]]"],
    }
    default_identifiers = _default_korean_name_identifiers(title)
    if default_identifiers:
        frontmatter["identifiers"] = [
            {
                "value": value,
                "basis": "user-confirmed",
                "evidence": (
                    "사용자가 일반 한국어 실명 카드의 성 제외 이름을 "
                    "기본 식별자로 사용하도록 명시함"
                ),
                "context_terms": [],
            }
            for value in default_identifiers
        ]
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return (
        f"---\n{yaml_text}---\n\n# {title}\n\n"
        f"> {purpose}\n\n"
        "## 관계\n\n"
        f"- 최우녕과의 관계: {relationship_to_owner}\n"
        f"- 카드 생성 근거: {creation_basis}\n"
        "- 개인 이력이나 추측은 적지 않고, 다시 찾아볼 문서 연결만 남긴다.\n\n"
        "## 연결 문서\n\n"
        "이 인물이 `people` 속성에 명시된 문서만 표시한다. "
        "본문에 이름이 언급됐다는 이유만으로는 연결하지 않는다.\n\n"
        "![[person-indexed-docs.base]]\n"
    )


def _record_operation(path: Path, *, operation: str, payload: dict[str, object]) -> None:
    state: dict[str, object] = {"version": 1, "operations": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise WoonError("person state is invalid JSON") from error
        if not isinstance(loaded, dict) or loaded.get("version") != 1:
            raise WoonError("person state has an unsupported shape")
        state = loaded
    operations = state.setdefault("operations", {})
    if not isinstance(operations, dict):
        raise WoonError("person state operations must be a mapping")
    operations[operation] = payload
    atomic_write(path, encode_json(state))


def _relative(path: Path, vault: Path) -> str:
    return path.relative_to(vault).as_posix()


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
