from __future__ import annotations

from io import StringIO
from pathlib import Path

from woon_core.people.cli import run_people
from woon_core.people.private_history import (
    VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY,
    VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH,
    PrivatePersonHistoryService,
    _history_role_label,
)
from woon_core.people.service import PersonService


def _write_person_card(
    vault: Path, *, person_id: str, title: str, person_scope: str = "general"
) -> None:
    path = vault / "users" / person_id / "README.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: Wiki\n"
        f"title: {title}\n"
        "entity_type: person\n"
        f"person_id: {person_id}\n"
        "person_kind: related-person\n"
        f"person_scope: {person_scope}\n"
        "relationship_to_owner: 테스트\n"
        "---\n\n"
        f"# {title}\n",
        encoding="utf-8",
    )


def _write_ledger(novel: Path, *, include_kim: bool, candidates: bool) -> None:
    people = """
  - person_id: lee-minjeong
    links:
      - path: vault-source/observations.md
        role: subject
        basis: user-confirmed
        evidence: 사용자가 이민정과 원문 자료의 연결을 확인함
"""
    if include_kim:
        people += """
  - person_id: kim-heejun
    links:
      - path: create/scene.md
        role: participant
        basis: user-confirmed
        evidence: 사용자가 김희준과 장면 기록의 연결을 확인함
"""
    review = ""
    if candidates:
        review = """
review_candidates:
  - name: 동명이 후보
    path: create/scene.md
    evidence: 사용자 확인 전에는 같은 사람인지 확정할 수 없음
"""
    ledger = (
        "version: 1\naccess: local-only\nsource_kind: novel\npeople:\n"
        + people
        + review
    )
    path = novel / "work/people/person-link-ledger.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ledger, encoding="utf-8")


def test_private_history_sync_projects_explicit_links_and_removes_resolved_review(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    _write_person_card(vault, person_id="kim-heejun", title="김희준")
    _write_person_card(
        vault, person_id="lee-minjeong", title="이민정", person_scope="novel-local-only"
    )
    source = novel / "vault-source/observations.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private source body\n", encoding="utf-8")
    scene = novel / "create/scene.md"
    scene.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text("private scene body\n", encoding="utf-8")
    _write_ledger(novel, include_kim=True, candidates=True)

    result = PrivatePersonHistoryService(vault, novel).sync()

    assert (result.people, result.links, result.candidates) == (2, 2, 1)
    vault_dashboard = (
        vault / VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY / "lee-minjeong.md"
    ).read_text(encoding="utf-8")
    assert "확정 자료: 1개" in vault_dashboard
    assert "observations.md" not in vault_dashboard
    assert "private source body" not in vault_dashboard
    novel_dashboard = (novel / "work/people/dashboards/lee-minjeong.md").read_text(
        encoding="utf-8"
    )
    assert "[observations.md]" in novel_dashboard
    assert "../../../vault-source/observations.md" in novel_dashboard
    assert "woon-knowledge" not in novel_dashboard
    review = vault / VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH
    assert review.exists()
    assert "동명이 후보" in review.read_text(encoding="utf-8")
    assert "비공개 이력" in (vault / "users/kim-heejun/README.md").read_text(encoding="utf-8")

    _write_ledger(novel, include_kim=False, candidates=False)
    second = PrivatePersonHistoryService(vault, novel).sync()

    assert second.candidates == 0
    assert not review.exists()
    assert not (vault / VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY / "kim-heejun.md").exists()
    assert "woon-private-person-history:start" not in (
        vault / "users/kim-heejun/README.md"
    ).read_text(encoding="utf-8")
    assert PersonService(vault).private_history_card("lee-minjeong").title == "이민정"


def test_private_history_cli_requires_explicit_local_novel_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    _write_person_card(vault, person_id="lee-minjeong", title="이민정")
    source = novel / "vault-source/observations.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private source body\n", encoding="utf-8")
    _write_ledger(novel, include_kim=False, candidates=False)
    output = StringIO()

    run_people(
        [
            "private-history-sync",
            "--vault",
            str(vault),
            "--novel-root",
            str(novel),
        ],
        output,
    )

    assert "status: ok" in output.getvalue()
    assert "people: 1" in output.getvalue()


def test_private_history_uses_korean_role_labels() -> None:
    assert _history_role_label("mentioned") == "일정에 언급"
    assert _history_role_label("organizer") == "일정 주관"
