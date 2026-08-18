"""Crash-safe local state for bounded second-brain automation runs.

This module owns only the local execution envelope: per-lane serialization,
hash-only receipts, and cursor checkpoints.  It never reads mail or chat data,
never invokes an LLM, and never writes Apple Calendar. Connector adapters
must produce a validated :class:`RunOutcome` outside this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock
from woon_core.knowledge.orchestration import AutomationContract, OrchestratorSettings

if TYPE_CHECKING:
    from woon_core.knowledge.second_brain_candidates import ReviewCandidate

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_REVIEW_INTERNAL_TERM_RE = re.compile(
    r"(?:candidate|governance|preflight|heartbeat|thread|hash|[0-9a-f]{12,})",
    flags=re.IGNORECASE,
)
_REVIEW_TITLE_LIMIT = 48
_CHECKPOINT_VERSION = 1
_RECEIPT_VERSION = 1


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Non-sensitive identity and cursor for one deterministic source range."""

    source_range: str
    input_sha256: str
    expected_owned_revision: str
    cursor_after: str


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Result identifiers only; raw source material never enters a receipt."""

    candidate_ids: tuple[str, ...]
    output_sha256: str


@dataclass(frozen=True, slots=True)
class RunResult:
    """Observable completion state for one idempotent automation operation."""

    receipt_id: str
    replayed: bool


def record_governance_preflight(
    settings: OrchestratorSettings, *, input_sha256: str, output_sha256: str
) -> RunResult:
    """Record one verified policy gate without waiting for its next heartbeat.

    The caller supplies only digests of the checked instruction inventory and
    check output.  Raw instructions and tool output never enter the receipt.
    """

    contract = next(
        (item for item in settings.automations if item.automation_id == "governance-audit"),
        None,
    )
    if contract is None:
        raise WoonError("second-brain governance-audit lane is required")
    policy_token = settings.policy_sha256[:16]
    request = RunRequest(
        source_range=f"governance-policy-{policy_token}",
        input_sha256=input_sha256,
        expected_owned_revision=snapshot_owned_paths(settings.vault, contract.owned_paths),
        cursor_after=f"governance-policy-{policy_token}",
    )
    store = AutomationRunStore(settings)
    store.prune_retired_checkpoint_lanes()
    return store.run(
        "governance-audit",
        request,
        lambda: RunOutcome(candidate_ids=(), output_sha256=output_sha256),
    )


class AutomationRunStore:
    """Serialize one lane and persist receipt-before-checkpoint state atomically."""

    def __init__(self, settings: OrchestratorSettings) -> None:
        self._settings = settings
        self._contracts = {item.automation_id: item for item in settings.automations}

    def run(
        self,
        automation_id: str,
        request: RunRequest,
        produce: Callable[[], RunOutcome],
    ) -> RunResult:
        """Run one local operation or replay its existing receipt.

        The receipt is written before the cursor checkpoint.  A crash between
        them is reconciled from the immutable receipt on the next identical
        request.  A failure before a receipt leaves the previous cursor intact.
        """

        contract = self._contract(automation_id)
        self._validate_request(request)
        receipt_id = _operation_id(contract, request, self._settings.policy_sha256)
        lock_path = self._settings.lock_directory / f"{automation_id}.lock"
        with exclusive_file_lock(lock_path):
            receipt_path = self._receipt_path(contract, receipt_id)
            checkpoint = self._load_checkpoint()
            self._require_current_governance_preflight(checkpoint, contract)
            existing = self._load_receipt(receipt_path)
            if existing is not None:
                self._validate_receipt(existing, contract, request, receipt_id)
                if self._checkpoint_matches(checkpoint, contract, receipt_id):
                    return RunResult(receipt_id=receipt_id, replayed=True)
                if self._checkpoint_has_other_receipt(checkpoint, contract):
                    return RunResult(receipt_id=receipt_id, replayed=True)
                self._write_checkpoint(checkpoint, contract, request, receipt_id)
                return RunResult(receipt_id=receipt_id, replayed=True)

            actual_revision = snapshot_owned_paths(self._settings.vault, contract.owned_paths)
            if actual_revision != request.expected_owned_revision:
                raise WoonError(
                    f"second-brain owned paths changed for {automation_id}; refresh before retry"
                )
            try:
                outcome = produce()
            except Exception as error:
                raise WoonError(
                    f"second-brain candidate producer failed for {automation_id}: "
                    f"{type(error).__name__}"
                ) from error
            self._validate_outcome(outcome)
            validate_review_cards(self._settings.vault, contract.owned_paths)
            receipt = _receipt(contract, request, outcome, receipt_id, self._settings.policy_sha256)
            atomic_write(receipt_path, encode_json(receipt), mode=0o600)
            self._write_checkpoint(checkpoint, contract, request, receipt_id)
            return RunResult(receipt_id=receipt_id, replayed=False)

    def prune_retired_checkpoint_lanes(self) -> tuple[str, ...]:
        """Remove only checkpoint lanes that no longer exist in the policy.

        This runs as part of the explicit governance preflight, never during a
        worker run.  A current lane with an old policy hash remains intact so
        its receipt can still enforce the normal policy-change gate.
        """

        current_keys = {contract.checkpoint_key for contract in self._contracts.values()}
        lock_path = self._settings.lock_directory / "checkpoint-retirement.lock"
        with exclusive_file_lock(lock_path):
            checkpoint = self._load_checkpoint()
            retired = tuple(
                sorted(key for key in checkpoint["lanes"] if key not in current_keys)
            )
            if not retired:
                return ()
            for key in retired:
                del checkpoint["lanes"][key]
            atomic_write(self._settings.checkpoint_path, encode_json(checkpoint), mode=0o600)
            return retired

    def run_review_candidates(
        self,
        automation_id: str,
        request: RunRequest,
        candidates: tuple[ReviewCandidate, ...],
    ) -> RunResult:
        """Persist candidates only in this lane's declared ``brain/review`` root."""

        contract = self._contract(automation_id)
        if contract.mode != "candidate-only":
            raise WoonError(
                f"second-brain candidate writer requires candidate-only lane: {automation_id}"
            )
        if len(contract.owned_paths) != 1 or not contract.owned_paths[0].startswith(
            "brain/review/"
        ):
            raise WoonError(
                f"second-brain candidate lane needs exactly one brain/review path: {automation_id}"
            )
        from woon_core.knowledge.second_brain_candidates import persist_review_candidates

        return self.run(
            automation_id,
            request,
            lambda: persist_review_candidates(
                self._settings.vault, contract.owned_paths[0], candidates
            ),
        )

    def _contract(self, automation_id: str) -> AutomationContract:
        contract = self._contracts.get(automation_id)
        if contract is None:
            raise WoonError(f"unknown second-brain automation: {automation_id}")
        if contract.mode == "policy-authorized" or contract.status != "enabled":
            raise WoonError(f"second-brain automation is not enabled: {automation_id}")
        return contract

    def _receipt_path(self, contract: AutomationContract, receipt_id: str) -> Path:
        return self._settings.receipt_directory / contract.automation_id / f"{receipt_id}.json"

    def _load_checkpoint(self) -> dict[str, Any]:
        path = self._settings.checkpoint_path
        if not path.exists():
            return {"version": _CHECKPOINT_VERSION, "lanes": {}}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"second-brain checkpoint is unreadable: {type(error).__name__}"
            raise WoonError(message) from error
        if not isinstance(loaded, dict):
            raise WoonError("second-brain checkpoint must be a mapping")
        if loaded.get("version") != _CHECKPOINT_VERSION:
            raise WoonError("unsupported second-brain checkpoint version")
        if not isinstance(loaded.get("lanes"), dict):
            raise WoonError("second-brain checkpoint lanes must be a mapping")
        return loaded

    def _load_receipt(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"second-brain receipt is unreadable: {type(error).__name__}"
            raise WoonError(message) from error
        if not isinstance(loaded, dict):
            raise WoonError("second-brain receipt must be a mapping")
        return loaded

    def _validate_request(self, request: RunRequest) -> None:
        _safe_token(request.source_range, "source_range")
        _safe_token(request.cursor_after, "cursor_after")
        _sha256(request.input_sha256, "input_sha256")
        _sha256(request.expected_owned_revision, "expected_owned_revision")

    def _validate_outcome(self, outcome: RunOutcome) -> None:
        if len(set(outcome.candidate_ids)) != len(outcome.candidate_ids):
            raise WoonError("second-brain candidate IDs must be unique")
        for candidate_id in outcome.candidate_ids:
            if not _IDENTIFIER_RE.fullmatch(candidate_id):
                raise WoonError("second-brain candidate ID must be lowercase kebab-case")
        _sha256(outcome.output_sha256, "output_sha256")

    def _validate_receipt(
        self,
        receipt: dict[str, Any],
        contract: AutomationContract,
        request: RunRequest,
        receipt_id: str,
    ) -> None:
        expected = {
            "version": _RECEIPT_VERSION,
            "operation_id": receipt_id,
            "automation_id": contract.automation_id,
            "source_range": request.source_range,
            "input_sha256": request.input_sha256,
            "cursor_after": request.cursor_after,
            "policy_sha256": self._settings.policy_sha256,
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise WoonError(f"second-brain receipt mismatch: {field}")
        candidate_ids = receipt.get("candidate_ids")
        if not isinstance(candidate_ids, list):
            raise WoonError("second-brain receipt candidate_ids must be a list")
        output_sha256 = receipt.get("output_sha256")
        if not isinstance(output_sha256, str):
            raise WoonError("second-brain receipt output_sha256 must be a string")
        self._validate_outcome(
            RunOutcome(
                candidate_ids=tuple(candidate_ids),
                output_sha256=output_sha256,
            )
        )

    def _checkpoint_matches(
        self, checkpoint: dict[str, Any], contract: AutomationContract, receipt_id: str
    ) -> bool:
        lane = checkpoint["lanes"].get(contract.checkpoint_key)
        return isinstance(lane, dict) and lane.get("receipt_id") == receipt_id

    def _checkpoint_has_other_receipt(
        self, checkpoint: dict[str, Any], contract: AutomationContract
    ) -> bool:
        lane = checkpoint["lanes"].get(contract.checkpoint_key)
        return isinstance(lane, dict) and isinstance(lane.get("receipt_id"), str)

    def _require_current_governance_preflight(
        self, checkpoint: dict[str, Any], contract: AutomationContract
    ) -> None:
        """Stop an existing lane after a policy change until governance records it."""

        if contract.automation_id == "governance-audit":
            return
        governance = self._contracts.get("governance-audit")
        if governance is None:
            return
        lane = checkpoint["lanes"].get(contract.checkpoint_key)
        if isinstance(lane, dict) and lane.get("policy_sha256") == self._settings.policy_sha256:
            return
        message = (
            f"second-brain {contract.automation_id} requires governance preflight "
            "after policy change"
        )
        preflight = checkpoint["lanes"].get(governance.checkpoint_key)
        if not isinstance(preflight, dict):
            raise WoonError(message)
        receipt_id = preflight.get("receipt_id")
        if (
            preflight.get("automation_id") != governance.automation_id
            or preflight.get("policy_sha256") != self._settings.policy_sha256
            or not isinstance(receipt_id, str)
        ):
            raise WoonError(message)
        receipt = self._load_receipt(self._receipt_path(governance, receipt_id))
        if (
            receipt is None
            or receipt.get("automation_id") != governance.automation_id
            or receipt.get("policy_sha256") != self._settings.policy_sha256
            or receipt.get("operation_id") != receipt_id
        ):
            raise WoonError(message)

    def _write_checkpoint(
        self,
        checkpoint: dict[str, Any],
        contract: AutomationContract,
        request: RunRequest,
        receipt_id: str,
    ) -> None:
        lanes = checkpoint["lanes"]
        lanes[contract.checkpoint_key] = {
            "automation_id": contract.automation_id,
            "cursor": request.cursor_after,
            "owned_revision": request.expected_owned_revision,
            "policy_sha256": self._settings.policy_sha256,
            "receipt_id": receipt_id,
        }
        atomic_write(self._settings.checkpoint_path, encode_json(checkpoint), mode=0o600)


def snapshot_owned_paths(vault: Path, owned_paths: tuple[str, ...]) -> str:
    """Return a deterministic digest of files a lane is permitted to change."""

    digest = hashlib.sha256()
    resolved_vault = vault.expanduser().resolve()
    for relative_path in sorted(owned_paths):
        root = (resolved_vault / relative_path).resolve()
        try:
            root.relative_to(resolved_vault)
        except ValueError as error:
            raise WoonError("second-brain owned path escapes vault") from error
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        if not root.exists():
            digest.update(b"missing\n")
            continue
        if root.is_symlink():
            raise WoonError("second-brain owned path must not be a symlink")
        if root.is_file():
            _digest_file(digest, root, relative_path)
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            if path.is_symlink():
                raise WoonError("second-brain owned file must not be a symlink")
            _digest_file(digest, path, path.relative_to(resolved_vault).as_posix())
    return digest.hexdigest()


def validate_review_cards(vault: Path, owned_paths: tuple[str, ...]) -> None:
    """Fail closed before a receipt when an automation-owned Review card is not human UI.

    Only paths under ``brain/review`` are inspected. Runtime-only lanes retain
    opaque IDs by design and must remain outside Obsidian's visible review UI.
    """

    resolved_vault = vault.expanduser().resolve()
    for owned_path in owned_paths:
        if not owned_path.startswith("brain/review/"):
            continue
        root = (resolved_vault / owned_path).resolve()
        try:
            root.relative_to(resolved_vault)
        except ValueError as error:
            raise WoonError("second-brain review path escapes vault") from error
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.name == "README.md":
                continue
            _validate_review_card(path)


def _validate_review_card(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise WoonError("second-brain review card is unreadable") from error
    frontmatter, body = _review_frontmatter(text)
    title = frontmatter.get("title")
    summary = frontmatter.get("summary")
    status = frontmatter.get("status")
    if (
        frontmatter.get("type") != "Candidate"
        or status not in {"Review", "Scheduled"}
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(summary, str)
        or not summary.strip()
    ):
        raise WoonError("second-brain review card must have human Candidate metadata")
    if len(title) > _REVIEW_TITLE_LIMIT:
        raise WoonError("second-brain review card title is too long for the human dashboard")
    heading = _first_h1(body)
    if heading != title:
        raise WoonError("second-brain review card title and H1 must match")
    visible = "\n".join((path.stem, title, summary, heading))
    if not re.search(r"[가-힣]", visible):
        raise WoonError("second-brain review card must use a Korean human title")
    if _REVIEW_INTERNAL_TERM_RE.search(visible):
        raise WoonError("second-brain review card exposes an internal identifier")
    if frontmatter.get("review_kind") == "인물 정리" and any(
        field in frontmatter for field in ("people", "person_roles", "attributions", "person_id")
    ):
        raise WoonError("person-memory review card must not resolve or link a person")


def _review_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise WoonError("second-brain review card is missing frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise WoonError("second-brain review card frontmatter is malformed")
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise WoonError("second-brain review card frontmatter is malformed")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values, text[end + 4 :]


def _first_h1(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _digest_file(digest: Any, path: Path, relative_path: str) -> None:
    digest.update(relative_path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\n")


def _operation_id(contract: AutomationContract, request: RunRequest, policy_sha256: str) -> str:
    stable = "\0".join(
        (contract.automation_id, request.source_range, request.input_sha256, policy_sha256)
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _receipt(
    contract: AutomationContract,
    request: RunRequest,
    outcome: RunOutcome,
    receipt_id: str,
    policy_sha256: str,
) -> dict[str, object]:
    return {
        "version": _RECEIPT_VERSION,
        "operation_id": receipt_id,
        "automation_id": contract.automation_id,
        "source_range": request.source_range,
        "input_sha256": request.input_sha256,
        "output_sha256": outcome.output_sha256,
        "candidate_ids": list(outcome.candidate_ids),
        "cursor_after": request.cursor_after,
        "policy_sha256": policy_sha256,
    }


def _safe_token(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(char in value for char in "\r\n")
    ):
        raise WoonError(f"second-brain {field} must be a bounded single-line token")


def _sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WoonError(f"second-brain {field} must be a lowercase SHA-256")
