"""Composition root for the private person index."""

from __future__ import annotations

from pathlib import Path

from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.people.service import PersonService


def build_person_service(vault: Path | None = None) -> PersonService:
    """Resolve the private vault once and expose the local person boundary."""

    return PersonService(vault or resolve_knowledge_vault())
