from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_people_mcp_stdio_lists_and_queries_general_people(tmp_path: Path) -> None:
    """Prove Codex can discover and query the local person boundary over stdio."""

    _write_owner_card(tmp_path)

    async def exercise() -> None:
        environment = dict(os.environ)
        environment["WOON_KNOWLEDGE_ROOT"] = str(tmp_path)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "woon_core.people.mcp_server"],
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
                "woon_people_find",
                "woon_people_documents",
                "woon_people_upsert_card",
                "woon_people_link_document",
                "woon_people_set_identity_identifiers",
                "woon_people_private_history_sync",
                "woon_people_materialize_default_owner",
            }.issubset({tool.name for tool in tools.tools})

            response = await session.call_tool("woon_people_find", {"query": "최우녕"})
            assert response.structuredContent == {
                "query": "최우녕",
                "count": 1,
                "people": [
                    {
                        "person_id": "choi-woonyoung",
                        "title": "최우녕",
                        "person_kind": "vault-owner",
                        "relationship_to_owner": "볼트 사용자",
                        "person_scope": "general",
                        "identifiers": [],
                        "relative_path": "wiki/personal/choi-woonyoung.md",
                        "revision": response.structuredContent["people"][0]["revision"],
                    }
                ],
            }

    anyio.run(exercise)


def _write_owner_card(vault: Path) -> None:
    path = vault / "wiki/personal/choi-woonyoung.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
type: Wiki
title: 최우녕
entity_type: person
person_id: choi-woonyoung
person_kind: vault-owner
person_scope: general
relationship_to_owner: 볼트 사용자
---

# 최우녕
""",
        encoding="utf-8",
    )
