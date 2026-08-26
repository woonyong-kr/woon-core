from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.reconciliation import audit_reconciliation
from woon_core.knowledge.source_catalog import (
    load_source_catalog,
    plan_source_catalog,
    write_source_catalog,
)


def test_source_catalog_classifies_each_file_without_absolute_paths(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "same.md").write_text("# 동일\n", encoding="utf-8")
    (target / "same.md").write_text("# 동일\n", encoding="utf-8")
    (source / "metadata.md").write_text("---\na: 1\n---\n# 본문\n", encoding="utf-8")
    (target / "metadata.md").write_text("---\na: 2\n---\n# 본문\n", encoding="utf-8")
    (source / "changed.md").write_text("# 원천\n", encoding="utf-8")
    (target / "changed.md").write_text("# 대상\n", encoding="utf-8")
    (source / "renamed.md").write_text("# 이동\n", encoding="utf-8")
    (target / "elsewhere.md").write_text("# 이동\n", encoding="utf-8")
    (source / "new.md").write_text("# 신규\n", encoding="utf-8")
    (source / "moved-title.md").write_text("# 같은 제목\n\n원천", encoding="utf-8")
    (target / "wiki/title.md").parent.mkdir(parents=True)
    (target / "wiki/title.md").write_text("# 같은 제목\n\n대상", encoding="utf-8")
    private = source / "private/raw.txt"
    private.parent.mkdir()
    private.write_text("비공개", encoding="utf-8")
    (source / ".git/ignored").parent.mkdir()
    (source / ".git/ignored").write_text("ignore", encoding="utf-8")
    (source / ".gitkeep").touch()
    (source / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    plan = plan_source_catalog(source, target, "vault")

    states = {record.locator: record.state for record in plan.records}
    assert states == {
        "changed.md": "merge-required",
        "metadata.md": "metadata-only",
        "moved-title.md": "semantic-match",
        "new.md": "new",
        "private/raw.txt": "new",
        "renamed.md": "content-alias",
        "same.md": "identical",
    }
    assert plan.excluded == (
        (".env", "secret-local"),
        (".gitkeep", "placeholder-or-cache"),
    )
    assert all(str(tmp_path) not in record.source_id for record in plan.records)


def test_source_catalog_output_is_deterministic_and_readable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "한글 문서.md").write_text("# 내용\n", encoding="utf-8")
    plan = plan_source_catalog(source, target, "vault")
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"

    write_source_catalog(plan, first)
    write_source_catalog(plan, second)

    assert first.read_bytes() == second.read_bytes()
    raw = yaml.safe_load(first.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["summary"] == {"new": 1}
    assert raw["records"][0]["locator"] == "한글 문서.md"
    assert "%ED%95%9C%EA%B8%80" in raw["records"][0]["source_id"]


@pytest.mark.parametrize("source_kind", ["same", "child", "parent", "symlink-child"])
def test_source_catalog_rejects_source_target_overlap(tmp_path: Path, source_kind: str) -> None:
    target = tmp_path / "target"
    child = target / "source"
    child.mkdir(parents=True)
    if source_kind == "same":
        source = target
    elif source_kind == "child":
        source = child
    elif source_kind == "parent":
        source = tmp_path
    else:
        source = tmp_path / "source-link"
        source.symlink_to(child, target_is_directory=True)

    with pytest.raises(WoonError, match="must be disjoint"):
        plan_source_catalog(source, target, "external")


def test_reconciliation_audit_reports_pending_and_detects_complete_ledger(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "same.md").write_text("# 동일\n", encoding="utf-8")
    (target / "same.md").write_text("# 동일\n", encoding="utf-8")
    catalog_path = target / "catalog/sources/vault.yaml"
    ledger_path = target / "catalog/reconciliation/vault.yaml"
    plan = plan_source_catalog(source, target, "vault")
    write_source_catalog(plan, catalog_path)
    pending = audit_reconciliation(source, target, catalog_path, ledger_path)
    record = plan.records[0]
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "source": "vault",
                "records": [
                    {
                        "source_id": record.source_id,
                        "locator": record.locator,
                        "source_sha256": record.sha256,
                        "status": "verified",
                        "target": record.target,
                        "target_after_sha256": record.target_sha256,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    complete = audit_reconciliation(source, target, catalog_path, ledger_path)

    assert pending.pending == 1
    assert pending.complete is False
    assert complete.complete is True
    assert complete.verified == 1


def test_source_identity_survives_exact_content_rename(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    original = source / "old.md"
    original.write_text("# 이동\n", encoding="utf-8")
    first = plan_source_catalog(source, target, "vault")
    catalog = tmp_path / "catalog.yaml"
    write_source_catalog(first, catalog)
    original.rename(source / "new.md")

    second = plan_source_catalog(
        source,
        target,
        "vault",
        previous_records=load_source_catalog(catalog).records,
    )

    assert second.records[0].locator == "new.md"
    assert second.records[0].source_id == first.records[0].source_id
