from __future__ import annotations

import json
import subprocess
from pathlib import Path

from woon_core.knowledge import reconciliation


def test_codex_reconciliation_isolates_tools_and_records_all_usage(
    tmp_path: Path, monkeypatch: object
) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    observed: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({"passed": True}), encoding="utf-8")
        stdout = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 7,
                },
            }
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(reconciliation.subprocess, "run", fake_run)  # type: ignore[attr-defined]

    result = reconciliation._run_codex("prompt", schema, "test-model")

    disabled = {observed[index + 1] for index, value in enumerate(observed) if value == "--disable"}
    assert {"plugins", "apps", "unified_exec", "shell_tool"} <= disabled
    assert 'web_search="disabled"' in observed
    assert result.input_tokens == 100
    assert result.cached_input_tokens == 80
    assert result.output_tokens == 20
    assert result.reasoning_output_tokens == 7
