from __future__ import annotations

import subprocess
import sys
from pathlib import Path

AUDIT = Path(__file__).parents[1] / "src/woon_core/knowledge/vault_tools/audit-folder-depth.py"


def _run_audit(vault: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(AUDIT)],
        cwd=vault,
        check=False,
        capture_output=True,
        text=True,
    )


def _write(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# fixture\n", encoding="utf-8")


def test_accepts_declared_semantic_wiki_collections(tmp_path: Path) -> None:
    _write(tmp_path / "wiki/ai/concept.md")
    _write(tmp_path / "wiki/personal/projects/service.md")
    _write(tmp_path / "wiki/personal/interview/README.md")
    _write(tmp_path / "wiki/personal/interview/ai-engineer/question.md")
    _write(tmp_path / "wiki/personal/career/README.md")
    _write(tmp_path / "wiki/personal/career/applications/company-role.md")
    _write(tmp_path / "wiki/personal/linked-graph/information.md")

    result = _run_audit(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "folder_depth_ok" in result.stdout


def test_accepts_repository_owned_private_depth_exclusion(tmp_path: Path) -> None:
    configuration = tmp_path / ".woon/repository.yaml"
    configuration.parent.mkdir(parents=True)
    configuration.write_text(
        "folder_depth_audit_ignored_roots: [wiki/private/novel]\n",
        encoding="utf-8",
    )
    _write(tmp_path / "wiki/private/novel/사건-히스토리/event-01.md")

    result = _run_audit(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "folder_depth_ok" in result.stdout


def test_rejects_undeclared_or_retired_deep_collections(tmp_path: Path) -> None:
    _write(tmp_path / "wiki/personal/notes/deep/note.md")
    _write(tmp_path / "wiki/canonical/retired/note.md")

    result = _run_audit(tmp_path)

    assert result.returncode == 1
    assert "folder_depth_violations=2" in result.stdout
    assert "depth>3: wiki/canonical/retired/note.md" in result.stdout
    assert "depth>4: wiki/personal/notes/deep/note.md" in result.stdout
