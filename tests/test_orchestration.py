import hashlib
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.orchestration import (
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
    (vault / "config/second-brain-orchestrator.yaml").write_text(
        f"""version: 1
policy_document: docs/second-brain-operating-model.md
timezone: Asia/Seoul
repository_contract:
  personal_vault: woon-knowledge
  brain_is_subdirectory_not_repository: true
  runtime_must_not_contain_git_repository: true
  reusable_code_repository: woon-core
  public_export: manual_verified_one_way_only
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
    direct_things_database_access: forbidden
    calendar_write_requires_policy_authorization: true
    public_publish_requires_separate_authorization: true
  schedule_bridge:
    auto_apply_allowlisted_datetime_mail_only: true
    schedule_apply_path: local-mail-automation
    calendar_name: Woon Tasks
    manual_apply_command: native-local-command
    native_adapters: [things-url-scheme-v2, eventkit-full-access]
    state_path: .local/woon-knowledge/schedule-bridge-state.json
things_3:
  authority: current-action
  write_interface: url-scheme-v2
  secret_ref: keychain:woon.second-brain.things-url-scheme/things-url-scheme
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
  tag_groups:
    - id: context
      tags: [컴퓨터, 전화, 외부, 집]
    - id: mode
      tags: [집중, 빠른 처리]
    - id: state
      tags: [대기, 일정, 위임]
  project:
    required: [concrete-outcome, closure-condition]
    prohibited: [knowledge-note, raw-original, person-profile, novel-manuscript]
  todo:
    required: [verb-first-title, independently-verifiable-action]
  calendar_context:
    calendar_name: Woon Tasks
    title_suffixes:
      career: 커리어
      learning: 학습
      creative: 창작
      life: 생활
      relationship: 관계
      health: 건강
      admin: 행정
automations:
  - id: mail-schedule-candidates
    owner: mail-schedule-task
    cadence: four-times-daily
    inputs: [allowlisted-mail]
    output: [candidate]
    checkpoint_key: mail-schedule-candidates
    required_signals: [allowlist]
    prohibited: [advertising-persistence]
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
    cadence: manual-confirmation-only
    inputs: [policy-authorized-schedule-candidate]
    output: [confirmed-write]
    checkpoint_key: policy-schedule-apply
    required_signals: [allowlisted-mail]
    prohibited: [ambiguous-mail-write]
    execution:
      mode: policy-authorized
      status: disabled
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
    prohibited: [automatic-policy-delete]
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
        path.read_text(encoding="utf-8")
        .replace(
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
        "mode: policy-authorized\n      status: disabled",
        "mode: policy-authorized\n      status: enabled",
    )
    path.write_text(unsafe_apply, encoding="utf-8")
    with pytest.raises(WoonError, match="policy-authorized schedule apply must not have"):
        load_orchestrator_settings(tmp_path)


def test_rejects_unsafe_things_3_contract(tmp_path: Path) -> None:
    write_policy(tmp_path)
    path = tmp_path / "config/second-brain-orchestrator.yaml"
    unsafe_policy = path.read_text(encoding="utf-8").replace(
        "write_interface: url-scheme-v2", "write_interface: direct-database"
    )
    path.write_text(unsafe_policy, encoding="utf-8")

    with pytest.raises(WoonError, match="Things 3 write interface"):
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
