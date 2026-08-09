from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.registry import Registry, Repository


def registry() -> Registry:
    return Registry(
        version=1,
        repositories={
            "knowledge": Repository(
                remote="https://github.com/example/knowledge.git",
                directory="woon-knowledge",
            )
        },
    )


def test_resolve_repo_uri(tmp_path: Path) -> None:
    resolved = registry().resolve(tmp_path, "repo://knowledge/wiki/os/page.md")
    assert resolved == tmp_path / "woon-knowledge/wiki/os/page.md"


def test_resolve_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(WoonError, match="may not escape"):
        registry().resolve(tmp_path, "repo://knowledge/../secret")


def test_validate_rejects_absolute_directory() -> None:
    invalid = Registry(
        version=1,
        repositories={
            "knowledge": Repository(
                remote="https://github.com/example/knowledge.git", directory="/absolute"
            )
        },
    )
    with pytest.raises(WoonError, match="unsafe directory"):
        invalid.validate()
