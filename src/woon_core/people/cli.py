"""Parsing helpers for the public ``woon people`` command."""

from __future__ import annotations

from pathlib import Path
from typing import TextIO

from woon_core.errors import WoonError
from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.people.factory import build_person_service
from woon_core.people.private_history import PrivatePersonHistoryService
from woon_core.people.service import PersonIdentityIdentifierInput


def run_people(arguments: list[str], output: TextIO) -> None:
    """Run a bounded local person-index command against an explicit or configured vault."""

    if not arguments:
        raise WoonError(
            "usage: woon people <find|documents|upsert|link|identify|"
            "private-history-sync|materialize-default-owner>"
        )
    command, *options = arguments
    values, positionals = _options(options)
    vault = Path(values.pop("--vault")).expanduser() if "--vault" in values else None
    if command == "private-history-sync":
        if positionals or set(values) != {"--novel-root"}:
            raise WoonError(
                "people private-history-sync requires --novel-root and optional --vault"
            )
        history_result = PrivatePersonHistoryService(
            vault or resolve_knowledge_vault(), Path(values["--novel-root"])
        ).sync()
        output.write(
            "status: ok\n"
            f"changed: {str(history_result.changed).lower()}\n"
            f"people: {history_result.people}\nlinks: {history_result.links}\n"
            f"candidates: {history_result.candidates}\n"
            f"vault_dashboard_directory: {history_result.vault_dashboard_directory}\n"
        )
        return
    service = build_person_service(vault)
    if command == "find":
        if len(positionals) != 1 or values:
            raise WoonError("people find requires one query")
        for card in service.find(positionals[0]):
            output.write(f"{card.title} ({card.person_id}) - {card.relationship_to_owner}\n")
        return
    if command == "documents":
        if len(positionals) != 1 or values:
            raise WoonError("people documents requires one person ID")
        for document in service.documents_for(positionals[0]):
            labels: list[str] = []
            if document.record_owner == positionals[0] or (
                document.record_owner
                and document.record_owner.startswith(f"[[users/{positionals[0]}/README")
            ):
                labels.append("record-owner")
            labels.extend(document.roles)
            roles = ", ".join(labels) if labels else "role-unspecified"
            output.write(f"{document.title} ({document.relative_path}) - {roles}\n")
        return
    if command == "upsert":
        required = {
            "--id",
            "--title",
            "--kind",
            "--relationship",
            "--purpose",
            "--basis",
        }
        if positionals or not required.issubset(values) or set(values).difference(required):
            raise WoonError(
                "people upsert requires --id --title --kind --relationship --purpose --basis"
            )
        upsert = service.upsert_card(
            person_id=values["--id"],
            title=values["--title"],
            person_kind=values["--kind"],
            relationship_to_owner=values["--relationship"],
            purpose=values["--purpose"],
            creation_basis=values["--basis"],
        )
        output.write(
            f"status: ok\ncreated: {str(upsert.created).lower()}\n"
            f"changed: {str(upsert.changed).lower()}\nperson: {upsert.card.relative_path}\n"
        )
        return
    if command == "link":
        required = {"--document", "--person", "--roles", "--evidence"}
        if positionals or not required.issubset(values) or set(values).difference(required):
            raise WoonError("people link requires --document --person --roles --evidence")
        link = service.link_document(
            relative_path=values["--document"],
            person_id=values["--person"],
            roles=tuple(item.strip() for item in values["--roles"].split(",") if item.strip()),
            evidence=values["--evidence"],
        )
        output.write(
            f"status: ok\nchanged: {str(link.changed).lower()}\n"
            f"document: {link.document.relative_path}\n"
        )
        return
    if command == "identify":
        required = {"--person", "--identifiers", "--evidence"}
        allowed = required | {"--context"}
        if positionals or not required.issubset(values) or set(values).difference(allowed):
            raise WoonError(
                "people identify requires --person --identifiers --evidence and optional --context"
            )
        context_terms = tuple(
            item.strip() for item in values.get("--context", "").split(",") if item.strip()
        )
        identifier_result = service.set_identity_identifiers(
            person_id=values["--person"],
            identifiers=tuple(
                PersonIdentityIdentifierInput(
                    value=item.strip(), context_terms=context_terms
                )
                for item in values["--identifiers"].split(",")
                if item.strip()
            ),
            evidence=values["--evidence"],
        )
        output.write(
            f"status: ok\nchanged: {str(identifier_result.changed).lower()}\n"
            f"person: {identifier_result.card.relative_path}\n"
        )
        return
    if command == "materialize-default-owner":
        if positionals or values:
            raise WoonError("people materialize-default-owner takes no options except --vault")
        result = service.materialize_default_owner()
        output.write(f"status: ok\nchanged: {result.changed}\nskipped: {result.skipped}\n")
        return
    raise WoonError(f"unknown people command {command!r}")


def _options(arguments: list[str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if not option.startswith("--"):
            positionals.append(option)
            index += 1
            continue
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    return values, positionals
