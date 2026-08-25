"""Local stdio MCP server for canonical knowledge retrieval and updates."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from woon_core.knowledge.codex_daily_digest import (
    entries_from_records as daily_digest_entries_from_records,
)
from woon_core.knowledge.codex_daily_digest import (
    record_codex_daily_digest,
    record_daily_digest_from_codex_ledger,
)
from woon_core.knowledge.codex_knowledge import (
    entries_from_records as codex_knowledge_entries_from_records,
)
from woon_core.knowledge.codex_knowledge import record_codex_knowledge_entries
from woon_core.knowledge.context_bundle import build_wiki_context_bundle
from woon_core.knowledge.domain import DocumentMetadata
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.mail_schedule_automation import (
    record_mail_schedule_candidates,
    submissions_from_records,
)
from woon_core.knowledge.service import KnowledgeService

# mcp 1.29.0 leaves the generic lifespan annotation unresolved until explicitly rebuilt.
FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "Woon Canonical Knowledge",
    instructions=(
        "Search and update the user's private canonical Markdown vault. "
        "Search with two to four discriminative topic terms, excluding generic intent words. "
        "When a map result points to a more specific concept, search that concept "
        "and read its excerpt. "
        "Read an existing document before updating it and pass its revision. "
        "Never create blog, portfolio, or alternate output variants."
    ),
    json_response=True,
)


@lru_cache(maxsize=1)
def _service() -> KnowledgeService:
    """Reuse one stat-aware service for the lifetime of the stdio MCP process."""

    return build_knowledge_service()[1]


@mcp.tool(
    name="woon_knowledge_search",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def search_knowledge(query: str, limit: int = 5) -> dict[str, object]:
    """Search all configured knowledge and return bounded section hits with stable IDs."""

    service = _service()
    results = service.search(query, limit)
    return {"query": query, "count": len(results), "results": [asdict(item) for item in results]}


@mcp.tool(
    name="woon_knowledge_get",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_knowledge(canonical_id: str) -> dict[str, object]:
    """Read one complete canonical document before answering or preparing an update."""

    service = _service()
    document = service.get(canonical_id)
    return {
        "metadata": asdict(document.metadata),
        "body": document.body,
        "relative_path": document.relative_path,
        "revision": document.revision,
    }


@mcp.tool(
    name="woon_knowledge_read_excerpt",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def read_knowledge_excerpt(document_id: str, chunk_id: str) -> dict[str, object]:
    """Read only the matched Markdown section instead of loading an entire source document."""

    service = _service()
    return asdict(service.read_excerpt(document_id, chunk_id))


@mcp.tool(
    name="woon_knowledge_context",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def get_wiki_context(
    subject: str, max_items: int = 24, max_chars: int = 30_000
) -> dict[str, object]:
    """Read one Wiki subject with its ancestors, children, history, and evidence."""

    settings, _ = build_knowledge_service()
    return build_wiki_context_bundle(
        settings.vault,
        subject,
        max_items=max_items,
        max_chars=max_chars,
    ).to_record()


@mcp.tool(
    name="woon_knowledge_archive_conversation",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def archive_conversation(
    canonical_id: str,
    title: str,
    domain: str,
    summary: str,
    purpose: str,
    body: str,
    difficulty: str = "foundation",
    prerequisites: list[str] | None = None,
    next_concepts: list[str] | None = None,
    related: list[str] | None = None,
    source_session_ids: list[str] | None = None,
    expected_revision: str | None = None,
    archive_origin: str = "manual-reviewed",
    approved_review_id: str | None = None,
) -> dict[str, object]:
    """Create or optimistically replace one deduplicated canonical document.

    canonical_id must be a lowercase ``domain/slug`` path and domain must match
    its first segment. purpose records why this knowledge is being retained and
    which future question, decision, or output it should support. prerequisites,
    next_concepts, and related accept only existing canonical IDs in the same
    form; use empty lists when no verified canonical relationship exists. They
    do not accept display titles or search keywords.
    """

    service = _service()
    result = service.archive(
        DocumentMetadata(
            canonical_id=canonical_id,
            title=title,
            domain=domain,
            summary=summary,
            purpose=purpose,
            difficulty=difficulty,
            prerequisites=tuple(prerequisites or ()),
            next_concepts=tuple(next_concepts or ()),
            related=tuple(related or ()),
            source_ids=tuple(source_session_ids or ()),
        ),
        body,
        expected_revision,
        archive_origin=archive_origin,
        approved_review_id=approved_review_id,
    )
    return {
        "created": result.created,
        "changed": result.changed,
        "canonical_id": result.document.metadata.canonical_id,
        "relative_path": result.document.relative_path,
        "revision": result.document.revision,
    }


@mcp.tool(
    name="woon_knowledge_reindex",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def reindex_knowledge() -> dict[str, object]:
    """Rebuild the replaceable local search index from canonical Markdown files, then exit."""

    # A reindex is also the explicit configuration-refresh boundary.  Without
    # clearing the cached service first, a newly added search root is silently
    # ignored until the stdio server restarts, while the returned settings
    # misleadingly describe the new configuration.
    _service.cache_clear()
    settings, service = build_knowledge_service()
    count = service.reindex()
    _service.cache_clear()
    return {"indexed": count, "adapter": settings.search_adapter}


@mcp.tool(
    name="woon_knowledge_compile",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def compile_knowledge(force: bool = False) -> dict[str, object]:
    """Compile source records, accepted claims, and page specs before retrieval uses them."""

    return asdict(_service().compile(force=force))


@mcp.tool(
    name="woon_knowledge_compile_audit",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def audit_compiled_knowledge() -> dict[str, object]:
    """Verify every compiled Wiki page has valid provenance and a matching receipt."""

    audit = _service().compilation_audit()
    return {"status": "ok" if audit.complete else "invalid", **asdict(audit)}


@mcp.tool(
    name="woon_automation_record_mail_schedule_candidates",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def record_mail_schedule_candidates_run(
    run_token: str,
    candidates: list[dict[str, object]],
) -> dict[str, object]:
    """Record a mail polling window with zero or more minimized candidates.

    Call this exactly once after reading only allowlisted mail. Pass ``[]`` when
    no new actionable item exists. The tool writes a hash-only receipt and
    never reads or writes Apple Calendar; Calendar application remains a
    separate policy-authorized local action.
    """

    result = record_mail_schedule_candidates(
        build_knowledge_service()[0].vault,
        run_token=run_token,
        submissions=submissions_from_records(candidates),
    )
    return asdict(result)


@mcp.tool(
    name="woon_automation_record_codex_daily_record",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def record_codex_daily_digest_run(
    day: str,
    entries: list[dict[str, object]],
) -> dict[str, object]:
    """Record one Korean block in the canonical daily record from opted-in Codex conclusions.

    ``entries`` contain a short ``kind``, ``title``, ``summary``, optional
    intent, readable question/answer/outcome exchanges, human attachment
    labels, and links to existing canonical ``wiki/`` documents.  Exact
    opted-in user/final-answer evidence is recorded separately through the
    local source-archive CLI; never pass system/developer text, tool output,
    reasoning, tokens, or opaque locators here.
    """

    try:
        target_day = date.fromisoformat(day)
    except ValueError as error:
        raise ValueError("day must use YYYY-MM-DD") from error
    result = record_codex_daily_digest(
        build_knowledge_service()[0].vault,
        day=target_day,
        entries=daily_digest_entries_from_records(entries),
    )
    return asdict(result)


@mcp.tool(
    name="woon_automation_record_codex_knowledge_entries",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def record_codex_knowledge_entries_run(
    source_range: str,
    day: str,
    entries: list[dict[str, object]],
    input_state: str = "processed",
) -> dict[str, object]:
    """Record one day of readable, transcript-free Codex topic summaries.

    Each entry has one Korean category such as ``활동``, ``일정``, ``인물``,
    ``학습``, ``개념``, ``커리어``, ``창작``, ``자료`` or
    ``프로젝트`` plus a short title, summary, intent, and readable exchanges
    containing the actual question, summarized answer, outcome, and human
    attachment labels.  Every organized entry updates the local daily ledger.
    Set ``wiki_update=true`` only for a reusable,
    stable subject; then the same run creates or updates one canonical
    ``wiki/`` document.  Every ``wiki_update=true`` entry must provide exactly
    one identity proof: ``wiki_subject_path`` for an existing canonical subject,
    or ``new_wiki_reason`` after searching the Wiki and finding no matching
    subject.  This prevents sentence-shaped duplicates from bypassing an
    existing project, book, resource, person, or concept page.  A one-time event keeps
    ``wiki_update=false`` and does not become a subject page.  Use
    ``disposition=review`` with a short ``review_reason`` when classification or
    identity needs a person; it creates no Wiki, Calendar, person, project, book or
    resource side effect.  Use ``disposition=excluded`` for advertisements,
    system/tool/reasoning text, secrets, raw private originals and Novel text;
    excluded input is not validated, hashed or persisted.  Explicitly named
    books may define ``contents`` with exactly one existing genre keyword. Non-book
    source references may define ``contents`` only with one existing
    ``resource_keyword`` and an ``official_url``; the run adds only that hyperlink
    to the matching resource topic and never creates a content/resource entity card.
    Meaning from every source is merged into the existing subject Wiki first.
    Explicit finite outcomes may define ``projects``. Existing matching subjects
    are reused instead of duplicated. Pass ``input_state=unavailable`` with
    ``entries=[]`` when the persisted session for that day is absent, so a blank
    note explains its cause.  Do not pass raw chat text, system/developer text,
    tool output, reasoning, credentials, opaque locators, private originals, or
    Novel text.
    """

    try:
        target_day = date.fromisoformat(day)
    except ValueError as error:
        raise ValueError("day must use YYYY-MM-DD") from error

    result = record_codex_knowledge_entries(
        build_knowledge_service()[0].vault,
        source_range=source_range,
        day=target_day,
        entries=codex_knowledge_entries_from_records(entries),
        input_state=input_state,  # type: ignore[arg-type]
    )
    return asdict(result)


@mcp.tool(
    name="woon_automation_materialize_codex_daily_record",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def materialize_codex_daily_digest_run(
    day: str,
) -> dict[str, object]:
    """Render one daily record from its semantic ledger and local source archive.

    The ledger owns the topic summary and canonical Wiki relationships.  The
    local-only archive supplies the allowed user questions, assistant final
    answers, timestamps, and attachment labels.  The daily record renders only
    a compact question index; exact answers stay in the local archive and the
    semantic conclusions remain in the normal daily sections.  This
    materializer does not reread Codex APIs or create a second Wiki.
    """

    try:
        target_day = date.fromisoformat(day)
    except ValueError as error:
        raise ValueError("day must use YYYY-MM-DD") from error
    result = record_daily_digest_from_codex_ledger(
        build_knowledge_service()[0].vault,
        day=target_day,
    )
    return asdict(result)


@mcp.tool(
    name="woon_knowledge_audit",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def audit_knowledge() -> dict[str, object]:
    """Check identity, duplicate titles, paths, and learning-relationship integrity."""

    service = _service()
    errors = service.audit()
    return {"status": "ok" if not errors else "invalid", "errors": errors}


@mcp.tool(
    name="woon_knowledge_history",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def knowledge_history(canonical_id: str, limit: int = 20) -> dict[str, object]:
    """List Git recovery points for one canonical document."""

    service = _service()
    entries = service.history(canonical_id, limit)
    return {"canonical_id": canonical_id, "history": [asdict(item) for item in entries]}


@mcp.tool(
    name="woon_knowledge_restore",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def restore_knowledge(
    canonical_id: str,
    git_revision: str,
    expected_revision: str,
    confirmed: bool,
) -> dict[str, object]:
    """Restore one document from Git after explicit confirmation and revision checks."""

    service = _service()
    result = service.restore(
        canonical_id,
        git_revision,
        expected_revision,
        confirmed=confirmed,
    )
    return {
        "changed": result.changed,
        "canonical_id": result.document.metadata.canonical_id,
        "relative_path": result.document.relative_path,
        "revision": result.document.revision,
    }


def main() -> None:
    """Run only while an MCP client owns the local stdio connection."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
