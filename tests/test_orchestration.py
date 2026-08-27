import hashlib
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.orchestration import (
    _AUTOMATION_PERSON_PROMPT_GUARD_TERMS,
    AutomationContract,
    _has_person_protection,
    _validate_wiki_prompt_contract,
    load_orchestrator_settings,
    verify_codex_automation_registry,
)


def write_policy(
    vault: Path,
    *,
    status: str = "planned",
    thread_id: str = "null",
    codex_automation_id: str = "null",
    rrule: str = "null",
    notification_policy: str = "null",
    prompt_sha256: str = "null",
) -> None:
    (vault / "docs").mkdir(parents=True, exist_ok=True)
    (vault / "config").mkdir(exist_ok=True)
    (vault / "docs/second-brain-operating-model.md").write_text("# policy\n", encoding="utf-8")
    (vault / "docs/wiki-information-architecture.md").write_text(
        "# Wiki information architecture\n", encoding="utf-8"
    )
    (vault / "config/person-schema.json").write_text("{}\n", encoding="utf-8")
    (vault / "config/second-brain-orchestrator.yaml").write_text(
        f"""version: 1
policy_document: docs/second-brain-operating-model.md
wiki_contract: docs/wiki-information-architecture.md
timezone: Asia/Seoul
repository_contract:
  personal_vault: woon-knowledge
  brain_is_subdirectory_not_repository: true
  runtime_must_not_contain_git_repository: true
  reusable_code_repository: woon-core
  vault_executable_sources: forbidden
  vault_tool_interface: core-owned-cli
  public_export: disabled_until_verified_projection_exists
runtime:
  checkpoint_path: .local/woon/checkpoints.yaml
  receipt_directory: .local/woon/receipts
  lock_directory: .local/woon/locks
  require_clean_target_revision: true
  no_resident_process: true
  no_auto_commit_or_push: true
global_guards:
  advertising:
    action: ignore_without_persistence
    persist_fields: []
    mutate_mailbox: false
  unallowlisted_or_ambiguous_mail:
    action: ignore_without_persistence
    persist_fields: []
  privacy:
    novel_default: excluded
    chat_default: excluded
    public_output_requires_verified_claim: true
  mutations:
    raw_source_delete: forbidden
    direct_app_database_access: forbidden
    calendar_write_requires_policy_authorization: true
    task_write_requires_purpose: true
    public_publish_requires_separate_authorization: true
  schedule_bridge:
    auto_apply_allowlisted_datetime_mail_only: false
    schedule_apply_path: local-user-authorized
    calendar_name: Woon 일정
    manual_apply_command: native-local-command
    native_adapters: [eventkit-full-access]
    state_path: .local/woon-knowledge/schedule-bridge-state.json
identity:
  schema_path: config/person-schema.json
  default_record_owner: choi-woonyoung
  default_owner_mode: implicit-if-omitted
  person_card_creation: explicit-or-repeated-evidence
  automated_person_card_creation: forbidden
  automated_person_candidate_creation: explicit-facts-review-only
  novel_identity_import: forbidden
  private_history_projection: explicit-local-ledger-only
  private_history_review: candidate-only-delete-when-empty
obsidian_tasks:
  authority: current-action
  write_interface: local-mcp
  routine_root: inbox/tasks/routines
  daily_root: inbox/daily
  receipt_path: .local/woon-knowledge/tasks-state.json
  recurrence: materialize-kst-daily
  areas:
    - id: career
      title: 커리어·일
    - id: learning
      title: 학습·지식
    - id: creative
      title: 창작·발행
    - id: life
      title: 생활·집
    - id: relationship
      title: 관계·사람
    - id: health
      title: 건강·성장
    - id: admin
      title: 행정·재정
  todo:
    required: [verb-first-title, independently-verifiable-action, purpose]
    prohibited: [knowledge-note, raw-original, person-profile, novel-manuscript]
daily_document_pipeline:
  knowledge_canonical_root: wiki
  daily_root: inbox/daily
  source_to_wiki_owner: codex-conversation-ingest
  wiki_to_daily_owner: daily-record-materialization
  daily_to_wiki_promotion: forbidden
  completion_mode: per-stage-reconciliation
  failed_stage: preserve-verified-stage-results
  stages:
    - id: task-projection
      owner: woon-tasks-service
      marker: woon-tasks
      proof: .local/woon-knowledge/tasks-state.json
    - id: codex-projection
      owner: daily-record-materialization
      marker: woon-codex-digest
      proof: .local/woon-knowledge/automation-receipts/daily-record-materialization
    - id: calendar-projection
      owner: woon-calendar-service
      marker: "woon_projection: apple-calendar"
      proof: output-hash-and-reread
    - id: activity-review
      owner: activity-history-review
      marker: review-only
      proof: review-card-and-vault-audit
automations:
  - id: mail-schedule-candidates
    owner: mail-schedule-task
    cadence: four-times-daily
    inputs: [allowlisted-mail]
    output: [candidate]
    checkpoint_key: mail-schedule-candidates
    required_signals: [allowlist]
    prohibited: [advertising-persistence, person-profile-inference, unresolved-identity-link]
    execution:
      mode: candidate-only
      status: {status}
      task_thread_id: {thread_id}
      codex_automation_id: {codex_automation_id}
      rrule: {rrule}
      notification_policy: {notification_policy}
      prompt_sha256: {prompt_sha256}
      owned_paths: [brain/review/mail]
  - id: policy-schedule-apply
    owner: policy-schedule-apply-task
    cadence: explicit-local-request
    inputs: [user-approved-schedule-candidate]
    output: [confirmed-write]
    checkpoint_key: policy-schedule-apply
    required_signals: [user-approval]
    prohibited: [ambiguous-mail-write, person-profile-inference, unresolved-identity-link]
    execution:
      mode: policy-authorized
      status: local-only
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null
      owned_paths: [brain/review/schedule-apply]
  - id: governance-audit
    owner: governance-audit-task
    cadence: daily-and-policy-gate
    inputs: [AGENTS]
    output: [proposal]
    checkpoint_key: governance-audit
    required_signals: [instruction-inventory]
    prohibited: [automatic-policy-delete, person-profile-inference, unresolved-identity-link]
    execution:
      mode: proposal-only
      status: planned
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null
      owned_paths: [brain/review/governance]
cursor_contract:
  policy_change: caller-must-run-governance-preflight
""",
        encoding="utf-8",
    )


def test_loads_planned_automation_without_creating_runtime_state(tmp_path: Path) -> None:
    write_policy(tmp_path)

    settings = load_orchestrator_settings(tmp_path)

    assert settings.policy_document == tmp_path / "docs/second-brain-operating-model.md"
    assert settings.enabled_automations == ()
    assert not settings.checkpoint_path.exists()
    assert settings.automations[0].mode == "candidate-only"


def test_enabled_automation_requires_dedicated_thread_id(tmp_path: Path) -> None:
    write_policy(tmp_path, status="enabled")

    with pytest.raises(WoonError, match="require task_thread_id"):
        load_orchestrator_settings(tmp_path)


def test_rejects_duplicate_checkpoint_and_owned_path(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "cursor_contract:\n",
            """
  - id: knowledge-curation
    owner: knowledge-curation-task
    cadence: daily
    inputs: [brain]
    output: [review]
    checkpoint_key: mail-schedule-candidates
    required_signals: [source-hash]
    prohibited: [raw-delete]
    execution:
      mode: review-only
      status: planned
      task_thread_id: null
      owned_paths: [brain/review/mail]
cursor_contract:
""",
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="duplicate second-brain checkpoint key"):
        load_orchestrator_settings(tmp_path)


def test_rejects_unbounded_cadence_and_runtime_paths(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "checkpoint_path: .local/woon/checkpoints.yaml", "checkpoint_path: checkpoints.yaml"
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(WoonError, match="must be under .local"):
        load_orchestrator_settings(tmp_path)


def test_reports_malformed_policy_yaml_as_woon_error(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    path.write_text("automations: [\n", encoding="utf-8")

    with pytest.raises(WoonError, match="invalid second-brain orchestrator YAML"):
        load_orchestrator_settings(tmp_path)


def test_rejects_unsafe_global_guard_and_enabled_schedule_apply(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "action: ignore_without_persistence\n    persist_fields: []\n    mutate_mailbox: false",
            "action: persist\n    persist_fields: []\n    mutate_mailbox: false",
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="advertising guard"):
        load_orchestrator_settings(tmp_path)


def test_rejects_privacy_contract_and_enabled_schedule_apply(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    unsafe_privacy = path.read_text(encoding="utf-8").replace(
        "novel_default: excluded", "novel_default: captured"
    )
    path.write_text(unsafe_privacy, encoding="utf-8")
    with pytest.raises(WoonError, match="Novel guard"):
        load_orchestrator_settings(tmp_path)

    write_policy(tmp_path)
    unsafe_apply = path.read_text(encoding="utf-8").replace(
        "mode: policy-authorized\n      status: local-only",
        "mode: policy-authorized\n      status: enabled",
    )
    path.write_text(unsafe_apply, encoding="utf-8")
    with pytest.raises(WoonError, match="policy-authorized schedule apply must stay local-only"):
        load_orchestrator_settings(tmp_path)


def test_rejects_registered_local_only_schedule_apply(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    before, schedule_lane = path.read_text(encoding="utf-8").split(
        "  - id: policy-schedule-apply", 1
    )
    schedule_lane = schedule_lane.replace("task_thread_id: null", 'task_thread_id: "thread-001"', 1)
    path.write_text(before + "  - id: policy-schedule-apply" + schedule_lane, encoding="utf-8")

    with pytest.raises(WoonError, match="local-only schedule apply must not register"):
        load_orchestrator_settings(tmp_path)


def test_rejects_unsafe_obsidian_task_contract(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    unsafe_policy = path.read_text(encoding="utf-8").replace(
        "write_interface: local-mcp", "write_interface: direct-database"
    )
    path.write_text(unsafe_policy, encoding="utf-8")

    with pytest.raises(WoonError, match="Obsidian tasks must use the local MCP interface"):
        load_orchestrator_settings(tmp_path)


def test_rejects_unsafe_identity_contract_and_inference_capability(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "automated_person_card_creation: forbidden",
            "automated_person_card_creation: allowed",
        ),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="automated_person_card_creation"):
        load_orchestrator_settings(tmp_path)

    write_policy(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "automated_person_candidate_creation: explicit-facts-review-only",
            "automated_person_candidate_creation: automatic-profile",
        ),
        encoding="utf-8",
    )
    with pytest.raises(WoonError, match="automated_person_candidate_creation"):
        load_orchestrator_settings(tmp_path)

    write_policy(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "prohibited: [advertising-persistence, person-profile-inference, "
            "unresolved-identity-link]",
            "prohibited: [advertising-persistence]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(WoonError, match="must prohibit identity inference"):
        load_orchestrator_settings(tmp_path)


def test_rejects_registered_heartbeat_prompt_drift(tmp_path: Path) -> None:
    expected_prompt = "Run only the policy-approved candidate lane."
    write_policy(
        tmp_path,
        status="enabled",
        thread_id='"thread-001"',
        codex_automation_id='"codex-001"',
        rrule='"FREQ=DAILY;BYHOUR=6;BYMINUTE=0;BYSECOND=0"',
        notification_policy='"failed_runs_only"',
        prompt_sha256=f'"{hashlib.sha256(expected_prompt.encode()).hexdigest()}"',
    )
    settings = load_orchestrator_settings(tmp_path)
    registry = tmp_path / "automations" / "codex-001"
    registry.mkdir(parents=True)
    (registry / "automation.toml").write_text(
        """id = \"codex-001\"
kind = \"heartbeat\"
status = \"ACTIVE\"
target_thread_id = \"thread-001\"
rrule = \"FREQ=DAILY;BYHOUR=6;BYMINUTE=0;BYSECOND=0\"
notification_policy = \"failed_runs_only\"
prompt = \"Run an unsafe unbounded lane.\"
""",
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="prompt digest"):
        verify_codex_automation_registry(settings, tmp_path / "automations")


def test_rejects_registered_heartbeat_without_person_protection(tmp_path: Path) -> None:
    unsafe_prompt = "Run only the policy-approved candidate lane."
    write_policy(
        tmp_path,
        status="enabled",
        thread_id='"thread-001"',
        codex_automation_id='"codex-001"',
        rrule='"FREQ=DAILY;BYHOUR=6;BYMINUTE=0;BYSECOND=0"',
        notification_policy='"failed_runs_only"',
        prompt_sha256=f'"{hashlib.sha256(unsafe_prompt.encode()).hexdigest()}"',
    )
    settings = load_orchestrator_settings(tmp_path)
    registry = tmp_path / "automations" / "codex-001"
    registry.mkdir(parents=True)
    (registry / "automation.toml").write_text(
        f'''id = "codex-001"
kind = "heartbeat"
status = "ACTIVE"
target_thread_id = "thread-001"
rrule = "FREQ=DAILY;BYHOUR=6;BYMINUTE=0;BYSECOND=0"
notification_policy = "failed_runs_only"
prompt = "{unsafe_prompt}"
''',
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="person protection"):
        verify_codex_automation_registry(settings, tmp_path / "automations")

    assert "인물 카드" in _AUTOMATION_PERSON_PROMPT_GUARD_TERMS[4]


def test_accepts_person_protection_with_safe_punctuation_variants() -> None:
    prompt = (
        "인물 이름, 저자, 자료 제공자, 참석자가 나타나도 외부 인물의 people, "
        "person_roles, attributions, 인물 카드를 자동으로 만들거나 바꾸지 말고 "
        "관계와 신상을 추정하지 마라. Novel과 private 원본의 인물은 읽거나 "
        "일반 인물 지도와 검색에 넣지 마라."
    )

    assert _has_person_protection(prompt)


def _prompt_contract(automation_id: str) -> AutomationContract:
    return AutomationContract(
        automation_id=automation_id,
        owner="fixture-owner",
        cadence="daily",
        inputs=("fixture-input",),
        outputs=("fixture-output",),
        checkpoint_key=f"{automation_id}-checkpoint",
        required_signals=("fixture-signal",),
        prohibited=("person-profile-inference", "unresolved-identity-link"),
        mode="materialize",
        status="enabled",
        task_thread_id="fixture-thread",
        codex_automation_id="fixture-automation",
        rrule="FREQ=DAILY;BYHOUR=1",
        notification_policy="failed_runs_only",
        prompt_sha256="a" * 64,
        owned_paths=("wiki",),
    )


def test_rejects_retired_wiki_fields_in_enabled_automation_prompts() -> None:
    with pytest.raises(WoonError, match="retired Wiki term parent_topics"):
        _validate_wiki_prompt_contract(
            _prompt_contract("codex-conversation-ingest"),
            "new_wiki_reason parent_topics parent keywords central_question wiki/** "
            "일일 기록은 Wiki 승격 입력이 아니다",
        )


def test_accepts_new_single_wiki_prompt_contracts() -> None:
    _validate_wiki_prompt_contract(
        _prompt_contract("codex-conversation-ingest"),
        "new_wiki_reason parent keywords central_question wiki/** "
        "일일 기록은 Wiki 승격 입력이 아니다 "
        "작은 순수 분류 허브는 일반 텍스트 불릿 아래 직접 하위 키워드 링크 "
        "direct child가 2개 이상이면 navigation_groups "
        "콘텐츠 subtree와 Facet 탐색 페이지를 만들지 않는다 "
        "facets metadata는 분류 보조 속성 resource_keyword "
        "책 → 장르 키워드 → 책 제목 리소스 → 주제 텍스트 → 들여쓴 원자료 링크 "
        "lifecycle_status started_on ended_on occurred_on wiki/private/_sources/codex "
        "Vault 밖 별도 source archive를 만들지 않는다",
    )
    _validate_wiki_prompt_contract(
        _prompt_contract("knowledge-curation"),
        "canonical_id parent keywords view_mode 하위 키워드 최신 문서 wiki/README.md "
        "작은 순수 분류 허브는 일반 텍스트 불릿 아래 직접 하위 키워드 링크 "
        "direct child가 2개 이상이면 navigation_groups "
        "콘텐츠 subtree와 Facet 탐색 페이지가 없는지 "
        "facets metadata는 분류 보조 속성 "
        "책 → 장르 키워드 → 책 제목 리소스 → 주제 텍스트 → 들여쓴 원자료 링크 "
        "lifecycle_status started_on ended_on occurred_on wiki/private/_sources "
        "Vault 밖 별도 보관소",
    )
    _validate_wiki_prompt_contract(
        _prompt_contract("daily-record-materialization"),
        "Wiki 문서를 새로 만들지 않는다. 단계별 검증만 하며 전체 완료 receipt를 만들지 않는다. "
        "일일 기록은 Wiki 승격 입력이 아니다. wiki/private/_sources/codex와 자유 메모를 구분한다.",
    )


def test_daily_pipeline_forbids_promoting_daily_notes_back_into_wiki(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    daily_lane = """
  - id: daily-record-materialization
    owner: daily-record-task
    cadence: daily
    inputs: [codex-synthesis-ledger]
    output: [daily-conversation-summary]
    checkpoint_key: daily-codex-projection
    required_signals: [kst-day]
    prohibited: [person-profile-inference, unresolved-identity-link]
    execution:
      mode: materialize
      status: planned
      task_thread_id: null
      codex_automation_id: null
      rrule: null
      notification_policy: null
      prompt_sha256: null
      owned_paths: [inbox/daily, inbox/calendar, brain/review/activity]
"""
    policy = path.read_text(encoding="utf-8").replace(
        "cursor_contract:\n", daily_lane + "cursor_contract:\n"
    )
    path.write_text(
        policy.replace("daily_to_wiki_promotion: forbidden", "daily_to_wiki_promotion: allowed"),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="daily_to_wiki_promotion"):
        load_orchestrator_settings(tmp_path)
