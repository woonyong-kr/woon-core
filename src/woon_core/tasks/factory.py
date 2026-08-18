"""Composition root for the local Woon task service."""

from __future__ import annotations

from pathlib import Path

from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.tasks.service import TaskService


def build_task_service(vault: Path | None = None) -> TaskService:
    """Resolve the private vault once and expose its Markdown task boundary."""

    return TaskService(vault or resolve_knowledge_vault())
