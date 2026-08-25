"""Local-only evidence archive for user-visible Codex conversations.

The Wiki remains the sole knowledge canon.  This module preserves the exact
user-authored questions, corresponding assistant final answers, and human attachment descriptions
needed to regenerate a daily history without retaining system instructions,
reasoning, or tool payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json

type SourceRole = Literal["user", "assistant"]

_ARCHIVE_SCHEMA_VERSION = 1
_ARCHIVE_RELATIVE_ROOT = Path("wiki/private/_sources/codex")
_MESSAGE_LIMIT = 200_000
_BUNDLE_LIMIT = 16_000_000
_LABEL_LIMIT = 160
_ATTACHMENT_LIMIT = 48
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b\s*"
    r"(?:[:=]|(?:is|은|는))\s*)([^\s`\"']{8,})"
)
_KOREAN_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:비밀번호|암호|인증\s*키|API\s*키|접근\s*토큰)\s*[:=]\s*)"
    r"([^\s`\"']{6,})"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_KOREAN_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_-]{20,})\b(?=[^\n]{0,30}(?:토큰|비밀번호|인증\s*키))"
)


@dataclass(frozen=True, slots=True)
class CodexSourceAttachment:
    """One human-identifiable attachment without an unstable temporary path."""

    label: str
    media_type: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class CodexSourceMessage:
    """One allowed user message or assistant final answer."""

    role: SourceRole
    text: str
    created_at: str
    attachments: tuple[CodexSourceAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexSourceBundle:
    """One task's allowed conversation evidence for a single KST day."""

    day: date
    source_locator: str
    title: str
    messages: tuple[CodexSourceMessage, ...]


@dataclass(frozen=True, slots=True)
class CodexSourceArchiveResult:
    """Non-sensitive result of one append-only archive update."""

    day: str
    message_count: int
    bundle_key: str
    replayed: bool


def bundle_from_record(record: dict[str, object]) -> CodexSourceBundle:
    """Parse a file payload and reject every non-conversation role or field."""

    allowed = {"day", "source_locator", "title", "messages"}
    if set(record).difference(allowed) or set(record) != allowed:
        raise WoonError("Codex source bundle has unsupported fields")
    raw_day = record["day"]
    source_locator = record["source_locator"]
    title = record["title"]
    messages = record["messages"]
    if not isinstance(raw_day, str) or not isinstance(source_locator, str):
        raise WoonError("Codex source bundle identity fields must be strings")
    if not isinstance(title, str) or not isinstance(messages, list):
        raise WoonError("Codex source bundle title and messages are invalid")
    try:
        parsed_day = date.fromisoformat(raw_day)
    except ValueError as error:
        raise WoonError("Codex source bundle day must be YYYY-MM-DD") from error
    parsed_messages = tuple(_message_from_record(item) for item in messages)
    bundle = CodexSourceBundle(
        day=parsed_day,
        source_locator=source_locator,
        title=title,
        messages=parsed_messages,
    )
    _validate_bundle(bundle)
    return bundle


def record_codex_source_bundle(vault: Path, bundle: CodexSourceBundle) -> CodexSourceArchiveResult:
    """Store an exact local source snapshot with append-only update semantics."""

    _validate_bundle(bundle)
    root = vault.expanduser().resolve()
    archive_root = root / _ARCHIVE_RELATIVE_ROOT
    day_root = archive_root / bundle.day.isoformat()
    _ensure_private_directory(root, day_root)
    bundle_key = hashlib.sha256(
        f"{bundle.day.isoformat()}\0{bundle.source_locator}".encode()
    ).hexdigest()[:24]
    destination = day_root / f"{bundle_key}.json"
    value = _bundle_value(bundle, bundle_key=bundle_key)
    serialized = encode_json(value)
    if len(serialized) > _BUNDLE_LIMIT:
        raise WoonError("Codex source bundle exceeds the local archive size limit")
    replayed = destination.is_file() and destination.read_bytes() == serialized
    if destination.is_file() and not replayed:
        previous = _load_bundle_value(destination)
        _validate_append_only(previous, value)
    if not replayed:
        atomic_write(destination, serialized, mode=0o600)
    return CodexSourceArchiveResult(
        day=bundle.day.isoformat(),
        message_count=len(bundle.messages),
        bundle_key=bundle_key,
        replayed=replayed,
    )


def load_codex_source_bundles(vault: Path, *, day: date) -> tuple[dict[str, object], ...]:
    """Read validated source bundles for a daily projection without locators."""

    day_root = vault.expanduser().resolve() / _ARCHIVE_RELATIVE_ROOT / day.isoformat()
    if not day_root.is_dir():
        return ()
    values: list[dict[str, object]] = []
    for path in sorted(day_root.glob("*.json")):
        value = _load_bundle_value(path)
        if value.get("day") != day.isoformat():
            raise WoonError("Codex source archive day does not match its directory")
        values.append(
            {
                "title": value["title"],
                "messages": value["messages"],
            }
        )
    return tuple(values)


def _message_from_record(value: object) -> CodexSourceMessage:
    if not isinstance(value, dict):
        raise WoonError("Codex source message must be a mapping")
    allowed = {"role", "text", "created_at", "attachments"}
    if set(value).difference(allowed) or not {"role", "text", "created_at"}.issubset(value):
        raise WoonError("Codex source message has unsupported fields")
    role = value["role"]
    text = value["text"]
    created_at = value["created_at"]
    attachments = value.get("attachments", [])
    if role not in {"user", "assistant"} or not isinstance(text, str):
        raise WoonError("Codex source message role or text is invalid")
    if not isinstance(created_at, str) or not isinstance(attachments, list):
        raise WoonError("Codex source message timestamp or attachments are invalid")
    return CodexSourceMessage(
        role=cast(SourceRole, role),
        text=text,
        created_at=_timestamp(created_at),
        attachments=tuple(_attachment_from_record(item) for item in attachments),
    )


def _attachment_from_record(value: object) -> CodexSourceAttachment:
    if not isinstance(value, dict):
        raise WoonError("Codex source attachment must be a mapping")
    allowed = {"label", "media_type", "description"}
    if set(value).difference(allowed) or "label" not in value:
        raise WoonError("Codex source attachment has unsupported fields")
    label = value["label"]
    media_type = value.get("media_type")
    description = value.get("description")
    if not isinstance(label, str):
        raise WoonError("Codex source attachment label must be a string")
    if media_type is not None and not isinstance(media_type, str):
        raise WoonError("Codex source attachment media_type must be a string or null")
    if description is not None and not isinstance(description, str):
        raise WoonError("Codex source attachment description must be a string or null")
    return CodexSourceAttachment(label=label, media_type=media_type, description=description)


def _validate_bundle(bundle: CodexSourceBundle) -> None:
    if not bundle.source_locator.strip() or len(bundle.source_locator) > 512:
        raise WoonError("Codex source locator is invalid")
    _short_text(bundle.title, "title", _LABEL_LIMIT)
    if not bundle.messages:
        raise WoonError("Codex source bundle must contain at least one message")
    for message in bundle.messages:
        if message.role not in {"user", "assistant"}:
            raise WoonError("Codex source archive accepts only user and assistant messages")
        if not message.text.strip() or len(message.text) > _MESSAGE_LIMIT:
            raise WoonError("Codex source message is empty or too large")
        _timestamp(message.created_at)
        if len(message.attachments) > _ATTACHMENT_LIMIT:
            raise WoonError("Codex source message has too many attachments")
        for attachment in message.attachments:
            _short_text(attachment.label, "attachment label", _LABEL_LIMIT)
            if attachment.media_type is not None:
                _short_text(attachment.media_type, "attachment media_type", 120)
            if attachment.description is not None:
                _short_text(attachment.description, "attachment description", 800)


def _bundle_value(bundle: CodexSourceBundle, *, bundle_key: str) -> dict[str, object]:
    return {
        "schema_version": _ARCHIVE_SCHEMA_VERSION,
        "bundle_key": bundle_key,
        "day": bundle.day.isoformat(),
        "title": redact_secrets(bundle.title).strip(),
        "messages": [
            {
                "role": message.role,
                "text": redact_secrets(message.text).strip(),
                "created_at": message.created_at,
                "attachments": [asdict(item) for item in message.attachments],
            }
            for message in bundle.messages
        ],
    }


def redact_secrets(text: str) -> str:
    """Remove credential-shaped values while preserving the surrounding meaning."""

    value = _SECRET_ASSIGNMENT_RE.sub(r"\1[민감정보 숨김]", text)
    value = _KOREAN_SECRET_ASSIGNMENT_RE.sub(r"\1[민감정보 숨김]", value)
    value = _BEARER_RE.sub("Bearer [민감정보 숨김]", value)
    value = _OPENAI_KEY_RE.sub("[민감정보 숨김]", value)
    return _KOREAN_SECRET_RE.sub("[민감정보 숨김]", value)


def _validate_append_only(previous: dict[str, object], current: dict[str, object]) -> None:
    if previous.get("title") != current.get("title") or previous.get("day") != current.get("day"):
        raise WoonError("Codex source archive identity changed")
    old_messages = previous.get("messages")
    new_messages = current.get("messages")
    if not isinstance(old_messages, list) or not isinstance(new_messages, list):
        raise WoonError("Codex source archive messages are unreadable")
    if len(new_messages) < len(old_messages) or new_messages[: len(old_messages)] != old_messages:
        raise WoonError("Codex source archive update must append without rewriting history")


def _load_bundle_value(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError("Codex source archive is unreadable") from error
    if not isinstance(value, dict) or value.get("schema_version") != _ARCHIVE_SCHEMA_VERSION:
        raise WoonError("Codex source archive schema is invalid")
    if not isinstance(value.get("title"), str) or not isinstance(value.get("messages"), list):
        raise WoonError("Codex source archive content is invalid")
    return value


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise WoonError("Codex source message timestamp must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise WoonError("Codex source message timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _short_text(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit or "\n" in value:
        raise WoonError(f"Codex source {field} is invalid")


def _ensure_private_directory(vault: Path, directory: Path) -> None:
    runtime_root = (vault / _ARCHIVE_RELATIVE_ROOT).resolve()
    target = directory.resolve()
    if not target.is_relative_to(runtime_root):
        raise WoonError("Codex source archive path is outside the Wiki private source root")
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    current = runtime_root
    for part in target.relative_to(runtime_root).parts:
        current /= part
        current.mkdir(exist_ok=True)
        current.chmod(0o700)
