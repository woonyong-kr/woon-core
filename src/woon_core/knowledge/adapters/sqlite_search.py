"""On-demand section-aware SQLite FTS5 knowledge search."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.domain import (
    IndexedDocument,
    IndexStatistics,
    KnowledgeExcerpt,
    SearchResult,
)
from woon_core.knowledge.generation import knowledge_generation

MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
QUERY_STOPWORDS = {
    "관계",
    "관련",
    "설명",
    "설명해",
    "알려줘",
    "찾아",
    "찾아라",
    "찾아줘",
}


@dataclass(frozen=True, slots=True)
class _Chunk:
    identifier: str
    heading: str
    text: str


class SQLiteFtsSearchIndex:
    """Persist a bounded section index without keeping a background process alive."""

    def __init__(self, database: Path, max_chunk_chars: int = 6000) -> None:
        self._database = database
        self._max_chunk_chars = max_chunk_chars

    def rebuild(self, documents: Iterable[IndexedDocument]) -> int:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        materialized = sorted(documents, key=lambda item: item.document_id)
        rows: list[tuple[object, ...]] = []
        document_count = 0
        for document in materialized:
            document_count += 1
            for chunk in _chunk_document(document, self._max_chunk_chars):
                rows.append(
                    (
                        document.document_id,
                        document.canonical_id,
                        document.title,
                        document.summary,
                        chunk.heading,
                        chunk.text,
                        document.relative_path,
                        document.revision,
                        document.source_type,
                        chunk.identifier,
                    )
                )
        with sqlite3.connect(self._database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS canonical_documents")
            connection.execute("DROP TABLE IF EXISTS knowledge_chunks")
            _initialize(connection)
            connection.executemany(
                """
                INSERT INTO knowledge_chunks
                  (document_id, canonical_id, title, summary, heading, body,
                   relative_path, revision, source_type, chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                "INSERT OR REPLACE INTO knowledge_metadata(key, value) VALUES ('generation', ?)",
                (knowledge_generation(materialized),),
            )
            connection.commit()
        return document_count

    def generation(self) -> str | None:
        if not self._database.is_file():
            return None
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            row = connection.execute(
                "SELECT value FROM knowledge_metadata WHERE key = 'generation'"
            ).fetchone()
        return str(row[0]) if row is not None else None

    def search(self, query: str, limit: int) -> list[SearchResult]:
        if not self._database.is_file():
            raise WoonError("knowledge index does not exist; run `woon knowledge index` first")
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            try:
                rows = _search_rows(connection, _fts_query(query, "AND"), limit)
                if not rows and len(_fts_tokens(query)) > 1:
                    rows = _search_rows(connection, _fts_query(query, "OR"), limit)
            except sqlite3.OperationalError as error:
                raise WoonError(f"knowledge search failed: {error}") from error
        results: list[SearchResult] = []
        seen: set[str] = set()
        for row in rows:
            document_id = str(row[0])
            if document_id in seen:
                continue
            seen.add(document_id)
            results.append(
                SearchResult(
                    document_id=document_id,
                    canonical_id=str(row[1]) if row[1] is not None else None,
                    title=str(row[2]),
                    summary=str(row[3]),
                    relative_path=str(row[4]),
                    revision=str(row[5]),
                    source_type=str(row[6]),
                    chunk_id=str(row[7]),
                    heading=str(row[8]),
                    score=-float(row[9]),
                    snippet=str(row[10]),
                )
            )
            if len(results) == limit:
                break
        return results

    def read_excerpt(self, document_id: str, chunk_id: str) -> KnowledgeExcerpt:
        if not self._database.is_file():
            raise WoonError("knowledge index does not exist; run `woon knowledge index` first")
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            row = connection.execute(
                """
                SELECT document_id, relative_path, revision, source_type, chunk_id, heading, body
                  FROM knowledge_chunks
                 WHERE document_id = ? AND chunk_id = ?
                 LIMIT 1
                """,
                (document_id, chunk_id),
            ).fetchone()
        if row is None:
            raise WoonError("knowledge excerpt not found; rebuild the index and search again")
        return KnowledgeExcerpt(
            document_id=str(row[0]),
            relative_path=str(row[1]),
            revision=str(row[2]),
            source_type=str(row[3]),
            chunk_id=str(row[4]),
            heading=str(row[5]),
            text=str(row[6]),
        )

    def statistics(self) -> IndexStatistics:
        if not self._database.is_file():
            raise WoonError("knowledge index does not exist; run `woon knowledge index` first")
        with sqlite3.connect(self._database) as connection:
            _initialize(connection)
            row = connection.execute(
                """
                SELECT count(DISTINCT document_id), count(*),
                       coalesce(sum(length(body)), 0), coalesce(max(length(body)), 0)
                  FROM knowledge_chunks
                """
            ).fetchone()
        assert row is not None
        return IndexStatistics(
            documents=int(row[0]),
            chunks=int(row[1]),
            total_chars=int(row[2]),
            max_chunk_chars=int(row[3]),
        )


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_metadata(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        )
        """
    )
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks USING fts5(
              document_id UNINDEXED,
              canonical_id UNINDEXED,
              title,
              summary,
              heading,
              body,
              relative_path UNINDEXED,
              revision UNINDEXED,
              source_type UNINDEXED,
              chunk_id UNINDEXED,
              tokenize='unicode61'
            )
            """
        )
    except sqlite3.OperationalError as error:
        raise WoonError("this Python build does not provide SQLite FTS5") from error


def _fts_tokens(query: str) -> list[str]:
    tokens = [
        token.replace('"', '""')
        for token in query.split()
        if token and token.casefold() not in QUERY_STOPWORDS
    ]
    if not tokens:
        tokens = [token.replace('"', '""') for token in query.split() if token]
    return tokens


def _fts_query(query: str, operator: str) -> str:
    return f" {operator} ".join(f'"{token}"*' for token in _fts_tokens(query))


def _search_rows(connection: sqlite3.Connection, query: str, limit: int) -> list[tuple[Any, ...]]:
    return connection.execute(
        """
        SELECT document_id, canonical_id, title, summary, relative_path,
               revision, source_type, chunk_id, heading,
               bm25(knowledge_chunks, 0.0, 0.0, 5.0, 3.0, 2.0, 1.0,
                    0.0, 0.0, 0.0, 0.0),
               snippet(knowledge_chunks, 5, '[', ']', ' … ', 32)
          FROM knowledge_chunks
         WHERE knowledge_chunks MATCH ?
         ORDER BY 10
         LIMIT ?
        """,
        (query, limit * 8),
    ).fetchall()


def _chunk_document(document: IndexedDocument, max_chars: int) -> list[_Chunk]:
    sections: list[tuple[str, list[str]]] = []
    heading = document.title
    lines: list[str] = []
    for line in document.body.splitlines():
        match = MARKDOWN_HEADING.match(line)
        if match:
            if _has_content(lines):
                sections.append((heading, lines))
            heading = match.group(2).strip()
            lines = [line]
        else:
            lines.append(line)
    if lines:
        sections.append((heading, lines))
    if not sections:
        sections.append((document.title, [document.body]))

    chunks: list[_Chunk] = []
    position = 0
    for section_heading, section_lines in sections:
        section = "\n".join(section_lines).strip()
        for text in _split_section(section, max_chars):
            identifier = hashlib.sha256(
                f"{document.document_id}\0{position}\0{text}".encode()
            ).hexdigest()[:20]
            chunks.append(_Chunk(identifier, section_heading, text))
            position += 1
    return chunks


def _has_content(lines: list[str]) -> bool:
    return any(line.strip() and not MARKDOWN_HEADING.match(line) for line in lines)


def _split_section(section: str, max_chars: int) -> list[str]:
    if len(section) <= max_chars:
        return [section]
    paragraphs = re.split(r"\n\s*\n", section)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                paragraph[start : start + max_chars]
                for start in range(0, len(paragraph), max_chars)
            )
            continue
        candidate = f"{current}\n\n{paragraph}".strip()
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
