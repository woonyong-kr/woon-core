"""Explicit record metadata shared by native writers and source renderers.

This module never reads a body, infers an identity, changes a record kind, or
writes files. Callers own source evidence, revision checks and atomic writes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
from pathlib import PurePosixPath

from woon_core.errors import WoonError

RECORD_KINDS = (
    "event",
    "recording",
    "meeting",
    "diary",
    "decision",
    "experience",
    "application",
    "resource",
    "scene",
    "idea",
)
RECORD_ID_FIELDS = ("canonical_id", "recording_id", "record_id")
RECORD_METADATA_FIELDS = frozenset(
    {
        *RECORD_ID_FIELDS,
        "record_kind",
        "people",
        "person_roles",
        "related_to",
        "candidate_person_ids",
        "unresolved_speaker_count",
        "recorded_at",
        "occurred_on",
        "started_on",
        "ended_on",
        "Date",
        "Time",
        "Start Date",
        "End Date",
        "event_people",
        "event_period",
        "review_status",
        "full_text_reviewed",
        "audio_verified",
        "event_change",
        "history_person_id",
        "sequence",
        "record_owner",
    }
)
_PERSON_ID = re.compile(r"[a-z][a-z0-9-]{2,79}\Z")
_LINK = re.compile(r"\[\[([^\[\]|#]+)(?:\|[^\[\]]+)?\]\]\Z")
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
        "related-record",
    }
)


def resolve_record_metadata(
    existing: Mapping[str, object],
    proposed: Mapping[str, object],
    *,
    confirmed_relations: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Merge renderer metadata without losing existing annotations or relationships.

    Omitted fields survive; explicit values replace them. Relations must already
    have a source-confirmed identity/path. Candidate IDs remain plain IDs and do
    not create links. A user-selected collection proves related-record only.
    """
    result = deepcopy(dict(existing))
    result.update(deepcopy(dict(proposed)))
    for field in RECORD_ID_FIELDS:
        if field in existing and result.get(field) != existing[field]:
            raise WoonError(f"record identity must be preserved: {field}")
    for relation in confirmed_relations:
        _merge_relation(result, relation)
    validate_record_metadata(result)
    return result


def validate_record_metadata_update(
    existing: Mapping[str, object],
    proposed: Mapping[str, object],
) -> None:
    """Reject silent loss before a writer accepts hash-pinned replacement bytes.

    Do not rewrite reviewed bytes internally. A caller regenerating frontmatter
    should first use resolve_record_metadata and then pin the resulting bytes.
    """
    if "record_kind" not in existing and "record_kind" not in proposed:
        return
    missing = sorted((existing.keys() & RECORD_METADATA_FIELDS) - proposed.keys())
    if missing:
        raise WoonError("record metadata omitted; resolve before writing: " + ", ".join(missing))
    for field in RECORD_ID_FIELDS:
        if field in existing and proposed.get(field) != existing[field]:
            raise WoonError(f"record identity must be preserved: {field}")
    validate_record_metadata(proposed)


def validate_record_metadata(metadata: Mapping[str, object]) -> None:
    """Validate only explicitly opted-in records, leaving entity schemas alone."""
    if "record_kind" not in metadata:
        return
    if metadata["record_kind"] not in RECORD_KINDS:
        raise WoonError("record_kind must be an explicit supported record kind")
    if metadata.get("access") != "local-only" or metadata.get("publish") is not False:
        raise WoonError("linked records require access: local-only and publish: false")
    if metadata.get("publication_state", "private") != "private":
        raise WoonError("linked record publication_state must be private")
    identities = [metadata[field] for field in RECORD_ID_FIELDS if field in metadata]
    if not identities or any(
        not isinstance(value, str) or not value.strip() or "[[" in value for value in identities
    ):
        raise WoonError("linked record requires a stable canonical_id, recording_id or record_id")
    if metadata["record_kind"] == "recording" and not metadata.get("recording_id"):
        raise WoonError("recording requires its existing recording_id")
    for field in ("candidate_person_ids", "event_people"):
        if field in metadata:
            _person_ids(metadata[field], field)
    if "history_person_id" in metadata:
        _person_ids([metadata["history_person_id"]], "history_person_id")
    if "unresolved_speaker_count" in metadata:
        value = metadata["unresolved_speaker_count"]
        if type(value) is not int or value < 0:
            raise WoonError("unresolved_speaker_count must be a nonnegative integer")
    if "sequence" in metadata:
        sequence = metadata["sequence"]
        if (
            not isinstance(sequence, (int, float))
            or isinstance(sequence, bool)
            or not math.isfinite(sequence)
        ):
            raise WoonError("sequence must be a finite number")
    for field in ("recorded_at", "occurred_on", "started_on", "ended_on", "Date"):
        if field in metadata and metadata[field] not in (None, ""):
            _validate_date(metadata[field], field)
    for field in ("people", "related_to"):
        if field in metadata:
            links = metadata[field]
            if not isinstance(links, list):
                raise WoonError(f"{field} must be a list of explicit wikilinks")
            targets = [_link_target(link) for link in links]
            if len(set(targets)) != len(targets):
                raise WoonError(f"{field} repeats a linked document")
    roles = metadata.get("person_roles", [])
    if not isinstance(roles, list):
        raise WoonError("person_roles must be a list")
    people_links = metadata.get("people", [])
    if not isinstance(people_links, list):
        raise WoonError("people must be a list")
    people = {_link_target(link) for link in people_links}
    seen: set[tuple[str, str]] = set()
    for entry in roles:
        if not isinstance(entry, dict):
            raise WoonError("person_roles entries must be mappings")
        target = _link_target(entry.get("person"))
        role = str(entry.get("role", ""))
        if target not in people or role not in _ROLES or not entry.get("evidence"):
            raise WoonError("person role requires its people link, supported role and evidence")
        if not entry.get("basis") or entry.get("basis") in ("context-inferred", "unresolved"):
            raise WoonError("candidate identity cannot become a confirmed person role")
        if entry.get("basis") == "user-selected-recording-collection" and role != "related-record":
            raise WoonError("a selected recording collection proves related-record only")
        if (target, role) in seen:
            raise WoonError("person_roles repeats a person and role")
        seen.add((target, role))


def validate_record_collection(records: Iterable[tuple[str, Mapping[str, object]]]) -> None:
    """Check a selected renderer batch for duplicate stable IDs before writing.

    This is a metadata-only check over caller-selected records, not a corpus gate.
    The same ID on two paths is an error, never an arbitrary deduplication choice.
    """
    seen: dict[tuple[str, str], str] = {}
    for path, metadata in records:
        validate_record_metadata(metadata)
        if "record_kind" not in metadata:
            continue
        for field in RECORD_ID_FIELDS:
            if field not in metadata:
                continue
            key = (field, str(metadata[field]))
            if key in seen:
                raise WoonError(f"record identity repeats: {field}: {seen[key]} / {path}")
            seen[key] = path


def _person_ids(value: object, field: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or _PERSON_ID.fullmatch(item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        raise WoonError(f"{field} must contain unique plain person IDs, never links or names")


def _link_target(value: object) -> str:
    match = _LINK.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise WoonError("record relation must be an explicit Vault wikilink")
    path = PurePosixPath(match[1])
    if path.is_absolute() or ".." in path.parts or "\\" in match[1]:
        raise WoonError("record relation must stay inside the Vault")
    return str(path).removesuffix(".md")


def _validate_date(value: object, field: str) -> None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return
    elif isinstance(value, str):
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                date.fromisoformat(value)
                return
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise WoonError(f"{field} must be an ISO date or timezone-aware datetime") from error
    else:
        raise WoonError(f"{field} must be an ISO date or timezone-aware datetime")
    if parsed.tzinfo is None:
        raise WoonError(f"{field} datetime must include its timezone")


def _merge_relation(result: dict[str, object], relation: Mapping[str, object]) -> None:
    required = {"person_id", "person_path", "role", "basis", "evidence"}
    if set(relation) != required or any(
        not isinstance(relation[key], str) or not str(relation[key]).strip() for key in required
    ):
        raise WoonError("confirmed relation requires person_id, person_path, role, basis, evidence")
    _person_ids([relation["person_id"]], "confirmed person_id")
    path = str(relation["person_path"])
    if not path.endswith(".md"):
        raise WoonError("confirmed person_path must be a Vault-relative Markdown path")
    link = "[[" + path.removesuffix(".md") + "]]"
    target = _link_target(link)
    people = result.setdefault("people", [])
    roles = result.setdefault("person_roles", [])
    if not isinstance(people, list) or not isinstance(roles, list):
        raise WoonError("people and person_roles must be lists")
    existing_link = next((item for item in people if _link_target(item) == target), None)
    if existing_link is None:
        people.append(link)
    else:
        link = str(existing_link)
    entry = {
        "person": link,
        **{key: relation[key] for key in ("person_id", "role", "basis", "evidence")},
    }
    for current in roles:
        if isinstance(current, dict) and (
            _link_target(current.get("person")),
            current.get("role"),
        ) == (target, relation["role"]):
            if current != entry:
                raise WoonError(
                    "confirmed role changed; review existing evidence before replacement"
                )
            return
    roles.append(entry)
