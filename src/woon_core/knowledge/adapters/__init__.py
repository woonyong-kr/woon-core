"""Concrete adapters for canonical knowledge ports."""

from woon_core.knowledge.adapters.filesystem import MarkdownDocumentRepository
from woon_core.knowledge.adapters.git_history import GitKnowledgeHistory
from woon_core.knowledge.adapters.sqlite_search import SQLiteFtsSearchIndex

__all__ = ["GitKnowledgeHistory", "MarkdownDocumentRepository", "SQLiteFtsSearchIndex"]
