"""On-demand SQLite FTS5 adapter for canonical Markdown search."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.domain import CanonicalDocument, SearchResult


class SQLiteFtsSearchIndex:
    """Persist an FTS index without keeping a background process alive."""

    def __init__(self, database: Path) -> None:
        self._database = database

    def rebuild(self, documents: Iterable[CanonicalDocument]) -> int:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM canonical_documents")
            rows = [
                (
                    document.metadata.canonical_id,
                    document.metadata.title,
                    document.metadata.summary,
                    document.body,
                    document.relative_path,
                    document.revision,
                )
                for document in documents
            ]
            connection.executemany(
                """
                INSERT INTO canonical_documents
                  (canonical_id, title, summary, body, relative_path, revision)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()
        return len(rows)

    def search(self, query: str, limit: int) -> list[SearchResult]:
        if not self._database.is_file():
            raise WoonError("knowledge index does not exist; run `woon knowledge index` first")
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            try:
                rows = connection.execute(
                    """
                    SELECT canonical_id, title, summary, relative_path, revision,
                           bm25(canonical_documents, 0.0, 4.0, 2.0, 1.0, 0.0, 0.0),
                           snippet(canonical_documents, 3, '[', ']', ' … ', 24)
                      FROM canonical_documents
                     WHERE canonical_documents MATCH ?
                     ORDER BY 6
                     LIMIT ?
                    """,
                    (_fts_query(query), limit),
                ).fetchall()
            except sqlite3.OperationalError as error:
                raise WoonError(f"knowledge search failed: {error}") from error
        return [
            SearchResult(
                canonical_id=str(row[0]),
                title=str(row[1]),
                summary=str(row[2]),
                relative_path=str(row[3]),
                revision=str(row[4]),
                score=float(-row[5]),
                snippet=str(row[6]),
            )
            for row in rows
        ]


def _initialize(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS canonical_documents USING fts5(
              canonical_id UNINDEXED,
              title,
              summary,
              body,
              relative_path UNINDEXED,
              revision UNINDEXED,
              tokenize='unicode61'
            )
            """
        )
    except sqlite3.OperationalError as error:
        raise WoonError("this Python build does not provide SQLite FTS5") from error


def _fts_query(query: str) -> str:
    tokens = [token.replace('"', '""') for token in query.split() if token]
    return " AND ".join(f'"{token}"' for token in tokens)
