from datetime import date
from pathlib import Path

from woon_core.knowledge.novel_wiki_projection import (
    apply_novel_wiki_projection,
    prepare_novel_wiki_projection,
)


def _page(
    path: Path,
    *,
    title: str,
    canonical_id: str,
    node_kind: str,
    parent: str | None,
    entity_kind: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_row = f"parent: '{parent}'\n" if parent else ""
    entity_row = f"entity_kind: {entity_kind}\n" if entity_kind else ""
    path.write_text(
        "---\n"
        f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n{entity_row}{parent_row}"
        f"keywords:\n- {title}\naliases: []\nview_mode: tree\n"
        "updated: 2026-08-25\nsummary: 테스트 문서다.\n"
        "knowledge_state: 확인 필요\n---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )


def test_projects_every_novel_navigation_source_into_private_wiki_and_replays(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    novel = vault / "wiki/private/_sources/novel"
    _page(
        vault / "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
    )
    _page(
        vault / "wiki/projects.md",
        title="프로젝트",
        canonical_id="projects",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
    )
    _page(
        vault / "wiki/personal/projects/(미정)소설-집필.md",
        title="(미정)소설 집필",
        canonical_id="private-novel",
        node_kind="entity",
        parent="[[wiki/projects|프로젝트]]",
        entity_kind="project",
    )
    source = novel / "vault-source/scene.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 장면 원본\n", encoding="utf-8")
    navigation = novel / "work/navigation/장면-원고.md"
    navigation.parent.mkdir(parents=True)
    navigation.write_text(
        "# 장면·원고\n\n- [첫 장면](../../vault-source/scene.md)\n",
        encoding="utf-8",
    )
    (navigation.parent / "집필-계획.md").write_text("# 집필 계획\n", encoding="utf-8")
    (navigation.parent / "인물.md").write_text("# 인물\n", encoding="utf-8")
    planning = novel / "work/planning/corpus-reading-2026-08-07.md"
    planning.parent.mkdir(parents=True)
    planning.write_text(
        "# 집필 판단\n\n## 다음 집필 순서\n\n첫 장면을 다듬는다.\n",
        encoding="utf-8",
    )
    people = novel / "work/people/person-link-ledger.yaml"
    people.parent.mkdir(parents=True)
    people.write_text(
        "people:\n- person_id: lee-minjeong\n  links:\n  - path: vault-source/scene.md\n",
        encoding="utf-8",
    )

    first = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 25))
    apply_novel_wiki_projection(vault, first)
    replay = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))

    project = (vault / "wiki/personal/projects/(미정)소설-집필.md").read_text(encoding="utf-8")
    source_page = (vault / "wiki/private/novel/장면-원고/01-첫-장면.md").read_text(encoding="utf-8")
    judgment_page = (
        vault / "wiki/private/novel/집필-계획/judgment-01-다음-집필-순서.md"
    ).read_text(encoding="utf-8")
    person_page = (vault / "wiki/private/novel/인물/lee-minjeong.md").read_text(encoding="utf-8")
    assert first.category_count == 3
    assert first.source_count == 1
    assert first.judgment_count == 1
    assert first.relation_count == 1
    assert "[[wiki/private/novel/장면-원고/README|소설 · 장면·원고]]" in project
    assert "summary: (미정)소설의 장면·원고 키워드다." in (
        vault / "wiki/private/novel/장면-원고/README.md"
    ).read_text(encoding="utf-8")
    assert "source_path: wiki/private/_sources/novel/vault-source/scene.md" in source_page
    assert "../../_sources/novel/vault-source/scene.md" in source_page
    assert "summary: (미정)소설의 다음 집필 순서 판단이다." in judgment_page
    assert "summary: (미정)소설과 이민정의 확인된 연결이다." in person_page
    assert replay.changed_count == 0

    source.write_text("# 장면 원본\n\n변경됨\n", encoding="utf-8")
    changed = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))
    assert changed.changed_count == 6
    assert b'"projection_day": "2026-08-26"' in changed.manifest
