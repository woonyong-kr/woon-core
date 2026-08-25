"""Markdown/Obsidian adapter for canonical documents."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import exclusive_file_lock
from woon_core.knowledge.domain import CanonicalDocument, DocumentMetadata, SaveResult


class MarkdownDocumentRepository:
    """Store each canonical ID as exactly one Markdown file under the wiki root."""

    def __init__(
        self,
        vault: Path,
        canonical_root: Path,
        lock_path: Path | None = None,
    ) -> None:
        self._vault = vault.expanduser().resolve()
        self._root = canonical_root.expanduser().resolve()
        try:
            self._root.relative_to(self._vault)
        except ValueError as error:
            raise WoonError("canonical root escapes the configured vault") from error
        self._lock_path = (
            lock_path.expanduser().resolve()
            if lock_path is not None
            else self._vault / ".local/woon-knowledge/mutation.lock"
        )
        try:
            self._lock_path.relative_to(self._vault)
        except ValueError as error:
            raise WoonError("knowledge mutation lock escapes the configured vault") from error

    def get(self, canonical_id: str) -> CanonicalDocument | None:
        path = self._find_path(canonical_id)
        if path is None:
            return None
        return self.parse(
            path.relative_to(self._vault).as_posix(),
            path.read_text(encoding="utf-8"),
        )

    def list_documents(self) -> Iterable[CanonicalDocument]:
        if not self._root.is_dir():
            return []
        documents: list[CanonicalDocument] = []
        for path in self._canonical_markdown_paths():
            relative = path.relative_to(self._vault).as_posix()
            try:
                documents.append(self.parse(relative, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, WoonError) as error:
                raise WoonError(f"invalid canonical document {relative}: {error}") from error
        return documents

    def state_token(self) -> tuple[tuple[str, int, int, int, int], ...]:
        """Return a cheap token that changes whenever a canonical file changes."""

        if not self._root.is_dir():
            return ()
        return tuple(_file_state(path, self._vault) for path in self._canonical_markdown_paths())

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Serialize all canonical validation, writes, and index rebuilds."""

        with exclusive_file_lock(self._lock_path):
            yield

    def snapshot(self, canonical_id: str) -> bytes | None:
        path = self._find_path(canonical_id)
        return path.read_bytes() if path is not None else None

    def restore_snapshot(self, canonical_id: str, snapshot: bytes | None) -> None:
        path = self._find_path(canonical_id) or self._path(canonical_id)
        if snapshot is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(snapshot)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(
        self,
        metadata: DocumentMetadata,
        body: str,
        expected_revision: str | None,
    ) -> SaveResult:
        path = self._find_path(metadata.canonical_id) or self._path(metadata.canonical_id)
        current = self.get(metadata.canonical_id)
        if current is not None and current.revision != expected_revision:
            raise WoonError(
                "canonical document changed after it was read; reload and merge before writing"
            )
        if current is None and expected_revision is not None:
            raise WoonError("expected_revision was provided for a document that does not exist")
        rendered = self._render(metadata, body)
        revision = _revision(rendered)
        if current is not None and current.revision == revision:
            return SaveResult(document=current, created=False, changed=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        document = self.parse(path.relative_to(self._vault).as_posix(), rendered)
        return SaveResult(document=document, created=current is None, changed=True)

    def validate(self) -> list[str]:
        errors: list[str] = []
        identifiers: dict[str, str] = {}
        titles: dict[str, str] = {}
        if not self._root.is_dir():
            return []
        for path in self._canonical_markdown_paths():
            relative = path.relative_to(self._vault).as_posix()
            try:
                document = self.parse(relative, path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, WoonError) as error:
                errors.append(f"{relative}: {error}")
                continue
            if document.metadata.canonical_id in identifiers:
                errors.append(
                    f"{relative}: duplicate canonical_id also used by "
                    f"{identifiers[document.metadata.canonical_id]}"
                )
            identifiers[document.metadata.canonical_id] = relative
            normalized_title = "".join(document.metadata.title.lower().split())
            if normalized_title in titles:
                errors.append(
                    f"{relative}: duplicate title also used by {titles[normalized_title]}"
                )
            titles[normalized_title] = relative
        return errors

    def parse(self, relative_path: str, text: str) -> CanonicalDocument:
        if not text.startswith("---\n"):
            raise WoonError("canonical document is missing YAML frontmatter")
        try:
            _, frontmatter, content = text.split("---\n", 2)
        except ValueError as error:
            raise WoonError("canonical document has invalid YAML frontmatter boundaries") from error
        content = content.lstrip("\n")
        raw = yaml.safe_load(frontmatter) or {}
        if not isinstance(raw, dict):
            raise WoonError("canonical document frontmatter must be a mapping")
        canonical_id = _required(raw, "canonical_id")
        metadata = DocumentMetadata(
            canonical_id=canonical_id,
            title=_required(raw, "title"),
            # Older unified Wiki pages did not persist this redundant field.
            # Derive it from the path identity so the whole ``wiki/`` tree is
            # one repository without rewriting valid human-authored notes.
            domain=str(raw.get("domain") or canonical_id.split("/", 1)[0]),
            summary=_required(raw, "summary"),
            purpose=str(raw.get("purpose", "")),
            difficulty=str(raw.get("difficulty", "foundation")),
            prerequisites=_strings(raw.get("prerequisites")),
            next_concepts=_strings(raw.get("next_concepts")),
            related=_strings(raw.get("related")),
            source_ids=_strings(raw.get("source_ids")),
        )
        heading = f"# {metadata.title}\n"
        if not content.startswith(heading):
            raise WoonError("canonical document H1 must match frontmatter title")
        body = content[len(heading) :].lstrip("\n")
        navigation = "\n## 이어서 읽기\n"
        if navigation in body:
            body = body.split(navigation, 1)[0].rstrip() + "\n"
        return CanonicalDocument(
            metadata=metadata,
            body=body,
            relative_path=relative_path,
            revision=_revision(text),
        )

    def _path(self, canonical_id: str) -> Path:
        candidate = (self._root / f"{canonical_id}.md").resolve()
        try:
            candidate.relative_to(self._root.resolve())
        except ValueError as error:
            raise WoonError("canonical document path escapes the configured root") from error
        return candidate

    def _find_path(self, canonical_id: str) -> Path | None:
        """Resolve a stable canonical identity independently from its current path."""

        if not self._root.is_dir():
            return None
        matches: list[Path] = []
        preferred = self._path(canonical_id)
        paths = [preferred] if preferred.is_file() else []
        paths.extend(path for path in self._canonical_markdown_paths() if path != preferred)
        for path in paths:
            try:
                document = self.parse(
                    path.relative_to(self._vault).as_posix(),
                    path.read_text(encoding="utf-8"),
                )
            except (OSError, UnicodeError, WoonError):
                continue
            if document.metadata.canonical_id == canonical_id:
                matches.append(path)
        if len(matches) > 1:
            locations = [path.relative_to(self._vault).as_posix() for path in matches]
            raise WoonError(
                f"canonical_id {canonical_id!r} resolves to multiple files: {locations}"
            )
        return matches[0] if matches else None

    def _canonical_markdown_paths(self) -> tuple[Path, ...]:
        """Exclude the raw Wiki-owned source archive from canonical documents."""

        return tuple(
            path
            for path in sorted(self._root.rglob("*.md"))
            if "_sources" not in path.relative_to(self._root).parts
        )

    @staticmethod
    def _render(metadata: DocumentMetadata, body: str) -> str:
        frontmatter: dict[str, Any] = {
            "type": "Wiki",
            "canonical_id": metadata.canonical_id,
            "title": metadata.title,
            "domain": metadata.domain,
            "summary": metadata.summary,
            "purpose": metadata.purpose,
            "status": "Canonical",
            "publish": False,
            "access": "local-only",
            "difficulty": metadata.difficulty,
            "prerequisites": list(metadata.prerequisites),
            "next_concepts": list(metadata.next_concepts),
            "related": list(metadata.related),
            "source_ids": list(metadata.source_ids),
        }
        yaml_text = yaml.safe_dump(
            frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        navigation = _navigation(metadata)
        return f"---\n{yaml_text}---\n\n# {metadata.title}\n\n{body.rstrip()}\n{navigation}"


def _navigation(metadata: DocumentMetadata) -> str:
    lines = ["", "## 이어서 읽기", ""]
    relationships = (
        ("먼저", metadata.prerequisites),
        ("다음", metadata.next_concepts),
        ("관련", metadata.related),
    )
    for label, identifiers in relationships:
        if identifiers:
            links = ", ".join(f"[[{identifier}]]" for identifier in identifiers)
            lines.append(f"- {label}: {links}")
    if len(lines) == 3:
        lines.append("- 연결할 개념 없음")
    return "\n".join(lines) + "\n"


def _file_state(path: Path, vault: Path) -> tuple[str, int, int, int, int]:
    stat = path.stat()
    return (
        path.relative_to(vault).as_posix(),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
    )


def _required(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"canonical document frontmatter requires {key!r}")
    return value.strip()


def _strings(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(value, str) for value in raw):
        raise WoonError("canonical relationship fields must be string lists")
    return tuple(value.strip() for value in raw if value.strip())


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
