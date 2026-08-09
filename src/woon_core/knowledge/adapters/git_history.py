"""Git adapter for canonical document history and recovery."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.knowledge.domain import HistoryEntry

REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")


class GitKnowledgeHistory:
    """Read bounded history for paths inside a private Git repository."""

    def __init__(self, vault: Path) -> None:
        self._vault = vault

    def list(self, relative_path: str, limit: int) -> list[HistoryEntry]:
        if limit < 1 or limit > 100:
            raise WoonError("history limit must be between 1 and 100")
        output = self._git(
            "log",
            f"--max-count={limit}",
            "--format=%H%x09%aI%x09%s",
            "--",
            relative_path,
        )
        entries: list[HistoryEntry] = []
        for line in output.splitlines():
            revision, authored_at, subject = line.split("\t", 2)
            entries.append(HistoryEntry(revision, authored_at, subject))
        return entries

    def read(self, relative_path: str, revision: str) -> str:
        if not REVISION.fullmatch(revision):
            raise WoonError("git revision must be a hexadecimal commit ID")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise WoonError("historical path must stay inside the vault")
        return self._git("show", f"{revision}:{path.as_posix()}")

    def _git(self, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self._vault), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise WoonError(f"git history operation failed: {detail}")
        return completed.stdout
