"""Validated, non-executing contracts for Woon second-brain automations.

This module deliberately does not call Gmail, Things, Calendar, or Codex.  It
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
    "manual-confirmation-only",
}
_EXECUTION_MODES = {"candidate-only", "review-only", "proposal-only", "approval-required"}
_EXECUTION_STATUSES = {"planned", "enabled", "disabled"}
_NOTIFICATION_POLICIES = {"failed_runs_only", "always", "none"}


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
    _validate_things_3_contract(raw.get("things_3"))
    _validate_schedule_apply_contract(contracts)

    return OrchestratorSettings(
        vault=resolved_vault,
        policy_document=policy_document,
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
        verified.append(lane.automation_id)
    return tuple(verified)


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
    if mode == "approval-required" and status != "disabled":
        raise WoonError("approval-required automation must stay disabled until bridge validation")
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
    if privacy.get("chat_default") != "opt_in_export_only":
        raise WoonError("chat guard must require opt-in export")
    if privacy.get("public_output_requires_verified_claim") is not True:
        raise WoonError("public output must require a verified claim")
    mutations = _mapping(guards.get("mutations"), "global_guards.mutations")
    for field in (
        "raw_source_delete",
        "direct_things_database_access",
    ):
        if mutations.get(field) != "forbidden":
            raise WoonError(f"mutation guard {field} must be forbidden")
    if mutations.get("calendar_write_requires_user_confirmation") is not True:
        raise WoonError("calendar writes must require user confirmation")
    if mutations.get("public_publish_requires_separate_authorization") is not True:
        raise WoonError("public publishing must require separate authorization")
    bridge = _mapping(guards.get("schedule_bridge"), "global_guards.schedule_bridge")
    if bridge.get("candidate_write_only_until_confirmed") is not True:
        raise WoonError("schedule bridge must remain candidate-only until confirmed")
    if bridge.get("confirmed_apply_automation") != "disabled":
        raise WoonError("confirmed schedule apply must remain disabled")
    if bridge.get("calendar_name") != "Woon Tasks":
        raise WoonError("schedule bridge must use the Woon Tasks calendar")
    if bridge.get("manual_apply_command") != "native-local-command":
        raise WoonError("schedule bridge must use the native manual apply command")
    if bridge.get("native_adapters") != ["things-url-scheme-v2", "eventkit-full-access"]:
        raise WoonError("schedule bridge must declare only approved native adapters")
    if bridge.get("state_path") != ".local/woon-knowledge/schedule-bridge-state.json":
        raise WoonError("schedule bridge state must stay in the local runtime path")


def _validate_repository_contract(value: object) -> None:
    contract = _mapping(value, "repository_contract")
    required = {
        "personal_vault": "woon-knowledge",
        "brain_is_subdirectory_not_repository": True,
        "runtime_must_not_contain_git_repository": True,
        "reusable_code_repository": "woon-core",
        "public_export": "manual_verified_one_way_only",
    }
    for field, expected in required.items():
        if contract.get(field) != expected:
            raise WoonError(f"repository contract {field} must be {expected!r}")


def _validate_things_3_contract(value: object) -> None:
    """Keep actionable Things taxonomy separate from knowledge and private originals."""

    contract = _mapping(value, "things_3")
    if contract.get("authority") != "current-action":
        raise WoonError("Things 3 authority must stay current-action")
    if contract.get("write_interface") != "url-scheme-v2":
        raise WoonError("Things 3 write interface must stay url-scheme-v2")
    required_secret_ref = (
        "keychain:woon.second-brain.things-url-scheme/things-url-scheme"
    )
    if contract.get("secret_ref") != required_secret_ref:
        raise WoonError("Things 3 token must stay in the declared Keychain reference")

    expected_areas = (
        ("career", "커리어·일"),
        ("learning", "학습·지식"),
        ("creative", "창작·발행"),
        ("life", "생활·집"),
        ("relationship", "관계·사람"),
        ("health", "건강·성장"),
        ("admin", "행정·재정"),
    )
    areas = _list(contract.get("areas"), "things_3.areas")
    actual_areas = tuple(
        (
            _required_slug(
                _mapping(item, "things_3.areas[]").get("id"),
                "things_3.areas[].id",
            ),
            _required_string(
                _mapping(item, "things_3.areas[]").get("title"),
                "things_3.areas[].title",
            ),
        )
        for item in areas
    )
    if actual_areas != expected_areas:
        raise WoonError("Things 3 areas must match the canonical responsibility taxonomy")

    expected_tags = {
        "context": ("Computer", "Phone", "Outside", "Home"),
        "mode": ("Deep Work", "Quick"),
        "state": ("Waiting", "Agenda", "Delegated"),
    }
    groups = _list(contract.get("tag_groups"), "things_3.tag_groups")
    actual_tags: dict[str, tuple[str, ...]] = {}
    for item in groups:
        group = _mapping(item, "things_3.tag_groups[]")
        identifier = _required_slug(group.get("id"), "things_3.tag_groups[].id")
        if identifier in actual_tags:
            raise WoonError("duplicate second-brain Things 3 tag group")
        actual_tags[identifier] = _string_list(group.get("tags"), "things_3.tag_groups[].tags")
    if actual_tags != expected_tags:
        raise WoonError("Things 3 tags must remain action-context and state only")

    project = _mapping(contract.get("project"), "things_3.project")
    if _string_list(project.get("required"), "things_3.project.required") != (
        "concrete-outcome",
        "closure-condition",
    ):
        raise WoonError("Things 3 projects must require a concrete closure")
    if _string_list(project.get("prohibited"), "things_3.project.prohibited") != (
        "knowledge-note",
        "raw-original",
        "person-profile",
        "novel-manuscript",
    ):
        raise WoonError("Things 3 projects must not store knowledge or private originals")
    todo = _mapping(contract.get("todo"), "things_3.todo")
    if _string_list(todo.get("required"), "things_3.todo.required") != (
        "verb-first-title",
        "independently-verifiable-action",
    ):
        raise WoonError("Things 3 to-dos must be independently verifiable actions")

    context = _mapping(contract.get("calendar_context"), "things_3.calendar_context")
    if context.get("calendar_name") != "Woon Tasks":
        raise WoonError("Things 3 calendar context must use Woon Tasks")
    expected_prefixes = {
        "career": "[커리어]",
        "learning": "[학습]",
        "creative": "[창작]",
        "life": "[생활]",
        "relationship": "[관계]",
        "health": "[건강]",
        "admin": "[행정]",
    }
    if context.get("title_prefixes") != expected_prefixes:
        raise WoonError("calendar context prefixes must match Things 3 areas")


def _validate_schedule_apply_contract(contracts: tuple[AutomationContract, ...]) -> None:
    matches = [item for item in contracts if item.automation_id == "confirmed-schedule-apply"]
    if len(matches) != 1:
        raise WoonError("second-brain orchestrator requires one confirmed-schedule-apply lane")
    lane = matches[0]
    if (
        lane.mode != "approval-required"
        or lane.status != "disabled"
        or lane.cadence != "manual-confirmation-only"
    ):
        raise WoonError("confirmed-schedule-apply must remain disabled until bridge validation")


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
