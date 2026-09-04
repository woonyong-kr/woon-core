from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from woon_core.knowledge.source_restructure import (
    prepare_source_restructure_preflight,
    render_source_restructure_template,
)


def _write_source(vault: Path, relative: str, content: bytes) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_source_restructure_template_classifies_only_unambiguous_prefixes(tmp_path: Path) -> None:
    web = _write_source(tmp_path, "wiki/private/_sources/knowledge/web/official.html", b"official")
    local = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    codex = _write_source(tmp_path, "wiki/private/_sources/codex/2026-09-05/talk.json", b"[]")

    payload = yaml.safe_load(render_source_restructure_template(tmp_path))
    records = {record["current_path"]: record for record in payload["records"]}

    assert records[web.relative_to(tmp_path).as_posix()] == {
        "current_path": "wiki/private/_sources/knowledge/web/official.html",
        "current_sha256": hashlib.sha256(b"official").hexdigest(),
        "bytes": 8,
        "storage_scope": "public-tracked",
        "disposition": "move",
        "catalog_reconciliation": "pending",
        "target_path": "sources/knowledge/web/official.html",
    }
    assert records[local.relative_to(tmp_path).as_posix()]["target_path"] == (
        "private/knowledge/local-only/book.pdf"
    )
    assert records[local.relative_to(tmp_path).as_posix()]["storage_scope"] == "private-tracked"
    assert records[codex.relative_to(tmp_path).as_posix()]["target_path"] == (
        "private/codex/2026-09-05/talk.json"
    )


def test_source_restructure_preflight_rejects_stale_or_incomplete_manifest(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/codex/day/talk.json", b"[]")
    manifest = tmp_path / "source-restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/private/_sources/codex/day/talk.json\n"
        "  current_sha256: stale\n"
        "  bytes: 2\n"
        "  storage_scope: local-only\n"
        "  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/talk.json\n",
        encoding="utf-8",
    )

    report = prepare_source_restructure_preflight(tmp_path, manifest)

    assert report.file_count == 1
    assert report.byte_count == source.stat().st_size
    assert report.catalog_pending_count == 1
    assert report.issues == (
        "records[1]: current_sha256 does not match: wiki/private/_sources/codex/day/talk.json",
    )


def test_source_restructure_preflight_rejects_an_extra_source_record(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/codex/day/talk.json", b"[]")
    manifest = tmp_path / "source-restructure.yaml"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/private/_sources/codex/day/talk.json\n"
        f"  current_sha256: {digest}\n"
        "  bytes: 2\n  storage_scope: local-only\n  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/talk.json\n"
        "- current_path: wiki/private/_sources/codex/day/missing.json\n"
        "  current_sha256: missing\n"
        "  bytes: 0\n  storage_scope: local-only\n  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/missing.json\n",
        encoding="utf-8",
    )

    report = prepare_source_restructure_preflight(tmp_path, manifest)

    assert report.issues == (
        "records[2]: current_path is not an active raw source: "
        "wiki/private/_sources/codex/day/missing.json",
        "manifest names 1 non-active raw source files",
    )
