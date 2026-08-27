import stat
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.source_archive import archive_private_source_corpus


def test_moves_private_corpus_into_wiki_source_boundary(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    subject = vault / "wiki/study.md"
    subject.parent.mkdir()
    subject.write_text("# Study\n", encoding="utf-8")
    source = tmp_path / "drop"
    source.mkdir()
    (source / "lesson.ipynb").write_text('{"cells": []}\n', encoding="utf-8")
    (source / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")

    result = archive_private_source_corpus(source, vault, "study-drop", "wiki/study.md")

    destination = vault / "wiki/private/_sources/knowledge/local-only/study-drop"
    assert result.moved is True
    assert result.files == 2
    assert not source.exists()
    assert (destination / "lesson.ipynb").is_file()
    catalog = yaml.safe_load((vault / "catalog/sources/study-drop.yaml").read_text())
    assert catalog["summary"] == {"canonical": 2}
    assert catalog["wiki_subject"] == "wiki/study.md"
    assert {record["state"] for record in catalog["records"]} == {"canonical"}
    assert all(
        str(record["target"]).startswith("wiki/private/_sources/") for record in catalog["records"]
    )
    ledger = yaml.safe_load((vault / "catalog/reconciliation/study-drop.yaml").read_text())
    assert {record["action"] for record in ledger["records"]} == {"move-to-wiki-source"}
    receipt_directory = vault / ".local/woon-knowledge/source-archive"
    assert stat.S_IMODE(receipt_directory.stat().st_mode) == 0o700
    receipt = receipt_directory / "study-drop.json"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600

    replay = archive_private_source_corpus(source, vault, "study-drop", "wiki/study.md")
    assert replay.moved is False
    assert replay.files == 2
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_secret_rejection_keeps_external_source_unchanged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    subject = vault / "wiki/study.md"
    subject.parent.mkdir()
    subject.write_text("# Study\n", encoding="utf-8")
    source = tmp_path / "drop"
    source.mkdir()
    (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    with pytest.raises(WoonError, match="rejects secret file"):
        archive_private_source_corpus(source, vault, "study-drop", "wiki/study.md")

    assert source.is_dir()
    assert not (vault / "wiki/private/_sources/knowledge/local-only/study-drop").exists()


def test_rejects_source_already_inside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    subject = vault / "wiki/study.md"
    subject.parent.mkdir(parents=True)
    subject.write_text("# Study\n", encoding="utf-8")
    source = vault / "drop"
    source.mkdir(parents=True)
    (source / "note.md").write_text("source\n", encoding="utf-8")

    with pytest.raises(WoonError, match="must be outside"):
        archive_private_source_corpus(source, vault, "study-drop", "wiki/study.md")


def test_rejects_source_that_contains_vault(tmp_path: Path) -> None:
    source = tmp_path / "drop"
    vault = source / "vault"
    subject = vault / "wiki/study.md"
    subject.parent.mkdir(parents=True)
    subject.write_text("# Study\n", encoding="utf-8")

    with pytest.raises(WoonError, match="must be outside"):
        archive_private_source_corpus(source, vault, "study-drop", "wiki/study.md")


def test_requires_existing_human_readable_wiki_subject(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source = tmp_path / "drop"
    source.mkdir()
    (source / "note.md").write_text("source\n", encoding="utf-8")

    with pytest.raises(WoonError, match="does not exist"):
        archive_private_source_corpus(source, vault, "study-drop", "wiki/missing.md")
