from __future__ import annotations

import hashlib
from pathlib import Path

from woon_core.knowledge.wiki_restructure import (
    prepare_wiki_restructure_preflight,
    render_wiki_restructure_classification,
    render_wiki_restructure_template,
)


def _write_page(vault: Path, relative: str, canonical_id: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = "" if relative == "wiki/README.md" else "parent: '[[wiki/README|Vault]]'\n"
    kind = "root" if relative == "wiki/README.md" else "topic"
    path.write_text(
        "---\n"
        "type: Wiki\n"
        f"title: {path.stem}\n"
        f"canonical_id: {canonical_id}\n"
        f"node_kind: {kind}\n"
        f"{parent}"
        "keywords: [test]\n"
        "aliases: []\n"
        "view_mode: tree\n"
        "updated: 2026-09-05\n"
        "summary: 테스트 문서입니다.\n"
        "knowledge_state: 확인 필요\n"
        "---\n\n"
        f"# {path.stem}\n",
        encoding="utf-8",
    )
    return path


def test_restructure_preflight_requires_every_active_page_once(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    child = _write_page(tmp_path, "wiki/old/topic.md", "old/topic")
    root_hash = hashlib.sha256(root.read_bytes()).hexdigest()
    child_hash = hashlib.sha256(child.read_bytes()).hexdigest()
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        f"- current_path: wiki/README.md\n  current_sha256: {root_hash}\n"
        "  canonical_id: README\n  source_owner: manual\n  disposition: keep\n"
        f"- current_path: wiki/old/topic.md\n  current_sha256: {child_hash}\n"
        "  canonical_id: old/topic\n  source_owner: manual\n  disposition: move\n"
        "  target_path: wiki/Wiki/programming-language-runtime/topic.md\n"
        "  target_parent: wiki/README.md\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert report.issues == ()
    assert report.document_count == 2
    assert report.disposition_counts == {"keep": 1, "move": 1}
    assert report.target_count == 1


def test_restructure_preflight_rejects_stale_hash_and_missing_record(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/old/topic.md", "old/topic")
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/README.md\n"
        f"  current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}\n"
        "  canonical_id: stale\n"
        "  source_owner: manual\n"
        "  disposition: keep\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert "records[1]: canonical_id does not match: wiki/README.md" in report.issues
    assert "manifest omits 1 active Wiki pages" in report.issues


def test_restructure_preflight_requires_compiler_ownership_from_page_catalog(
    tmp_path: Path,
) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    catalog = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "version: 1\npages:\n- page_id: README\n  output_path: README.md\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/README.md\n"
        f"  current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}\n"
        "  canonical_id: README\n"
        "  source_owner: manual\n"
        "  disposition: keep\n",
        encoding="utf-8",
    )

    report = prepare_wiki_restructure_preflight(tmp_path, manifest)

    assert report.issues == ("records[1]: source_owner must be 'compiler' for wiki/README.md",)


def test_restructure_template_covers_each_active_page_and_marks_owner(tmp_path: Path) -> None:
    root = _write_page(tmp_path, "wiki/README.md", "README")
    catalog = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "version: 1\npages:\n- page_id: README\n  output_path: README.md\n",
        encoding="utf-8",
    )

    template = render_wiki_restructure_template(tmp_path).decode("utf-8")

    assert "current_path: wiki/README.md" in template
    assert f"current_sha256: {hashlib.sha256(root.read_bytes()).hexdigest()}" in template
    assert "source_owner: compiler" in template
    assert "disposition: review" in template


def test_restructure_classification_assigns_known_legacy_areas_once(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README")
    _write_page(tmp_path, "wiki/ai/model.md", "ai/model")
    _write_page(tmp_path, "wiki/personal/kotlin-in-action/chapter-01.md", "book/kotlin/1")
    _write_page(tmp_path, "wiki/hubs/legacy.md", "hubs/legacy")

    rendered = render_wiki_restructure_classification(tmp_path).decode("utf-8")

    assert "document_count: 4" in rendered
    assert "target_scope: Wiki > AI·머신러닝" in rendered
    assert "target_scope: Wiki > 책 > 프로그래밍 언어·설계" in rendered
    assert "rationale: legacy-navigation-wrapper" in rendered
