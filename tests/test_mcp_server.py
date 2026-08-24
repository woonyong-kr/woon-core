from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from woon_core.knowledge import mcp_server


def test_mcp_server_imports_without_incomplete_settings_warning() -> None:
    script = """
import warnings
from pydantic_settings.sources.utils import IncompleteFieldDefinitionWarning

warnings.simplefilter('error', IncompleteFieldDefinitionWarning)
import woon_core.knowledge.mcp_server
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_reindex_reloads_the_cached_service_after_a_configuration_change(monkeypatch) -> None:
    class Service:
        def __init__(self, count: int) -> None:
            self.count = count
            self.calls = 0

        def reindex(self) -> int:
            self.calls += 1
            return self.count

    cached_service = Service(1)
    refreshed_service = Service(2)
    services = iter(
        [
            (SimpleNamespace(search_adapter="sqlite-fts"), cached_service),
            (SimpleNamespace(search_adapter="sqlite-fts"), refreshed_service),
        ]
    )
    mcp_server._service.cache_clear()
    monkeypatch.setattr(mcp_server, "build_knowledge_service", lambda: next(services))

    # Simulate normal retrieval caching the old configuration first.
    assert mcp_server._service() is cached_service

    result = mcp_server.reindex_knowledge()

    assert result == {"indexed": 2, "adapter": "sqlite-fts"}
    assert cached_service.calls == 0
    assert refreshed_service.calls == 1
    assert mcp_server._service.cache_info().currsize == 0


def test_mcp_server_stdio_archives_searches_and_reads_a_section(tmp_path: Path) -> None:
    """Prove the registered stdio server can serve one complete retrieval flow."""

    _write_vault_config(tmp_path)

    async def exercise() -> None:
        environment = dict(os.environ)
        environment["WOON_KNOWLEDGE_ROOT"] = str(tmp_path)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "woon_core.knowledge.mcp_server"],
            env=environment,
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {
                "woon_knowledge_archive_conversation",
                "woon_knowledge_read_excerpt",
                "woon_knowledge_search",
                "woon_automation_record_mail_schedule_candidates",
            }.issubset({tool.name for tool in tools.tools})

            created = await session.call_tool(
                "woon_knowledge_archive_conversation",
                {
                    "canonical_id": "testing/mcp-smoke",
                    "title": "MCP 검색 검증",
                    "domain": "testing",
                    "summary": "stdio MCP가 실제 Vault에 저장하고 검색하는지 확인한다.",
                    "purpose": "MCP 검색과 발췌 경로를 검증한다.",
                    "body": "## 핵심\n\nMCP stdio 검증 토큰을 검색할 수 있어야 한다.",
                },
            )
            assert created.structuredContent == {
                "created": True,
                "changed": True,
                "canonical_id": "testing/mcp-smoke",
                "relative_path": "wiki/canonical/testing/mcp-smoke.md",
                "revision": created.structuredContent["revision"],
            }

            result = await session.call_tool(
                "woon_knowledge_search", {"query": "stdio 검증 토큰", "limit": 3}
            )
            payload = result.structuredContent
            assert payload is not None
            assert payload["count"] == 1
            hit = payload["results"][0]
            assert hit["canonical_id"] == "testing/mcp-smoke"

            excerpt = await session.call_tool(
                "woon_knowledge_read_excerpt",
                {"document_id": hit["document_id"], "chunk_id": hit["chunk_id"]},
            )
            assert excerpt.structuredContent is not None
            assert "MCP stdio 검증 토큰" in excerpt.structuredContent["text"]

    anyio.run(exercise)


def _write_vault_config(vault: Path) -> None:
    (vault / "config").mkdir()
    (vault / "guides").mkdir()
    (vault / "wiki/canonical").mkdir(parents=True)
    (vault / "guides/document.md").write_text("# document\n", encoding="utf-8")
    (vault / "guides/diagram.md").write_text("# diagram\n", encoding="utf-8")
    (vault / "config/canonical-knowledge.yaml").write_text(
        """version: 1
runtime_root: .local/knowledge
canonical:
  root: wiki/canonical
search:
  adapter: sqlite-fts
  database: .local/knowledge/search.sqlite3
  roots: []
  exclude: []
style:
  document_guide: guides/document.md
  diagram_guide: guides/diagram.md
""",
        encoding="utf-8",
    )
