from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from woon_core.calendar.projection import (
    APPLE_CALENDAR_ICS_RELATIVE_PATH,
    PRISMA_EMPTY_VIRTUAL_EVENTS,
    PRISMA_VIRTUAL_EVENTS_FILENAME,
)
from woon_core.errors import WoonError
from woon_core.knowledge import obsidian_plugins
from woon_core.knowledge.obsidian_plugins import (
    FULL_CALENDAR_REMASTERED_ID,
    FULL_CALENDAR_SOURCE_COLOR,
    NOTION_BASES_ID,
    PRISMA_CALENDAR_EVENTS_DIRECTORY,
    PRISMA_CALENDAR_ID,
    PRISMA_READONLY_CONTEXT_MENU,
    PRISMA_READONLY_TOOLBAR,
    PRISMA_VIRTUAL_EVENTS_STEM,
    SIMPLE_CALENDAR_ID,
    SIMPLE_CALENDAR_SOURCE,
    ObsidianPluginService,
)


def _release(
    plugin_id: str, version: str, base_url: str
) -> tuple[dict[str, object], dict[str, bytes]]:
    assets = {
        "main.js": b"module.exports = {};\n",
        "manifest.json": json.dumps(
            {"id": plugin_id, "name": plugin_id, "version": version, "minAppVersion": "1.4.0"}
        ).encode(),
        "styles.css": b".mindmap { color: inherit; }\n",
    }
    return (
        {
            "tag_name": version,
            "html_url": f"{base_url}/release",
            "assets": [
                {
                    "name": name,
                    "browser_download_url": f"https://github.com/example/{plugin_id}/{name}",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                }
                for name, content in assets.items()
            ],
        },
        assets,
    )


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / ".obsidian" / "plugins").mkdir(parents=True)
    (vault / ".obsidian" / "community-plugins.json").write_text('["homepage"]\n', encoding="utf-8")
    return vault


def _write_core_notion_bases_projection(vault: Path) -> None:
    events = vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
    events.mkdir(parents=True)
    (events / "_database.md").write_text(
        """---
notion-bases: true
woon_projection: apple-calendar-notion-bases
schema:
  - id: Date
    name: 날짜
    type: date
    visible: false
views:
  - id: apple-calendar-month
    name: 월간 일정
    type: calendar
    filters: []
    sorts: []
    hiddenColumns:
      - Date
    columnWidths: {}
    calendarDateField: Date
    calendarViewMode: month
---
""",
        encoding="utf-8",
    )
    (events / "2026-08-18-example.md").write_text(
        """---
type: calendar-event
Date: 2026-08-18
woon_projection: apple-calendar
---
""",
        encoding="utf-8",
    )
    dashboard = vault / "inbox/calendar/apple-calendar.md"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(
        """---
woon_projection: apple-calendar-dashboard
---

```nb-database
path: inbox/calendar/events
type: calendar
```
""",
        encoding="utf-8",
    )
    for path in (*events.glob("*.md"), dashboard):
        path.chmod(0o400)
    events.chmod(0o500)


def _write_core_simple_calendar_projection(vault: Path) -> None:
    events = vault / SIMPLE_CALENDAR_SOURCE
    events.mkdir(parents=True)
    (events / "일정.md").write_text(
        """---
type: calendar-event
title: 일정
publish: false
access: local-only
status: Generated
source: apple-calendar-readonly
calendar: Woon 일정
Date: 2026-08-18
Category: 학습
Category ID: learning
All Day: true
woon_projection: apple-calendar
---
""",
        encoding="utf-8",
    )
    dashboard = vault / "inbox/calendar/apple-calendar.md"
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.write_text(
        """---
type: calendar-dashboard
title: Apple Calendar
publish: false
access: local-only
status: Generated
source: apple-calendar-readonly
woon_projection: apple-calendar-dashboard
cssclasses: woon-simple-calendar-dashboard
---

```woon-simple-calendar
source: inbox/calendar/events
date_field: Date
category_field: Category
category_id_field: Category ID
```
""",
        encoding="utf-8",
    )
    for path in (*events.glob("*.md"), dashboard):
        path.chmod(0o400)
    events.chmod(0o500)


def test_install_verifies_release_manifest_assets_and_enabled_config(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    releases: dict[str, bytes] = {}
    for plugin_id, version, repository in (
        ("light-mindmap", "1.5.0", "ninglg/light-mindmap"),
        ("markdown-mindmap", "1.4.2", "kikocastro/markdown-mindmap"),
        (PRISMA_CALENDAR_ID, "2.22.0", "Real1tyy/Prisma-Calendar"),
        (NOTION_BASES_ID, "1.12.0", "bgarciamoura/obsidian-notion-bases-plugin"),
    ):
        release, assets = _release(plugin_id, version, f"https://github.com/{repository}")
        releases[f"https://api.github.com/repos/{repository}/releases/latest"] = json.dumps(
            release
        ).encode()
        releases.update(
            {
                f"https://github.com/example/{plugin_id}/{name}": content
                for name, content in assets.items()
            }
        )

    receipt = ObsidianPluginService(vault, download=releases.__getitem__).install(
        [
            "light-mindmap",
            "markdown-mindmap",
            PRISMA_CALENDAR_ID,
            NOTION_BASES_ID,
        ]
    )

    assert [item["id"] for item in receipt["plugins"]] == [
        "light-mindmap",
        "markdown-mindmap",
        PRISMA_CALENDAR_ID,
        NOTION_BASES_ID,
    ]
    status = ObsidianPluginService(vault, download=releases.__getitem__).status()
    assert {item["id"] for item in status["plugins"]} == {
        "light-mindmap",
        "markdown-mindmap",
        PRISMA_CALENDAR_ID,
        NOTION_BASES_ID,
    }
    assert all(item["enabled_in_config"] for item in status["plugins"])
    assert list(
        (vault / ".local" / "woon-knowledge" / "obsidian-plugins" / "receipts").glob("*.json")
    )


def test_remove_detected_mindmaps_preserves_backup_and_unrelated_plugins(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian" / "plugins"
    old_map = plugins / "old-mindmap"
    old_map.mkdir()
    (old_map / "manifest.json").write_text(
        json.dumps({"id": "old-mindmap", "name": "Old Mindmap", "version": "0.1.0"}),
        encoding="utf-8",
    )
    unrelated = plugins / "homepage"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text(
        json.dumps({"id": "homepage", "name": "Homepage", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (vault / ".obsidian" / "community-plugins.json").write_text(
        '["old-mindmap", "homepage"]\n', encoding="utf-8"
    )

    receipt = ObsidianPluginService(vault).remove_detected_mindmaps()

    assert receipt["removed"] == ["old-mindmap"]
    assert not old_map.exists()
    assert unrelated.is_dir()
    assert "old-mindmap" not in json.loads(
        (vault / ".obsidian" / "community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local" / "woon-knowledge" / "obsidian-plugins" / "backups").rglob("old-mindmap")
    )


def test_install_restores_existing_plugin_if_stage_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian" / "plugins"
    existing = plugins / "light-mindmap"
    existing.mkdir()
    (existing / "manifest.json").write_text(
        json.dumps({"id": "light-mindmap", "name": "Old Light Mindmap", "version": "0.1.0"}),
        encoding="utf-8",
    )
    release, assets = _release("light-mindmap", "1.5.0", "https://github.com/ninglg/light-mindmap")
    downloads = {
        "https://api.github.com/repos/ninglg/light-mindmap/releases/latest": json.dumps(
            release
        ).encode(),
        **{
            f"https://github.com/example/light-mindmap/{name}": content
            for name, content in assets.items()
        },
    }
    original_replace = os.replace

    def fail_stage_replace(source: str | Path, destination: str | Path) -> None:
        if Path(source).name.startswith(".light-mindmap.staging-"):
            raise OSError("simulated staging replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr(obsidian_plugins.os, "replace", fail_stage_replace)

    with pytest.raises(OSError, match="simulated staging replacement failure"):
        ObsidianPluginService(vault, download=downloads.__getitem__).install(["light-mindmap"])

    restored = json.loads((existing / "manifest.json").read_text(encoding="utf-8"))
    assert restored["name"] == "Old Light Mindmap"


def test_configure_prisma_calendar_writes_a_read_only_projection_mapping(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / PRISMA_CALENDAR_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": PRISMA_CALENDAR_ID, "version": "2.22.0"}), encoding="utf-8"
    )

    receipt = ObsidianPluginService(vault).configure_prisma_calendar()
    configuration = json.loads((plugin / "data.json").read_text(encoding="utf-8"))

    assert receipt["plugin"] == {"id": PRISMA_CALENDAR_ID, "version": "2.22.0"}
    assert receipt["external_sync"] == "disabled"
    assert receipt["projection_write"] == "core-only"
    assert receipt["virtual_events_store"] == "hidden-empty-readonly"
    calendar = configuration["calendars"][0]
    assert calendar["id"] == "woon-apple-calendar"
    assert calendar["name"] == "Apple Calendar"
    assert calendar["enabled"] is True
    assert calendar["directory"] == PRISMA_CALENDAR_EVENTS_DIRECTORY
    assert calendar["startProp"] == "Start Date"
    assert calendar["endProp"] == "End Date"
    assert calendar["dateProp"] == "Date"
    assert calendar["allDayProp"] == "All Day"
    assert calendar["titleProp"] == "title"
    assert calendar["defaultView"] == "dayGridMonth"
    assert calendar["defaultMobileView"] == "dayGridMonth"
    assert calendar["locale"] == "ko"
    assert calendar["toolbarButtons"] == list(PRISMA_READONLY_TOOLBAR)
    assert calendar["mobileToolbarButtons"] == list(PRISMA_READONLY_TOOLBAR)
    assert calendar["contextMenuItems"] == list(PRISMA_READONLY_CONTEXT_MENU)
    assert calendar["batchActionButtons"] == []
    assert calendar["enableNotifications"] is False
    assert calendar["virtualEventsFileName"] == PRISMA_VIRTUAL_EVENTS_STEM
    assert configuration["caldav"]["accounts"] == []
    assert configuration["icsSubscriptions"]["subscriptions"] == []


def test_configure_prisma_moves_only_its_empty_legacy_virtual_events_store(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / PRISMA_CALENDAR_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": PRISMA_CALENDAR_ID, "version": "2.22.0"}), encoding="utf-8"
    )
    events = vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
    events.mkdir(parents=True)
    legacy_store = events / "Virtual Events.md"
    legacy_store.write_text(PRISMA_EMPTY_VIRTUAL_EVENTS, encoding="utf-8")

    ObsidianPluginService(vault).configure_prisma_calendar()

    hidden_store = events / PRISMA_VIRTUAL_EVENTS_FILENAME
    assert not legacy_store.exists()
    assert hidden_store.read_text(encoding="utf-8") == PRISMA_EMPTY_VIRTUAL_EVENTS
    assert hidden_store.stat().st_mode & 0o777 == 0o400
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob("Virtual Events.md")
    )


def test_configure_full_calendar_remastered_uses_month_only_local_ics(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / FULL_CALENDAR_REMASTERED_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}),
        encoding="utf-8",
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID]), encoding="utf-8"
    )
    events = vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
    events.mkdir(parents=True)
    support = events / PRISMA_VIRTUAL_EVENTS_FILENAME
    support.write_text(PRISMA_EMPTY_VIRTUAL_EVENTS, encoding="utf-8")

    receipt = ObsidianPluginService(vault).configure_full_calendar_remastered()
    configuration = json.loads((plugin / "data.json").read_text(encoding="utf-8"))

    assert receipt["plugin"] == {"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}
    assert receipt["external_sync"] == "disabled"
    assert receipt["projection_write"] == "core-only"
    assert receipt["initial_view"] == {"desktop": "dayGridMonth", "mobile": "dayGridMonth"}
    assert receipt["calendar_source"] == {
        "type": "ical",
        "id": "ical-woon-apple",
        "name": "Apple Calendar",
        "url": APPLE_CALENDAR_ICS_RELATIVE_PATH,
        "color": FULL_CALENDAR_SOURCE_COLOR,
    }
    assert configuration["dayMaxEvents"] == 4
    assert configuration["clickToCreateEventFromMonthView"] is False
    assert configuration["googleAccounts"] == []
    assert configuration["microsoftAccounts"] == []
    assert configuration["enableLocalServer"] is False
    assert configuration["activityWatch"]["enabled"] is False
    assert not support.exists()
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(
            PRISMA_VIRTUAL_EVENTS_FILENAME
        )
    )


def test_configure_full_calendar_rejects_nonempty_prisma_support_file(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / FULL_CALENDAR_REMASTERED_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}),
        encoding="utf-8",
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID]), encoding="utf-8"
    )
    events = vault / PRISMA_CALENDAR_EVENTS_DIRECTORY
    events.mkdir(parents=True)
    (events / PRISMA_VIRTUAL_EVENTS_FILENAME).write_text(
        "```prisma-virtual-events\n[{}]\n```\n", encoding="utf-8"
    )

    with pytest.raises(WoonError, match="Prisma virtual events store is not empty"):
        ObsidianPluginService(vault).configure_full_calendar_remastered()


def test_configure_notion_bases_requires_the_core_owned_month_projection(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / NOTION_BASES_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", NOTION_BASES_ID]), encoding="utf-8"
    )
    _write_core_notion_bases_projection(vault)

    receipt = ObsidianPluginService(vault).configure_notion_bases_calendar()

    assert receipt["plugin"] == {"id": NOTION_BASES_ID, "version": "1.12.0"}
    assert receipt["database"] == {
        "path": "inbox/calendar/events/_database.md",
        "date_field": "Date",
        "view": "calendar",
        "view_mode": "month",
        "card_fields": "title-only",
    }
    assert receipt["dashboard"] == "inbox/calendar/apple-calendar.md"
    assert receipt["external_sync"] == "disabled"
    assert receipt["projection_write"] == "core-only"


def test_configure_simple_calendar_installs_core_owned_assets_with_a_receipt(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_simple_calendar_projection(vault)
    release, assets = _release(
        SIMPLE_CALENDAR_ID, "1.0.3", "https://github.com/woonyong-kr/simple-calendar"
    )
    downloads = {
        "https://api.github.com/repos/woonyong-kr/simple-calendar/releases/latest": json.dumps(
            release
        ).encode(),
        **{
            f"https://github.com/example/{SIMPLE_CALENDAR_ID}/{name}": content
            for name, content in assets.items()
        },
    }

    service = ObsidianPluginService(vault, download=downloads.__getitem__)
    receipt = service.configure_simple_calendar()
    plugin = vault / ".obsidian/plugins" / SIMPLE_CALENDAR_ID
    status = service.status()
    installed = next(item for item in status["plugins"] if item["id"] == SIMPLE_CALENDAR_ID)

    assert receipt["plugin"]["id"] == SIMPLE_CALENDAR_ID
    assert receipt["plugin"]["repository"] == "woonyong-kr/simple-calendar"
    assert receipt["plugin"]["version"] == "1.0.3"
    assert receipt["source"] == SIMPLE_CALENDAR_SOURCE
    assert receipt["view_mode"] == "month-only"
    assert receipt["entrypoints"] == {
        "ribbon_icon": "calendar-days",
        "command": "open-simple-calendar",
    }
    assert receipt["card_behavior"] == {
        "time": "hidden",
        "category_color": "Core-projected-category-fill",
        "title": "two-lines-with-tooltip-and-readonly-markdown-detail",
    }
    renderer = (plugin / "main.js").read_text(encoding="utf-8")
    assert renderer == assets["main.js"].decode()
    manifest = json.loads((plugin / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == SIMPLE_CALENDAR_ID
    assert (plugin / "styles.css").is_file()
    assert installed["enabled_in_config"] is True
    assert SIMPLE_CALENDAR_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )


def test_retire_notion_bases_requires_simple_calendar_projection_and_plugin(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / NOTION_BASES_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps([NOTION_BASES_ID]), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="Simple Calendar source directory"):
        ObsidianPluginService(vault).retire([NOTION_BASES_ID])


def test_retire_notion_bases_keeps_a_backup_after_simple_calendar_validates(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    notion_bases = vault / ".obsidian/plugins" / NOTION_BASES_ID
    notion_bases.mkdir()
    (notion_bases / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    _write_core_simple_calendar_projection(vault)
    ObsidianPluginService(vault).configure_simple_calendar()

    receipt = ObsidianPluginService(vault).retire([NOTION_BASES_ID])

    assert receipt["retired"] == [NOTION_BASES_ID]
    assert not notion_bases.exists()
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(NOTION_BASES_ID))


def test_retire_full_calendar_requires_notion_bases_month_projection(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / FULL_CALENDAR_REMASTERED_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID]), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="Notion Bases must be enabled"):
        ObsidianPluginService(vault).retire([FULL_CALENDAR_REMASTERED_ID])


def test_retire_full_calendar_keeps_a_local_backup_after_notion_bases_validates(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian/plugins"
    full_calendar = plugins / FULL_CALENDAR_REMASTERED_ID
    full_calendar.mkdir()
    (full_calendar / "manifest.json").write_text(
        json.dumps({"id": FULL_CALENDAR_REMASTERED_ID, "version": "0.13.5"}), encoding="utf-8"
    )
    notion_bases = plugins / NOTION_BASES_ID
    notion_bases.mkdir()
    (notion_bases / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", FULL_CALENDAR_REMASTERED_ID, NOTION_BASES_ID]),
        encoding="utf-8",
    )
    _write_core_notion_bases_projection(vault)

    receipt = ObsidianPluginService(vault).retire([FULL_CALENDAR_REMASTERED_ID])

    assert receipt["retired"] == [FULL_CALENDAR_REMASTERED_ID]
    assert not full_calendar.exists()
    assert FULL_CALENDAR_REMASTERED_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(
            FULL_CALENDAR_REMASTERED_ID
        )
    )


def test_retire_moves_only_the_explicit_legacy_calendar_renderer(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    plugins = vault / ".obsidian/plugins"
    legacy = plugins / PRISMA_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": PRISMA_CALENDAR_ID, "version": "2.22.0"}), encoding="utf-8"
    )
    unrelated = plugins / "homepage"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text(
        json.dumps({"id": "homepage", "version": "1.0.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps([PRISMA_CALENDAR_ID, "homepage"]), encoding="utf-8"
    )

    receipt = ObsidianPluginService(vault).retire([PRISMA_CALENDAR_ID])

    assert receipt["retired"] == [PRISMA_CALENDAR_ID]
    assert not legacy.exists()
    assert unrelated.is_dir()
    assert PRISMA_CALENDAR_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text()
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(PRISMA_CALENDAR_ID)
    )
