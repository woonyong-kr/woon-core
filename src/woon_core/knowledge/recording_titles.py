"""Validate reviewed recording titles without editing source evidence or inferring people."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import split_markdown

if TYPE_CHECKING:
    from woon_core.knowledge.service import ManualWikiWrite


@dataclass(frozen=True, slots=True)
class RecordingTitleBundle:
    """One reviewed local recording rename, including its current producer and owners.

    All writes are existing, exact hash-pinned files. Native/compiler backlinks
    remain inputs to the enclosing Wiki transaction. This bundle never runs the
    producer, changes original audio/transcripts, or replays historical plans.
    """

    readers: tuple[ManualWikiWrite, ...]
    catalog: ManualWikiWrite
    references: tuple[ManualWikiWrite, ...]
    producer: ManualWikiWrite
    source_owners: ManualWikiWrite | None = None


_ARCHIVE = "private/knowledge/voice-memos/"
_OWNERS = "catalog/sources/raw-archive-ownership.yaml"
_LINK = re.compile(r"\[\[([^\]|#\\]+)([^\]]*)\]\]")
# source, target, original bytes, reviewed bytes, original mode
RecordingWrite = tuple[Path, Path, bytes, bytes, int]


def validate_recording_title_change(before: bytes, after: bytes, recording_id: str) -> str:
    """Allow only a recording's frontmatter title and first H1 to change.

    The caller owns the exact input/output hashes, private path scope, destination
    availability, file modes, shared lock, reference writes and rollback.
    """

    try:
        before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
        original, _ = split_markdown(before_text)
        revised, _ = split_markdown(after_text)
    except (UnicodeError, WoonError) as error:
        raise WoonError("recording title change requires valid Markdown") from error
    if (
        original.get("record_kind") != "recording"
        or original.get("recording_id") != recording_id
        or original.get("access") != "local-only"
        or original.get("publish") is not False
        or original.get("publication_state") != "private"
    ):
        raise WoonError("recording title change requires its existing private recording identity")
    title = revised.get("title")
    if not isinstance(title, str) or not title.strip() or any(c in title for c in "\r\n/\\|[]"):
        raise WoonError("recording title must be one plain filename label")
    if title != title.strip() or revised != {**original, "title": title}:
        raise WoonError("recording metadata other than title must be preserved")

    normalized = []
    for text, expected_h1 in ((before_text, None), (after_text, f"# {title}")):
        if not text.startswith("---\n"):
            raise WoonError("recording must retain its frontmatter boundary")
        front, separator, body = text[4:].partition("\n---\n")
        h1 = body.lstrip().partition("\n")[0]
        if (
            not separator
            or not h1.startswith("# ")
            or (expected_h1 is not None and h1 != expected_h1)
        ):
            raise WoonError("recording H1 must match its title")
        front, count = re.subn(r"(?m)^title: [^\n]*$", "title: TITLE", front)
        if count != 1:
            raise WoonError("recording must retain one explicit title row")
        body = re.sub(r"(?m)^# [^\n]*", "# TITLE", body, count=1)
        normalized.append((front, body))
    if normalized[0] != normalized[1]:
        raise WoonError("recording source text and all non-title bytes must be preserved")
    return title


def validate_recording_catalog_change(
    before: bytes, after: bytes, renames: Mapping[str, tuple[str, str, str]]
) -> None:
    """Allow only each known recording's reader_path and reviewed title to change.

    ``renames`` maps recording ID to the old reader path, new reader path and title.
    Historical locators, source hashes, relation and provider states remain intact.
    """

    try:
        original, revised = json.loads(before), json.loads(after)
    except (UnicodeError, ValueError) as error:
        raise WoonError("recording catalog must contain valid JSON") from error
    if not isinstance(original, dict) or (
        original.get("access") != "local-only"
        or original.get("publication_state") != "private"
        or not isinstance(original.get("records"), list)
    ):
        raise WoonError("recording catalog must remain a private recording inventory")
    expected = copy.deepcopy(original)
    seen: set[str] = set()
    for row in expected["records"]:
        if not isinstance(row, dict) or not isinstance(row.get("recording_id"), str):
            raise WoonError("recording catalog record requires its stable ID")
        recording_id = row["recording_id"]
        if recording_id in seen:
            raise WoonError("recording catalog repeats an identity")
        seen.add(recording_id)
        if recording_id not in renames:
            continue
        current, target, title = renames[recording_id]
        if row.get("reader_path") != current:
            raise WoonError("recording catalog reader path changed after review")
        row.update(reader_path=target, title=title)
    if not set(renames).issubset(seen) or revised != expected:
        raise WoonError(
            "recording catalog must preserve all fields outside reviewed title locators"
        )


def _exact_path(vault: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    path = vault / relative
    if (
        not relative
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != relative
        or path.resolve() != path
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise WoonError("recording transaction requires an exact non-symlink Vault path")
    return path


def _private_metadata(content: bytes) -> dict[str, Any]:
    header, _ = split_markdown(content.decode("utf-8"))
    if header.get("access") != "local-only" or header.get("publish") is not False:
        raise WoonError("recording references must retain private publication")
    return header


def _rewrite_links(content: bytes, renames: Mapping[str, tuple[str, str, str]]) -> bytes:
    paths = {
        old.removesuffix(".md"): new.removesuffix(".md") for old, new, _title in renames.values()
    }
    paths.update({old: new for old, new, _title in renames.values()})
    return _LINK.sub(
        lambda match: "[[" + paths.get(match[1], match[1]) + match[2] + "]]",
        content.decode("utf-8"),
    ).encode()


def _validate_source_owners(
    before: bytes, after: bytes, redirects: Mapping[str, tuple[bytes, str]]
) -> None:
    """Keep original SHA/size/history and every byte outside two existing scalar values."""
    original, revised = yaml.safe_load(before), yaml.safe_load(after)
    expected = copy.deepcopy(original)
    seen = set()
    for row in expected["records"]:
        target = row.get("target")
        if target not in redirects:
            continue
        if target in seen or (
            row.get("source_id") != "source://raw-archive/" + target.removeprefix("private/")
            or row.get("locator") != target.removeprefix("private/")
            or row.get("role") != "retired-transcript-pointer"
            or row.get("state") != "canonical"
            or row.get("privacy") != "private/local-only"
        ):
            raise WoonError("recording redirect requires its distinct existing source owner")
        seen.add(target)
        content, reader = redirects[target]
        row.update(target_sha256=hashlib.sha256(content).hexdigest(), replaced_by=reader)
    if seen != set(redirects) or revised != expected:
        raise WoonError("recording source owners may change only target hash and reader locator")

    normalized = []
    for content in (before, after):
        text = content.decode("utf-8")
        root = yaml.compose(text)
        records = next(value for key, value in root.value if key.value == "records")
        spans = []
        for node in records.value:
            fields = {key.value: value for key, value in node.value}
            if fields.get("target") is None or fields["target"].value not in redirects:
                continue
            if len(fields) != len(node.value):
                raise WoonError("recording source owner contains duplicate fields")
            for name in ("target_sha256", "replaced_by"):
                value = fields[name]
                if not isinstance(value, yaml.ScalarNode):
                    raise WoonError("recording source owner update requires existing scalars")
                spans.append((value.start_mark.index, value.end_mark.index))
        for start, end in sorted(spans, reverse=True):
            text = text[:start] + "REVIEWED-SCALAR" + text[end:]
        normalized.append(text)
    if normalized[0] != normalized[1]:
        raise WoonError("recording source owner formatting and all other bytes must be preserved")


def prepare_recording_title_bundle(
    vault: Path,
    bundle: RecordingTitleBundle | None,
) -> tuple[RecordingWrite, ...]:
    """Validate the complete participant before its enclosing transaction mutates anything."""
    if bundle is None:
        return ()
    if not bundle.readers or bundle.catalog.current_path != _ARCHIVE + "archive-catalog.json":
        raise WoonError("recording bundle requires readers and their existing archive catalog")
    producer_match = re.fullmatch(
        re.escape(_ARCHIVE) + r"(intake-\d{8})/processing/render_clovanote_local\.py",
        bundle.producer.current_path,
    )
    if producer_match is None:
        raise WoonError("recording bundle requires its existing local renderer")
    intake = _ARCHIVE + producer_match[1] + "/"
    requested = (
        *bundle.readers,
        *bundle.references,
        bundle.producer,
        bundle.catalog,
        *((bundle.source_owners,) if bundle.source_owners is not None else ()),
    )
    writes = []
    occupied: set[Path] = set()
    for write in requested:
        if not isinstance(write.content, bytes) or write.target_path is None:
            raise WoonError("recording bundle cannot create or retire documents")
        source = _exact_path(vault, write.current_path)
        target = _exact_path(vault, write.target_path)
        paths = {source, target}
        if paths & occupied or not source.is_file():
            raise WoonError("recording bundle requires distinct existing files")
        occupied.update(paths)
        before = source.read_bytes()
        if hashlib.sha256(before).hexdigest() != write.current_sha256:
            raise WoonError(f"recording source changed; replan: {write.current_path}")
        if hashlib.sha256(write.content).hexdigest() != write.target_sha256:
            raise WoonError(f"recording target SHA-256 mismatch: {write.target_path}")
        if source != target and (target.exists() or target.is_symlink()):
            raise WoonError(f"recording destination must be absent: {write.target_path}")
        writes.append((source, target, before, write.content, source.stat().st_mode & 0o777))
    values = {
        source.relative_to(vault).as_posix(): (before, after)
        for source, _target, before, after, _mode in writes
    }
    renames: dict[str, tuple[str, str, str]] = {}
    for write in bundle.readers:
        reader_source = PurePosixPath(write.current_path)
        reader_target = PurePosixPath(cast(str, write.target_path))
        if (
            not reader_source.as_posix().startswith(_ARCHIVE + "recordings/")
            or reader_source.parent != reader_target.parent
            or reader_source == reader_target
            or reader_source.suffix != ".md"
            or reader_target.suffix != ".md"
        ):
            raise WoonError("recording readers allow only same-directory Markdown renames")
        before, after = values[write.current_path]
        recording_id = _private_metadata(before).get("recording_id")
        if not isinstance(recording_id, str) or recording_id in renames:
            raise WoonError("recording reader requires a distinct stable recording ID")
        title = validate_recording_title_change(before, after, recording_id)
        if reader_target.stem != title:
            raise WoonError("recording target filename must equal the reviewed title")
        renames[recording_id] = (write.current_path, cast(str, write.target_path), title)
    validate_recording_catalog_change(*values[bundle.catalog.current_path], renames)
    catalog = json.loads(values[bundle.catalog.current_path][0])
    records = {row["recording_id"]: row for row in catalog["records"]}
    corrections = {
        intake + f"clovanote-corrections-local/record-{records[key]['intake_record']:02d}.md": key
        for key in renames
        if records[key].get("intake_path") == intake.rstrip("/")
    }
    if len(corrections) != len(renames):
        raise WoonError("recording IDs must belong to the producer's existing intake")
    redirects = {}
    seen_corrections = set()
    support = {_ARCHIVE + "archive-design.md", _ARCHIVE + "recordings-index.md"}
    support.update(
        _ARCHIVE + "people/" + str(person["person_id"]) + ".md"
        for row in records.values()
        for person in row.get("related_people", [])
    )
    for write in requested[len(bundle.readers) :]:
        if write.current_path != write.target_path:
            raise WoonError("recording non-reader participants must update existing paths")
    for write in bundle.references:
        path = write.current_path
        before, after = values[path]
        original, revised = _private_metadata(before), _private_metadata(after)
        rewritten = _rewrite_links(before, renames)
        if path in corrections:
            key = corrections[path]
            seen_corrections.add(path)
            title = renames[key][2] + " 교정 기록"
            text = rewritten.decode("utf-8")
            text, count = re.subn(
                r"(?m)^title: [^\n]*$",
                "title: " + json.dumps(title, ensure_ascii=False),
                text,
                count=1,
            )
            text = re.sub(r"(?m)^# [^\n]*$", "# " + title, text, count=1)
            if count != 1 or text.encode() != after or revised != {**original, "title": title}:
                raise WoonError("recording correction text may change only title and reader links")
        elif path.startswith("private/novel/vault-source/"):
            changed = [
                match
                for match in _LINK.finditer(before.decode())
                if _rewrite_links(match[0].encode(), renames) != match[0].encode()
            ]
            if (
                original.get("role") != "transcript-redirect"
                or original.get("status") != "Superseded"
                or len(changed) != 1
                or rewritten != after
            ):
                raise WoonError("source redirect may change only its one current-reader link")
            old = changed[0][1].removesuffix(".md")
            reader = next(
                new for current, new, _ in renames.values() if current.removesuffix(".md") == old
            )
            redirects[path] = (after, reader)
        elif path in support:
            if revised != original:
                raise WoonError("recording index/design metadata must be preserved")
        elif path == intake + "clovanote-transcript-replacement.md":
            names = {
                old.removesuffix(".md"): (new.removesuffix(".md"), title)
                for old, new, title in renames.values()
            }

            def current_table_link(
                match: re.Match[str],
                names: Mapping[str, tuple[str, str]] = names,
            ) -> str:
                if match[1] not in names or re.fullmatch(r"\\?\|[^\]]*", match[2]) is None:
                    return match[0]
                target, title = names[match[1]]
                separator = "\\|" if match[2].startswith("\\") else "|"
                return "[[" + target + separator + title + "]]"

            expected = _LINK.sub(current_table_link, before.decode()).encode()
            if expected != after:
                raise WoonError("historical transcript summary permits current reader links only")
        else:
            raise WoonError(f"file is outside recording title ownership: {path}")
        if _rewrite_links(after, renames) != after:
            raise WoonError("obsolete current-reader link remains in recording reference")
    if seen_corrections != set(corrections):
        raise WoonError("every renamed recording requires its existing correction reference")
    if redirects:
        if bundle.source_owners is None or bundle.source_owners.current_path != _OWNERS:
            raise WoonError("source redirects require their existing raw-archive owners")
        _validate_source_owners(*values[_OWNERS], redirects)
    elif bundle.source_owners is not None:
        raise WoonError("source owner update requires its source redirects")
    try:
        ast.parse(values[bundle.producer.current_path][1])
    except SyntaxError as error:
        raise WoonError("recording producer candidate has invalid Python syntax") from error
    return tuple(writes)


def _matches(path: Path, content: bytes, mode: int) -> bool:
    return (
        path.resolve() == path
        and not path.is_symlink()
        and path.is_file()
        and path.read_bytes() == content
        and path.stat().st_mode & 0o777 == mode
    )


def check_recording_inputs(writes: tuple[RecordingWrite, ...]) -> None:
    for source, target, before, _after, mode in writes:
        if not _matches(source, before, mode):
            raise WoonError(f"recording changed before write; replan: {source}")
        if target != source and (target.exists() or target.is_symlink()):
            raise WoonError(f"recording destination must be absent: {target}")


def _publish_new(path: Path, content: bytes, mode: int) -> None:
    """Publish complete bytes without replacing an existing destination (including rollback)."""
    descriptor, name = tempfile.mkstemp(prefix=".recording-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        os.link(temporary, path, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)


def apply_recording_writes(
    writes: tuple[RecordingWrite, ...],
    owned: list[RecordingWrite],
) -> None:
    for entry in writes:
        source, target, before, after, mode = entry
        check_recording_inputs((entry,))
        if source == target:
            atomic_write(target, after, mode=mode)
            owned.append(entry)
        else:
            _publish_new(target, after, mode)
            owned.append(entry)
            if not _matches(source, before, mode):
                raise WoonError(f"recording source changed before removal: {source}")
            source.unlink()


def verify_recording_writes(writes: tuple[RecordingWrite, ...]) -> None:
    for source, target, _before, after, mode in writes:
        if not _matches(target, after, mode) or (
            source != target and (source.exists() or source.is_symlink())
        ):
            raise WoonError(f"recording verification failed; preserve concurrent edit: {target}")


def restore_recording_writes(owned: list[RecordingWrite]) -> None:
    conflicts = []
    for source, target, before, after, mode in reversed(owned):
        try:
            if source != target:
                if not source.exists() and not source.is_symlink():
                    _publish_new(source, before, mode)
                if not _matches(source, before, mode):
                    raise WoonError("original path changed")
            if not _matches(target, after, mode):
                raise WoonError("written target changed")
            if source == target:
                atomic_write(source, before, mode=mode)
            else:
                target.unlink()
        except (OSError, WoonError):
            conflicts.append(str(source))
    if conflicts:
        raise WoonError("concurrent recording edits preserved: " + ", ".join(conflicts))
