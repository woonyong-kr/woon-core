from __future__ import annotations

import subprocess
import sys


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
