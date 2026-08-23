from __future__ import annotations

from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.people.service import PersonIdentityIdentifierInput, PersonService


def _write_document(path: Path, *, title: str = "회의 기록") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: Source\ntitle: {title}\npublish: false\naccess: local-only\n"
        "status: Active\npeople: []\n---\n\n# "
        f"{title}\n",
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> PersonService:
    _write_document(
        tmp_path / "users/choi-woonyoung/README.md",
        title="최우녕",
    )
    owner = tmp_path / "users/choi-woonyoung/README.md"
    owner.write_text(
        owner.read_text(encoding="utf-8").replace(
            "status: Active\npeople: []",
            "status: Active\nentity_type: person\nperson_id: choi-woonyoung\n"
            "person_kind: vault-owner\nperson_scope: general\n"
            "relationship_to_owner: 볼트 사용자\npeople:\n"
            '  - "[[users/choi-woonyoung/README|최우녕]]"',
        ),
        encoding="utf-8",
    )
    return PersonService(tmp_path)


def test_upserts_a_small_general_person_card_idempotently(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="관계와 맥락 확인 중",
        purpose="김희준과 직접 관련된 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    second = service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="관계와 맥락 확인 중",
        purpose="김희준과 직접 관련된 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
        expected_revision=first.card.revision,
    )

    assert first.created is True
    assert first.changed is True
    assert second.created is False
    assert second.changed is False
    assert service.find("희준") == (second.card,)


def test_links_one_document_with_explicit_roles_without_duplicate_entries(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="학습 관련 맥락 확인 중",
        purpose="직접 관련된 학습 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    _write_document(tmp_path / "inbox/meeting.md", title="학습 회의")

    first = service.link_document(
        relative_path="inbox/meeting.md",
        person_id="kim-heejun",
        roles=("participant", "source-provider"),
        evidence="회의 기록의 참석자와 자료 제공자 표기",
    )
    second = service.link_document(
        relative_path="inbox/meeting.md",
        person_id="kim-heejun",
        roles=("participant", "source-provider"),
        evidence="회의 기록의 참석자와 자료 제공자 표기",
    )

    assert first.changed is True
    assert second.changed is False
    documents = service.documents_for("kim-heejun")
    assert documents == (documents[0],)
    assert documents[0].relative_path == "inbox/meeting.md"
    assert documents[0].roles == ("participant", "source-provider")


def test_ignores_repository_instructions_outside_person_index_content_roots(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/copilot-instructions.md").write_text(
        "plain instructions\n", encoding="utf-8"
    )

    assert service.documents_for("choi-woonyoung") == ()


def test_ignores_unparseable_legacy_source_during_person_dashboard_lookup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    legacy = tmp_path / "sources/imports/legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\ntitle: [broken\n---\n", encoding="utf-8")

    assert service.documents_for("choi-woonyoung") == ()


def test_ignores_auxiliary_markdown_without_a_title_during_dashboard_lookup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    path = tmp_path / "maps/sample.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: mindmap\n---\n\n# 보조 지도\n", encoding="utf-8")

    assert service.documents_for("choi-woonyoung") == ()


def test_default_owner_finds_existing_records_without_mass_metadata_rewrite(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _write_document(tmp_path / "brain/decision.md", title="학습 결정")

    documents = service.documents_for("choi-woonyoung")

    assert [document.relative_path for document in documents] == ["brain/decision.md"]
    assert documents[0].record_owner == "choi-woonyoung"


def test_private_history_limits_owner_records(tmp_path: Path) -> None:
    service = _service(tmp_path)
    general = tmp_path / "maps/general.md"
    general.parent.mkdir(parents=True)
    general.write_text(
        "---\ntype: Wiki\ntitle: 일반 지도\nrecord_owner: choi-woonyoung\npeople: []\n---\n",
        encoding="utf-8",
    )
    daily = tmp_path / "inbox/daily/2026-08-18.md"
    daily.parent.mkdir(parents=True)
    daily.write_text(
        '---\ntype: Daily\ntitle: "2026-08-18"\nrecord_owner: choi-woonyoung\npeople: []\n---\n',
        encoding="utf-8",
    )
    digest = tmp_path / "inbox/daily-digests/2026-08-18.md"
    digest.parent.mkdir(parents=True)
    digest.write_text(
        '---\ntype: DailyDigest\ntitle: "2026-08-18 Codex 하루 정리"\n'
        "record_owner: choi-woonyoung\npeople: []\n---\n",
        encoding="utf-8",
    )

    documents = service.private_history_documents("choi-woonyoung")

    assert [document.relative_path for document in documents] == ["inbox/daily/2026-08-18.md"]
    assert all(
        "daily-digests" not in document.relative_path
        for document in service.documents_for("choi-woonyoung")
    )


def test_default_owner_does_not_turn_unrelated_person_into_document_link(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="관계와 맥락 확인 중",
        purpose="직접 관련된 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    _write_document(tmp_path / "brain/decision.md", title="학습 결정")

    assert service.documents_for("kim-heejun") == ()


def test_calendar_title_resolution_uses_explicit_private_identifiers_only(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="학습 관련 맥락 확인 중",
        purpose="김희준과 직접 관련된 기록을 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    private_card = tmp_path / "users/lee-minjeong/README.md"
    _write_document(private_card, title="이민정")
    private_card.write_text(
        private_card.read_text(encoding="utf-8").replace(
            "status: Active\npeople: []",
            "status: Active\nentity_type: person\nperson_id: lee-minjeong\n"
            "person_kind: related-person\nperson_scope: novel-local-only\n"
            "relationship_to_owner: 창작 원문 경계\npeople: []",
        ),
        encoding="utf-8",
    )

    general = service.calendar_title_resolution("김희준과 일정 조율")
    private_before_confirmation = service.calendar_title_resolution("이민정 면접")
    update = service.set_identity_identifiers(
        person_id="lee-minjeong",
        identifiers=(
            PersonIdentityIdentifierInput("이민정"),
            PersonIdentityIdentifierInput("민정"),
        ),
        evidence="사용자가 이민정과 민정이 같은 사람이라고 직접 확인함",
    )
    private_after_confirmation = service.calendar_title_resolution("민정이 면접")

    assert [(item.reference.person_id, item.reference.title) for item in general.matches] == [
        ("kim-heejun", "김희준")
    ]
    assert service.calendar_title_resolution("김희준관리 일정").matches == ()
    assert private_before_confirmation.matches == ()
    assert update.changed is True
    assert [identifier.value for identifier in update.card.identifiers] == ["이민정", "민정"]
    private_matches = [
        (item.reference.person_id, item.identifier.value)
        for item in private_after_confirmation.matches
    ]
    assert private_matches == [("lee-minjeong", "민정")]


def test_upsert_records_korean_full_and_surname_free_default_identifiers(tmp_path: Path) -> None:
    service = _service(tmp_path)

    card = service.upsert_card(
        person_id="hong-yoonki",
        title="홍윤기",
        person_kind="related-person",
        relationship_to_owner="관계와 맥락 확인 중",
        purpose="홍윤기와 직접 관련된 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    ).card

    assert [identifier.value for identifier in card.identifiers] == ["홍윤기", "윤기"]
    resolution = service.calendar_title_resolution("윤기와 약속")
    assert [match.reference.person_id for match in resolution.matches] == ["hong-yoonki"]


def test_calendar_title_resolution_leaves_same_identifier_for_review_until_context_resolves(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.upsert_card(
        person_id="park-minjeong",
        title="박민정",
        person_kind="related-person",
        relationship_to_owner="면접 관련 맥락 확인 중",
        purpose="같은 이름의 인물을 일정에서 구분하기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    service.set_identity_identifiers(
        person_id="park-minjeong",
        identifiers=(PersonIdentityIdentifierInput("민정"),),
        evidence="사용자가 박민정을 민정으로 부른다고 직접 확인함",
    )
    service.upsert_card(
        person_id="kim-minjeong",
        title="김민정",
        person_kind="related-person",
        relationship_to_owner="프로젝트 관련 맥락 확인 중",
        purpose="같은 이름의 인물을 일정에서 구분하기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    service.set_identity_identifiers(
        person_id="kim-minjeong",
        identifiers=(PersonIdentityIdentifierInput("민정", context_terms=("프로젝트",)),),
        evidence="사용자가 프로젝트 일정의 민정은 김민정이라고 직접 확인함",
    )

    ambiguous = service.calendar_title_resolution("민정과 일정 조율")
    contextual = service.calendar_title_resolution("민정 프로젝트 일정 조율")

    assert ambiguous.matches == ()
    ambiguities = [
        (item.identifier, [candidate.person_id for candidate in item.candidates])
        for item in ambiguous.ambiguities
    ]
    assert ambiguities == [("민정", ["kim-minjeong", "park-minjeong"])]
    assert [(item.reference.person_id, item.identifier.value) for item in contextual.matches] == [
        ("kim-minjeong", "민정")
    ]


def test_materializes_default_owner_without_rewriting_private_or_novel_records(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _write_document(tmp_path / "brain/decision.md", title="학습 결정")
    _write_document(tmp_path / "sources/private/original.md", title="비공개 원본")
    novel_card = tmp_path / "users/lee-minjeong/README.md"
    _write_document(novel_card, title="이민정")
    novel_card.write_text(
        novel_card.read_text(encoding="utf-8").replace(
            "status: Active\npeople: []",
            "status: Active\nentity_type: person\nperson_id: lee-minjeong\n"
            "person_kind: related-person\nperson_scope: novel-local-only\n"
            "relationship_to_owner: 창작 원문 경계\npeople: []",
        ),
        encoding="utf-8",
    )
    _write_document(tmp_path / "users/lee-minjeong/notes.md", title="창작 메모")

    first = service.materialize_default_owner()
    second = service.materialize_default_owner()

    assert first.changed == 1
    assert second.changed == 0
    assert "record_owner: choi-woonyoung" in (tmp_path / "brain/decision.md").read_text(
        encoding="utf-8"
    )
    assert "record_owner:" not in (tmp_path / "sources/private/original.md").read_text(
        encoding="utf-8"
    )
    assert "record_owner:" not in (tmp_path / "users/lee-minjeong/notes.md").read_text(
        encoding="utf-8"
    )


def test_rejects_guess_driven_cards_and_compiled_or_private_link_targets(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(WoonError, match="explicit-request or repeated-evidence"):
        service.upsert_card(
            person_id="kim-heejun",
            title="김희준",
            person_kind="related-person",
            relationship_to_owner="확인 필요",
            purpose="이름이 한 번 나왔다.",
            creation_basis="name-mention",
        )

    service.upsert_card(
        person_id="kim-heejun",
        title="김희준",
        person_kind="related-person",
        relationship_to_owner="확인 필요",
        purpose="직접 관련 자료를 다시 찾기 위한 연결점이다.",
        creation_basis="explicit-request",
    )
    _write_document(tmp_path / "wiki/example.md", title="컴파일 산출물")
    _write_document(tmp_path / "sources/private/example.md", title="개인 원본")

    with pytest.raises(WoonError, match="compiled Wiki"):
        service.link_document(
            relative_path="wiki/example.md",
            person_id="kim-heejun",
            roles=("participant",),
            evidence="명시된 참석자",
        )
    with pytest.raises(WoonError, match="compiled Wiki"):
        service.link_document(
            relative_path="sources/private/example.md",
            person_id="kim-heejun",
            roles=("participant",),
            evidence="명시된 참석자",
        )
