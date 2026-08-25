from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.people.cli import run_people
from woon_core.people.private_history import (
    RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY,
    VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH,
    PrivatePersonHistoryService,
)
from woon_core.people.service import PersonService


def _write_person_card(
    vault: Path, *, person_id: str, title: str, person_scope: str = "general"
) -> None:
    root = "private" if person_scope == "novel-local-only" else "personal"
    path = vault / "wiki" / root / f"{person_id}.md"
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


def _write_ledger(novel: Path, *, include_participant: bool, candidates: bool) -> None:
    people = """
  - person_id: subject-person
    links:
      - work_id: creative-work-1
        path: vault-source/observations.md
        role: subject
        basis: user-confirmed
        evidence: 사용자가 대상 인물과 원문 자료의 연결을 확인함
"""
    if include_participant:
        people += """
  - person_id: participant-person
    links:
      - work_id: creative-work-1
        path: create/scene.md
        role: participant
        basis: user-confirmed
        evidence: 사용자가 참여 인물과 장면 기록의 연결을 확인함
"""
    review = ""
    if candidates:
        review = """
review_candidates:
  - work_id: creative-work-1
    name: 동명이 후보
    path: create/scene.md
    evidence: 사용자 확인 전에는 같은 사람인지 확정할 수 없음
"""
    ledger = (
        "version: 2\naccess: local-only\nsource_kind: novel\n"
        "work_catalog: work/work-catalog.yaml\npeople:\n" + people + review
    )
    path = novel / "work/people/person-link-ledger.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ledger, encoding="utf-8")


def _write_work_catalog(novel: Path) -> None:
    path = novel / "work/work-catalog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 1\n"
        "access: local-only\n"
        "publish: false\n"
        "works:\n"
        "  - work_id: creative-work-1\n"
        "    work_number: 1\n"
        "    entity_type: creative-work\n"
        "    category: novel\n"
        '    title: "(미정)"\n'
        "    status: active\n"
        "    canonical_entry: create/README.md\n"
        "    navigation:\n"
        "      - path: work/analysis.md\n"
        "        role: event-ledger\n",
        encoding="utf-8",
    )


def test_private_history_sync_projects_explicit_links_and_removes_resolved_review(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    _write_person_card(vault, person_id="participant-person", title="참여 인물")
    _write_person_card(
        vault, person_id="subject-person", title="대상 인물", person_scope="novel-local-only"
    )
    source = novel / "vault-source/observations.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private source body\n", encoding="utf-8")
    scene = novel / "create/scene.md"
    scene.parent.mkdir(parents=True, exist_ok=True)
    scene.write_text("private scene body\n", encoding="utf-8")
    canonical = novel / "create/README.md"
    canonical.write_text("# private creative work\n", encoding="utf-8")
    analysis = novel / "work/analysis.md"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text("private analysis\n", encoding="utf-8")
    _write_work_catalog(novel)
    _write_ledger(novel, include_participant=True, candidates=True)
    retired_views = (
        (
            vault / RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY / "README.md",
            "woon_projection: vault-private-person-history\n",
        ),
        (
            novel / "work/dashboards/README.md",
            "woon_projection: novel-private-work\n",
        ),
        (
            novel / "work/people/dashboards/README.md",
            "woon_projection: novel-private-person-history\n",
        ),
    )
    for path, marker in retired_views:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(marker, encoding="utf-8")
    subject_card = vault / "wiki/private/subject-person.md"
    subject_card.write_text(
        subject_card.read_text(encoding="utf-8")
        + "\n<!-- woon-private-person-history:start -->\n"
        + "duplicate dashboard link\n"
        + "<!-- woon-private-person-history:end -->\n",
        encoding="utf-8",
    )

    result = PrivatePersonHistoryService(vault, novel).sync()

    assert (result.works, result.people, result.links, result.candidates) == (1, 2, 2, 1)
    assert not (vault / RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY).exists()
    assert not (novel / "work/dashboards").exists()
    assert not (novel / "work/people/dashboards").exists()
    assert "woon-private-person-history:start" not in subject_card.read_text(encoding="utf-8")
    review = vault / VAULT_PRIVATE_HISTORY_REVIEW_RELATIVE_PATH
    assert review.exists()
    review_text = review.read_text(encoding="utf-8")
    assert "동명이 후보" in review_text
    assert "creative-work-1" in review_text
    assert "사용자 확인 전에는 같은 사람인지 확정할 수 없음" in review_text
    assert "create/scene.md" not in review_text
    assert "woon-private-person-history:start" not in (
        vault / "wiki/personal/participant-person.md"
    ).read_text(encoding="utf-8")

    _write_ledger(novel, include_participant=False, candidates=False)
    second = PrivatePersonHistoryService(vault, novel).sync()

    assert second.candidates == 0
    assert not review.exists()
    assert not (
        vault / RETIRED_VAULT_PRIVATE_HISTORY_RELATIVE_DIRECTORY / "participant-person.md"
    ).exists()
    assert "woon-private-person-history:start" not in (
        vault / "wiki/personal/participant-person.md"
    ).read_text(encoding="utf-8")
    assert PersonService(vault).private_history_card("subject-person").title == "대상 인물"


def test_private_history_cli_requires_explicit_local_novel_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    _write_person_card(vault, person_id="subject-person", title="대상 인물")
    source = novel / "vault-source/observations.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private source body\n", encoding="utf-8")
    canonical = novel / "create/README.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# private creative work\n", encoding="utf-8")
    analysis = novel / "work/analysis.md"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text("private analysis\n", encoding="utf-8")
    _write_work_catalog(novel)
    _write_ledger(novel, include_participant=False, candidates=False)
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
    assert "works: 1" in output.getvalue()
    assert "people: 1" in output.getvalue()


def test_private_history_rejects_links_to_an_unknown_work(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    novel = tmp_path / "novel"
    _write_person_card(vault, person_id="subject-person", title="대상 인물")
    source = novel / "vault-source/observations.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private source body\n", encoding="utf-8")
    canonical = novel / "create/README.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# private creative work\n", encoding="utf-8")
    analysis = novel / "work/analysis.md"
    analysis.parent.mkdir(parents=True, exist_ok=True)
    analysis.write_text("private analysis\n", encoding="utf-8")
    _write_work_catalog(novel)
    _write_ledger(novel, include_participant=False, candidates=False)
    ledger = novel / "work/people/person-link-ledger.yaml"
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace("creative-work-1", "missing-work"),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="unknown work"):
        PrivatePersonHistoryService(vault, novel).sync()
