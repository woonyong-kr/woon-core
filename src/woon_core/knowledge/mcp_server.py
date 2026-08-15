"""Local stdio MCP server for canonical knowledge retrieval and updates."""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from woon_core.knowledge.domain import DocumentMetadata
from woon_core.knowledge.factory import build_knowledge_service
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

    settings, _ = build_knowledge_service()
    service = _service()
    count = service.reindex()
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
