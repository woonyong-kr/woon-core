"""MCP entry point for the local person index."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from woon_core.errors import WoonError
from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.people.factory import build_person_service
from woon_core.people.private_history import PrivatePersonHistoryService
from woon_core.people.service import PersonIdentityIdentifierInput, PersonService

# mcp 1.29.0 leaves the generic lifespan annotation unresolved until explicitly rebuilt.
FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "Woon People",
    instructions=(
        "Use person cards only for explicit or repeatedly verified relationships. "
        "Keep ownership as a stable ID, add roles and evidence before linking a person, "
        "and never expose Novel or private-original text. The private-history sync reads "
        "only an explicit local ledger and returns counts, never source contents."
    ),
    json_response=True,
)


def _service() -> PersonService:
    return build_person_service()


@mcp.tool(
    name="woon_people_find",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def find_people(query: str) -> dict[str, object]:
    """Find reusable general-scope person cards without searching private Novel identities."""

    cards = _service().find(query)
    return {"query": query, "count": len(cards), "people": [asdict(card) for card in cards]}


@mcp.tool(
    name="woon_people_documents",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def documents_for_person(person_id: str) -> dict[str, object]:
    """List deliberate person links and the Vault owner's default-owned documents."""

    documents = _service().documents_for(person_id)
    return {
        "person_id": person_id,
        "count": len(documents),
        "documents": [asdict(item) for item in documents],
    }


@mcp.tool(
    name="woon_people_upsert_card",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_person_card(
    person_id: str,
    title: str,
    person_kind: str,
    relationship_to_owner: str,
    purpose: str,
    creation_basis: str,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Create a person dashboard only from an explicit request or repeated evidence."""

    result = _service().upsert_card(
        person_id=person_id,
        title=title,
        person_kind=person_kind,
        relationship_to_owner=relationship_to_owner,
        purpose=purpose,
        creation_basis=creation_basis,
        expected_revision=expected_revision,
    )
    return {"created": result.created, "changed": result.changed, "person": asdict(result.card)}


@mcp.tool(
    name="woon_people_link_document",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def link_person_to_document(
    relative_path: str,
    person_id: str,
    roles: list[str],
    evidence: str,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Link a resolved person card with stated roles; compiled Wiki and Novel paths are rejected."""

    result = _service().link_document(
        relative_path=relative_path,
        person_id=person_id,
        roles=tuple(roles),
        evidence=evidence,
        expected_revision=expected_revision,
    )
    return {"changed": result.changed, "document": asdict(result.document)}


@mcp.tool(
    name="woon_people_set_identity_identifiers",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def set_identity_identifiers(
    person_id: str,
    identifiers: list[dict[str, object]],
    evidence: str,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Store only user-confirmed name forms used in local Calendar title resolution.

    When a title is ambiguous, call this only after the user identifies the
    correct existing card. Optional context_terms may disambiguate a shared name.
    """

    inputs: list[PersonIdentityIdentifierInput] = []
    for item in identifiers:
        value = item.get("value")
        context_terms = item.get("context_terms", [])
        if (
            not isinstance(value, str)
            or not isinstance(context_terms, list)
            or not all(isinstance(term, str) for term in context_terms)
        ):
            raise WoonError("identifiers need string value and optional string context_terms")
        inputs.append(
            PersonIdentityIdentifierInput(value=value, context_terms=tuple(context_terms))
        )
    result = _service().set_identity_identifiers(
        person_id=person_id,
        identifiers=tuple(inputs),
        evidence=evidence,
        expected_revision=expected_revision,
    )
    return {"changed": result.changed, "person": asdict(result.card)}


@mcp.tool(
    name="woon_people_private_history_sync",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def sync_private_history(novel_root: str) -> dict[str, object]:
    """Rebuild local-only person views from an explicit private Novel ledger.

    This never infers people from source text and never returns source paths or contents.
    """

    result = PrivatePersonHistoryService(resolve_knowledge_vault(), Path(novel_root)).sync()
    return {
        "changed": result.changed,
        "works": result.works,
        "people": result.people,
        "links": result.links,
        "candidates": result.candidates,
        "novel_work_catalog": result.novel_work_catalog_path,
        "vault_dashboard_directory": result.vault_dashboard_directory,
    }


@mcp.tool(
    name="woon_people_materialize_default_owner",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def materialize_default_owner() -> dict[str, int]:
    """Add the default Vault owner only to editable records that omit it."""

    result = _service().materialize_default_owner()
    return {"changed": result.changed, "skipped": result.skipped}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
