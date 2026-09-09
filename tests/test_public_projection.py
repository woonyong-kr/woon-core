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


def test_keyword_preview_preserves_planned_state_and_child_navigation(tmp_path: Path) -> None:
    page = _page(
        page_id="Wiki/kotlin",
        title="Kotlin",
        publication_state="publish",
        access="public",
        slug="kotlin",
        body="<!-- planned -->",
    )
    page["frontmatter"]["content_status"] = "planned"
    vault, site = _write_fixture(tmp_path, [page])
    report = prepare_public_projection(vault, site)
    rendered = report.documents[0].content.decode()
    assert "content_status: planned" in rendered
    assert "has_toc: true" in rendered
    assert "작성 예정" in rendered
    assert "## 목차" not in rendered


def test_keyword_preview_does_not_override_private_provenance(tmp_path: Path) -> None:
    page = _page(
        page_id="Wiki/kotlin",
        title="Kotlin",
        publication_state="publish",
        access="public",
        slug="kotlin",
        source_ids=["source://private/book"],
    )
    page["frontmatter"]["content_status"] = "planned"
    vault, site = _write_fixture(tmp_path, [page], source_privacy="local-only")
    with pytest.raises(WoonError, match="non-public provenance"):
        prepare_public_projection(vault, site)


@pytest.mark.parametrize(
    ("status", "navigation", "has_groups", "expected_toc"),
    [
        ("ready", "sidebar-only", True, False),
        ("overview", "sidebar-only", True, False),
        ("planned", "sidebar-only", True, True),
        ("ready", "sidebar-only", False, True),
        ("ready", "sidebar-only", None, True),
        ("ready", "inline", True, True),
        ("ready", None, True, True),
    ],
)
def test_authored_child_guide_avoids_duplicate_list_without_hiding_navigation(
    tmp_path: Path, status: str, navigation: str | None,
    has_groups: bool | None, expected_toc: bool,
) -> None:
    parent = _page(
        page_id="Wiki/structures", title="자료구조", publication_state="publish", access="public",
        slug="structures", body="## 위치로 접근하기\n\n- [[Wiki/array|Array]]",
    )
    parent["frontmatter"]["content_status"] = status
    if navigation is not None:
        parent["frontmatter"]["reader_navigation"] = navigation
    parent["frontmatter"]["navigation_groups"] = None if has_groups is None else [
        {"label": "위치로 접근하기", "children": ["Wiki/array"] if has_groups else []}
    ]
    child = _page(
        page_id="Wiki/array", title="Array", publication_state="publish", access="public",
        slug="array", parent="[[Wiki/structures|자료구조]]",
    )
    vault, site = _write_fixture(tmp_path, [parent, child])

    documents = {
        doc.page_id: doc.content.decode()
        for doc in prepare_public_projection(vault, site).documents
    }
    metadata = yaml.safe_load(documents["Wiki/structures"].split("---", 2)[1])
    assert metadata["has_toc"] is expected_toc
    assert "## 위치로 접근하기\n\n- [Array](/wiki/array/)" in documents["Wiki/structures"]
    child_metadata = yaml.safe_load(documents["Wiki/array"].split("---", 2)[1])
    assert child_metadata["parent"] == "자료구조"


def test_keyword_parent_identity_cannot_disagree_with_navigation(tmp_path: Path) -> None:
    page = _page(
        page_id="Wiki/kotlin",
        title="Kotlin",
        publication_state="publish",
        access="public",
        slug="kotlin",
    )
    page["frontmatter"]["public_parent_id"] = "Wiki/private"
    vault, site = _write_fixture(tmp_path, [page])
    with pytest.raises(WoonError, match="public_parent_id does not match parent"):
        prepare_public_projection(vault, site)


def test_only_explicit_public_search_terms_are_projected(tmp_path: Path) -> None:
    page = _page(
        page_id="Wiki/heap", title="Heap", publication_state="publish", access="public", slug="heap"
    )
    page["frontmatter"]["public_search_terms"] = ["힙"]
    page["frontmatter"]["aliases"] = ["internal alias"]
    vault, site = _write_fixture(tmp_path, [page])

    rendered = prepare_public_projection(vault, site).documents[0].content.decode()

    assert "search_terms:\n- 힙" in rendered
    assert "internal alias" not in rendered


@pytest.mark.parametrize("terms", ["힙", [None], [""]])
def test_invalid_public_search_terms_fail(tmp_path: Path, terms: object) -> None:
    page = _page(
        page_id="Wiki/heap", title="Heap", publication_state="publish", access="public", slug="heap"
    )
    page["frontmatter"]["public_search_terms"] = terms
    vault, site = _write_fixture(tmp_path, [page])
    with pytest.raises(WoonError, match="search terms are invalid"):
        prepare_public_projection(vault, site)


def test_redirects_are_separate_deterministic_artifacts_and_replay_safely(tmp_path: Path) -> None:
    page = _page(page_id="Wiki/observability", title="Observability",
                 publication_state="publish", access="public", slug="observability")
    page["frontmatter"].update(content_status="planned", public_redirect_from=["old-observability"])
    vault, site = _write_fixture(tmp_path, [page])
    report = prepare_public_projection(vault, site)
    assert len(report.documents) == 1
    assert len(report.redirects) == 1
    redirect = report.redirects[0]
    assert redirect.target_slug == "observability"
    assert redirect.relative_path == Path("old-observability.html")
    assert b"projection_id" not in redirect.content
    assert b"redirect_target: /wiki/observability/" in redirect.content
    assert b"absolute_url" in redirect.content and b"relative_url" in redirect.content
    assert report.receipt == prepare_public_projection(vault, site).receipt
    assert apply_public_projection(report).changed
    assert (report.content_root / redirect.relative_path).read_bytes() == redirect.content
    assert not apply_public_projection(report).changed
    assert set(json.loads(report.receipt)["documents"]) == {"observability.md"}
    assert set(json.loads(report.receipt)["redirects"]) == {"old-observability.html"}


@pytest.mark.parametrize("former", ["old-name", None, [None], ["../private"],
                                   ["https://example.com"], ["Old-Name"], ["%2e%2e"], [""]])
def test_redirect_input_cannot_be_a_path_or_unvalidated_url(tmp_path: Path, former: object) -> None:
    page = _page(page_id="Wiki/one", title="One", publication_state="publish",
                 access="public", slug="one")
    page["frontmatter"]["public_redirect_from"] = former
    vault, site = _write_fixture(tmp_path, [page])
    with pytest.raises(WoonError, match="safe public slugs"):
        prepare_public_projection(vault, site)


@pytest.mark.parametrize("former", [["one"], ["old", "old"], ["two"], ["private"]])
def test_redirects_cannot_shadow_current_or_private_urls(tmp_path: Path, former: list[str]) -> None:
    one = _page(page_id="Wiki/one", title="One", publication_state="publish",
                access="public", slug="one")
    one["frontmatter"]["public_redirect_from"] = former
    two = _page(page_id="Wiki/two", title="Two", publication_state="publish",
                access="public", slug="two")
    private = _page(page_id="Wiki/private", title="Private", publication_state="private",
                    access="local-only", slug="private")
    private["frontmatter"]["public_redirect_from"] = ["private-alias"]
    vault, site = _write_fixture(tmp_path, [one, two, private])
    with pytest.raises(WoonError, match="redirect slug conflicts"):
        prepare_public_projection(vault, site)


def test_redirect_owners_require_public_provenance_and_compiled_metadata(tmp_path: Path) -> None:
    page = _page(page_id="Wiki/one", title="One", publication_state="publish",
                 access="public", slug="one", source_ids=["source://private/book"])
    page["frontmatter"]["public_redirect_from"] = ["old"]
    vault, site = _write_fixture(tmp_path, [page], source_privacy="local-only")
    with pytest.raises(WoonError, match="non-public provenance"):
        prepare_public_projection(vault, site)
    page["source_ids"] = []
    page["frontmatter"]["public_redirect_from"] = ["uncompiled-change"]
    _write_yaml(vault / "catalog/llm-wiki/pages.yaml", {"version": 1, "pages": [page]})
    with pytest.raises(WoonError, match="metadata drift.*public_redirect_from"):
        prepare_public_projection(vault, site)


def test_private_owner_produces_no_redirect_and_target_removal_invalidates_apply(
    tmp_path: Path,
) -> None:
    page = _page(page_id="Wiki/one", title="One", publication_state="publish",
                 access="public", slug="one")
    page["frontmatter"]["public_redirect_from"] = ["old"]
    vault, site = _write_fixture(tmp_path, [page])
    report = prepare_public_projection(vault, site)
    apply_public_projection(report)
    assert (report.content_root / "old.html").exists()
    page["frontmatter"].update(publication_state="private", access="local-only")
    _write_yaml(vault / "catalog/llm-wiki/pages.yaml", {"version": 1, "pages": [page]})
    hidden = prepare_public_projection(vault, site)
    assert not hidden.documents and not hidden.redirects
    with pytest.raises(WoonError, match="preflight is stale"):
        apply_public_projection(report)
    apply_public_projection(hidden)
    assert not (hidden.content_root / "old.html").exists()


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


def test_public_heading_links_match_gfm_ids_and_preserve_uri_links(tmp_path: Path) -> None:
    # Expected IDs were rendered by the site's locked Jekyll/GFM converter.
    headings = (
        ("실행 파일의 쓰기를 막는 이유", "실행-파일의-쓰기를-막는-이유"),
        ("HTTP API: 오류 & 복구", "http-api-오류--복구"),
        ("A  B\tC_D", "a--b-c_d"),
        ("C++ / Kotlin (JVM)", "c--kotlin-jvm"),
        ("École é ²Ⅳ‿", "école-é-ⅳ‿"),
    )
    uri = "[원문](https://example.com/doc#A%20B)\n[참조][source]\n\n[source]: https://example.com/#C_D"
    body = "\n".join(
        f"[[Wiki/target#{heading}|절 {index}]]" for index, (heading, _) in enumerate(headings)
    )
    body += "\n[[Wiki/target|전체]]\n" + uri
    source_ids = ["source://public/example"]
    pages = [
        _hidden_wiki_hub(),
        _page(
            page_id="Wiki/home",
            title="Home",
            publication_state="publish",
            access="public",
            slug="home",
            parent="[[wiki/Wiki/README|Wiki]]",
            body=body,
            source_ids=source_ids,
        ),
        _page(
            page_id="Wiki/target",
            title="Target",
            publication_state="publish",
            access="public",
            slug="target",
            parent="[[wiki/Wiki/README|Wiki]]",
            source_ids=source_ids,
            body="\n\n".join(f"## {heading}" for heading, _ in headings),
        ),
    ]
    vault, site = _write_fixture(tmp_path, pages)
    report = prepare_public_projection(vault, site)
    projected = next(
        page.content.decode() for page in report.documents if page.page_id == "Wiki/home"
    )
    for index, (_heading, fragment) in enumerate(headings):
        assert f"[절 {index}](/wiki/target/#{fragment})" in projected
    assert "[전체](/wiki/target/)" in projected
    assert uri in projected
    assert body in (vault / "wiki/Wiki/home.md").read_text()


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
                "## 직접 하위 키워드\n\n"
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
    assert "## 직접 하위 키워드\n\n- [가이드](/wiki/guide/)" in projected
    assert "## 준비\n\n개발 환경을 확인한다." in projected
    assert "woon-wiki-children" not in projected
    assert "### 확인\n\n컴파일러를 실행한다." in projected
    assert "## 함수\n\n값을 받아 결과를 돌려준다." in projected
    assert projected.index("## 목차") < projected.index("## 직접 하위 키워드")
    assert projected.index("## 직접 하위 키워드") < projected.index("## 준비")


def test_public_projection_disambiguates_repeated_parent_titles_with_ancestry(
    tmp_path: Path,
) -> None:
    source_id = "source://public/example"
    pages = [
        _hidden_wiki_hub(),
        _page(
            page_id="Wiki/ai",
            title="AI·머신러닝",
            publication_state="publish",
            access="public",
            slug="ai",
            parent="[[wiki/Wiki/README|Wiki]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/books",
            title="책",
            publication_state="publish",
            access="public",
            slug="books",
            parent="[[wiki/Wiki/README|Wiki]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/books/ai",
            title="AI·머신러닝",
            publication_state="publish",
            access="public",
            slug="books-ai",
            parent="[[wiki/Wiki/books|책]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/ai/foundations",
            title="머신러닝 기초",
            publication_state="publish",
            access="public",
            slug="ml-foundations",
            parent="[[wiki/Wiki/ai|AI·머신러닝]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/books/ai/guide",
            title="AI 학습서",
            publication_state="publish",
            access="public",
            slug="ai-guide",
            parent="[[wiki/Wiki/books/ai|AI·머신러닝]]",
            source_ids=[source_id],
        ),
        _page(
            page_id="Wiki/books/ai/guide/chapter",
            title="첫 장",
            publication_state="publish",
            access="public",
            slug="ai-guide-chapter",
            parent="[[wiki/Wiki/books/ai/guide|AI 학습서]]",
            source_ids=[source_id],
        ),
    ]
    pages[1]["frontmatter"]["public_nav_root"] = True
    pages[2]["frontmatter"]["public_nav_root"] = True
    vault, site = _write_fixture(tmp_path, pages)

    apply_public_projection(prepare_public_projection(vault, site))

    def metadata(slug: str) -> dict[str, Any]:
        content = (site / f"generated/public-content/{slug}.md").read_text(encoding="utf-8")
        return yaml.safe_load(content.split("---", 2)[1])

    technical_child = metadata("ml-foundations")
    assert technical_child["parent"] == "AI·머신러닝"
    assert "grand_parent" not in technical_child
    book_child = metadata("ai-guide")
    assert book_child["parent"] == "AI·머신러닝"
    assert book_child["grand_parent"] == "책"
    chapter = metadata("ai-guide-chapter")
    assert chapter["parent"] == "AI 학습서"
    assert chapter["grand_parent"] == "AI·머신러닝"
    assert chapter["ancestor"] == "책"


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
