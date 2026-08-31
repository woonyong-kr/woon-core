import json
from datetime import date
from pathlib import Path

from woon_core.knowledge.novel_wiki_projection import (
    _render_page,
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
    entity_section: str | None = None,
    sequence: int | None = None,
    navigation_groups: str = "",
    body: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_row = f"parent: '{parent}'\n" if parent else ""
    entity_row = f"entity_kind: {entity_kind}\n" if entity_kind else ""
    entity_section_row = f"entity_section: {entity_section}\n" if entity_section else ""
    lifecycle_row = (
        "lifecycle_status: active\n"
        if entity_kind in {"project", "person", "career", "application"}
        else ""
    )
    sequence_row = f"sequence: {sequence}\n" if sequence is not None else ""
    path.write_text(
        "---\n"
        f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n{entity_row}{entity_section_row}{lifecycle_row}"
        f"{sequence_row}{navigation_groups}{parent_row}"
        f"keywords:\n- {title}\naliases: []\nview_mode: tree\n"
        "updated: 2026-08-25\nsummary: 테스트 문서다.\n"
        "knowledge_state: 확인 필요\n---\n\n"
        f"# {title}\n\n{body}".rstrip()
        + "\n",
        encoding="utf-8",
    )


def test_render_preserves_existing_human_reviewed_navigation_groups(tmp_path: Path) -> None:
    path = tmp_path / "hub.md"
    _page(
        path,
        title="기존 허브",
        canonical_id="private/novel/existing",
        node_kind="hub",
        parent=None,
        navigation_groups=(
            "navigation_groups:\n"
            "- label: 선형 탐색\n"
            "  children:\n"
            "  - private/novel/existing/first\n"
        ),
    )

    rendered = _render_page(
        path,
        {
            "type": "Wiki",
            "title": "갱신된 허브",
            "canonical_id": "private/novel/existing",
            "node_kind": "hub",
        },
        "# 갱신된 허브\n",
    ).decode("utf-8")

    assert "navigation_groups:" in rendered
    assert "label: 선형 탐색" in rendered
    assert "private/novel/existing/first" in rendered


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
        sequence=1,
    )
    _page(
        vault / "wiki/personal/projects/(미정)소설-집필.md",
        title="(미정)소설 집필",
        canonical_id="private-novel",
        node_kind="entity",
        parent="[[wiki/projects|프로젝트]]",
        entity_kind="project",
        body="현재 목표와 집필 기준을 관리한다.",
        navigation_groups=(
            "navigation_groups:\n"
            "- label: 작품 탐색\n"
            "  children:\n"
            "  - private/novel/장면-원고\n"
            "  - private/novel/집필-계획\n"
            "  - private/novel/인물\n"
        ),
    )
    _page(
        vault / "wiki/private/이민정.md",
        title="이민정",
        canonical_id="private/이민정",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        entity_kind="person",
        sequence=2,
        body="소설에 연결된 인물이다.",
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
    scene_hub = (vault / "wiki/private/novel/장면-원고/README.md").read_text(encoding="utf-8")
    people_hub = (vault / "wiki/private/novel/인물/README.md").read_text(encoding="utf-8")
    assert first.category_count == 3
    assert first.source_count == 1
    assert first.judgment_count == 1
    assert first.relation_count == 1
    assert "[[wiki/private/novel/장면-원고/README|소설 · 장면·원고]]" in project
    assert "summary: (미정)소설의 장면·원고 키워드다." in (
        vault / "wiki/private/novel/장면-원고/README.md"
    ).read_text(encoding="utf-8")
    assert "[첫 장면](../../_sources/novel/vault-source/scene.md)" in scene_hub
    assert "[[wiki/private/이민정|이민정]]" in people_hub
    assert not (vault / "wiki/private/novel/장면-원고/01-첫-장면.md").exists()
    assert not (vault / "wiki/private/novel/집필-계획/judgment-01-다음-집필-순서.md").exists()
    assert not (vault / "wiki/private/novel/인물/lee-minjeong.md").exists()
    assert replay.changed_count == 0

    legacy_page = vault / "wiki/private/novel/인물/legacy-detail.md"
    _page(
        legacy_page,
        title="보존할 기존 상세 기록",
        canonical_id="private/novel/인물/legacy-detail",
        node_kind="topic",
        parent="[[wiki/private/novel/인물/README|소설 · 인물]]",
    )
    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    legacy_manifest = json.loads(receipt.read_text(encoding="utf-8"))
    legacy_manifest["version"] = 2
    legacy_manifest.pop("owned_pages")
    receipt.write_text(json.dumps(legacy_manifest), encoding="utf-8")

    migration = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))
    assert legacy_page not in migration.stale_pages
    apply_novel_wiki_projection(vault, migration)
    assert legacy_page.is_file()

    source.write_text("# 장면 원본\n\n변경됨\n", encoding="utf-8")
    changed = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))
    assert changed.changed_count == 3
    assert b'"projection_day": "2026-08-26"' in changed.manifest


def test_groups_large_event_timeline_into_linear_stages(tmp_path: Path) -> None:
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
        body="현재 사건 구조를 선형으로 관리한다.",
    )
    navigation = novel / "work/navigation/사건-히스토리.md"
    navigation.parent.mkdir(parents=True)
    navigation.write_text(
        "# 사건·히스토리\n\n"
        "## 사건 장부\n\n"
        "- [사건 근거 장부](../analysis/event-evidence-ledger-2026-08-07.md)\n",
        encoding="utf-8",
    )
    ledger = novel / "work/analysis/event-evidence-ledger-2026-08-07.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "# 사건\n\n" + "\n".join(f"## {number}. 사건 {number}\n\n근거" for number in range(1, 26)),
        encoding="utf-8",
    )

    report = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 25))
    apply_novel_wiki_projection(vault, report)

    hub = (vault / "wiki/private/novel/사건-히스토리/README.md").read_text(encoding="utf-8")
    assert report.event_count == 25
    assert "- 사건 장부" in hub
    assert "[사건 근거 장부]" in hub
    assert not (vault / "wiki/private/novel/사건-히스토리/event-01.md").exists()
