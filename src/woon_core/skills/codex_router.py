"""Evaluate skill descriptions with an isolated Codex routing decision."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.skills.routing_contract import (
    parse_routing_payload,
    routing_prompt,
    routing_schema,
)
from woon_core.skills.service import CatalogSkill


@dataclass(frozen=True, slots=True)
class CodexRoutingSelector:
    """Select skill names without loading skill bodies or repository rules."""

    timeout_seconds: int = 120

    def __call__(
        self,
        catalog: tuple[CatalogSkill, ...],
        prompts: dict[str, str],
    ) -> dict[str, list[str]]:
        if not catalog or not prompts:
            raise WoonError("routing evaluation requires catalog skills and prompts")
        with tempfile.TemporaryDirectory(prefix="woon-routing-") as temporary:
            directory = Path(temporary)
            schema_path = directory / "schema.json"
            output_path = directory / "result.json"
            schema_path.write_text(
                json.dumps(routing_schema(sorted(prompts)), ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                "codex",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-C",
                str(directory),
                "-",
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
                raise WoonError("codex executable is not available") from error
            except subprocess.TimeoutExpired as error:
                raise WoonError("Codex routing evaluation timed out") from error
            if completed.returncode != 0:
                detail = completed.stderr.strip().splitlines()[-12:]
                suffix = f": {' | '.join(detail)}" if detail else ""
                raise WoonError(f"Codex routing evaluation failed{suffix}")
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                return parse_routing_payload(payload)
            except (json.JSONDecodeError, OSError) as error:
                raise WoonError("Codex routing evaluation returned invalid JSON") from error
