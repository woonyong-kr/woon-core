"""Verified management for a small, approved set of Obsidian Community Plugins."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from woon_core.calendar.constants import (
    LINK_CALENDAR_DASHBOARD_CSS_CLASS,
    LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS,
    LINK_CALENDAR_PLUGIN_ID,
    LINK_CALENDAR_PROFILE_ID,
    LINK_CALENDAR_VERSION,
)
from woon_core.calendar.projection import (
    APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH,
    APPLE_CALENDAR_EVENTS_RELATIVE_PATH,
    APPLE_CALENDAR_ICS_RELATIVE_PATH,
    APPLE_CALENDAR_NOTION_DATABASE_RELATIVE_PATH,
    PRISMA_EMPTY_VIRTUAL_EVENTS,
    PRISMA_VIRTUAL_EVENTS_FILENAME,
    is_core_calendar_dashboard,
    is_core_calendar_notion_database,
)
from woon_core.errors import WoonError
from woon_core.io import exclusive_file_lock

REQUIRED_ASSETS = ("main.js", "manifest.json", "styles.css")
PRISMA_CALENDAR_ID = "prisma-calendar"
PRISMA_CALENDAR_EVENTS_DIRECTORY = "inbox/calendar/events"
PRISMA_VIRTUAL_EVENTS_STEM = PRISMA_VIRTUAL_EVENTS_FILENAME.removesuffix(".md")
PRISMA_READONLY_TOOLBAR = ("prevNext", "today", "now", "zoomLevel", "searchInput")
PRISMA_READONLY_CONTEXT_MENU = ("preview", "goToSource", "openFile")
FULL_CALENDAR_REMASTERED_ID = "full-calendar-remastered"
FULL_CALENDAR_SOURCE_COLOR = "#687B86"
NOTION_BASES_ID = "notion-bases"
LINK_CALENDAR_ID = LINK_CALENDAR_PLUGIN_ID
LINK_CALENDAR_SOURCE = APPLE_CALENDAR_EVENTS_RELATIVE_PATH
LINK_CALENDAR_PROPERTY_FIELDS = (
    ("title", "title"),
    ("start", "Date"),
    ("end", "End Date"),
    ("startTime", "Start Date"),
    ("endTime", "End Date"),
    ("allDay", "All Day"),
    ("category", "Category"),
)
LEGACY_SIMPLE_CALENDAR_ID = "woon-simple-calendar"
LEGACY_CONTEXT_CALENDAR_ID = "context-calendar"
LINKED_GRAPH_ID = "linked-graph"
LEGACY_CONTEXT_GRAPH_ID = "context-graph"
LINKED_GRAPH_VERSION = "1.5.0"
RUNNABLE_CODE_BLOCKS_ID = "runnable-code-blocks"
RUNNABLE_CODE_BLOCKS_VERSION = "0.2.1"
LOCAL_DEVELOPMENT_PLUGINS = frozenset(
    {LINK_CALENDAR_ID, LINKED_GRAPH_ID, RUNNABLE_CODE_BLOCKS_ID}
)
LINK_CALENDAR_SOURCE_REPOSITORY = "https://github.com/woonyong-kr/link-calendar.git"
LINKED_GRAPH_SOURCE_REPOSITORY = "https://github.com/woonyong-kr/linked-graph.git"
RUNNABLE_CODE_BLOCKS_SOURCE_REPOSITORY = (
    "https://github.com/woonyong-kr/runnable-code-blocks.git"
)
LOCAL_PLUGIN_SOURCE_REPOSITORIES = {
    LINK_CALENDAR_ID: LINK_CALENDAR_SOURCE_REPOSITORY,
    LINKED_GRAPH_ID: LINKED_GRAPH_SOURCE_REPOSITORY,
    RUNNABLE_CODE_BLOCKS_ID: RUNNABLE_CODE_BLOCKS_SOURCE_REPOSITORY,
}


@dataclass(frozen=True)
class OfficialPlugin:
    plugin_id: str
    repository: str


@dataclass(frozen=True)
class GitSourceProvenance:
    repository: str
    head_commit: str


OFFICIAL_PLUGINS = {
    LINK_CALENDAR_ID: OfficialPlugin(
        plugin_id=LINK_CALENDAR_ID, repository="woonyong-kr/link-calendar"
    ),
    LINKED_GRAPH_ID: OfficialPlugin(
        plugin_id=LINKED_GRAPH_ID, repository="woonyong-kr/linked-graph"
    ),
    "light-mindmap": OfficialPlugin(plugin_id="light-mindmap", repository="ninglg/light-mindmap"),
    "markdown-mindmap": OfficialPlugin(
        plugin_id="markdown-mindmap", repository="kikocastro/markdown-mindmap"
    ),
    PRISMA_CALENDAR_ID: OfficialPlugin(
        plugin_id=PRISMA_CALENDAR_ID, repository="Real1tyy/Prisma-Calendar"
    ),
    NOTION_BASES_ID: OfficialPlugin(
        plugin_id=NOTION_BASES_ID,
        repository="bgarciamoura/obsidian-notion-bases-plugin",
    ),
}

# This is deliberately not a generic uninstall API. Each legacy renderer has a named
# replacement guard and is moved to a local rollback backup only after that guard passes.
RETIRABLE_PLUGINS = {
    PRISMA_CALENDAR_ID,
    FULL_CALENDAR_REMASTERED_ID,
    NOTION_BASES_ID,
    LEGACY_SIMPLE_CALENDAR_ID,
    LEGACY_CONTEXT_CALENDAR_ID,
    LEGACY_CONTEXT_GRAPH_ID,
}


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "woon-core"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - allowlisted GitHub API
        return bytes(response.read())


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _current_file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _require_unchanged_file(path: Path, expected: bytes | None, label: str) -> None:
    if _current_file_bytes(path) != expected:
        raise WoonError(f"{label} changed concurrently; refresh before retry")


def _restore_file_if_owned(
    path: Path,
    *,
    expected_current: bytes,
    previous: bytes | None,
    label: str,
    cause: Exception,
) -> None:
    current = _current_file_bytes(path)
    if current not in {expected_current, previous}:
        raise WoonError(f"{label} changed concurrently; rollback refused") from cause
    if current == previous:
        return
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_write(path, previous)


def _enabled_ids_content(enabled: set[str]) -> bytes:
    return (json.dumps(sorted(enabled), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _regular_tree_fingerprint(path: Path) -> str:
    """Hash one regular directory tree without following links or nested directories."""

    if path.is_symlink() or not path.is_dir():
        raise WoonError(f"plugin tree must be a regular directory: {path.name}")
    entries: list[tuple[str, str]] = []
    for item in sorted(path.iterdir(), key=lambda candidate: candidate.name):
        if item.is_symlink() or not item.is_file():
            raise WoonError(f"plugin tree contains an unsupported entry: {item.name}")
        entries.append((item.name, _sha256(item.read_bytes())))
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _sha256(payload)


def _normalize_github_repository(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def _git_output(source: Path, plugin_id: str, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(source), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError(f"{plugin_id} Git provenance could not be inspected") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "Git command failed"
        raise WoonError(f"{plugin_id} Git provenance is invalid: {detail}")
    return result.stdout.strip()


def _local_plugin_git_provenance(plugin_id: str, source: Path) -> GitSourceProvenance:
    approved_repository = LOCAL_PLUGIN_SOURCE_REPOSITORIES.get(plugin_id)
    if approved_repository is None:
        raise WoonError(f"{plugin_id} has no approved source repository")
    repository_root = Path(_git_output(source, plugin_id, "rev-parse", "--show-toplevel")).resolve()
    if repository_root != source:
        raise WoonError(f"{plugin_id} build source must be the Git repository root")
    if _git_output(source, plugin_id, "status", "--porcelain", "--untracked-files=all"):
        raise WoonError(f"{plugin_id} build source Git repository must be clean")
    remote = _git_output(source, plugin_id, "remote", "get-url", "origin")
    if _normalize_github_repository(remote) != _normalize_github_repository(approved_repository):
        raise WoonError(f"{plugin_id} build source origin is not approved")
    head_commit = _git_output(source, plugin_id, "rev-parse", "--verify", "HEAD^{commit}")
    return GitSourceProvenance(
        repository=_normalize_github_repository(remote) + ".git",
        head_commit=head_commit,
    )


def _require_vault_local_directory(vault: Path, path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise WoonError(f"{label} must be a regular Vault directory")
    _require_within_vault(vault, path, label)


def _require_vault_local_file(vault: Path, path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise WoonError(f"{label} must be a regular Vault file")
    _require_within_vault(vault, path, label)


def _require_within_vault(vault: Path, path: Path, label: str) -> None:
    try:
        path.resolve().relative_to(vault.resolve())
    except ValueError as error:
        raise WoonError(f"{label} resolves outside the Vault") from error


class ObsidianPluginService:
    """Install verified Obsidian plugin releases and configure their Woon boundaries."""

    def __init__(self, vault: Path, download: Callable[[str], bytes] = _download):
        self._vault = vault.expanduser().resolve()
        self._download = download
        self._obsidian = self._vault / ".obsidian"
        self._plugins = self._obsidian / "plugins"
        self._enabled_path = self._obsidian / "community-plugins.json"
        self._local = self._vault / ".local" / "woon-knowledge" / "obsidian-plugins"
        self._mutation_lock = self._local / "mutation.lock"

    def status(self) -> dict[str, Any]:
        self._require_vault()
        enabled = self._enabled_ids()
        plugins: list[dict[str, Any]] = []
        for directory in sorted(self._plugins.iterdir()) if self._plugins.is_dir() else []:
            if not directory.is_dir():
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            plugin_id = manifest.get("id")
            if not isinstance(plugin_id, str) or not plugin_id:
                continue
            settings = sorted(
                item.name
                for item in directory.iterdir()
                if item.is_file() and item.name not in REQUIRED_ASSETS
            )
            plugins.append(
                {
                    "id": plugin_id,
                    "name": manifest.get("name"),
                    "version": manifest.get("version"),
                    "enabled_in_config": plugin_id in enabled,
                    "is_mindmap": "mindmap" in plugin_id.casefold()
                    or "mind map" in str(manifest.get("name", "")).casefold(),
                    "settings_files": settings,
                }
            )
        return {
            "vault": str(self._vault),
            "community_enabled_ids": sorted(enabled),
            "plugins": plugins,
            "runtime_loaded": "unknown-until-Obsidian-reloads",
        }

    def install(self, plugin_ids: list[str]) -> dict[str, Any]:
        """Serialize installation of approved official releases."""

        return self._mutate(lambda: self._install_locked(plugin_ids))

    def recover_settings_from_backup(
        self, plugin_id: str, source_receipt_id: str
    ) -> dict[str, Any]:
        """Restore non-runtime plugin files from one exact install backup.

        This is a narrow recovery lane for an interrupted or older official
        installer. Runtime assets are never copied back from the backup.
        """

        return self._mutate(
            lambda: self._recover_settings_from_backup_locked(plugin_id, source_receipt_id)
        )

    def _recover_settings_from_backup_locked(
        self, plugin_id: str, source_receipt_id: str
    ) -> dict[str, Any]:
        self._require_vault()
        if plugin_id not in OFFICIAL_PLUGINS:
            raise WoonError(f"unapproved Obsidian plugin ID: {plugin_id}")
        if not source_receipt_id.startswith("obsidian-plugin-") or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for character in source_receipt_id
        ):
            raise WoonError("Obsidian plugin recovery receipt ID is invalid")

        source_receipt_path = self._local / "receipts" / f"{source_receipt_id}.json"
        _require_vault_local_file(
            self._vault, source_receipt_path, "Obsidian plugin recovery receipt"
        )
        source_receipt = self._read_json_object(source_receipt_path)
        installed = source_receipt.get("plugins")
        if (
            source_receipt.get("action") != "install"
            or not isinstance(installed, list)
            or not any(isinstance(item, dict) and item.get("id") == plugin_id for item in installed)
        ):
            raise WoonError("Obsidian plugin recovery receipt does not own the requested plugin")

        backup = self._local / "backups" / source_receipt_id / plugin_id
        destination = self._plugins / plugin_id
        _require_vault_local_directory(self._vault, backup, "Obsidian plugin recovery backup")
        _require_vault_local_directory(
            self._vault, destination, "Obsidian plugin recovery destination"
        )
        recovered: dict[str, str] = {}
        for item in sorted(backup.iterdir(), key=lambda path: path.name):
            if item.name in REQUIRED_ASSETS:
                continue
            if item.is_symlink() or not item.is_file():
                raise WoonError(
                    f"Obsidian plugin recovery backup has unsupported settings: {item.name}"
                )
            target = destination / item.name
            content = item.read_bytes()
            current = _current_file_bytes(target)
            if current is not None and current != content:
                raise WoonError(
                    f"Obsidian plugin setting already differs; recovery refused: {item.name}"
                )
            if current is None:
                _atomic_write(target, content)
            recovered[item.name] = _sha256(content)
        if not recovered:
            raise WoonError("Obsidian plugin recovery backup contains no settings")

        receipt_id = self._receipt_id()
        receipt = {
            "receipt_id": receipt_id,
            "action": "recover-settings",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": {"id": plugin_id, "settings_sha256": recovered},
            "source_receipt_id": source_receipt_id,
            "runtime_loaded": "unchanged",
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        status = next((item for item in self.status()["plugins"] if item["id"] == plugin_id), None)
        if status is None or not set(recovered).issubset(status["settings_files"]):
            raise WoonError("Obsidian plugin settings recovery could not be verified")
        return receipt

    def _install_locked(self, plugin_ids: list[str]) -> dict[str, Any]:
        self._require_vault()
        requested = self._approved_ids(plugin_ids)
        receipt_id = self._receipt_id()
        receipt_root = self._local / "receipts"
        backup_root = self._local / "backups" / receipt_id
        self._plugins.mkdir(parents=True, exist_ok=True)
        enabled = self._enabled_ids()
        records: list[dict[str, Any]] = []
        for plugin_id in requested:
            records.append(self._install_one(plugin_id, backup_root))
            enabled.add(plugin_id)
        self._write_enabled_ids(enabled, backup_root)
        after = self.status()
        if any(
            not item["enabled_in_config"] for item in after["plugins"] if item["id"] in requested
        ):
            raise WoonError("Obsidian plugin configuration could not be verified after install")
        receipt = {
            "receipt_id": receipt_id,
            "action": "install",
            "created_at": datetime.now(UTC).isoformat(),
            "plugins": records,
            "configured_enabled": requested,
            "runtime_loaded": after["runtime_loaded"],
        }
        _atomic_write(
            receipt_root / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def install_local_build(
        self,
        plugin_id: str,
        source_directory: Path,
        expected_version: str,
    ) -> dict[str, Any]:
        """Serialize and install one explicitly approved local development build."""

        return self._mutate(
            lambda: self._install_local_build_locked(
                plugin_id,
                source_directory,
                expected_version,
            )
        )

    def _install_local_build_locked(
        self,
        plugin_id: str,
        source_directory: Path,
        expected_version: str,
    ) -> dict[str, Any]:
        """Install one explicitly approved local development build with rollback evidence.

        This path is intentionally narrower than the official release installer: only
        allowlisted user-owned development plugins are accepted, the caller must pin
        the manifest version, and every runtime asset is hashed into a local receipt.
        Existing non-runtime files such as plugin settings are preserved verbatim.
        """

        self._require_vault()
        if plugin_id not in LOCAL_DEVELOPMENT_PLUGINS:
            raise WoonError(f"unapproved local Obsidian plugin ID: {plugin_id}")
        if not expected_version.strip():
            raise WoonError("local Obsidian plugin version is required")

        source = source_directory.expanduser().resolve()
        if not source.is_dir():
            raise WoonError(f"local Obsidian plugin source directory is missing: {source}")
        assets: dict[str, bytes] = {}
        asset_hashes: dict[str, str] = {}
        for asset_name in REQUIRED_ASSETS:
            asset_path = source / asset_name
            if not asset_path.is_file() or asset_path.is_symlink():
                raise WoonError(f"local Obsidian plugin asset is invalid: {asset_name}")
            content = asset_path.read_bytes()
            assets[asset_name] = content
            asset_hashes[asset_name] = _sha256(content)

        try:
            manifest = json.loads(assets["manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WoonError("local Obsidian plugin manifest is unreadable") from error
        if not isinstance(manifest, dict) or manifest.get("id") != plugin_id:
            raise WoonError(f"local manifest ID does not match requested plugin: {plugin_id}")
        if manifest.get("version") != expected_version:
            raise WoonError(
                f"local manifest version does not match expected version: {expected_version}"
            )
        provenance = _local_plugin_git_provenance(plugin_id, source)

        receipt_id = self._receipt_id()
        receipt_root = self._local / "receipts"
        backup_root = self._local / "backups" / receipt_id
        destination = self._plugins / plugin_id
        backup = backup_root / plugin_id
        stage = self._plugins / f".{plugin_id}.staging-{uuid.uuid4().hex}"
        enabled_before = _current_file_bytes(self._enabled_path)
        enabled = self._enabled_ids()
        _require_unchanged_file(
            self._enabled_path,
            enabled_before,
            "Obsidian enabled plugin configuration",
        )
        self._plugins.mkdir(parents=True, exist_ok=True)
        stage.mkdir()
        preserved: list[str] = []
        replaced_existing = False
        installed_stage = False
        enabled_written = False
        destination_before_fingerprint: str | None = None
        installed_fingerprint: str | None = None
        enabled_after: bytes | None = None
        receipt_path = receipt_root / f"{receipt_id}.json"
        try:
            for asset_name, content in assets.items():
                _atomic_write(stage / asset_name, content)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise WoonError(
                        f"local Obsidian plugin destination is not a regular directory: {plugin_id}"
                    )
                _require_within_vault(self._vault, destination, "Obsidian plugin destination")
                for item in destination.iterdir():
                    if item.name in REQUIRED_ASSETS:
                        continue
                    if not item.is_file() or item.is_symlink():
                        raise WoonError(
                            f"local Obsidian plugin has unsupported settings entry: {item.name}"
                        )
                    _atomic_write(stage / item.name, item.read_bytes())
                    preserved.append(item.name)
                destination_before_fingerprint = _regular_tree_fingerprint(destination)
            installed_fingerprint = _regular_tree_fingerprint(stage)
            if destination_before_fingerprint is not None:
                if _regular_tree_fingerprint(destination) != destination_before_fingerprint:
                    raise WoonError("local Obsidian plugin changed concurrently before install")
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                replaced_existing = True
            elif destination.exists() or destination.is_symlink():
                raise WoonError("local Obsidian plugin appeared concurrently before install")
            os.replace(stage, destination)
            installed_stage = True
            enabled.add(plugin_id)
            enabled_after = _enabled_ids_content(enabled)
            _require_unchanged_file(
                self._enabled_path,
                enabled_before,
                "Obsidian enabled plugin configuration",
            )
            enabled_written = True
            self._write_enabled_ids(enabled, backup_root)
            _require_unchanged_file(
                self._enabled_path,
                enabled_after,
                "Obsidian enabled plugin configuration",
            )
            if _regular_tree_fingerprint(destination) != installed_fingerprint:
                raise WoonError("local Obsidian plugin changed concurrently after install")
            installed = next(
                (item for item in self.status()["plugins"] if item["id"] == plugin_id),
                None,
            )
            if (
                installed is None
                or installed["version"] != expected_version
                or not installed["enabled_in_config"]
            ):
                raise WoonError("local Obsidian plugin install could not be verified")

            plugin_receipt: dict[str, Any] = {
                "id": plugin_id,
                "version": expected_version,
                "assets_sha256": asset_hashes,
                "preserved_settings": sorted(preserved),
            }
            if provenance is not None:
                plugin_receipt["source"] = {
                    "repository": provenance.repository,
                    "head_commit": provenance.head_commit,
                    "clean": True,
                }
            receipt = {
                "receipt_id": receipt_id,
                "action": "install-local-build",
                "created_at": datetime.now(UTC).isoformat(),
                "plugin": plugin_receipt,
                "backup": backup.relative_to(self._vault).as_posix() if replaced_existing else None,
                "runtime_loaded": "unknown-until-Obsidian-reloads",
            }
            _atomic_write(
                receipt_path,
                (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        except Exception as error:
            rollback_errors: list[str] = []
            shutil.rmtree(stage, ignore_errors=True)
            if enabled_written and enabled_after is not None:
                try:
                    _restore_file_if_owned(
                        self._enabled_path,
                        expected_current=enabled_after,
                        previous=enabled_before,
                        label="Obsidian enabled plugin configuration",
                        cause=error,
                    )
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if installed_stage and (destination.exists() or destination.is_symlink()):
                try:
                    if (
                        installed_fingerprint is None
                        or _regular_tree_fingerprint(destination) != installed_fingerprint
                    ):
                        raise WoonError(
                            "installed plugin changed concurrently; destructive rollback refused"
                        )
                    shutil.rmtree(destination)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if replaced_existing and backup.exists():
                try:
                    if destination.exists() or destination.is_symlink():
                        raise WoonError("plugin rollback destination already exists")
                    if (
                        destination_before_fingerprint is None
                        or _regular_tree_fingerprint(backup) != destination_before_fingerprint
                    ):
                        raise WoonError("plugin backup changed concurrently; restore refused")
                    os.replace(backup, destination)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            receipt_path.unlink(missing_ok=True)
            if not rollback_errors:
                shutil.rmtree(backup_root, ignore_errors=True)
                raise
            raise WoonError(
                "local Obsidian plugin install failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        return receipt

    def remove_detected_mindmaps(self) -> dict[str, Any]:
        """Serialize removal of manifest-confirmed mindmap plugins."""

        return self._mutate(self._remove_detected_mindmaps_locked)

    def _remove_detected_mindmaps_locked(self) -> dict[str, Any]:
        """Remove only plugin folders whose installed manifest identifies a mindmap."""

        before = self.status()
        targets = [item["id"] for item in before["plugins"] if item["is_mindmap"]]
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        enabled = self._enabled_ids()
        removed: list[str] = []
        for plugin_id in targets:
            source = self._plugins / plugin_id
            if not source.is_dir():
                continue
            destination = backup_root / plugin_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            enabled.discard(plugin_id)
            removed.append(plugin_id)
        if removed:
            self._write_enabled_ids(enabled, backup_root)
        after = self.status()
        receipt = {
            "receipt_id": receipt_id,
            "action": "remove-detected-mindmaps",
            "created_at": datetime.now(UTC).isoformat(),
            "before": before,
            "removed": removed,
            "after": after,
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def configure_prisma_calendar(self) -> dict[str, Any]:
        """Serialize the legacy Prisma configuration migration."""

        return self._mutate(self._configure_prisma_calendar_locked)

    def _configure_prisma_calendar_locked(self) -> dict[str, Any]:
        """Point Prisma at the Core-owned, read-only Apple Calendar projection."""

        self._require_vault()
        manifest = self._installed_manifest(PRISMA_CALENDAR_ID)
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        settings_path = self._plugins / PRISMA_CALENDAR_ID / "data.json"
        if settings_path.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(settings_path, backup_root / "data.json")
        self._prepare_prisma_virtual_events_store(backup_root)
        configuration = _prisma_calendar_configuration(manifest["version"])
        _atomic_write(
            settings_path,
            (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        calendars = loaded.get("calendars") if isinstance(loaded, dict) else None
        calendar = calendars[0] if isinstance(calendars, list) and len(calendars) == 1 else None
        if (
            not isinstance(calendar, dict)
            or calendar.get("directory") != PRISMA_CALENDAR_EVENTS_DIRECTORY
            or calendar.get("startProp") != "Start Date"
            or calendar.get("endProp") != "End Date"
            or calendar.get("dateProp") != "Date"
            or calendar.get("allDayProp") != "All Day"
            or calendar.get("titleProp") != "title"
            or calendar.get("defaultView") != "dayGridMonth"
            or calendar.get("defaultMobileView") != "dayGridMonth"
            or calendar.get("toolbarButtons") != list(PRISMA_READONLY_TOOLBAR)
            or calendar.get("mobileToolbarButtons") != list(PRISMA_READONLY_TOOLBAR)
            or calendar.get("contextMenuItems") != list(PRISMA_READONLY_CONTEXT_MENU)
            or calendar.get("batchActionButtons") != []
            or calendar.get("virtualEventsFileName") != PRISMA_VIRTUAL_EVENTS_STEM
        ):
            raise WoonError("Prisma Calendar configuration could not be verified")
        receipt = {
            "receipt_id": receipt_id,
            "action": "configure-prisma-calendar",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": {
                "id": PRISMA_CALENDAR_ID,
                "version": manifest["version"],
            },
            "calendar": calendar,
            "external_sync": "disabled",
            "projection_write": "core-only",
            "virtual_events_store": "hidden-empty-readonly",
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def configure_full_calendar_remastered(self) -> dict[str, Any]:
        """Serialize the legacy Full Calendar configuration migration."""

        return self._mutate(self._configure_full_calendar_remastered_locked)

    def _configure_full_calendar_remastered_locked(self) -> dict[str, Any]:
        """Configure FCR as a month-only, renderer-only view of the Core ICS output."""

        self._require_vault()
        manifest = self._installed_manifest(FULL_CALENDAR_REMASTERED_ID)
        if FULL_CALENDAR_REMASTERED_ID not in self._enabled_ids():
            raise WoonError("Full Calendar Remastered must be enabled before configuration")
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        settings_path = self._plugins / FULL_CALENDAR_REMASTERED_ID / "data.json"
        if settings_path.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(settings_path, backup_root / "data.json")
        retired_prisma_support = self._retire_prisma_virtual_events_store(backup_root)
        configuration = _full_calendar_configuration(manifest["version"])
        _atomic_write(
            settings_path,
            (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        _validate_full_calendar_configuration(loaded, manifest["version"])
        receipt = {
            "receipt_id": receipt_id,
            "action": "configure-full-calendar-remastered",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": {
                "id": FULL_CALENDAR_REMASTERED_ID,
                "version": manifest["version"],
            },
            "calendar_source": configuration["calendarSources"][0],
            "initial_view": configuration["initialView"],
            "external_sync": "disabled",
            "projection_write": "core-only",
            "retired_prisma_support_files": retired_prisma_support,
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def configure_notion_bases_calendar(self) -> dict[str, Any]:
        """Serialize verification and receipt creation for the Notion Bases view."""

        return self._mutate(self._configure_notion_bases_calendar_locked)

    def _configure_notion_bases_calendar_locked(self) -> dict[str, Any]:
        """Verify the Core-owned Markdown month view consumed by Notion Bases.

        Notion Bases keeps its schema and view metadata in Markdown, so there is no
        plugin-local settings file to create.  The generated database and dashboard are
        the complete, read-only configuration contract.
        """

        self._require_vault()
        manifest = self._installed_manifest(NOTION_BASES_ID)
        if NOTION_BASES_ID not in self._enabled_ids():
            raise WoonError("Notion Bases must be enabled before configuration")
        self._require_notion_bases_calendar_projection()
        receipt_id = self._receipt_id()
        receipt = {
            "receipt_id": receipt_id,
            "action": "configure-notion-bases-calendar",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": {"id": NOTION_BASES_ID, "version": manifest["version"]},
            "database": {
                "path": APPLE_CALENDAR_NOTION_DATABASE_RELATIVE_PATH,
                "date_field": "Date",
                "view": "calendar",
                "view_mode": "month",
                "card_fields": "title-only",
            },
            "dashboard": APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH,
            "external_sync": "disabled",
            "projection_write": "core-only",
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def configure_link_calendar(self) -> dict[str, Any]:
        """Serialize Link Calendar settings changes for this Vault."""

        return self._mutate(self._configure_link_calendar_locked)

    def _configure_link_calendar_locked(self) -> dict[str, Any]:
        """Upsert Woon's read-only source profile into an installed local build.

        The plugin remains independently configurable: unrelated settings and source
        profiles are retained.  A failed write, semantic verification, or receipt write
        restores the exact previous ``data.json`` state.
        """

        self._require_vault()
        self._require_link_calendar_projection()
        manifest = self._installed_manifest(LINK_CALENDAR_ID)
        if manifest["version"] != LINK_CALENDAR_VERSION:
            raise WoonError(
                "Link Calendar version must match the approved local development version"
            )
        if LINK_CALENDAR_ID not in self._enabled_ids():
            raise WoonError("Link Calendar must be enabled before configuration")
        self._require_verified_local_build(LINK_CALENDAR_ID, LINK_CALENDAR_VERSION)

        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        settings_path = self._plugins / LINK_CALENDAR_ID / "data.json"
        settings_before = settings_path.read_bytes() if settings_path.is_file() else None
        legacy_settings_path = self._plugins / LEGACY_CONTEXT_CALENDAR_ID / "data.json"
        legacy_settings_before: bytes | None = None
        if settings_before is not None:
            existing = self._read_json_object(settings_path)
        elif legacy_settings_path.exists():
            _require_vault_local_file(
                self._vault, legacy_settings_path, "legacy Context Calendar settings"
            )
            legacy_settings_before = legacy_settings_path.read_bytes()
            existing = self._read_json_object(legacy_settings_path)
        else:
            existing = {}
        configuration = _link_calendar_configuration(existing)
        content = (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        backup_path = backup_root / LINK_CALENDAR_ID / "data.json"
        if settings_before is not None:
            _atomic_write(backup_path, settings_before)
        _require_unchanged_file(settings_path, settings_before, "Link Calendar settings")

        receipt_path = self._local / "receipts" / f"{receipt_id}.json"
        try:
            _atomic_write(settings_path, content)
            loaded = self._read_json_object(settings_path)
            profile = _validate_link_calendar_configuration(loaded)
            receipt = {
                "receipt_id": receipt_id,
                "action": "configure-link-calendar",
                "created_at": datetime.now(UTC).isoformat(),
                "plugin": {
                    "id": LINK_CALENDAR_ID,
                    "version": manifest["version"],
                },
                "settings": {
                    "path": settings_path.relative_to(self._vault).as_posix(),
                    "sha256": _sha256(content),
                    "backup": (
                        backup_path.relative_to(self._vault).as_posix()
                        if settings_before is not None
                        else None
                    ),
                    "migrated_from": (
                        {
                            "path": legacy_settings_path.relative_to(self._vault).as_posix(),
                            "sha256": _sha256(legacy_settings_before),
                        }
                        if legacy_settings_before is not None
                        else None
                    ),
                },
                "source_profile": profile,
                "dashboard": APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH,
                "external_sync": "disabled",
                "projection_write": "core-only",
            }
            _atomic_write(
                receipt_path,
                (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        except Exception as error:
            _restore_file_if_owned(
                settings_path,
                expected_current=content,
                previous=settings_before,
                label="Link Calendar settings",
                cause=error,
            )
            receipt_path.unlink(missing_ok=True)
            raise
        return receipt

    def attest_link_calendar_runtime(self, checks: list[str]) -> dict[str, Any]:
        """Record a manual operator attestation bound to the current static evidence."""

        return self._mutate(lambda: self._attest_link_calendar_runtime_locked(checks))

    def _attest_link_calendar_runtime_locked(self, checks: list[str]) -> dict[str, Any]:
        required = set(LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS)
        provided = set(checks)
        if len(checks) != len(provided) or provided != required:
            missing = sorted(required.difference(provided))
            unexpected = sorted(provided.difference(required))
            detail = []
            if missing:
                detail.append("missing=" + ",".join(missing))
            if unexpected:
                detail.append("unexpected=" + ",".join(unexpected))
            if len(checks) != len(provided):
                detail.append("duplicate-check")
            raise WoonError(
                "Link Calendar manual runtime attestation requires the complete UI checklist"
                + (": " + "; ".join(detail) if detail else "")
            )

        asset_hashes, settings_hash, dashboard_hash = self._link_calendar_static_evidence()
        receipt_id = self._receipt_id()
        receipt = {
            "receipt_id": receipt_id,
            "action": "attest-link-calendar-runtime",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": {
                "id": LINK_CALENDAR_ID,
                "version": LINK_CALENDAR_VERSION,
                "assets_sha256": asset_hashes,
            },
            "settings": {"sha256": settings_hash},
            "dashboard": {"sha256": dashboard_hash},
            "operator_attested_checks": list(LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS),
            "attestation": "manual-operator-confirmation-after-Obsidian-reload",
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def retire(self, plugin_ids: list[str]) -> dict[str, Any]:
        """Serialize one guarded legacy-renderer retirement."""

        return self._mutate(lambda: self._retire_locked(plugin_ids))

    def _retire_locked(self, plugin_ids: list[str]) -> dict[str, Any]:
        """Move an explicitly approved legacy renderer to a local rollback backup."""

        self._require_vault()
        requested = self._retirable_ids(plugin_ids)
        if FULL_CALENDAR_REMASTERED_ID in requested:
            self._require_notion_bases_calendar_projection()
        if NOTION_BASES_ID in requested:
            self._require_link_calendar_ready()
        if LEGACY_SIMPLE_CALENDAR_ID in requested:
            self._require_link_calendar_ready()
        if LEGACY_CONTEXT_CALENDAR_ID in requested:
            self._require_link_calendar_ready()
        if LEGACY_CONTEXT_GRAPH_ID in requested:
            self._require_linked_graph_ready()
        before = self.status()
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        receipt_path = self._local / "receipts" / f"{receipt_id}.json"
        enabled_before = self._enabled_path.read_bytes() if self._enabled_path.is_file() else None
        enabled = self._enabled_ids()
        retired: list[str] = []
        enabled_written = False
        for plugin_id in requested:
            source = self._plugins / plugin_id
            if source.exists():
                if not source.is_dir() or source.is_symlink():
                    raise WoonError(f"retire target is not a regular plugin directory: {plugin_id}")
                self._installed_manifest(plugin_id)
        try:
            for plugin_id in requested:
                source = self._plugins / plugin_id
                if source.is_dir():
                    destination = backup_root / plugin_id
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                    retired.append(plugin_id)
                enabled.discard(plugin_id)
            _require_unchanged_file(
                self._enabled_path,
                enabled_before,
                "Obsidian enabled plugin configuration",
            )
            self._write_enabled_ids(enabled, backup_root)
            enabled_written = True
            after = self.status()
            receipt = {
                "receipt_id": receipt_id,
                "action": "retire",
                "created_at": datetime.now(UTC).isoformat(),
                "before": before,
                "retired": retired,
                "after": after,
            }
            _atomic_write(
                receipt_path,
                (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        except Exception as error:
            rollback_errors: list[str] = []
            if enabled_written:
                try:
                    _restore_file_if_owned(
                        self._enabled_path,
                        expected_current=_enabled_ids_content(enabled),
                        previous=enabled_before,
                        label="Obsidian enabled plugin configuration",
                        cause=error,
                    )
                except Exception as rollback_error:  # pragma: no cover - defensive aggregation
                    rollback_errors.append(str(rollback_error))
            for plugin_id in reversed(retired):
                source = backup_root / plugin_id
                destination = self._plugins / plugin_id
                try:
                    if destination.exists():
                        raise WoonError(f"retire rollback destination already exists: {plugin_id}")
                    os.replace(source, destination)
                except Exception as rollback_error:  # pragma: no cover - defensive aggregation
                    rollback_errors.append(str(rollback_error))
            receipt_path.unlink(missing_ok=True)
            if not rollback_errors:
                shutil.rmtree(backup_root, ignore_errors=True)
                raise
            raise WoonError(
                "Obsidian plugin retirement failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        return receipt

    def _install_one(self, plugin_id: str, backup_root: Path) -> dict[str, Any]:
        official = OFFICIAL_PLUGINS[plugin_id]
        release_url = f"https://api.github.com/repos/{official.repository}/releases/latest"
        try:
            release = json.loads(self._download(release_url).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WoonError(
                f"official release metadata could not be read for {plugin_id}"
            ) from error
        assets = {
            asset.get("name"): asset
            for asset in release.get("assets", [])
            if isinstance(asset, dict)
        }
        if any(asset not in assets for asset in REQUIRED_ASSETS):
            raise WoonError(
                f"official release for {plugin_id} is missing a required Obsidian asset"
            )

        stage = self._plugins / f".{plugin_id}.staging-{uuid.uuid4().hex}"
        stage.mkdir()
        downloaded: dict[str, str] = {}
        try:
            for asset_name in REQUIRED_ASSETS:
                asset = assets[asset_name]
                download_url = asset.get("browser_download_url")
                if not isinstance(download_url, str) or not download_url.startswith(
                    "https://github.com/"
                ):
                    raise WoonError(
                        f"official release asset URL is invalid for {plugin_id}:{asset_name}"
                    )
                content = self._download(download_url)
                digest = _sha256(content)
                expected = asset.get("digest")
                if (
                    isinstance(expected, str)
                    and expected.startswith("sha256:")
                    and digest != expected.removeprefix("sha256:")
                ):
                    raise WoonError(
                        f"official release asset hash mismatch for {plugin_id}:{asset_name}"
                    )
                _atomic_write(stage / asset_name, content)
                downloaded[asset_name] = digest
            manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("id") != plugin_id:
                raise WoonError(
                    f"official manifest ID does not match requested plugin: {plugin_id}"
                )
            if not isinstance(manifest.get("version"), str) or not manifest["version"]:
                raise WoonError(f"official manifest has no version: {plugin_id}")
            destination = self._plugins / plugin_id
            backup = backup_root / plugin_id
            replaced_existing = False
            preserved: list[str] = []
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise WoonError(
                        "official Obsidian plugin destination is not a regular directory: "
                        f"{plugin_id}"
                    )
                _require_within_vault(self._vault, destination, "Obsidian plugin destination")
                for item in destination.iterdir():
                    if item.name in REQUIRED_ASSETS:
                        continue
                    if item.is_symlink() or not item.is_file():
                        raise WoonError(
                            f"official Obsidian plugin has unsupported settings: {item.name}"
                        )
                    _atomic_write(stage / item.name, item.read_bytes())
                    preserved.append(item.name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, backup)
                replaced_existing = True
            try:
                os.replace(stage, destination)
            except OSError:
                # Do not leave a failed update without the previously installed plugin.
                if replaced_existing and backup.exists() and not destination.exists():
                    os.replace(backup, destination)
                raise
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return {
            "id": plugin_id,
            "repository": official.repository,
            "release_tag": release.get("tag_name"),
            "version": manifest["version"],
            "release_url": release.get("html_url"),
            "assets_sha256": downloaded,
            "preserved_settings": sorted(preserved),
        }

    def _write_enabled_ids(self, enabled: set[str], backup_root: Path) -> None:
        if self._enabled_path.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._enabled_path, backup_root / "community-plugins.json")
        _atomic_write(
            self._enabled_path,
            _enabled_ids_content(enabled),
        )

    def _enabled_ids(self) -> set[str]:
        if not self._enabled_path.is_file():
            return set()
        try:
            value = json.loads(self._enabled_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError("Obsidian community-plugins.json is unreadable") from error
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise WoonError("Obsidian community-plugins.json must contain plugin IDs")
        return set(value)

    def _approved_ids(self, plugin_ids: list[str]) -> list[str]:
        if not plugin_ids:
            raise WoonError("at least one approved Obsidian plugin ID is required")
        unknown = sorted(set(plugin_ids).difference(OFFICIAL_PLUGINS))
        if unknown:
            raise WoonError("unapproved Obsidian plugin ID: " + ", ".join(unknown))
        return list(dict.fromkeys(plugin_ids))

    def _retirable_ids(self, plugin_ids: list[str]) -> list[str]:
        if not plugin_ids:
            raise WoonError("at least one retirable Obsidian plugin ID is required")
        unknown = sorted(set(plugin_ids).difference(RETIRABLE_PLUGINS))
        if unknown:
            raise WoonError("plugin cannot be retired by this migration: " + ", ".join(unknown))
        return list(dict.fromkeys(plugin_ids))

    def _installed_manifest(self, plugin_id: str) -> dict[str, Any]:
        manifest_path = self._plugins / plugin_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError(
                f"installed Obsidian plugin manifest is unreadable: {plugin_id}"
            ) from error
        if not isinstance(manifest, dict):
            raise WoonError(f"installed Obsidian plugin manifest is invalid: {plugin_id}")
        if manifest.get("id") != plugin_id or not isinstance(manifest.get("version"), str):
            raise WoonError(f"installed Obsidian plugin manifest is invalid: {plugin_id}")
        return manifest

    def _require_notion_bases_calendar_projection(self) -> None:
        """Fail closed unless the visible calendar is backed by the Core projection."""

        if NOTION_BASES_ID not in self._enabled_ids():
            raise WoonError("Notion Bases must be enabled before retiring the calendar renderer")
        database_path = self._vault / APPLE_CALENDAR_NOTION_DATABASE_RELATIVE_PATH
        dashboard_path = self._vault / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH
        if not is_core_calendar_notion_database(database_path):
            raise WoonError(
                "Notion Bases database must be generated by the Core calendar projection"
            )
        if not is_core_calendar_dashboard(dashboard_path):
            raise WoonError(
                "Notion Bases dashboard must be generated by the Core calendar projection"
            )
        if database_path.stat().st_mode & 0o777 != 0o400:
            raise WoonError("Notion Bases database must be read-only")
        if dashboard_path.stat().st_mode & 0o777 != 0o400:
            raise WoonError("Notion Bases dashboard must be read-only")
        database = database_path.read_text(encoding="utf-8")
        dashboard = dashboard_path.read_text(encoding="utf-8")
        required_database = ("calendarDateField: Date\n", "calendarViewMode: month\n")
        required_dashboard = (
            f"```nb-database\npath: {APPLE_CALENDAR_EVENTS_RELATIVE_PATH}\ntype: calendar\n```\n"
        )
        if any(field not in database for field in required_database):
            raise WoonError("Notion Bases database must use the date-only month view")
        if required_dashboard not in dashboard:
            raise WoonError("Notion Bases dashboard must embed the Core event directory")
        for path in self._vault.joinpath(APPLE_CALENDAR_EVENTS_RELATIVE_PATH).glob("*.md"):
            if path.name == Path(APPLE_CALENDAR_NOTION_DATABASE_RELATIVE_PATH).name:
                continue
            content = path.read_text(encoding="utf-8")
            if "woon_projection: apple-calendar\n" not in content or "Date: " not in content:
                raise WoonError("Notion Bases calendar rows must be Core-generated date-only notes")

    def _require_link_calendar_projection(self) -> None:
        """Require the exact read-only Markdown projection Link Calendar may read."""

        directory = self._vault / LINK_CALENDAR_SOURCE
        dashboard_path = self._vault / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH
        _require_vault_local_directory(self._vault, directory, "Link Calendar source directory")
        if directory.stat().st_mode & 0o777 != 0o500:
            raise WoonError("Link Calendar source directory must be Core-owned and read-only")
        _require_vault_local_file(self._vault, dashboard_path, "Link Calendar dashboard")
        if not is_core_calendar_dashboard(dashboard_path):
            raise WoonError("Link Calendar dashboard must be generated by the Core projection")
        if dashboard_path.stat().st_mode & 0o777 != 0o400:
            raise WoonError("Link Calendar dashboard must be read-only")
        dashboard = dashboard_path.read_text(encoding="utf-8")
        required_dashboard = (
            f"cssclasses: {LINK_CALENDAR_DASHBOARD_CSS_CLASS}\n"
            "---\n\n"
            f"```{LINK_CALENDAR_ID}\n"
            f"profile: {LINK_CALENDAR_PROFILE_ID}\n"
            "```\n"
        )
        if required_dashboard not in dashboard:
            raise WoonError("Link Calendar dashboard must use the Core source profile")
        for path in directory.glob("*.md"):
            _require_vault_local_file(self._vault, path, "Link Calendar event")
            content = path.read_text(encoding="utf-8")
            if (
                "woon_projection: apple-calendar\n" not in content
                or "Date: " not in content
                or "Category: " not in content
                or "Category ID: " not in content
            ):
                raise WoonError("Link Calendar rows must be Core-generated categorized notes")
            if path.stat().st_mode & 0o777 != 0o400:
                raise WoonError("Link Calendar rows must be read-only")

    def _require_link_calendar_ready(self) -> None:
        """Fail closed unless static evidence and a matching manual attestation exist."""

        asset_hashes, settings_hash, dashboard_hash = self._link_calendar_static_evidence()
        if not self._matching_receipt(
            "attest-link-calendar-runtime",
            LINK_CALENDAR_ID,
            LINK_CALENDAR_VERSION,
            asset_hashes=asset_hashes,
            settings_hash=settings_hash,
            dashboard_hash=dashboard_hash,
            attestation_checks=LINK_CALENDAR_MANUAL_ATTESTATION_CHECKS,
        ):
            raise WoonError(
                "Link Calendar runtime must have a manual operator attestation after reload"
            )

    def _require_linked_graph_ready(self) -> None:
        """Require the receipted read-only replacement before retiring the legacy ID."""

        manifest = self._installed_manifest(LINKED_GRAPH_ID)
        if manifest["version"] != LINKED_GRAPH_VERSION:
            raise WoonError("Linked Graph version is not approved for legacy retirement")
        if LINKED_GRAPH_ID not in self._enabled_ids():
            raise WoonError("Linked Graph must be enabled before retiring Context Graph")
        self._require_verified_local_build(LINKED_GRAPH_ID, LINKED_GRAPH_VERSION)

    def _link_calendar_static_evidence(self) -> tuple[dict[str, str], str, str]:
        """Return hashes only after install, activation, settings, and projection verify."""

        self._require_link_calendar_projection()
        manifest = self._installed_manifest(LINK_CALENDAR_ID)
        if manifest["version"] != LINK_CALENDAR_VERSION:
            raise WoonError("Link Calendar version is not approved for legacy retirement")
        if LINK_CALENDAR_ID not in self._enabled_ids():
            raise WoonError("Link Calendar must be enabled before retiring the legacy plugin")
        asset_hashes = self._require_verified_local_build(LINK_CALENDAR_ID, LINK_CALENDAR_VERSION)
        settings_path = self._plugins / LINK_CALENDAR_ID / "data.json"
        _require_vault_local_file(self._vault, settings_path, "Link Calendar settings")
        configuration = self._read_json_object(settings_path)
        _validate_link_calendar_configuration(configuration)
        settings_hash = _sha256(settings_path.read_bytes())
        if not self._matching_receipt(
            "configure-link-calendar",
            LINK_CALENDAR_ID,
            LINK_CALENDAR_VERSION,
            settings_hash=settings_hash,
        ):
            raise WoonError(
                "Link Calendar configuration receipt does not match the installed settings"
            )
        dashboard_hash = _sha256(
            (self._vault / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH).read_bytes()
        )
        return asset_hashes, settings_hash, dashboard_hash

    def _require_verified_local_build(self, plugin_id: str, version: str) -> dict[str, str]:
        try:
            asset_hashes: dict[str, str] = {}
            for name in REQUIRED_ASSETS:
                path = self._plugins / plugin_id / name
                _require_vault_local_file(self._vault, path, f"{plugin_id} runtime asset")
                asset_hashes[name] = _sha256(path.read_bytes())
        except OSError as error:
            raise WoonError(f"{plugin_id} runtime assets are incomplete") from error
        if not self._matching_receipt(
            "install-local-build",
            plugin_id,
            version,
            asset_hashes=asset_hashes,
            require_local_git_provenance=plugin_id in LOCAL_PLUGIN_SOURCE_REPOSITORIES,
        ):
            raise WoonError(f"{plugin_id} must be installed by the verified local-build adapter")
        return asset_hashes

    def _matching_receipt(
        self,
        action: str,
        plugin_id: str,
        version: str,
        *,
        asset_hashes: Mapping[str, str] | None = None,
        settings_hash: str | None = None,
        dashboard_hash: str | None = None,
        attestation_checks: tuple[str, ...] | None = None,
        require_local_git_provenance: bool = False,
    ) -> bool:
        receipt_root = self._local / "receipts"
        for path in sorted(receipt_root.glob("*.json"), reverse=True):
            try:
                receipt = self._read_json_object(path)
            except WoonError:
                continue
            plugin = receipt.get("plugin")
            if (
                receipt.get("action") != action
                or not isinstance(plugin, dict)
                or plugin.get("id") != plugin_id
                or plugin.get("version") != version
            ):
                continue
            if asset_hashes is not None and plugin.get("assets_sha256") != dict(asset_hashes):
                continue
            if require_local_git_provenance:
                source = plugin.get("source")
                head_commit = source.get("head_commit") if isinstance(source, dict) else None
                if (
                    not isinstance(source, dict)
                    or source.get("repository") != LOCAL_PLUGIN_SOURCE_REPOSITORIES.get(plugin_id)
                    or source.get("clean") is not True
                    or not isinstance(head_commit, str)
                    or len(head_commit) not in {40, 64}
                    or any(character not in "0123456789abcdef" for character in head_commit)
                ):
                    continue
            settings = receipt.get("settings")
            if settings_hash is not None and (
                not isinstance(settings, dict) or settings.get("sha256") != settings_hash
            ):
                continue
            dashboard = receipt.get("dashboard")
            if dashboard_hash is not None and (
                not isinstance(dashboard, dict) or dashboard.get("sha256") != dashboard_hash
            ):
                continue
            if attestation_checks is not None and receipt.get("operator_attested_checks") != list(
                attestation_checks
            ):
                continue
            return True
        return False

    @staticmethod
    def _read_json_object(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError(f"JSON object is unreadable: {path.name}") from error
        if not isinstance(value, dict):
            raise WoonError(f"JSON object is invalid: {path.name}")
        return value

    def _require_vault(self) -> None:
        if not self._obsidian.is_dir():
            raise WoonError("target does not contain an Obsidian vault")

    def _mutate(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Apply one mutation only while all control paths remain Vault-local."""

        self._require_mutation_boundary()
        self._secure_local_state_permissions()
        with exclusive_file_lock(self._mutation_lock):
            self._require_mutation_boundary()
            try:
                return operation()
            finally:
                self._secure_local_state_permissions()

    def _secure_local_state_permissions(self) -> None:
        """Keep plugin receipts and rollback backups private to the local user."""

        if not self._local.exists():
            return
        for path in (self._local, *self._local.rglob("*")):
            if path.is_symlink():
                raise WoonError("Obsidian plugin local state must not contain symlinks")
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)

    def _require_mutation_boundary(self) -> None:
        _require_vault_local_directory(self._vault, self._obsidian, "Obsidian config directory")
        if self._plugins.exists() or self._plugins.is_symlink():
            _require_vault_local_directory(
                self._vault,
                self._plugins,
                "Obsidian plugins directory",
            )
        if self._enabled_path.exists() or self._enabled_path.is_symlink():
            _require_vault_local_file(
                self._vault,
                self._enabled_path,
                "Obsidian enabled plugin configuration",
            )
        local_ancestor = self._vault
        for part in (".local", "woon-knowledge", "obsidian-plugins"):
            local_ancestor /= part
            if not local_ancestor.exists() and not local_ancestor.is_symlink():
                continue
            _require_vault_local_directory(
                self._vault,
                local_ancestor,
                "Obsidian plugin local state directory",
            )
        for directory in (self._local / "backups", self._local / "receipts"):
            if not directory.exists() and not directory.is_symlink():
                continue
            _require_vault_local_directory(
                self._vault,
                directory,
                "Obsidian plugin local state directory",
            )
        if self._mutation_lock.exists() or self._mutation_lock.is_symlink():
            _require_vault_local_file(
                self._vault,
                self._mutation_lock,
                "Obsidian plugin mutation lock",
            )

    def _prepare_prisma_virtual_events_store(self, backup_root: Path) -> None:
        """Keep Prisma's empty internal store hidden and out of the event-note contract."""

        events_directory = self._vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
        events_directory.mkdir(parents=True, exist_ok=True)
        original_mode = events_directory.stat().st_mode & 0o777
        events_directory.chmod(0o700)
        legacy_store = events_directory / "Virtual Events.md"
        hidden_store = events_directory / PRISMA_VIRTUAL_EVENTS_FILENAME
        try:
            if legacy_store.exists():
                if legacy_store.read_text(encoding="utf-8") != PRISMA_EMPTY_VIRTUAL_EVENTS:
                    raise WoonError("Prisma virtual events store is not empty")
                backup = backup_root / PRISMA_CALENDAR_ID / legacy_store.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(legacy_store, backup)
            if hidden_store.exists():
                if hidden_store.read_text(encoding="utf-8") != PRISMA_EMPTY_VIRTUAL_EVENTS:
                    raise WoonError("Prisma hidden virtual events store is not empty")
            else:
                _atomic_write(
                    hidden_store,
                    PRISMA_EMPTY_VIRTUAL_EVENTS.encode("utf-8"),
                )
            hidden_store.chmod(0o400)
        finally:
            events_directory.chmod(original_mode)

    def _retire_prisma_virtual_events_store(self, backup_root: Path) -> list[str]:
        """Back up only Prisma's known-empty support files before retiring the renderer."""

        events_directory = self._vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
        if not events_directory.exists():
            return []
        original_mode = events_directory.stat().st_mode & 0o777
        events_directory.chmod(0o700)
        retired: list[str] = []
        try:
            for filename in ("Virtual Events.md", PRISMA_VIRTUAL_EVENTS_FILENAME):
                source = events_directory / filename
                if not source.exists():
                    continue
                if (
                    not source.is_file()
                    or source.read_text(encoding="utf-8") != PRISMA_EMPTY_VIRTUAL_EVENTS
                ):
                    raise WoonError("Prisma virtual events store is not empty")
                destination = backup_root / PRISMA_CALENDAR_ID / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                retired.append(source.relative_to(self._vault).as_posix())
        finally:
            events_directory.chmod(original_mode)
        return retired

    @staticmethod
    def _receipt_id() -> str:
        return datetime.now(UTC).strftime("obsidian-plugin-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]


def _link_calendar_source_profile() -> dict[str, Any]:
    """Return the single Woon-managed profile understood by Link Calendar 2.0."""

    return {
        "id": LINK_CALENDAR_PROFILE_ID,
        "name": "Apple Calendar",
        "enabled": True,
        "source": {
            "type": "folder",
            "path": LINK_CALENDAR_SOURCE,
            "recursive": False,
            "tag": "",
        },
        "editable": False,
        "properties": dict(LINK_CALENDAR_PROPERTY_FIELDS),
    }


def _link_calendar_configuration(existing: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve user settings while deterministically upserting Woon's source profile."""

    configuration = dict(existing)
    if "showAgenda" not in configuration and "showContext" in configuration:
        configuration["showAgenda"] = configuration["showContext"] is not False
    configuration.pop("showContext", None)
    raw_profiles = configuration.get("sourceProfiles", [])
    if not isinstance(raw_profiles, list) or not all(
        isinstance(profile, dict) for profile in raw_profiles
    ):
        raise WoonError("Link Calendar sourceProfiles must be a list of objects")
    profile_ids = [profile.get("id") for profile in raw_profiles]
    if any(not isinstance(profile_id, str) or not profile_id for profile_id in profile_ids):
        raise WoonError("Link Calendar source profile IDs must be non-empty strings")
    if len(profile_ids) != len(set(profile_ids)):
        raise WoonError("Link Calendar source profile IDs must be unique")

    managed_profile = _link_calendar_source_profile()
    configuration["schemaVersion"] = 1
    configuration["sourceProfiles"] = [
        *(profile for profile in raw_profiles if profile["id"] != LINK_CALENDAR_PROFILE_ID),
        managed_profile,
    ]
    return configuration


def _validate_link_calendar_configuration(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """Return the verified Woon profile or fail closed on a writable/drifted source."""

    profiles = configuration.get("sourceProfiles")
    matching = (
        [profile for profile in profiles if profile.get("id") == LINK_CALENDAR_PROFILE_ID]
        if isinstance(profiles, list) and all(isinstance(profile, dict) for profile in profiles)
        else []
    )
    expected = _link_calendar_source_profile()
    if configuration.get("schemaVersion") != 1 or matching != [expected]:
        raise WoonError("Link Calendar read-only source profile could not be verified")
    return expected


def _prisma_calendar_configuration(version: str) -> dict[str, Any]:
    """Use Prisma's documented frontmatter keys without enabling network providers."""

    return {
        "version": version,
        "tutorialCompleted": True,
        "checkForReleaseUpdates": False,
        "calendars": [
            {
                "id": "woon-apple-calendar",
                "name": "Apple Calendar",
                "enabled": True,
                "directory": PRISMA_CALENDAR_EVENTS_DIRECTORY,
                "startProp": "Start Date",
                "endProp": "End Date",
                "dateProp": "Date",
                "allDayProp": "All Day",
                "titleProp": "title",
                "calendarTitleProp": "Calendar Title",
                "locale": "ko",
                "indexSubdirectories": False,
                "autoAssignZettelId": "disabled",
                "showRibbonIcon": True,
                "showDurationField": False,
                "showStopwatch": False,
                "markPastInstancesAsDone": False,
                "sortingStrategy": "none",
                "defaultView": "dayGridMonth",
                "defaultMobileView": "dayGridMonth",
                "hourStart": 7,
                "hourEnd": 23,
                "slotDurationMinutes": 30,
                "density": "comfortable",
                "thickerHourLines": True,
                "toolbarButtons": list(PRISMA_READONLY_TOOLBAR),
                "mobileToolbarButtons": list(PRISMA_READONLY_TOOLBAR),
                "batchActionButtons": [],
                "contextMenuItems": list(PRISMA_READONLY_CONTEXT_MENU),
                "enableNotifications": False,
                "notificationSound": False,
                "titleAutocomplete": False,
                "autoAssignCategoryByName": False,
                "autoAssignCategoryByIncludes": False,
                "showDurationInTitle": False,
                "colorMode": "off",
                "showEventColorDots": False,
                "defaultNodeColor": "#68737a",
                "virtualEventsFileName": PRISMA_VIRTUAL_EVENTS_STEM,
            }
        ],
        "caldav": {
            "accounts": [],
            "enableAutoSync": False,
            "syncOnStartup": False,
            "notifyOnSync": False,
        },
        "icsSubscriptions": {
            "subscriptions": [],
            "enableAutoSync": False,
            "syncOnStartup": False,
            "notifyOnSync": False,
        },
    }


def _full_calendar_configuration(version: str) -> dict[str, Any]:
    """Use FCR's local ICS source without credentials, network accounts, or edit features."""

    return {
        "calendarSources": [
            {
                "type": "ical",
                "id": "ical-woon-apple",
                "name": "Apple Calendar",
                "url": APPLE_CALENDAR_ICS_RELATIVE_PATH,
                "color": FULL_CALENDAR_SOURCE_COLOR,
            }
        ],
        "defaultCalendar": 0,
        "firstDay": 1,
        "initialView": {"desktop": "dayGridMonth", "mobile": "dayGridMonth"},
        "timeFormat24h": True,
        "clickToCreateEventFromMonthView": False,
        "displayTimezone": "Asia/Seoul",
        "lastSystemTimezone": "Asia/Seoul",
        "enableAdvancedCategorization": False,
        "chrono_analyser_config": None,
        "categorySettings": [],
        "useCustomGoogleClient": False,
        "googleClientId": "",
        "googleClientSecret": "",
        "googleUseCopyPasteAuth": False,
        "googleAccounts": [],
        "useCustomMicrosoftClient": False,
        "microsoftClientId": "",
        "microsoftProxyBaseUrl": "",
        "microsoftAccounts": [],
        "enableLocalServer": False,
        "localServerPort": 8540,
        "useLegacyPlaintextCredentials": False,
        "businessHours": {
            "enabled": False,
            "daysOfWeek": [1, 2, 3, 4, 5],
            "startTime": "09:00",
            "endTime": "17:00",
        },
        "enableBackgroundEvents": False,
        "enableReminders": False,
        "enableDefaultReminder": False,
        "defaultReminderMinutes": 10,
        "workspaces": [],
        "activeWorkspace": None,
        "showEventInStatusBar": False,
        "highlightCurrentOrNextEvent": False,
        "slotMinTime": "00:00",
        "slotMaxTime": "24:00",
        "allDaySlot": True,
        "timeGridDayHeaderFormat": "day-mmdd",
        "weekends": True,
        "hiddenDays": [],
        "dayMaxEvents": 4,
        "activityWatch": {
            "enabled": False,
            "apiUrl": "http://127.0.0.1:5600",
            "lastSyncTime": 0,
            "autoSyncEnabled": False,
            "autoSyncIntervalMins": 10,
            "targetCalendarId": "",
            "syncStrategy": "auto",
            "customDateStart": "",
            "customDateEnd": "",
            "profiles": [],
        },
        "tasksIntegration": {
            "backlogDateTarget": "scheduledDate",
            "calendarDisplayDateTarget": "scheduledDate",
            "openEditModalAfterBacklogDrop": False,
            "taskDisplayFormat": "dayPlanner",
            "includeGlobalQueryInBacklog": False,
            "backlogQuery": "",
        },
        "fcrReminderCompanion": {"enabled": False, "apiUrl": "http://127.0.0.1:45677"},
        "apiTokens": {},
        "authorizedTokens": {},
        "milestones": {"counters": {}, "unlockedAt": {}, "shown": {}},
        "enableMonthlyStatsReport": False,
        "lastMonthlyMilestonesGeneratedMonth": None,
        "lastMonthlyMilestonesCheckDate": None,
        "milestoneNotifierDuration": 8000,
        "currentVersion": version,
        "linkedNotesDirectory": "",
        "linkedNoteLinkStrategy": "deadline",
        "taskBacklogLastProviderId": "",
        "caldavTaskInboxLastCalendarId": "",
        "linkedNoteTemplate": "",
        "enableLinkedNoteTemplatesPreset": False,
        "linkedNoteTemplatesPresets": [],
        "weatherCity": "",
        "weatherLatitude": None,
        "weatherLongitude": None,
        "weatherHide": True,
        "weatherInputMode": "city",
        "weatherUnit": "C",
        "openDailyNoteOnDateClick": False,
        "breakTimer": {
            "enabled": False,
            "intervalMins": 60,
            "idleThresholdMins": 30,
            "breakDurationSecs": 30,
        },
    }


def _validate_full_calendar_configuration(configuration: object, version: str) -> None:
    """Fail closed if the renderer would gain a writable or external data path."""

    if not isinstance(configuration, dict):
        raise WoonError("Full Calendar Remastered configuration could not be verified")
    sources = configuration.get("calendarSources")
    source = sources[0] if isinstance(sources, list) and len(sources) == 1 else None
    expected_source = {
        "type": "ical",
        "id": "ical-woon-apple",
        "name": "Apple Calendar",
        "url": APPLE_CALENDAR_ICS_RELATIVE_PATH,
        "color": FULL_CALENDAR_SOURCE_COLOR,
    }
    expected_disabled = {
        "clickToCreateEventFromMonthView": False,
        "enableAdvancedCategorization": False,
        "useCustomGoogleClient": False,
        "googleClientId": "",
        "googleClientSecret": "",
        "googleAccounts": [],
        "useCustomMicrosoftClient": False,
        "microsoftClientId": "",
        "microsoftAccounts": [],
        "enableLocalServer": False,
        "enableReminders": False,
        "enableDefaultReminder": False,
        "activityWatch": {"enabled": False},
        "apiTokens": {},
        "authorizedTokens": {},
        "linkedNotesDirectory": "",
        "openDailyNoteOnDateClick": False,
    }
    if (
        source != expected_source
        or configuration.get("initialView") != {"desktop": "dayGridMonth", "mobile": "dayGridMonth"}
        or configuration.get("dayMaxEvents") != 4
        or configuration.get("currentVersion") != version
    ):
        raise WoonError("Full Calendar Remastered configuration could not be verified")
    for key, expected in expected_disabled.items():
        actual = configuration.get(key)
        if isinstance(expected, dict):
            if not isinstance(actual, dict) or any(
                actual.get(field) != value for field, value in expected.items()
            ):
                raise WoonError("Full Calendar Remastered configuration could not be verified")
        elif actual != expected:
            raise WoonError("Full Calendar Remastered configuration could not be verified")
