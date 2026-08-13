"""Deterministic source-schema compiler for private LLM Wiki pages.

The compiler keeps its editable inputs outside ``wiki/``.  A compiled page is
therefore recoverable from a source record, accepted claim records, and a page
specification instead of becoming an untracked AI rewrite.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.domain import DocumentMetadata

FRONTMATTER = re.compile(r"\A---\n(?P<yaml>[\s\S]*?)\n---\n?(?P<body>[\s\S]*)\Z")
H1 = re.compile(r"\A(?:\n)*#\s+(?P<title>.+?)\s*\n(?:\n)?")
COMPILED_KEY = "llm_wiki"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CompiledWikiSettings:
    """Resolved repository-relative locations for the compiler inputs and outputs."""

    vault: Path
    output_root: Path
    sources_path: Path
    claims_path: Path
    pages_path: Path
    relations_path: Path
    receipts_path: Path
    review_queue_path: Path


@dataclass(frozen=True, slots=True)
class CompileReport:
    """Observable result of one deterministic compiler invocation."""

    compiled: int
    unchanged: int
    page_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Result of converting direct Markdown pages into compiler inputs."""

    migrated: int
    skipped: int
    compiled: int
    unchanged: int


@dataclass(frozen=True, slots=True)
class CompilationAudit:
    """Compiler health used by MCP, CLI, and the retrieval freshness gate."""

    pages: int
    receipts: int
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.errors


class CompiledWiki:
    """Compile and audit one private source-schema Wiki without model calls."""

    def __init__(self, settings: CompiledWikiSettings) -> None:
        self._settings = settings
        self._last_input_state: tuple[tuple[str, int, int], ...] | None = None

    @property
    def enabled(self) -> bool:
        return True

    def migrate(self) -> MigrationReport:
        """Capture existing Wiki Markdown as lossless source-backed page specs once."""

        if any(path.exists() for path in self._input_paths()):
            raise WoonError(
                "compiled Wiki inputs already exist; refuse to replace them during migration"
            )
        pages = self._discover_pages()
        if not pages:
            raise WoonError("compiled Wiki migration found no Markdown pages")

        source_records: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        specs: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for relative, text in pages:
            frontmatter, title, body = _parse_markdown(text, relative)
            page_id = relative.with_suffix("").as_posix()
            source_id = f"source://legacy-wiki/{quote(relative.as_posix(), safe='/._-')}"
            claim_id = f"claim://legacy-wiki/{quote(page_id, safe='/._-')}"
            source_records.append(
                {
                    "source_id": source_id,
                    "kind": "legacy-wiki",
                    "locator": relative.as_posix(),
                    "original_sha256": _sha256_text(text),
                    "normalized_sha256": _sha256_text(_normalize(body)),
                    "privacy": str(frontmatter.get("access", "local-only")),
                    "lifecycle": "compiled",
                    "title": title,
                    "body": body,
                }
            )
            claims.append(
                {
                    "claim_id": claim_id,
                    "kind": "legacy-document",
                    "status": "accepted",
                    "statement": title,
                    "source_ids": [source_id],
                    "markdown": "",
                }
            )
            specs.append(
                {
                    "page_id": page_id,
                    "output_path": relative.as_posix(),
                    "title": title,
                    "frontmatter": frontmatter,
                    "source_ids": [source_id],
                    "claim_ids": [claim_id],
                    "render": {"kind": "source-body", "source_id": source_id},
                }
            )
            relations.extend(_relations_for(page_id, frontmatter))

        snapshot = self.snapshot_inputs()
        try:
            _write_yaml(
                self._settings.sources_path,
                {"version": SCHEMA_VERSION, "sources": source_records},
            )
            _write_yaml(self._settings.claims_path, {"version": SCHEMA_VERSION, "claims": claims})
            _write_yaml(self._settings.pages_path, {"version": SCHEMA_VERSION, "pages": specs})
            _write_yaml(
                self._settings.relations_path,
                {"version": SCHEMA_VERSION, "relations": relations},
            )
            _write_yaml(
                self._settings.review_queue_path,
                {"version": SCHEMA_VERSION, "items": []},
            )
            _write_yaml(self._settings.receipts_path, {"version": SCHEMA_VERSION, "receipts": []})
            report = self.compile(force=True)
        except Exception:
            self.restore_inputs(snapshot)
            raise
        return MigrationReport(
            migrated=len(pages),
            skipped=0,
            compiled=report.compiled,
            unchanged=report.unchanged,
        )

    def compile(self, *, force: bool = False, page_ids: tuple[str, ...] = ()) -> CompileReport:
        """Compile stale or requested pages only after validating every input relation."""

        sources, claims, pages, receipts = self._load_inputs()
        selected = set(page_ids)
        unknown = selected.difference(pages)
        if unknown:
            raise WoonError(f"compiled Wiki page spec not found: {sorted(unknown)[0]}")
        compiled = 0
        unchanged = 0
        changed_ids: list[str] = []
        updated_receipts = dict(receipts)
        writes: list[tuple[Path, str]] = []

        for page_id in sorted(pages):
            if selected and page_id not in selected:
                continue
            page = pages[page_id]
            source_records = self._page_sources(page, sources)
            claim_records = self._page_claims(page, claims)
            _validate_page(page, source_records, claim_records)
            input_sha256 = _input_hash(page, source_records, claim_records)
            output = _render_page(page, source_records, claim_records, input_sha256)
            output_path = _inside(
                self._settings.output_root, page["output_path"], "page output_path"
            )
            existing_hash = (
                _sha256_bytes(output_path.read_bytes()) if output_path.is_file() else None
            )
            receipt = receipts.get(page_id)
            expected_hash = _sha256_text(output)
            if (
                not force
                and receipt is not None
                and receipt.get("input_sha256") == input_sha256
                and receipt.get("output_sha256") == expected_hash
                and existing_hash == expected_hash
            ):
                unchanged += 1
                continue
            writes.append((output_path, output))
            updated_receipts[page_id] = {
                "page_id": page_id,
                "compiler": "woon-core/llm-wiki-v1",
                "input_sha256": input_sha256,
                "output_sha256": expected_hash,
                "source_ids": [record["source_id"] for record in source_records],
                "claim_ids": [record["claim_id"] for record in claim_records],
                "checks": [
                    "schema",
                    "source-provenance",
                    "accepted-claims",
                    "frontmatter-h1",
                    "privacy",
                ],
            }
            compiled += 1
            changed_ids.append(page_id)

        relation_records = _expected_relations(pages)
        try:
            current_relations = _load_yaml_list(self._settings.relations_path, "relations")
            relation_changed = current_relations != relation_records
        except WoonError:
            relation_changed = True
        if not writes and not relation_changed:
            self._last_input_state = None
            return CompileReport(compiled, unchanged, tuple(changed_ids))

        snapshots = [(path, path.read_bytes() if path.is_file() else None) for path, _ in writes]
        receipt_snapshot = (
            self._settings.receipts_path.read_bytes()
            if self._settings.receipts_path.is_file()
            else None
        )
        relation_snapshot = (
            self._settings.relations_path.read_bytes()
            if self._settings.relations_path.is_file()
            else None
        )
        try:
            for path, output in writes:
                atomic_write(path, output.encode("utf-8"))
            if relation_changed:
                _write_yaml(
                    self._settings.relations_path,
                    {"version": SCHEMA_VERSION, "relations": relation_records},
                )
            if writes:
                _write_yaml(
                    self._settings.receipts_path,
                    {
                        "version": SCHEMA_VERSION,
                        "receipts": [updated_receipts[key] for key in sorted(updated_receipts)],
                    },
                )
        except Exception:
            for path, snapshot in snapshots:
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, snapshot)
            if receipt_snapshot is None:
                self._settings.receipts_path.unlink(missing_ok=True)
            else:
                atomic_write(self._settings.receipts_path, receipt_snapshot)
            if relation_snapshot is None:
                self._settings.relations_path.unlink(missing_ok=True)
            else:
                atomic_write(self._settings.relations_path, relation_snapshot)
            raise
        self._last_input_state = None
        return CompileReport(compiled, unchanged, tuple(changed_ids))

    def archive(
        self,
        metadata: DocumentMetadata,
        body: str,
        source_session_ids: tuple[str, ...],
    ) -> CompileReport:
        """Turn a conversation body into source and accepted claim compiler inputs."""

        sources, claims, pages, _ = self._load_inputs()
        page_id = f"wiki/canonical/{metadata.canonical_id}"
        body_hash = _sha256_text(_normalize(body))
        source_id = f"source://conversation/{metadata.canonical_id}/{body_hash[:24]}"
        claim_id = f"claim://conversation/{metadata.canonical_id}/{body_hash[:24]}"
        sources[source_id] = {
            "source_id": source_id,
            "kind": "conversation",
            "locator": metadata.canonical_id,
            "original_sha256": body_hash,
            "normalized_sha256": body_hash,
            "privacy": "local-only",
            "lifecycle": "compiled",
            "title": metadata.title,
            "body": body.rstrip() + "\n",
            "source_session_ids": list(source_session_ids),
        }
        claims[claim_id] = {
            "claim_id": claim_id,
            "kind": "conversation-summary",
            "status": "accepted",
            "statement": metadata.summary,
            "source_ids": [source_id],
            "markdown": body.rstrip() + "\n",
        }
        frontmatter = _canonical_frontmatter(metadata)
        frontmatter["source_ids"] = [source_id]
        pages[page_id] = {
            "page_id": page_id,
            "output_path": f"canonical/{metadata.canonical_id}.md",
            "title": metadata.title,
            "frontmatter": frontmatter,
            "source_ids": [source_id],
            "claim_ids": [claim_id],
            "render": {"kind": "claims"},
        }
        self._write_inputs(sources, claims, pages)
        return self.compile(page_ids=(page_id,))

    def snapshot_inputs(self) -> dict[Path, bytes | None]:
        """Capture small compiler catalogs before a transactional canonical mutation."""

        return {
            path: path.read_bytes() if path.is_file() else None
            for path in (*self._input_paths(), self._settings.review_queue_path)
        }

    def restore_inputs(self, snapshot: dict[Path, bytes | None]) -> None:
        """Restore a previously captured compiler catalog without touching raw sources."""

        for path, content in snapshot.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, content)
        self._last_input_state = None

    def audit(self) -> CompilationAudit:
        """Check that every page can be reproduced from valid compiler inputs."""

        errors: list[str] = []
        try:
            sources, claims, pages, receipts = self._load_inputs()
        except WoonError as error:
            return CompilationAudit(0, 0, (str(error),))
        outputs: set[str] = set()
        for page_id, page in sorted(pages.items()):
            try:
                source_records = self._page_sources(page, sources)
                claim_records = self._page_claims(page, claims)
                _validate_page(page, source_records, claim_records)
                relative = page["output_path"]
                if relative in outputs:
                    raise WoonError(f"duplicate compiled output_path: {relative}")
                outputs.add(relative)
                path = _inside(self._settings.output_root, relative, "page output_path")
                if not path.is_file():
                    raise WoonError("compiled output is missing")
                input_sha256 = _input_hash(page, source_records, claim_records)
                receipt = receipts.get(page_id)
                if receipt is None:
                    raise WoonError("compiled receipt is missing")
                if receipt.get("input_sha256") != input_sha256:
                    raise WoonError("compiled output is stale for its source or claims")
                if receipt.get("output_sha256") != _sha256_bytes(path.read_bytes()):
                    raise WoonError("compiled output bytes differ from its receipt")
                _parse_markdown(path.read_text(encoding="utf-8"), Path(relative))
            except (OSError, UnicodeError, WoonError) as error:
                errors.append(f"{page_id}: {error}")
        for page_id in sorted(set(receipts).difference(pages)):
            errors.append(f"{page_id}: receipt has no page spec")
        try:
            current_relations = _load_yaml_list(self._settings.relations_path, "relations")
            if current_relations != _expected_relations(pages):
                errors.append("relations catalog is stale for current page specs")
        except WoonError as error:
            errors.append(str(error))
        return CompilationAudit(len(pages), len(receipts), tuple(errors))

    def assert_current(self) -> None:
        """Fail closed if compiler inputs changed without a matching build receipt."""

        state = self._input_state()
        if state == self._last_input_state:
            return
        audit = self.audit()
        if not audit.complete:
            raise WoonError(f"compiled Wiki is stale: {audit.errors[0]}")
        self._last_input_state = state

    def _discover_pages(self) -> list[tuple[Path, str]]:
        if not self._settings.output_root.is_dir():
            return []
        pages: list[tuple[Path, str]] = []
        for path in sorted(self._settings.output_root.rglob("*.md")):
            relative = path.relative_to(self._settings.output_root)
            if relative.parts and relative.parts[0].startswith("_"):
                continue
            pages.append((relative, path.read_text(encoding="utf-8")))
        return pages

    def _load_inputs(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        raw_sources = _load_yaml_list(self._settings.sources_path, "sources")
        raw_claims = _load_yaml_list(self._settings.claims_path, "claims")
        raw_pages = _load_yaml_list(self._settings.pages_path, "pages")
        raw_receipts = _load_yaml_list(self._settings.receipts_path, "receipts")
        sources = _indexed(raw_sources, "source_id", "source")
        claims = _indexed(raw_claims, "claim_id", "claim")
        pages = _indexed(raw_pages, "page_id", "page")
        receipts = _indexed(raw_receipts, "page_id", "receipt")
        return sources, claims, pages, receipts

    def _write_inputs(
        self,
        sources: dict[str, dict[str, Any]],
        claims: dict[str, dict[str, Any]],
        pages: dict[str, dict[str, Any]],
    ) -> None:
        _write_yaml(
            self._settings.sources_path,
            {"version": SCHEMA_VERSION, "sources": [sources[key] for key in sorted(sources)]},
        )
        _write_yaml(
            self._settings.claims_path,
            {"version": SCHEMA_VERSION, "claims": [claims[key] for key in sorted(claims)]},
        )
        _write_yaml(
            self._settings.pages_path,
            {"version": SCHEMA_VERSION, "pages": [pages[key] for key in sorted(pages)]},
        )

    def _page_sources(
        self, page: dict[str, Any], sources: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        identifiers = _string_list(page.get("source_ids"), "page source_ids")
        records: list[dict[str, Any]] = []
        for identifier in identifiers:
            record = sources.get(identifier)
            if record is None:
                raise WoonError(f"page references missing source_id {identifier!r}")
            records.append(record)
        return records

    def _page_claims(
        self, page: dict[str, Any], claims: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        identifiers = _string_list(page.get("claim_ids"), "page claim_ids")
        records: list[dict[str, Any]] = []
        for identifier in identifiers:
            record = claims.get(identifier)
            if record is None:
                raise WoonError(f"page references missing claim_id {identifier!r}")
            records.append(record)
        return records

    def _input_paths(self) -> tuple[Path, ...]:
        return (
            self._settings.sources_path,
            self._settings.claims_path,
            self._settings.pages_path,
            self._settings.relations_path,
            self._settings.receipts_path,
        )

    def _input_state(self) -> tuple[tuple[str, int, int], ...]:
        state: list[tuple[str, int, int]] = []
        for path in self._input_paths():
            if path.is_file():
                stat = path.stat()
                state.append(
                    (
                        path.relative_to(self._settings.vault).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                )
            else:
                state.append((path.relative_to(self._settings.vault).as_posix(), -1, -1))
        return tuple(state)


def _validate_page(
    page: dict[str, Any], sources: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> None:
    page_id = _required_string(page, "page_id")
    output_path = _required_string(page, "output_path")
    if output_path.startswith("/") or ".." in Path(output_path).parts:
        raise WoonError("page output_path must be a safe relative Markdown path")
    if not output_path.endswith(".md"):
        raise WoonError("page output_path must end with .md")
    title = _required_string(page, "title")
    frontmatter = page.get("frontmatter")
    if not isinstance(frontmatter, dict):
        raise WoonError("page frontmatter must be a mapping")
    if str(frontmatter.get("title", "")).strip() != title:
        raise WoonError("page frontmatter title must match page title")
    render = page.get("render")
    if not isinstance(render, dict):
        raise WoonError("page render must be a mapping")
    kind = render.get("kind")
    if kind not in {"source-body", "claims"}:
        raise WoonError("page render.kind must be source-body or claims")
    if kind == "source-body":
        source_id = _required_string(render, "source_id")
        if source_id not in {record.get("source_id") for record in sources}:
            raise WoonError("source-body render source_id must be included in page source_ids")
    for source in sources:
        _validate_source(source)
    for claim in claims:
        _validate_claim(claim, sources)
    access = str(frontmatter.get("access", "local-only"))
    if access == "public" and any(str(source.get("privacy")) != "public" for source in sources):
        raise WoonError("public compiled page requires public source provenance")
    if not page_id.endswith(output_path.removesuffix(".md")) and not page_id.startswith("wiki/"):
        raise WoonError("page_id must identify a Wiki output")


def _validate_source(source: dict[str, Any]) -> None:
    _required_string(source, "source_id")
    _required_string(source, "kind")
    _required_string(source, "locator")
    _required_digest(source, "original_sha256")
    _required_digest(source, "normalized_sha256")
    if source.get("privacy") not in {"local-only", "private", "public", "external-private"}:
        raise WoonError("source privacy is invalid")
    if source.get("lifecycle") not in {"captured", "compiled", "archived"}:
        raise WoonError("source lifecycle is invalid")
    if not isinstance(source.get("body"), str):
        raise WoonError("source body must be a string")


def _validate_claim(claim: dict[str, Any], sources: list[dict[str, Any]]) -> None:
    _required_string(claim, "claim_id")
    _required_string(claim, "kind")
    _required_string(claim, "statement")
    if claim.get("status") != "accepted":
        raise WoonError("compiled page may only use accepted claims")
    source_ids = _string_list(claim.get("source_ids"), "claim source_ids")
    known = {str(source.get("source_id")) for source in sources}
    if not set(source_ids).issubset(known):
        raise WoonError("claim evidence source_id must belong to its page")
    if not isinstance(claim.get("markdown"), str):
        raise WoonError("claim markdown must be a string")


def _render_page(
    page: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    input_sha256: str,
) -> str:
    render = page["render"]
    if render["kind"] == "source-body":
        source_id = render["source_id"]
        body = next(record["body"] for record in sources if record["source_id"] == source_id)
    else:
        body = "\n\n".join(
            record["markdown"].strip() for record in claims if record["markdown"].strip()
        )
    if not body.strip():
        raise WoonError("compiled page body must not be empty")
    frontmatter = dict(page["frontmatter"])
    frontmatter[COMPILED_KEY] = {
        "schema_version": SCHEMA_VERSION,
        "build_id": input_sha256[:24],
        "page_id": page["page_id"],
    }
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{yaml_text}---\n\n# {page['title']}\n\n{body.rstrip()}\n"


def _input_hash(
    page: dict[str, Any], sources: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> str:
    payload = {
        "version": SCHEMA_VERSION,
        "page": page,
        "sources": sources,
        "claims": claims,
    }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return _sha256_text(serialized)


def _canonical_frontmatter(metadata: DocumentMetadata) -> dict[str, Any]:
    return {
        "type": "Wiki",
        "canonical_id": metadata.canonical_id,
        "title": metadata.title,
        "domain": metadata.domain,
        "summary": metadata.summary,
        "status": "Canonical",
        "publish": False,
        "access": "local-only",
        "difficulty": metadata.difficulty,
        "prerequisites": list(metadata.prerequisites),
        "next_concepts": list(metadata.next_concepts),
        "related": list(metadata.related),
    }


def _relations_for(page_id: str, frontmatter: dict[str, Any]) -> list[dict[str, str]]:
    relations: list[dict[str, str]] = []
    for field, relation_type in (
        ("prerequisites", "requires"),
        ("next_concepts", "next"),
        ("related", "related"),
    ):
        raw = frontmatter.get(field)
        if not isinstance(raw, list):
            continue
        for target in raw:
            if isinstance(target, str) and target.strip():
                relations.append(
                    {"from_page_id": page_id, "type": relation_type, "to_id": target.strip()}
                )
    return relations


def _expected_relations(pages: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Derive the review graph from page specs instead of trusting a manual copy."""

    relations: list[dict[str, str]] = []
    for page_id in sorted(pages):
        frontmatter = pages[page_id].get("frontmatter")
        if isinstance(frontmatter, dict):
            relations.extend(_relations_for(page_id, frontmatter))
    return sorted(relations, key=lambda item: (item["from_page_id"], item["type"], item["to_id"]))


def _parse_markdown(text: str, relative: Path) -> tuple[dict[str, Any], str, str]:
    match = FRONTMATTER.fullmatch(text)
    if match is None:
        raise WoonError(f"{relative.as_posix()}: Wiki source is missing YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as error:
        raise WoonError(f"{relative.as_posix()}: invalid YAML frontmatter") from error
    if not isinstance(frontmatter, dict):
        raise WoonError(f"{relative.as_posix()}: frontmatter must be a mapping")
    h1 = H1.match(match.group("body"))
    if h1 is None:
        raise WoonError(f"{relative.as_posix()}: Wiki source is missing H1")
    title = str(frontmatter.get("title", "")).strip()
    if not title or title != h1.group("title").strip():
        raise WoonError(f"{relative.as_posix()}: frontmatter title must match H1")
    return frontmatter, title, match.group("body")[h1.end() :].rstrip() + "\n"


def _load_yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise WoonError(f"compiled Wiki {key} catalog is missing: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise WoonError(f"load compiled Wiki {key}: {error}") from error
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        raise WoonError(f"compiled Wiki {key} catalog has unsupported version")
    records = raw.get(key)
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise WoonError(f"compiled Wiki {key} catalog must contain a mapping list")
    return [dict(item) for item in records]


def _indexed(records: list[dict[str, Any]], key: str, name: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        identifier = _required_string(record, key)
        if identifier in indexed:
            raise WoonError(f"duplicate compiled Wiki {name} identifier: {identifier}")
        indexed[identifier] = record
    return indexed


def _required_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"compiled Wiki record requires non-empty {key}")
    return value.strip()


def _string_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise WoonError(f"{field} must be a non-empty string list")
    return [item.strip() for item in value]


def _required_digest(record: dict[str, Any], key: str) -> None:
    value = record.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise WoonError(f"compiled Wiki record requires SHA-256 {key}")


def _inside(root: Path, relative: str, field: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise WoonError(f"{field} must be a safe relative path")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise WoonError(f"{field} escapes the compiler output root") from error
    return resolved


def _write_yaml(path: Path, value: dict[str, Any]) -> None:
    data = yaml.safe_dump(
        value, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).encode("utf-8")
    atomic_write(path, data)


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize(value: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").split("\n")
    ).strip()
    return normalized + "\n"
