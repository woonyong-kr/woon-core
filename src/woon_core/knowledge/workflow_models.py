"""Use the user's unmodified, authenticated CLI for isolated document-only calls."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.knowledge.codex_quality_review import _require_chatgpt_login, _run_codex


def run_model(
    provider: str,
    prompt: str,
    schema: dict[str, Any],
    *,
    model: str = "subscription-default",
    binary: str | None = None,
) -> dict[str, Any]:
    """Return one JSON object; no tools, retained sessions, retries, or API fallback.

    CLI authentication remains native. No credential files or tokens are read by
    this module. A running call has a 600 second deadline; cancellation of a job
    prevents application but does not promise cancellation of provider billing.
    """
    executable = shutil.which(binary or provider)
    if executable is None:
        raise WoonError(f"install and sign into the {provider} CLI first")
    if provider == "codex":
        _require_chatgpt_login(executable)
        return _run_codex(prompt, schema, executable, model, 600)
    if provider != "claude":
        raise WoonError("workflow provider must be codex or claude")
    try:
        login = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        status = json.loads(login.stdout)
        if (
            login.returncode != 0
            or not status.get("loggedIn")
            or status.get("authMethod") != "claude.ai"
        ):
            raise WoonError("sign into the Claude CLI with your subscription before running")
        with tempfile.TemporaryDirectory(prefix="woon-knowledge-claude-") as temporary:
            command = [
                executable,
                "--print",
                "--safe-mode",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-chrome",
                "--no-session-persistence",
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
            ]
            if model != "subscription-default":
                command.extend(["--model", model])
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
                cwd=Path(temporary),
            )
        envelope = json.loads(completed.stdout)
        if completed.returncode or envelope.get("is_error"):
            if envelope.get("api_error_status") in {401, 403}:
                raise WoonError(
                    "Claude login was rejected or revoked; run claude auth login in a terminal. "
                    "No API-key fallback was used."
                )
            raise WoonError(f"Claude CLI could not complete (exit {completed.returncode})")
        value = envelope.get("structured_output")
        if not isinstance(value, dict):
            raise WoonError("Claude CLI did not return the required structured result")
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as error:
        raise WoonError(f"Claude CLI could not complete: {type(error).__name__}") from error
