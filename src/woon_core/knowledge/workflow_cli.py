"""Standalone CLI and stdio MCP entry points; no private Woon registry is required."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from woon_core.errors import WoonError
from woon_core.knowledge.workflow import (
    cancel_workflow,
    run_workflow,
    undo_workflow,
    workflow_status,
)


def execute_workflow_request(vault: str, action: str, request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one operation and return bounded state, not the entire source snapshot."""
    root = Path(vault)
    if action == "run":
        result = run_workflow(root, request)
    else:
        operations = {"status": workflow_status, "cancel": cancel_workflow, "undo": undo_workflow}
        if action not in operations or set(request) != {"job_id"}:
            raise WoonError("status, cancel and undo require exactly one job_id")
        result = operations[action](root, request["job_id"])
    return {key: value for key, value in result.items() if key not in {"request", "source"}}


def run_workflow_command(arguments: list[str], output: TextIO) -> None:
    """Run ``woon knowledge workflow run --vault DIR --request FILE|-`` or an ID action."""
    parser = argparse.ArgumentParser(prog="woon knowledge workflow")
    parser.add_argument("action", choices=("run", "status", "cancel", "undo", "mcp"))
    parser.add_argument("--vault", help="Explicit vault directory; no private registry fallback")
    parser.add_argument("--request", help="JSON request file, or - for stdin")
    parser.add_argument("--job", help="Existing job ID for status, cancel, or undo")
    args = parser.parse_args(arguments)
    if args.action == "mcp":
        if args.vault or args.request or args.job:
            raise WoonError("MCP vault selection is explicit in each tool call")
        _run_mcp()
        return
    if not args.vault:
        raise WoonError("workflow requires --vault")
    if args.action == "run":
        if not args.request or args.job:
            raise WoonError("workflow run requires --request FILE|- and no --job")
        try:
            raw = sys.stdin.read() if args.request == "-" else Path(args.request).read_text()
            request = json.loads(raw)
        except (OSError, ValueError) as error:
            raise WoonError("workflow request must be readable JSON") from error
    else:
        if not args.job or args.request:
            raise WoonError("workflow status, cancel and undo require --job and no --request")
        request = {"job_id": args.job}
    result = execute_workflow_request(args.vault, args.action, request)
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)


def _run_mcp() -> None:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.server import Settings as FastMCPSettings
    from mcp.types import ToolAnnotations

    FastMCPSettings.model_rebuild()
    server = FastMCP(
        "Woon Knowledge Workflow",
        instructions=(
            "Work only on the user's explicitly selected vault and existing Markdown note. "
            "Run takes one explicit source and an editing instruction, calls the user's "
            "authenticated CLI twice, and auto-applies only after validation. Use a unique "
            "job_id before running; after response loss inspect status rather than starting "
            "another job. Canonical Woon outputs require their existing owner writer. "
            "Report held or cancelled states truthfully. Never infer permission to select "
            "additional files or fetch additional sources."
        ),
        json_response=True,
    )

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def woon_knowledge_workflow_run(vault: str, request: dict[str, Any]) -> dict[str, Any]:
        """Generate, independently review, and apply one note using the CLI subscription."""
        return execute_workflow_request(vault, "run", request)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def woon_knowledge_workflow_status(vault: str, job_id: str) -> dict[str, Any]:
        """Read one job; recover its receipt if a prior write's response was lost. No note write."""
        return execute_workflow_request(vault, "status", {"job_id": job_id})

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def woon_knowledge_workflow_cancel(vault: str, job_id: str) -> dict[str, Any]:
        """Cancel before writing starts; applying/undoing is not a confirmed cancellation."""
        return execute_workflow_request(vault, "cancel", {"job_id": job_id})

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def woon_knowledge_workflow_undo(vault: str, job_id: str) -> dict[str, Any]:
        """Restore the exact backup only when the note still matches this job's output."""
        return execute_workflow_request(vault, "undo", {"job_id": job_id})

    server.run(transport="stdio")
