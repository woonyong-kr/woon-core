"""Validated, non-executing contracts for Woon second-brain automations.

This module deliberately does not call Gmail, Calendar, or Codex.  It
only makes the policy file machine-checkable so a later adapter cannot silently
turn a planned candidate task into an unbounded writer.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError

_CADENCES = {
    "four-times-daily",
    "four-hourly",
    "daily",
    "daily-and-policy-gate",
    "explicit-local-request",
}
_EXECUTION_MODES = {
    "candidate-only",
    "review-only",
    "proposal-only",
    "policy-authorized",
    "materialize",
}
_EXECUTION_STATUSES = {"planned", "enabled", "disabled", "local-only"}
_NOTIFICATION_POLICIES = {"failed_runs_only", "always", "none"}
_AUTOMATION_PERSON_PROMPT_GUARD_TERMS = (
    "인물 이름",
    "저자",
    "자료 제공자",
    "참석자",
    "외부 인물의 people, person_roles, attributions, 인물 카드를 자동으로 만들거나 바꾸지 말고",
    "관계",
    "신상",
    "추정하지 마라",
    "Novel",
    "private 원본",
    "일반 인물 지도",
    "검색",
    "넣지 마라",
)
_RETIRED_WIKI_PROMPT_TERMS = (
    "parent_topics",
    "parent_moc",
    "map_role",
    "mindmap_role",
    "maps/** Markdown",
    "콘텐츠·책·프로젝트·인물도 같은 Wiki tree",
)


@dataclass(frozen=True, slots=True)
class AutomationContract:
    """One bounded automation lane with a single write ownership boundary."""

    automation_id: str
    owner: str
    cadence: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    checkpoint_key: str
    required_signals: tuple[str, ...]
    prohibited: tuple[str, ...]
    mode: str
    status: str
    task_thread_id: str | None
    codex_automation_id: str | None
    rrule: str | None
    notification_policy: str | None
    prompt_sha256: str | None
    owned_paths: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """An enabled task must have a dedicated Codex thread identity."""

        return self.status != "enabled" or (
            self.task_thread_id is not None
            and self.codex_automation_id is not None
            and self.rrule is not None
            and self.notification_policy is not None
            and self.prompt_sha256 is not None
        )


@dataclass(frozen=True, slots=True)
class OrchestratorSettings:
    """Validated second-brain automation policy for one private vault."""

    vault: Path
    policy_document: Path
    wiki_contract: Path
    timezone: str
    checkpoint_path: Path
    receipt_directory: Path
    lock_directory: Path
    policy_sha256: str
    automations: tuple[AutomationContract, ...]

    @property
    def enabled_automations(self) -> tuple[AutomationContract, ...]:
        return tuple(item for item in self.automations if item.status == "enabled")


def load_orchestrator_settings(vault: Path) -> OrchestratorSettings:
    """Load and validate the non-executing second-brain policy.

    Validation fails closed for duplicate IDs, unbounded cadences, unsafe
    execution modes, missing policy files, and enabled lanes without a separate
    Codex task thread.  It intentionally does not create runtime state.
    """

    resolved_vault = vault.expanduser().resolve()
    config_path = resolved_vault / "config" / "second-brain-orchestrator.yaml"
    if not config_path.is_file():
        raise WoonError(f"second-brain orchestrator not found: {config_path}")
    raw_bytes = config_path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes) or {}
    except yaml.YAMLError as error:
        raise WoonError(f"invalid second-brain orchestrator YAML: {error}") from error
    if not isinstance(raw, dict):
        raise WoonError("second-brain orchestrator must be a mapping")
    if raw.get("version") != 1:
        raise WoonError(f"unsupported second-brain orchestrator version: {raw.get('version')!r}")

    _validate_repository_contract(raw.get("repository_contract"))
    policy_document = _inside(resolved_vault, raw.get("policy_document"), "policy_document")
    if not policy_document.is_file():
        raise WoonError(f"second-brain policy document not found: {policy_document}")
    wiki_contract = _inside(resolved_vault, raw.get("wiki_contract"), "wiki_contract")
    if not wiki_contract.is_file():
        raise WoonError(f"Wiki information architecture contract not found: {wiki_contract}")
    timezone = _required_string(raw.get("timezone"), "timezone")
    runtime = _mapping(raw.get("runtime"), "runtime")
    _require_boolean(
        runtime.get("require_clean_target_revision"), "runtime.require_clean_target_revision"
    )
    _require_boolean(runtime.get("no_resident_process"), "runtime.no_resident_process")
    _require_boolean(runtime.get("no_auto_commit_or_push"), "runtime.no_auto_commit_or_push")
    checkpoint_path = _inside(
        resolved_vault, runtime.get("checkpoint_path"), "runtime.checkpoint_path"
    )

    receipt_directory = _inside(
        resolved_vault, runtime.get("receipt_directory"), "runtime.receipt_directory"
    )
    lock_directory = _inside(
        resolved_vault, runtime.get("lock_directory"), "runtime.lock_directory"
    )
    _require_runtime_path(checkpoint_path, "runtime.checkpoint_path")
    _require_runtime_path(receipt_directory, "runtime.receipt_directory")
    _require_runtime_path(lock_directory, "runtime.lock_directory")

    items = _list(raw.get("automations"), "automations")
    contracts = tuple(_automation(item, index) for index, item in enumerate(items))
    if not contracts:
        raise WoonError("second-brain orchestrator requires at least one automation")
    _unique((item.automation_id for item in contracts), "automation id")
    _unique((item.checkpoint_key for item in contracts), "checkpoint key")
    _unique_non_null(tuple(item.codex_automation_id for item in contracts), "Codex automation id")
    _unique_paths(contracts)
    invalid_ready = [item.automation_id for item in contracts if not item.ready]
    if invalid_ready:
        raise WoonError(
            "enabled second-brain automations require task_thread_id: "
            + ", ".join(sorted(invalid_ready))
        )
    if any(item.cadence == "daily-and-policy-gate" for item in contracts):
        policy_change = _mapping(raw.get("cursor_contract"), "cursor_contract").get("policy_change")
        if policy_change != "caller-must-run-governance-preflight":
            raise WoonError("policy-gated automation requires caller-must-run-governance-preflight")
    _validate_global_guards(raw.get("global_guards"))
    _validate_identity_contract(resolved_vault, raw.get("identity"), contracts)
    _validate_codex_conversation_contract(contracts)
    _validate_daily_record_contract(raw.get("daily_document_pipeline"), contracts)
    _validate_obsidian_tasks_contract(raw.get("obsidian_tasks"))
    _validate_schedule_apply_contract(contracts)

    return OrchestratorSettings(
        vault=resolved_vault,
        policy_document=policy_document,
        wiki_contract=wiki_contract,
        timezone=timezone,
        checkpoint_path=checkpoint_path,
        receipt_directory=receipt_directory,
        lock_directory=lock_directory,
        policy_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        automations=contracts,
    )


def verify_codex_automation_registry(
    settings: OrchestratorSettings, automation_root: Path
) -> tuple[str, ...]:
    """Cross-check enabled lanes against locally registered heartbeat metadata."""

    registered: dict[str, dict[str, object]] = {}
    for path in sorted(automation_root.glob("*/automation.toml")):
        try:
            entry = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise WoonError(f"invalid Codex automation registry entry: {path.name}") from error
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise WoonError(f"Codex automation registry entry has no id: {path}")
        if identifier in registered:
            raise WoonError(f"duplicate Codex automation registry id: {identifier}")
        registered[identifier] = entry
    verified: list[str] = []
    for lane in settings.enabled_automations:
        assert lane.codex_automation_id is not None
        registered_item = registered.get(lane.codex_automation_id)
        if registered_item is None:
            raise WoonError(f"missing Codex automation for {lane.automation_id}")
        expected = {
            "kind": "heartbeat",
            "target_thread_id": lane.task_thread_id,
            "status": "ACTIVE",
            "rrule": lane.rrule,
            "notification_policy": lane.notification_policy,
        }
        for field, value in expected.items():
            if registered_item.get(field) != value:
                raise WoonError(f"Codex automation mismatch for {lane.automation_id}: {field}")
        prompt = registered_item.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise WoonError(f"Codex automation mismatch for {lane.automation_id}: prompt")
        assert lane.prompt_sha256 is not None
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if prompt_digest != lane.prompt_sha256:
            raise WoonError(f"Codex automation mismatch for {lane.automation_id}: prompt digest")
        if not _has_person_protection(prompt):
            raise WoonError(
                f"Codex automation mismatch for {lane.automation_id}: person protection"
            )
        _validate_wiki_prompt_contract(lane, prompt)
        verified.append(lane.automation_id)
    return tuple(verified)


def _has_person_protection(prompt: str) -> bool:
    """Require an identity boundary, allowing the explicit-facts candidate lane.

    Most lanes must promise not to create person records at all.  The Codex
    projection lane is intentionally narrower: it may create a local review
    *candidate* from explicit facts, while still prohibiting identity matching,
    relation inference, and general-map insertion.  Requiring the former text
    verbatim would reject the safer, user-requested candidate workflow.
    """

    strict = all(term in prompt for term in _AUTOMATION_PERSON_PROMPT_GUARD_TERMS)
    candidate = (
        any(term in prompt for term in ("local-only 인물 정리 후보", "review-only people 후보"))
        and any(term in prompt for term in ("같은 이름의 카드와 연결", "동명이인"))
        and "관계" in prompt
        and "신상" in prompt
        and "추정하지 않는다" in prompt
        and "Novel" in prompt
        and "private 원본" in prompt
        and any(term in prompt for term in ("일반 인물 지도", "일반 검색"))
    )
    return strict or candidate


def _validate_wiki_prompt_contract(lane: AutomationContract, prompt: str) -> None:
    """Reject scheduled instructions that can recreate a parallel Wiki hierarchy."""

    if lane.automation_id not in {
        "codex-conversation-ingest",
        "knowledge-curation",
        "daily-record-materialization",
    }:
        return
    retired = tuple(term for term in _RETIRED_WIKI_PROMPT_TERMS if term in prompt)
    if retired:
        raise WoonError(
            f"Codex automation mismatch for {lane.automation_id}: retired Wiki term "
            + ", ".join(retired)
        )
    if lane.automation_id == "codex-conversation-ingest":
        required = {
            "new_wiki_reason",
            "parent",
            "keywords",
            "central_question",
            "wiki/**",
            "일일 기록은 Wiki 승격 입력이 아니다",
            "작은 순수 분류 허브는 일반 텍스트 불릿 아래 직접 하위 키워드 링크",
            "direct child가 2개 이상이면 navigation_groups",
            "콘텐츠 subtree와 Facet 탐색 페이지를 만들지 않는다",
            "facets metadata는 분류 보조 속성",
            "resource_keyword",
            "책 → 장르 키워드 → 책 제목",
            "리소스 → 주제 텍스트 → 들여쓴 원자료 링크",
            "lifecycle_status",
            "started_on",
            "ended_on",
            "occurred_on",
            "wiki/private/_sources/codex",
            "Vault 밖 별도 source archive를 만들지 않는다",
        }
    elif lane.automation_id == "knowledge-curation":
        required = {
            "canonical_id",
            "parent",
            "keywords",
            "view_mode",
            "하위 키워드",
            "최신 문서",
            "wiki/README.md",
            "작은 순수 분류 허브는 일반 텍스트 불릿 아래 직접 하위 키워드 링크",
            "direct child가 2개 이상이면 navigation_groups",
            "콘텐츠 subtree와 Facet 탐색 페이지가 없는지",
            "facets metadata는 분류 보조 속성",
            "책 → 장르 키워드 → 책 제목",
            "리소스 → 주제 텍스트 → 들여쓴 원자료 링크",
            "lifecycle_status",
            "started_on",
            "ended_on",
            "occurred_on",
            "wiki/private/_sources",
            "Vault 밖 별도 보관소",
        }
    else:
        required = {
            "Wiki 문서를 새로 만들지 않는다",
            "단계별",
            "전체 완료 receipt를 만들지 않는다",
            "일일 기록은 Wiki 승격 입력이 아니다",
            "wiki/private/_sources/codex",
            "자유 메모",
        }
    missing = tuple(sorted(term for term in required if term not in prompt))
    if missing:
        raise WoonError(
            f"Codex automation mismatch for {lane.automation_id}: missing Wiki contract "
            + ", ".join(missing)
        )


def _automation(raw: object, index: int) -> AutomationContract:
    item = _mapping(raw, f"automations[{index}]")
    automation_id = _required_slug(item.get("id"), f"automations[{index}].id")
    owner = _required_slug(item.get("owner"), f"automations[{index}].owner")
    cadence = _required_string(item.get("cadence"), f"automations[{index}].cadence")
    if cadence not in _CADENCES:
        raise WoonError(f"unsupported automation cadence: {cadence}")
    execution = _mapping(item.get("execution"), f"automations[{index}].execution")
    mode = _required_string(execution.get("mode"), f"automations[{index}].execution.mode")
    if mode not in _EXECUTION_MODES:
        raise WoonError(f"unsupported automation execution mode: {mode}")
    status = _required_string(execution.get("status"), f"automations[{index}].execution.status")
    if status not in _EXECUTION_STATUSES:
        raise WoonError(f"unsupported automation execution status: {status}")
    if mode == "policy-authorized" and status != "local-only":
        raise WoonError("policy-authorized schedule apply must stay local-only")
    task_thread_id = execution.get("task_thread_id")
    if task_thread_id is not None:
        task_thread_id = _required_string(
            task_thread_id, f"automations[{index}].execution.task_thread_id"
        )
    codex_automation_id = execution.get("codex_automation_id")
    if codex_automation_id is not None:
        codex_automation_id = _required_slug(
            codex_automation_id, f"automations[{index}].execution.codex_automation_id"
        )
    rrule = execution.get("rrule")
    if rrule is not None:
        rrule = _required_string(rrule, f"automations[{index}].execution.rrule")
        if not rrule.startswith("FREQ="):
            raise WoonError("second-brain automation rrule must start with FREQ=")
    notification_policy = execution.get("notification_policy")
    if notification_policy is not None:
        notification_policy = _required_string(
            notification_policy, f"automations[{index}].execution.notification_policy"
        )
        if notification_policy not in _NOTIFICATION_POLICIES:
            raise WoonError("unsupported second-brain automation notification policy")
    prompt_sha256 = _optional_sha256(
        execution.get("prompt_sha256"), f"automations[{index}].execution.prompt_sha256"
    )
    return AutomationContract(
        automation_id=automation_id,
        owner=owner,
        cadence=cadence,
        inputs=_string_list(item.get("inputs"), f"automations[{index}].inputs"),
        outputs=_string_list(item.get("output"), f"automations[{index}].output"),
        checkpoint_key=_required_slug(
            item.get("checkpoint_key"), f"automations[{index}].checkpoint_key"
        ),
        required_signals=_string_list(
            item.get("required_signals"), f"automations[{index}].required_signals"
        ),
        prohibited=_string_list(item.get("prohibited"), f"automations[{index}].prohibited"),
        mode=mode,
        status=status,
        task_thread_id=task_thread_id,
        codex_automation_id=codex_automation_id,
        rrule=rrule,
        notification_policy=notification_policy,
        prompt_sha256=prompt_sha256,
        owned_paths=_relative_paths(
            execution.get("owned_paths"), f"automations[{index}].execution.owned_paths"
        ),
    )


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"second-brain orchestrator {field} must be a mapping")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise WoonError(f"second-brain orchestrator {field} must be a list")
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"second-brain orchestrator {field} must be a non-empty string")
    return value.strip()


def _required_slug(value: object, field: str) -> str:
    candidate = _required_string(value, field)
    if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in candidate):
        raise WoonError(f"second-brain orchestrator {field} must be lowercase kebab-case")
    return candidate


def _optional_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    candidate = _required_string(value, field)
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise WoonError(f"second-brain orchestrator {field} must be a lowercase SHA-256 digest")
    return candidate


def _require_boolean(value: object, field: str) -> None:
    if value is not True:
        raise WoonError(f"second-brain orchestrator {field} must be true")


def _validate_global_guards(value: object) -> None:
    guards = _mapping(value, "global_guards")
    advertising = _mapping(guards.get("advertising"), "global_guards.advertising")
    if advertising.get("action") != "ignore_without_persistence":
        raise WoonError("advertising guard must ignore_without_persistence")
    if advertising.get("persist_fields") != [] or advertising.get("mutate_mailbox") is not False:
        raise WoonError("advertising guard must not persist or mutate mailbox")
    ambiguous = _mapping(
        guards.get("unallowlisted_or_ambiguous_mail"),
        "global_guards.unallowlisted_or_ambiguous_mail",
    )
    if (
        ambiguous.get("action") != "ignore_without_persistence"
        or ambiguous.get("persist_fields") != []
    ):
        raise WoonError("unallowlisted mail guard must not persist")
    privacy = _mapping(guards.get("privacy"), "global_guards.privacy")
    if privacy.get("novel_default") != "excluded":
        raise WoonError("Novel guard must stay excluded")
    if privacy.get("chat_default") != "excluded":
        raise WoonError("chat guard must stay excluded")
    if privacy.get("public_output_requires_verified_claim") is not True:
        raise WoonError("public output must require a verified claim")
    mutations = _mapping(guards.get("mutations"), "global_guards.mutations")
    for field in (
        "raw_source_delete",
        "direct_app_database_access",
    ):
        if mutations.get(field) != "forbidden":
            raise WoonError(f"mutation guard {field} must be forbidden")
    if mutations.get("calendar_write_requires_policy_authorization") is not True:
        raise WoonError("calendar writes must require policy authorization")
    if mutations.get("task_write_requires_purpose") is not True:
        raise WoonError("task writes must require a purpose")
    if mutations.get("public_publish_requires_separate_authorization") is not True:
        raise WoonError("public publishing must require separate authorization")
    bridge = _mapping(guards.get("schedule_bridge"), "global_guards.schedule_bridge")
    if bridge.get("auto_apply_allowlisted_datetime_mail_only") is not False:
        raise WoonError("schedule bridge must not auto-apply mail candidates")
    if bridge.get("schedule_apply_path") != "local-user-authorized":
        raise WoonError("schedule bridge must use the local user-authorized path")
    if bridge.get("calendar_name") != "Woon 일정":
        raise WoonError("schedule bridge must use the Woon 일정 calendar")
    if bridge.get("manual_apply_command") != "native-local-command":
        raise WoonError("schedule bridge must use the native manual apply command")
    if bridge.get("native_adapters") != ["eventkit-full-access"]:
        raise WoonError("schedule bridge must declare only the EventKit adapter")
    if bridge.get("state_path") != ".local/woon-knowledge/schedule-bridge-state.json":
        raise WoonError("schedule bridge state must stay in the local runtime path")


def _validate_identity_contract(
    vault: Path, value: object, contracts: tuple[AutomationContract, ...]
) -> None:
    """Keep person cards deliberate and prevent every automation from guessing identities."""

    identity = _mapping(value, "identity")
    if identity.get("schema_path") != "config/person-schema.json":
        raise WoonError("identity schema_path must use config/person-schema.json")
    if not (vault / "config/person-schema.json").is_file():
        raise WoonError("identity schema document is missing")
    expected = {
        "default_record_owner": "choi-woonyoung",
        "default_owner_mode": "implicit-if-omitted",
        "person_card_creation": "explicit-or-repeated-evidence",
        "automated_person_card_creation": "forbidden",
        "automated_person_candidate_creation": "explicit-facts-review-only",
        "novel_identity_import": "forbidden",
        "private_history_projection": "explicit-local-ledger-only",
        "private_history_review": "candidate-only-delete-when-empty",
    }
    for field, required in expected.items():
        if identity.get(field) != required:
            raise WoonError(f"identity contract {field} must be {required!r}")
    forbidden = {"person-profile-inference", "unresolved-identity-link"}
    for contract in contracts:
        missing = forbidden.difference(contract.prohibited)
        if missing:
            raise WoonError(
                f"automation {contract.automation_id} must prohibit identity inference: "
                f"{', '.join(sorted(missing))}"
            )


def _validate_codex_conversation_contract(contracts: tuple[AutomationContract, ...]) -> None:
    """Keep one Codex input transaction on the single Wiki and private receipt."""

    matches = [item for item in contracts if item.automation_id == "codex-conversation-ingest"]
    if not matches:
        return
    if len(matches) != 1:
        raise WoonError("second-brain orchestrator has duplicate codex-conversation-ingest lanes")
    lane = matches[0]
    expected_paths = {
        "wiki",
        "brain/review/codex",
        ".local/woon-knowledge/codex-knowledge",
        "wiki/private/_sources/codex",
    }
    if lane.mode != "materialize" or set(lane.owned_paths) != expected_paths:
        raise WoonError("codex conversation ingest must own the Wiki and local receipt boundary")
    expected_outputs = {
        "wiki-private-conversation-source-archive",
        "wiki-upsert",
        "runtime-history-receipt",
        "calendar-document-context",
        "schedule-action-review-candidate",
        "person-memory-review-candidate",
        "career-evidence-review-candidate",
        "creative-link-review-candidate",
        "source-intake-review-candidate",
    }
    if set(lane.outputs) != expected_outputs:
        raise WoonError("codex conversation ingest outputs must use the single Wiki transaction")


def _validate_daily_record_contract(
    pipeline_value: object, contracts: tuple[AutomationContract, ...]
) -> None:
    """A daily note is a stage-reconciled projection, never a second Wiki input."""

    matches = [item for item in contracts if item.automation_id == "daily-record-materialization"]
    if not matches:
        return
    if len(matches) != 1:
        raise WoonError(
            "second-brain orchestrator has duplicate daily-record-materialization lanes"
        )
    lane = matches[0]
    required_paths = {"inbox/daily", "inbox/calendar", "brain/review/activity"}
    if lane.mode != "materialize" or set(lane.owned_paths) != required_paths:
        raise WoonError("daily record materialization has an unsafe write boundary")
    if lane.checkpoint_key != "daily-codex-projection":
        raise WoonError("daily record checkpoint must track only the Codex projection stage")

    pipeline = _mapping(pipeline_value, "daily_document_pipeline")
    expected = {
        "knowledge_canonical_root": "wiki",
        "daily_root": "inbox/daily",
        "source_to_wiki_owner": "codex-conversation-ingest",
        "wiki_to_daily_owner": "daily-record-materialization",
        "daily_to_wiki_promotion": "forbidden",
        "completion_mode": "per-stage-reconciliation",
        "failed_stage": "preserve-verified-stage-results",
    }
    for field, required in expected.items():
        if pipeline.get(field) != required:
            raise WoonError(f"daily document pipeline {field} must be {required!r}")

    stages = _list(pipeline.get("stages"), "daily_document_pipeline.stages")
    expected_stages = (
        (
            "task-projection",
            "woon-tasks-service",
            "woon-tasks",
            ".local/woon-knowledge/tasks-state.json",
        ),
        (
            "codex-projection",
            "daily-record-materialization",
            "woon-codex-digest",
            ".local/woon-knowledge/automation-receipts/daily-record-materialization",
        ),
        (
            "calendar-projection",
            "woon-calendar-service",
            "woon_projection: apple-calendar",
            "output-hash-and-reread",
        ),
        (
            "activity-review",
            "activity-history-review",
            "review-only",
            "review-card-and-vault-audit",
        ),
    )
    actual_stages: list[tuple[str, str, str, str]] = []
    for index, raw_stage in enumerate(stages):
        stage = _mapping(raw_stage, f"daily_document_pipeline.stages[{index}]")
        actual_stages.append(
            (
                _required_slug(stage.get("id"), f"daily_document_pipeline.stages[{index}].id"),
                _required_string(
                    stage.get("owner"), f"daily_document_pipeline.stages[{index}].owner"
                ),
                _required_string(
                    stage.get("marker"), f"daily_document_pipeline.stages[{index}].marker"
                ),
                _required_string(
                    stage.get("proof"), f"daily_document_pipeline.stages[{index}].proof"
                ),
            )
        )
    if tuple(actual_stages) != expected_stages:
        raise WoonError("daily document pipeline stages must have exact independent ownership")


def _validate_repository_contract(value: object) -> None:
    contract = _mapping(value, "repository_contract")
    required = {
        "personal_vault": "woon-knowledge",
        "brain_is_subdirectory_not_repository": True,
        "runtime_must_not_contain_git_repository": True,
        "reusable_code_repository": "woon-core",
        "vault_executable_sources": "forbidden",
        "vault_tool_interface": "core-owned-cli",
        "public_export": "disabled_until_verified_projection_exists",
    }
    for field, expected in required.items():
        if contract.get(field) != expected:
            raise WoonError(f"repository contract {field} must be {expected!r}")


def _validate_obsidian_tasks_contract(value: object) -> None:
    """Keep Markdown tasks actionable and separate from knowledge and originals."""

    contract = _mapping(value, "obsidian_tasks")
    if contract.get("authority") != "current-action":
        raise WoonError("Obsidian task authority must stay current-action")
    if contract.get("write_interface") != "local-mcp":
        raise WoonError("Obsidian tasks must use the local MCP interface")
    if contract.get("routine_root") != "inbox/tasks/routines":
        raise WoonError("Obsidian tasks must use the routine source root")
    if contract.get("daily_root") != "inbox/daily":
        raise WoonError("Obsidian tasks must materialize in the daily root")
    if contract.get("receipt_path") != ".local/woon-knowledge/tasks-state.json":
        raise WoonError("Obsidian task receipts must stay in the local runtime path")
    if contract.get("recurrence") != "materialize-kst-daily":
        raise WoonError("Obsidian tasks must materialize with the KST daily rule")

    expected_areas = (
        ("career", "커리어·일"),
        ("learning", "학습·지식"),
        ("creative", "창작·발행"),
        ("life", "생활·집"),
        ("relationship", "관계·사람"),
        ("health", "건강·성장"),
        ("admin", "행정·재정"),
    )
    areas = _list(contract.get("areas"), "obsidian_tasks.areas")
    actual_areas = tuple(
        (
            _required_slug(
                _mapping(item, "obsidian_tasks.areas[]").get("id"),
                "obsidian_tasks.areas[].id",
            ),
            _required_string(
                _mapping(item, "obsidian_tasks.areas[]").get("title"),
                "obsidian_tasks.areas[].title",
            ),
        )
        for item in areas
    )
    if actual_areas != expected_areas:
        raise WoonError("Obsidian task areas must match the canonical responsibility taxonomy")

    todo = _mapping(contract.get("todo"), "obsidian_tasks.todo")
    if _string_list(todo.get("required"), "obsidian_tasks.todo.required") != (
        "verb-first-title",
        "independently-verifiable-action",
        "purpose",
    ):
        raise WoonError("Obsidian tasks must have a purpose and a verifiable action")
    if _string_list(todo.get("prohibited"), "obsidian_tasks.todo.prohibited") != (
        "knowledge-note",
        "raw-original",
        "person-profile",
        "novel-manuscript",
    ):
        raise WoonError("Obsidian tasks must not store knowledge or private originals")


def _validate_schedule_apply_contract(contracts: tuple[AutomationContract, ...]) -> None:
    matches = [item for item in contracts if item.automation_id == "policy-schedule-apply"]
    if len(matches) != 1:
        raise WoonError("second-brain orchestrator requires one policy-schedule-apply lane")
    lane = matches[0]
    if (
        lane.mode != "policy-authorized"
        or lane.status != "local-only"
        or lane.cadence != "explicit-local-request"
    ):
        raise WoonError("policy-schedule-apply must stay local-only and unscheduled")
    if any(
        value is not None
        for value in (
            lane.task_thread_id,
            lane.codex_automation_id,
            lane.rrule,
            lane.notification_policy,
            lane.prompt_sha256,
        )
    ):
        raise WoonError("local-only schedule apply must not register a Codex automation")


def _string_list(value: object, field: str) -> tuple[str, ...]:
    values = _list(value, field)
    result = tuple(_required_string(item, field) for item in values)
    if not result:
        raise WoonError(f"second-brain orchestrator {field} must not be empty")
    _unique(result, field)
    return result


def _relative_paths(value: object, field: str) -> tuple[str, ...]:
    result = _string_list(value, field)
    for path in result:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise WoonError(f"second-brain orchestrator {field} must stay inside the vault")
    return result


def _inside(vault: Path, value: object, field: str) -> Path:
    candidate = Path(_required_string(value, field))
    if candidate.is_absolute():
        raise WoonError(f"second-brain orchestrator {field} must be relative")
    resolved = (vault / candidate).resolve()
    try:
        resolved.relative_to(vault)
    except ValueError as error:
        raise WoonError(f"second-brain orchestrator {field} escapes the vault") from error
    return resolved


def _require_runtime_path(path: Path, field: str) -> None:
    if ".local" not in path.parts:
        raise WoonError(f"second-brain orchestrator {field} must be under .local")


def _unique(values: Iterable[str], field: str) -> None:
    values_tuple: tuple[str, ...] = tuple(values)
    if len(values_tuple) != len(set(values_tuple)):
        raise WoonError(f"duplicate second-brain {field}")


def _unique_non_null(values: tuple[str | None, ...], field: str) -> None:
    _unique(tuple(value for value in values if value is not None), field)


def _unique_paths(contracts: tuple[AutomationContract, ...]) -> None:
    owners: dict[str, str] = {}
    for contract in contracts:
        for path in contract.owned_paths:
            previous = owners.setdefault(path, contract.automation_id)
            if previous != contract.automation_id:
                raise WoonError(
                    "second-brain owned path "
                    f"{path!r} is shared by {previous} and {contract.automation_id}"
                )
