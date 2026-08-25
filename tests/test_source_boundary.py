from pathlib import Path

from woon_core.knowledge.source_boundary import (
    apply_source_boundary_migration,
    audit_source_boundary,
    prepare_source_boundary_migration,
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
