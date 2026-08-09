"""Stable identity for one complete canonical and read-only search snapshot."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from woon_core.knowledge.domain import IndexedDocument


def knowledge_generation(documents: Iterable[IndexedDocument]) -> str:
    """Hash document identities and revisions in deterministic order."""

    digest = hashlib.sha256()
    for document in sorted(documents, key=lambda item: item.document_id):
        digest.update(document.document_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(document.revision.encode("ascii"))
        digest.update(b"\0")
        digest.update(document.source_type.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
