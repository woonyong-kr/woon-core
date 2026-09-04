from pathlib import Path

from woon_core.knowledge.source_boundary import (
    apply_source_boundary_migration,
    audit_source_boundary,
    prepare_source_boundary_migration,
    private_source_relative,
    source_storage_layout,
)


def test_moves_all_sources_inside_wiki_and_replays_audit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    (vault / "sources/web").mkdir(parents=True)
    (vault / "sources/web/source.md").write_text("source\n", encoding="utf-8")
    (novel / "vault-source").mkdir(parents=True)
    (novel / "vault-source/original.md").write_text("original\n", encoding="utf-8")

    report = prepare_source_boundary_migration(vault, external_novel=novel)
    receipt = apply_source_boundary_migration(vault, report)

    assert report.source_count == 2
    assert report.file_count == 2
    assert receipt.is_file()
    assert not (vault / "sources").exists()
    assert not novel.exists()
    assert (vault / "wiki/private/_sources/knowledge/web/source.md").is_file()
    assert (vault / "wiki/private/_sources/novel/vault-source/original.md").is_file()
    assert audit_source_boundary(vault, legacy_novel=novel) == ()


def test_private_source_resolver_rejects_mixed_layout_and_uses_target_after_migration(
    tmp_path: Path,
) -> None:
    (tmp_path / "wiki/private/_sources").mkdir(parents=True)
    assert source_storage_layout(tmp_path) == "legacy"
    assert private_source_relative(tmp_path, "knowledge", "book.pdf").as_posix() == (
        "wiki/private/_sources/knowledge/book.pdf"
    )

    (tmp_path / "private").mkdir()
    assert source_storage_layout(tmp_path) == "mixed"

    (tmp_path / "wiki/private/_sources").rename(tmp_path / "legacy-sources")
    assert source_storage_layout(tmp_path) == "target"
    assert private_source_relative(tmp_path, "knowledge", "book.pdf").as_posix() == (
        "private/knowledge/book.pdf"
    )
