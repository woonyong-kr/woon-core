"""Verified management for a small, approved set of Obsidian Community Plugins."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

REQUIRED_ASSETS = ("main.js", "manifest.json", "styles.css")
PRISMA_CALENDAR_ID = "prisma-calendar"
PRISMA_CALENDAR_EVENTS_DIRECTORY = "inbox/calendar/events"
PRISMA_VIRTUAL_EVENTS_STEM = PRISMA_VIRTUAL_EVENTS_FILENAME.removesuffix(".md")
PRISMA_READONLY_TOOLBAR = ("prevNext", "today", "now", "zoomLevel", "searchInput")
PRISMA_READONLY_CONTEXT_MENU = ("preview", "goToSource", "openFile")
FULL_CALENDAR_REMASTERED_ID = "full-calendar-remastered"
FULL_CALENDAR_SOURCE_COLOR = "#687B86"
NOTION_BASES_ID = "notion-bases"
SIMPLE_CALENDAR_ID = "woon-simple-calendar"
SIMPLE_CALENDAR_SOURCE = "inbox/calendar/events"


@dataclass(frozen=True)
class OfficialPlugin:
    plugin_id: str
    repository: str


OFFICIAL_PLUGINS = {
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
    SIMPLE_CALENDAR_ID: OfficialPlugin(
        plugin_id=SIMPLE_CALENDAR_ID, repository="woonyong-kr/simple-calendar"
    ),
}

# This is deliberately not a generic uninstall API. Full Calendar Remastered may leave only
# after the Core-owned Notion Bases month view is installed and its source contract validates.
RETIRABLE_PLUGINS = {PRISMA_CALENDAR_ID, FULL_CALENDAR_REMASTERED_ID, NOTION_BASES_ID}


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


class ObsidianPluginService:
    """Install verified Obsidian plugin releases and configure their Woon boundaries."""

    def __init__(self, vault: Path, download: Callable[[str], bytes] = _download):
        self._vault = vault.expanduser().resolve()
        self._download = download
        self._obsidian = self._vault / ".obsidian"
        self._plugins = self._obsidian / "plugins"
        self._enabled_path = self._obsidian / "community-plugins.json"
        self._local = self._vault / ".local" / "woon-knowledge" / "obsidian-plugins"

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

    def remove_detected_mindmaps(self) -> dict[str, Any]:
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

    def configure_simple_calendar(self) -> dict[str, Any]:
        """Install the verified Simple Calendar release after its Markdown-only source validates."""

        self._require_vault()
        self._require_simple_calendar_projection()
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        plugin = self._install_one(SIMPLE_CALENDAR_ID, backup_root)
        enabled = self._enabled_ids()
        enabled.add(SIMPLE_CALENDAR_ID)
        self._write_enabled_ids(enabled, backup_root)
        after = self.status()
        installed = next(
            (item for item in after["plugins"] if item["id"] == SIMPLE_CALENDAR_ID), None
        )
        if not isinstance(installed, dict) or installed.get("enabled_in_config") is not True:
            raise WoonError("Simple Calendar configuration could not be verified after install")
        receipt = {
            "receipt_id": receipt_id,
            "action": "configure-simple-calendar",
            "created_at": datetime.now(UTC).isoformat(),
            "plugin": plugin,
            "dashboard": APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH,
            "source": SIMPLE_CALENDAR_SOURCE,
            "view_mode": "month-only",
            "entrypoints": {
                "ribbon_icon": "calendar-days",
                "command": "open-simple-calendar",
            },
            "card_behavior": {
                "time": "hidden",
                "category_color": "Core-projected-category-fill",
                "title": "two-lines-with-tooltip-and-readonly-markdown-detail",
            },
            "external_sync": "disabled",
            "projection_write": "core-only",
        }
        _atomic_write(
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        return receipt

    def retire(self, plugin_ids: list[str]) -> dict[str, Any]:
        """Move an explicitly approved legacy renderer to a local rollback backup."""

        self._require_vault()
        requested = self._retirable_ids(plugin_ids)
        if FULL_CALENDAR_REMASTERED_ID in requested:
            self._require_notion_bases_calendar_projection()
        if NOTION_BASES_ID in requested:
            self._require_simple_calendar_projection()
            if SIMPLE_CALENDAR_ID not in self._enabled_ids():
                raise WoonError("Simple Calendar must be enabled before retiring Notion Bases")
        before = self.status()
        receipt_id = self._receipt_id()
        backup_root = self._local / "backups" / receipt_id
        enabled = self._enabled_ids()
        retired: list[str] = []
        for plugin_id in requested:
            source = self._plugins / plugin_id
            if source.is_dir():
                destination = backup_root / plugin_id
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                retired.append(plugin_id)
            enabled.discard(plugin_id)
        self._write_enabled_ids(enabled, backup_root)
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
            self._local / "receipts" / f"{receipt_id}.json",
            (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
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
            if destination.exists():
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
        }

    def _write_enabled_ids(self, enabled: set[str], backup_root: Path) -> None:
        if self._enabled_path.exists():
            backup_root.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self._enabled_path, backup_root / "community-plugins.json")
        _atomic_write(
            self._enabled_path,
            (json.dumps(sorted(enabled), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
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
            "```nb-database\n"
            f"path: {APPLE_CALENDAR_EVENTS_RELATIVE_PATH}\n"
            "type: calendar\n"
            "```\n"
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

    def _require_simple_calendar_projection(self) -> None:
        """Require the exact read-only Markdown projection Simple Calendar is allowed to read."""

        directory = self._vault / SIMPLE_CALENDAR_SOURCE
        dashboard_path = self._vault / APPLE_CALENDAR_DASHBOARD_RELATIVE_PATH
        if not directory.is_dir() or directory.stat().st_mode & 0o777 != 0o500:
            raise WoonError("Simple Calendar source directory must be Core-owned and read-only")
        if not is_core_calendar_dashboard(dashboard_path):
            raise WoonError("Simple Calendar dashboard must be generated by the Core projection")
        if dashboard_path.stat().st_mode & 0o777 != 0o400:
            raise WoonError("Simple Calendar dashboard must be read-only")
        dashboard = dashboard_path.read_text(encoding="utf-8")
        required_dashboard = (
            "cssclasses: woon-simple-calendar-dashboard\n"
            "---\n\n"
            "```woon-simple-calendar\n"
            f"source: {SIMPLE_CALENDAR_SOURCE}\n"
            "date_field: Date\n"
            "category_field: Category\n"
            "```\n"
        )
        if required_dashboard not in dashboard:
            raise WoonError("Simple Calendar dashboard must use the Core month-only block")
        for path in directory.glob("*.md"):
            content = path.read_text(encoding="utf-8")
            if (
                "woon_projection: apple-calendar\n" not in content
                or "Date: " not in content
                or "Category: " not in content
            ):
                raise WoonError("Simple Calendar rows must be Core-generated categorized notes")
            if path.stat().st_mode & 0o777 != 0o400:
                raise WoonError("Simple Calendar rows must be read-only")

    def _require_vault(self) -> None:
        if not self._obsidian.is_dir():
            raise WoonError("target does not contain an Obsidian vault")

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
