import hashlib
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml

from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.knowledge.public_projection import (
    apply_public_projection,
    prepare_public_projection,
)

_SITE_CONFIG = """content_root: generated/public-content
input_owner: Obsidian Vault
write_policy: compiler-only
publish_policy: approved-documents-only
site_behavior: read-only-build-input
required_front_matter:
  - layout
  - title
  - nav_order
  - permalink
  - publication_state
  - projection_id
  - projection_sha256
privacy_prohibited:
  - obsidian-wikilink
  - local-file-path
  - private-source-link
  - source-session-id
"""


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _page(
    *,
    page_id: str,
    title: str,
    publication_state: str,
    access: str,
    slug: str | None = None,
    parent: str | None = None,
    body: str = "본문입니다.",
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    frontmatter: dict[str, Any] = {
        "type": "Wiki",
        "title": title,
        "canonical_id": page_id,
        "publication_state": publication_state,
        "access": access,
    }
    if slug is not None:
        frontmatter["public_slug"] = slug
    if parent is not None:
        frontmatter["parent"] = parent
    return {
        "page_id": page_id,
        "output_path": f"{page_id}.md",
        "title": title,
        "frontmatter": frontmatter,
        "source_ids": source_ids or [],
        "claim_ids": [],
        "render": {"kind": "toc-only"},
        "test_body": body,
    }


def _write_fixture(
    tmp_path: Path,
    pages: list[dict[str, Any]],
    *,
    source_privacy: str = "public",
) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    site = tmp_path / "site"
    (site / "config").mkdir(parents=True)
    (site / "config/public-projection.yml").write_text(_SITE_CONFIG, encoding="utf-8")
    generated = site / "generated/public-content"
    generated.mkdir(parents=True)
    (generated / "README.md").write_text("human boundary note\n", encoding="utf-8")

    receipts: list[dict[str, Any]] = []
    for page in pages:
        output = vault / "wiki" / str(page["output_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        metadata = dict(page["frontmatter"])
        metadata["llm_wiki"] = {"page_id": page["page_id"]}
        rendered = (
            "---\n"
            + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            + "---\n\n"
            + f"# {page['title']}\n\n{page['test_body'].rstrip()}\n"
        ).encode()
        output.write_bytes(rendered)
        receipts.append(
            {
                "page_id": page["page_id"],
                "output_sha256": hashlib.sha256(rendered).hexdigest(),
            }
        )

    source_ids = sorted(
        {
            source_id
            for page in pages
            for source_id in page["source_ids"]
            if isinstance(source_id, str)
        }
    )
    _write_yaml(
        vault / "catalog/llm-wiki/pages.yaml",
        {
            "version": 1,
            "pages": [
                {key: value for key, value in page.items() if key != "test_body"} for page in pages
            ],
        },
    )
    _write_yaml(
        vault / "catalog/llm-wiki/sources.yaml",
        {
            "version": 1,
            "sources": [
                {
                    "source_id": source_id,
                    "privacy": source_privacy,
                    "lifecycle": "compiled",
                    "locator": "https://example.com/source",
                }
                for source_id in source_ids
            ],
        },
    )
    _write_yaml(vault / "catalog/llm-wiki/receipts.yaml", {"version": 1, "receipts": receipts})
    return vault, site


def _hidden_wiki_hub() -> dict[str, Any]:
    return _page(
        page_id="Wiki/README",
        title="Wiki",
        publication_state="private",
        access="local-only",
    )


def test_empty_projection_is_valid_and_preserves_human_readme(tmp_path: Path) -> None:
    vault, site = _write_fixture(tmp_path, [_hidden_wiki_hub()])

    report = prepare_public_projection(vault, site)

    assert report.documents == ()
    assert report.excluded_private_targets == ("Wiki/README",)
    result = apply_public_projection(report)
    assert result.changed is False
    assert (site / "generated/public-content/README.md").read_text(encoding="utf-8") == (
        "human boundary note\n"
    )
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["documents"] == {}


def test_public_projection_rewrites_only_approved_wikilinks_and_replays(tmp_path: Path) -> None:
    source_id = "source://public/example"
    pages = [
        _hidden_wiki_hub(),
        _page(
            page_id="Wiki/home",
            title="홈",
            publication_state="publish",
            access="public",
            slug="home",
            parent="[[wiki/Wiki/README|Wiki]]",
            body="[[Wiki/kotlin|Kotlin]]을 학습합니다.",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/kotlin",
            title="Kotlin",
            publication_state="publish",
            access="public",
            slug="kotlin",
            parent="[[wiki/Wiki/README|Wiki]]",
            source_ids=[source_id],
        ),
    ]
    vault, site = _write_fixture(tmp_path, pages)

    first = prepare_public_projection(vault, site)
    result = apply_public_projection(first)
    second = prepare_public_projection(vault, site)
    replay = apply_public_projection(second)

    assert result.changed is True
    assert replay.changed is False
    assert first.input_sha256 == second.input_sha256
    home = (site / "generated/public-content/home.md").read_text(encoding="utf-8")
    assert "layout: default" in home
    assert "permalink: /wiki/home/" in home
    assert "publication_state: publish" in home
    assert "has_toc: false" in home
    assert "projection_id: Wiki/home" in home
    assert "\n# 홈\n" in home
    assert "\n# 홈\n{: .no_toc }\n" in home
    assert "[[" not in home
    assert "[Kotlin](/wiki/kotlin/)" in home
    assert "llm_wiki:" not in home
    assert set(path.name for path in (site / "generated/public-content").iterdir()) == {
        "README.md",
        "home.md",
        "kotlin.md",
    }


def test_public_nav_root_keeps_semantic_parent_check_and_inserts_reader_toc(tmp_path: Path) -> None:
    source_id = "source://public/example"
    pages = [
        _hidden_wiki_hub(),
        _page(
            page_id="Wiki/guide",
            title="가이드",
            publication_state="publish",
            access="public",
            slug="guide",
            parent="[[wiki/Wiki/README|Wiki]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/kotlin",
            title="Kotlin",
            publication_state="publish",
            access="public",
            slug="kotlin",
            parent="[[wiki/Wiki/guide|가이드]]",
            body=(
                "Kotlin을 시작하기 전에 필요한 내용을 먼저 살핀다.\n\n"
                "<!-- woon-wiki-children:start -->\n"
                "## 공개 sidebar가 맡는 목록\n\n"
                "- [[Wiki/guide|가이드]]\n"
                "<!-- woon-wiki-children:end -->\n\n"
                "## 준비\n\n"
                "개발 환경을 확인한다.\n\n"
                "### 확인\n\n"
                "컴파일러를 실행한다.\n\n"
                "## 함수\n\n"
                "값을 받아 결과를 돌려준다."
            ),
            source_ids=[source_id],
        ),
    ]
    pages[2]["frontmatter"]["public_nav_root"] = True
    vault, site = _write_fixture(tmp_path, pages)

    report = prepare_public_projection(vault, site)
    result = apply_public_projection(report)

    assert result.changed is True
    assert "Wiki/kotlin:public:Wiki/guide" in report.link_checks
    projected = (site / "generated/public-content/kotlin.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(projected.split("---", 2)[1])
    assert "parent" not in frontmatter
    assert "has_toc: false" in projected
    assert "# Kotlin" in projected
    assert "# Kotlin\n{: .no_toc }" in projected
    assert "## 목차\n{: .no_toc .text-delta }\n\n1. TOC\n{:toc}" in projected
    assert "## 준비\n\n개발 환경을 확인한다." in projected
    assert "woon-wiki-children" not in projected
    assert "공개 sidebar가 맡는 목록" not in projected
    assert "### 확인\n\n컴파일러를 실행한다." in projected
    assert "## 함수\n\n값을 받아 결과를 돌려준다." in projected
    assert projected.index("## 목차") < projected.index("## 준비")


def test_public_projection_rejects_nonpublic_provenance(tmp_path: Path) -> None:
    page = _page(
        page_id="Wiki/home",
        title="홈",
        publication_state="publish",
        access="public",
        slug="home",
        source_ids=["source://private/book"],
    )
    vault, site = _write_fixture(tmp_path, [_hidden_wiki_hub(), page], source_privacy="local-only")

    with pytest.raises(WoonError, match="non-public provenance"):
        prepare_public_projection(vault, site)


def test_public_projection_rejects_link_to_private_page(tmp_path: Path) -> None:
    pages = [
        _hidden_wiki_hub(),
        _page(
            page_id="Wiki/home",
            title="홈",
            publication_state="publish",
            access="public",
            slug="home",
            body="[[Wiki/private-note|비공개]]",
        ),
        _page(
            page_id="Wiki/private-note",
            title="비공개",
            publication_state="private",
            access="local-only",
        ),
    ]
    vault, site = _write_fixture(tmp_path, pages)

    with pytest.raises(WoonError, match="non-published page"):
        prepare_public_projection(vault, site)


def test_cli_preflight_does_not_write_and_apply_requires_explicit_flag(tmp_path: Path) -> None:
    vault, site = _write_fixture(tmp_path, [_hidden_wiki_hub()])
    output = StringIO()

    run(["knowledge", "public-projection", "--vault", str(vault), "--site", str(site)], output)

    assert '"apply": false' in output.getvalue()
    assert not (vault / ".local/woon-knowledge/public-projection/receipt.json").exists()
    run(
        [
            "knowledge",
            "public-projection",
            "--vault",
            str(vault),
            "--site",
            str(site),
            "--apply",
        ],
        StringIO(),
    )
    assert (vault / ".local/woon-knowledge/public-projection/receipt.json").is_file()
