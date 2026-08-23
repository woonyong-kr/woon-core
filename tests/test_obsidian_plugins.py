from __future__ import annotations

import hashlib
import json
import os
import subprocess
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
    CONTEXT_CALENDAR_ID,
    CONTEXT_CALENDAR_MANUAL_ATTESTATION_CHECKS,
    CONTEXT_CALENDAR_PROFILE_ID,
    CONTEXT_CALENDAR_PROPERTY_FIELDS,
    CONTEXT_CALENDAR_SOURCE,
    CONTEXT_CALENDAR_VERSION,
    CONTEXT_GRAPH_ID,
    FULL_CALENDAR_REMASTERED_ID,
    FULL_CALENDAR_SOURCE_COLOR,
    LEGACY_SIMPLE_CALENDAR_ID,
    NOTION_BASES_ID,
    PRISMA_CALENDAR_EVENTS_DIRECTORY,
    PRISMA_CALENDAR_ID,
    PRISMA_READONLY_CONTEXT_MENU,
    PRISMA_READONLY_TOOLBAR,
    PRISMA_VIRTUAL_EVENTS_STEM,
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


def _write_core_context_calendar_projection(vault: Path) -> None:
    events = vault / CONTEXT_CALENDAR_SOURCE
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
cssclasses: context-calendar-dashboard
---

```context-calendar
profile: woon-apple-calendar
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


def _local_context_graph_build(root: Path, version: str = "0.4.1") -> Path:
    source = root / "context-graph-build"
    source.mkdir()
    (source / "main.js").write_text("module.exports = { version: 'new' };\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": CONTEXT_GRAPH_ID,
                "name": "Context Graph",
                "version": version,
                "minAppVersion": "1.8.0",
            }
        ),
        encoding="utf-8",
    )
    (source / "styles.css").write_text(".context-graph { display: block; }\n", encoding="utf-8")
    return source


def _local_context_calendar_build(root: Path, version: str = CONTEXT_CALENDAR_VERSION) -> Path:
    source = root / "context-calendar-build"
    source.mkdir()
    (source / "main.js").write_text("module.exports = { version: 'new' };\n", encoding="utf-8")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "id": CONTEXT_CALENDAR_ID,
                "name": "Context Calendar",
                "version": version,
                "minAppVersion": "1.10.0",
            }
        ),
        encoding="utf-8",
    )
    (source / "styles.css").write_text(".context-calendar { display: block; }\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Woon Test"), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "add",
            "origin",
            "https://github.com/woonyong-kr/simple-calendar.git",
        ),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "."), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-q", "-m", "fixture"), check=True)
    return source


def _install_context_calendar(vault: Path, build_root: Path) -> ObsidianPluginService:
    service = ObsidianPluginService(vault)
    service.install_local_build(
        CONTEXT_CALENDAR_ID,
        _local_context_calendar_build(build_root),
        CONTEXT_CALENDAR_VERSION,
    )
    return service


def _attest_context_calendar_runtime(service: ObsidianPluginService) -> dict[str, object]:
    return service.attest_context_calendar_runtime(list(CONTEXT_CALENDAR_MANUAL_ATTESTATION_CHECKS))


def test_install_local_build_preserves_settings_backup_hashes_and_enabled_config(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    settings = b'{"defaultGraphId":"interview"}\n'
    (destination / "data.json").write_bytes(settings)
    source = _local_context_graph_build(tmp_path)

    receipt = ObsidianPluginService(vault).install_local_build(CONTEXT_GRAPH_ID, source, "0.4.1")

    assert receipt["action"] == "install-local-build"
    assert receipt["plugin"]["id"] == CONTEXT_GRAPH_ID
    assert receipt["plugin"]["version"] == "0.4.1"
    assert receipt["plugin"]["preserved_settings"] == ["data.json"]
    assert (
        receipt["plugin"]["assets_sha256"]["main.js"]
        == hashlib.sha256((source / "main.js").read_bytes()).hexdigest()
    )
    assert (destination / "main.js").read_bytes() == (source / "main.js").read_bytes()
    assert (destination / "data.json").read_bytes() == settings
    backup = vault / str(receipt["backup"])
    assert (backup / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert CONTEXT_GRAPH_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"))


def test_install_local_build_rejects_a_version_mismatch_before_mutating_the_vault(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    source = _local_context_graph_build(tmp_path, version="0.4.0")

    with pytest.raises(WoonError, match="version does not match"):
        ObsidianPluginService(vault).install_local_build(CONTEXT_GRAPH_ID, source, "0.4.1")

    assert not (vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID).exists()
    assert json.loads((vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")) == [
        "homepage"
    ]


def test_install_local_build_preserves_destination_when_preflight_rejects_settings_entry(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_CALENDAR_ID, "version": "1.0.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    nested_settings = destination / "nested-settings"
    nested_settings.mkdir()
    (nested_settings / "state.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()

    with pytest.raises(WoonError, match="unsupported settings entry"):
        ObsidianPluginService(vault).install_local_build(
            CONTEXT_CALENDAR_ID,
            _local_context_calendar_build(tmp_path),
            CONTEXT_CALENDAR_VERSION,
        )

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (nested_settings / "state.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_accepts_context_calendar_at_the_pinned_version(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)

    receipt = ObsidianPluginService(vault).install_local_build(
        CONTEXT_CALENDAR_ID,
        _local_context_calendar_build(tmp_path),
        CONTEXT_CALENDAR_VERSION,
    )

    assert receipt["plugin"]["id"] == CONTEXT_CALENDAR_ID
    assert receipt["plugin"]["version"] == CONTEXT_CALENDAR_VERSION
    assert receipt["plugin"]["source"]["repository"] == (
        "https://github.com/woonyong-kr/simple-calendar.git"
    )
    assert len(receipt["plugin"]["source"]["head_commit"]) == 40
    assert receipt["plugin"]["source"]["clean"] is True
    assert CONTEXT_CALENDAR_ID in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("failure", ["dirty", "wrong-origin", "not-git"])
def test_context_calendar_local_build_requires_approved_clean_git_provenance(
    tmp_path: Path, failure: str
) -> None:
    vault = _vault(tmp_path)
    source = _local_context_calendar_build(tmp_path)
    if failure == "dirty":
        (source / "main.js").write_text("dirty runtime\n", encoding="utf-8")
        expected = "must be clean"
    elif failure == "wrong-origin":
        subprocess.run(
            ("git", "-C", str(source), "remote", "set-url", "origin", "https://example.com/x.git"),
            check=True,
        )
        expected = "origin is not approved"
    else:
        (source / ".git").rename(source / ".not-git")
        expected = "Git provenance is invalid"

    with pytest.raises(WoonError, match=expected):
        ObsidianPluginService(vault).install_local_build(
            CONTEXT_CALENDAR_ID,
            source,
            CONTEXT_CALENDAR_VERSION,
        )

    assert not (vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID).exists()


def test_context_calendar_git_origin_accepts_the_normalized_url_without_git_suffix(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    source = _local_context_calendar_build(tmp_path)
    subprocess.run(
        (
            "git",
            "-C",
            str(source),
            "remote",
            "set-url",
            "origin",
            "https://github.com/woonyong-kr/simple-calendar",
        ),
        check=True,
    )

    receipt = ObsidianPluginService(vault).install_local_build(
        CONTEXT_CALENDAR_ID,
        source,
        CONTEXT_CALENDAR_VERSION,
    )

    assert receipt["plugin"]["source"]["repository"].endswith("simple-calendar.git")


@pytest.mark.parametrize("linked_root", ["obsidian", "plugins", "local", "receipts"])
def test_plugin_mutations_reject_control_path_symlinks_before_lock_or_write(
    tmp_path: Path, linked_root: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    vault = tmp_path / "vault"
    if linked_root == "obsidian":
        vault.mkdir()
        (outside / "plugins").mkdir()
        (outside / "community-plugins.json").write_text("[]\n", encoding="utf-8")
        (vault / ".obsidian").symlink_to(outside, target_is_directory=True)
    else:
        vault = _vault(tmp_path)
        if linked_root == "plugins":
            (vault / ".obsidian/plugins").rmdir()
            (vault / ".obsidian/plugins").symlink_to(outside, target_is_directory=True)
        elif linked_root == "local":
            (vault / ".local").symlink_to(outside, target_is_directory=True)
        else:
            local = vault / ".local/woon-knowledge/obsidian-plugins"
            local.mkdir(parents=True)
            (local / "receipts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WoonError, match="regular Vault directory"):
        ObsidianPluginService(vault).install_local_build(
            CONTEXT_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert not (outside / CONTEXT_GRAPH_ID).exists()
    assert not (outside / "woon-knowledge/obsidian-plugins/mutation.lock").exists()
    assert not (vault / ".local/woon-knowledge/obsidian-plugins/mutation.lock").exists()


def test_install_local_build_restores_runtime_settings_and_enabled_config_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    (destination / "data.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()
    source = _local_context_graph_build(tmp_path)
    service = ObsidianPluginService(vault)

    def fail_enabled_write(enabled: set[str], backup_root: Path) -> None:
        raise OSError("simulated enabled config failure")

    monkeypatch.setattr(service, "_write_enabled_ids", fail_enabled_write)

    with pytest.raises(OSError, match="simulated enabled config failure"):
        service.install_local_build(CONTEXT_GRAPH_ID, source, "0.4.1")

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (destination / "data.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_rolls_back_if_receipt_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    (destination / "data.json").write_text('{"kept":true}\n', encoding="utf-8")
    enabled_before = (vault / ".obsidian/community-plugins.json").read_bytes()
    source = _local_context_graph_build(tmp_path)
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_write(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            raise OSError("simulated receipt failure")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_write)

    with pytest.raises(OSError, match="simulated receipt failure"):
        ObsidianPluginService(vault).install_local_build(CONTEXT_GRAPH_ID, source, "0.4.1")

    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"
    assert (destination / "data.json").read_text(encoding="utf-8") == '{"kept":true}\n'
    assert (vault / ".obsidian/community-plugins.json").read_bytes() == enabled_before


def test_install_local_build_refuses_destructive_rollback_after_plugin_tree_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_after_drift(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            (destination / "concurrent.json").write_text('{"owner":"Obsidian"}\n', encoding="utf-8")
            raise OSError("simulated receipt failure after plugin drift")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_after_drift)

    with pytest.raises(WoonError, match="destructive rollback refused"):
        ObsidianPluginService(vault).install_local_build(
            CONTEXT_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert (destination / "concurrent.json").is_file()
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob("main.js"))


def test_install_local_build_preserves_concurrent_enabled_config_during_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    destination = vault / ".obsidian/plugins" / CONTEXT_GRAPH_ID
    destination.mkdir()
    (destination / "main.js").write_text("old runtime\n", encoding="utf-8")
    (destination / "manifest.json").write_text(
        json.dumps({"id": CONTEXT_GRAPH_ID, "version": "0.4.0"}), encoding="utf-8"
    )
    (destination / "styles.css").write_text("old styles\n", encoding="utf-8")
    enabled_path = vault / ".obsidian/community-plugins.json"
    concurrent = b'["homepage", "concurrent-plugin"]\n'
    atomic_write = obsidian_plugins._atomic_write

    def fail_receipt_after_enabled_drift(path: Path, content: bytes) -> None:
        if "receipts" in path.parts:
            enabled_path.write_bytes(concurrent)
            raise OSError("simulated receipt failure after enabled drift")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt_after_enabled_drift)

    with pytest.raises(WoonError, match="rollback refused"):
        ObsidianPluginService(vault).install_local_build(
            CONTEXT_GRAPH_ID,
            _local_context_graph_build(tmp_path),
            "0.4.1",
        )

    assert enabled_path.read_bytes() == concurrent
    assert (destination / "main.js").read_text(encoding="utf-8") == "old runtime\n"


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


def test_configure_context_calendar_preserves_user_settings_and_receipts_readonly_profile(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    settings = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID / "data.json"
    settings.write_text(
        json.dumps(
            {
                "locale": "ko",
                "sourceProfiles": [
                    {
                        "id": "personal-notes",
                        "name": "Personal notes",
                        "enabled": True,
                        "source": {"type": "folder", "path": "notes", "recursive": True},
                        "editable": True,
                        "properties": {"date": "date"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings_before = settings.read_bytes()

    receipt = service.configure_context_calendar()
    configured = json.loads(settings.read_text(encoding="utf-8"))

    assert receipt["action"] == "configure-context-calendar"
    assert receipt["plugin"] == {
        "id": CONTEXT_CALENDAR_ID,
        "version": CONTEXT_CALENDAR_VERSION,
    }
    assert receipt["source_profile"] == {
        "id": CONTEXT_CALENDAR_PROFILE_ID,
        "name": "Apple Calendar",
        "enabled": True,
        "source": {
            "type": "folder",
            "path": CONTEXT_CALENDAR_SOURCE,
            "recursive": False,
            "tag": "",
        },
        "editable": False,
        "properties": dict(CONTEXT_CALENDAR_PROPERTY_FIELDS),
    }
    assert configured["locale"] == "ko"
    assert configured["sourceProfiles"][0]["id"] == "personal-notes"
    assert configured["sourceProfiles"][1] == receipt["source_profile"]
    assert "activeSourceProfileId" not in configured
    backup = vault / receipt["settings"]["backup"]
    assert backup.read_bytes() == settings_before
    assert receipt["settings"]["sha256"] == hashlib.sha256(settings.read_bytes()).hexdigest()


@pytest.mark.parametrize("linked_path", ["directory", "dashboard", "event"])
def test_configure_context_calendar_rejects_symlinked_projection_paths(
    tmp_path: Path, linked_path: str
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    events = vault / CONTEXT_CALENDAR_SOURCE
    dashboard = vault / "inbox/calendar/apple-calendar.md"
    if linked_path == "directory":
        outside = tmp_path / "outside-events"
        events.chmod(0o700)
        os.replace(events, outside)
        events.symlink_to(outside, target_is_directory=True)
    elif linked_path == "dashboard":
        outside = tmp_path / "outside-dashboard.md"
        os.replace(dashboard, outside)
        dashboard.symlink_to(outside)
    else:
        event = events / "일정.md"
        outside = tmp_path / "outside-event.md"
        events.chmod(0o700)
        os.replace(event, outside)
        event.symlink_to(outside)
        events.chmod(0o500)
    service = _install_context_calendar(vault, tmp_path)

    try:
        with pytest.raises(WoonError, match="regular Vault"):
            service.configure_context_calendar()
    finally:
        if events.is_symlink():
            events.unlink()
            outside.chmod(0o700)
        elif events.exists():
            events.chmod(0o700)
            for path in events.iterdir():
                if path.is_symlink():
                    path.unlink()
                elif path.is_file():
                    path.chmod(0o600)
        if dashboard.is_symlink():
            dashboard.unlink()
        elif dashboard.exists():
            dashboard.chmod(0o600)
        if outside.is_file():
            outside.chmod(0o600)


def test_configure_context_calendar_restores_settings_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    settings = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID / "data.json"
    settings_before = b'{"locale":"en"}\n'
    settings.write_bytes(settings_before)
    original_atomic_write = obsidian_plugins._atomic_write

    def fail_receipt(path: Path, content: bytes) -> None:
        if path.parent.name == "receipts" and b'"configure-context-calendar"' in content:
            raise OSError("simulated receipt failure")
        original_atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_receipt)

    with pytest.raises(OSError, match="simulated receipt failure"):
        service.configure_context_calendar()

    assert settings.read_bytes() == settings_before


def test_configure_context_calendar_rejects_concurrent_settings_drift_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    settings = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID / "data.json"
    settings.write_text('{"locale":"en"}\n', encoding="utf-8")
    concurrent = b'{"locale":"ko","changedBy":"Obsidian"}\n'
    original_configuration = obsidian_plugins._context_calendar_configuration

    def drift_after_read(existing: dict[str, object]) -> dict[str, object]:
        configuration = original_configuration(existing)
        settings.write_bytes(concurrent)
        return configuration

    monkeypatch.setattr(
        obsidian_plugins,
        "_context_calendar_configuration",
        drift_after_read,
    )

    with pytest.raises(WoonError, match="changed concurrently"):
        service.configure_context_calendar()

    assert settings.read_bytes() == concurrent


def test_configure_context_calendar_requires_enabled_verified_local_build(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    (vault / ".obsidian/community-plugins.json").write_text('["homepage"]\n', encoding="utf-8")

    with pytest.raises(WoonError, match="must be enabled"):
        service.configure_context_calendar()

    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", CONTEXT_CALENDAR_ID]), encoding="utf-8"
    )
    for receipt in (vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json"):
        receipt.unlink()

    with pytest.raises(WoonError, match="verified local-build adapter"):
        service.configure_context_calendar()


def test_context_calendar_static_gate_rejects_install_receipt_without_git_provenance(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    receipt_root = vault / ".local/woon-knowledge/obsidian-plugins/receipts"
    install_receipt = next(
        path
        for path in receipt_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["action"] == "install-local-build"
    )
    payload = json.loads(install_receipt.read_text(encoding="utf-8"))
    payload["plugin"].pop("source")
    install_receipt.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WoonError, match="verified local-build adapter"):
        service.configure_context_calendar()


def test_configure_context_calendar_is_deterministic_and_does_not_duplicate_profile(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    settings = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID / "data.json"

    service.configure_context_calendar()
    first = settings.read_bytes()
    service.configure_context_calendar()
    second = settings.read_bytes()
    configuration = json.loads(second)

    assert second == first
    assert [
        profile["id"]
        for profile in configuration["sourceProfiles"]
        if profile["id"] == CONTEXT_CALENDAR_PROFILE_ID
    ] == [CONTEXT_CALENDAR_PROFILE_ID]


def test_configure_context_calendar_rejects_an_unapproved_local_version(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    ObsidianPluginService(vault).install_local_build(
        CONTEXT_CALENDAR_ID,
        _local_context_calendar_build(tmp_path, version="2.0.3"),
        "2.0.3",
    )

    with pytest.raises(WoonError, match="version must match"):
        ObsidianPluginService(vault).configure_context_calendar()


def test_attest_context_calendar_runtime_requires_complete_explicit_checklist(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()

    with pytest.raises(WoonError, match="complete UI checklist"):
        service.attest_context_calendar_runtime(["ribbon", "month-view"])

    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("action")
        == "attest-context-calendar-runtime"
        for path in (vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json")
    )


def test_attest_context_calendar_runtime_receipt_binds_current_static_evidence(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()

    receipt = _attest_context_calendar_runtime(service)

    assert receipt["action"] == "attest-context-calendar-runtime"
    assert receipt["operator_attested_checks"] == list(CONTEXT_CALENDAR_MANUAL_ATTESTATION_CHECKS)
    assert receipt["attestation"] == "manual-operator-confirmation-after-Obsidian-reload"
    assert receipt["plugin"]["assets_sha256"]
    assert receipt["settings"]["sha256"]
    assert receipt["dashboard"]["sha256"]


def test_retire_legacy_simple_calendar_requires_manual_runtime_attestation(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()

    with pytest.raises(WoonError, match="manual operator attestation after reload"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_rejects_stale_runtime_receipt_after_dashboard_changes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)
    dashboard = vault / "inbox/calendar/apple-calendar.md"
    dashboard.chmod(0o600)
    dashboard.write_text(dashboard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    dashboard.chmod(0o400)

    with pytest.raises(WoonError, match="manual operator attestation after reload"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_notion_bases_requires_context_calendar_projection_and_plugin(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    plugin = vault / ".obsidian/plugins" / NOTION_BASES_ID
    plugin.mkdir()
    (plugin / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps([NOTION_BASES_ID]), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="Context Calendar source directory"):
        ObsidianPluginService(vault).retire([NOTION_BASES_ID])


def test_retire_notion_bases_keeps_a_backup_after_context_calendar_validates(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    notion_bases = vault / ".obsidian/plugins" / NOTION_BASES_ID
    notion_bases.mkdir()
    (notion_bases / "manifest.json").write_text(
        json.dumps({"id": NOTION_BASES_ID, "version": "1.12.0"}), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)

    receipt = service.retire([NOTION_BASES_ID])

    assert receipt["retired"] == [NOTION_BASES_ID]
    assert not notion_bases.exists()
    assert list((vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(NOTION_BASES_ID))


def test_retire_legacy_simple_calendar_requires_verified_context_calendar(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="Context Calendar source directory"):
        ObsidianPluginService(vault).retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_legacy_simple_calendar_rejects_settings_drift_after_configuration(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)
    settings = vault / ".obsidian/plugins" / CONTEXT_CALENDAR_ID / "data.json"
    configuration = json.loads(settings.read_text(encoding="utf-8"))
    configuration["sourceProfiles"][-1]["editable"] = True
    settings.write_text(json.dumps(configuration), encoding="utf-8")

    with pytest.raises(WoonError, match="read-only source profile"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


def test_retire_legacy_simple_calendar_keeps_backup_after_all_guards_pass(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"}), encoding="utf-8"
    )
    (vault / ".obsidian/community-plugins.json").write_text(
        json.dumps(["homepage", LEGACY_SIMPLE_CALENDAR_ID]), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)

    receipt = service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert receipt["retired"] == [LEGACY_SIMPLE_CALENDAR_ID]
    assert not legacy.exists()
    assert LEGACY_SIMPLE_CALENDAR_ID not in json.loads(
        (vault / ".obsidian/community-plugins.json").read_text(encoding="utf-8")
    )
    assert list(
        (vault / ".local/woon-knowledge/obsidian-plugins/backups").rglob(LEGACY_SIMPLE_CALENDAR_ID)
    )


def test_retire_rolls_back_plugin_and_enabled_config_when_receipt_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    manifest = json.dumps({"id": LEGACY_SIMPLE_CALENDAR_ID, "version": "1.1.1"})
    (legacy / "manifest.json").write_text(manifest, encoding="utf-8")
    enabled_path = vault / ".obsidian/community-plugins.json"
    enabled_path.write_text(json.dumps(["homepage", LEGACY_SIMPLE_CALENDAR_ID]), encoding="utf-8")
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)
    enabled_before = enabled_path.read_bytes()
    atomic_write = obsidian_plugins._atomic_write

    def fail_retire_receipt(path: Path, content: bytes) -> None:
        if path.parent.name == "receipts" and b'"action": "retire"' in content:
            raise OSError("simulated retire receipt failure")
        atomic_write(path, content)

    monkeypatch.setattr(obsidian_plugins, "_atomic_write", fail_retire_receipt)

    with pytest.raises(OSError, match="simulated retire receipt failure"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert (legacy / "manifest.json").read_text(encoding="utf-8") == manifest
    assert enabled_path.read_bytes() == enabled_before
    assert not any(
        json.loads(path.read_text(encoding="utf-8")).get("action") == "retire"
        for path in (vault / ".local/woon-knowledge/obsidian-plugins/receipts").glob("*.json")
    )


def test_retire_rejects_a_directory_whose_manifest_id_does_not_match(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    legacy = vault / ".obsidian/plugins" / LEGACY_SIMPLE_CALENDAR_ID
    legacy.mkdir()
    (legacy / "manifest.json").write_text(
        json.dumps({"id": "unrelated-plugin", "version": "1.0.0"}), encoding="utf-8"
    )
    _write_core_context_calendar_projection(vault)
    service = _install_context_calendar(vault, tmp_path)
    service.configure_context_calendar()
    _attest_context_calendar_runtime(service)

    with pytest.raises(WoonError, match="manifest is invalid"):
        service.retire([LEGACY_SIMPLE_CALENDAR_ID])

    assert legacy.is_dir()


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
