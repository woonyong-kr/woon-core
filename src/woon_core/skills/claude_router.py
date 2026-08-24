"""Evaluate skill descriptions with an isolated Claude routing decision."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from woon_core.errors import WoonError
from woon_core.skills.routing_contract import (
    parse_routing_payload,
    routing_prompt,
    routing_schema,
)
from woon_core.skills.service import CatalogSkill


@dataclass(frozen=True, slots=True)
class ClaudeRoutingSelector:
    """Select skill names with tools, project rules, plugins and MCP disabled."""

    timeout_seconds: int = 300

    def __call__(
        self,
        catalog: tuple[CatalogSkill, ...],
        prompts: dict[str, str],
    ) -> dict[str, list[str]]:
        if not catalog or not prompts:
            raise WoonError("routing evaluation requires catalog skills and prompts")
        schema = json.dumps(
            routing_schema(
                sorted(prompts),
                sorted(skill.name for skill in catalog),
            ),
            ensure_ascii=False,
        )
        command = [
            "claude",
            "--print",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "plan",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--output-format",
            "json",
            "--json-schema",
            schema,
        ]
        try:
            completed = subprocess.run(
                command,
                input=routing_prompt(catalog, prompts),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as error:
            raise WoonError("claude executable is not available") from error
        except subprocess.TimeoutExpired as error:
            raise WoonError("Claude routing evaluation timed out") from error

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise WoonError(_failure_message(completed)) from error
        if completed.returncode != 0 or envelope.get("is_error") is True:
            raise WoonError(_failure_message(completed, envelope))
        payload = envelope.get("structured_output")
        if payload is None and isinstance(envelope.get("result"), str):
            try:
                payload = json.loads(envelope["result"])
            except json.JSONDecodeError as error:
                raise WoonError("Claude routing evaluation returned invalid JSON") from error
        return parse_routing_payload(payload)


def _failure_message(
    completed: subprocess.CompletedProcess[str], envelope: object | None = None
) -> str:
    details: list[str] = []
    if isinstance(envelope, dict) and isinstance(envelope.get("result"), str):
        details.append(envelope["result"].strip())
    details.extend(line.strip() for line in completed.stderr.splitlines()[-12:] if line.strip())
    suffix = f": {' | '.join(details)}" if details else ""
    return f"Claude routing evaluation failed{suffix}"
