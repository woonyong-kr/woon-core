from pathlib import Path

from woon_core.environment.generator import equal_jetbrains_keymaps, jetbrains_key
from woon_core.environment.machine import backup_destination, differs_semantically, rollback


def test_jetbrains_key_uses_semantic_tokens() -> None:
    assert jetbrains_key("cmd+t") == "meta t"
    assert jetbrains_key("ctrl+alt+left") == "ctrl alt left"
    assert jetbrains_key("alt+[") == "alt open_bracket"
    assert jetbrains_key("shift+alt+]") == "shift alt close_bracket"


def test_jetbrains_keymap_comparison_ignores_action_order() -> None:
    left = (
        b'<keymap name="Woon" parent="Mac"><action id="Back">'
        b'<keyboard-shortcut first-keystroke="ctrl minus"/></action>'
        b'<action id="Forward"/></keymap>'
    )
    right = (
        b'<keymap parent="Mac" name="Woon"><action id="Forward"/>'
        b'<action id="Back"><keyboard-shortcut first-keystroke="ctrl minus" />'
        b"</action></keymap>"
    )
    assert equal_jetbrains_keymaps(left, right)


def test_json_comparison_ignores_formatting_and_key_order(tmp_path: Path) -> None:
    source = tmp_path / "expected.json"
    destination = tmp_path / "actual.json"
    source.write_text('{"a":1,"b":[2,3]}')
    destination.write_text('{\n  "b": [2, 3],\n  "a": 1\n}\n')
    assert not differs_semantically("vscode/settings.json", source, destination)


def test_rollback_restores_existing_and_removes_created_files(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    created = tmp_path / "created.json"
    existing.write_text("before")
    existing_record = backup_destination(tmp_path / "backup", 0, existing)
    created_record = backup_destination(tmp_path / "backup", 1, created)
    existing.write_text("after")
    created.write_text("created")
    assert rollback([existing_record, created_record]) == []
    assert existing.read_text() == "before"
    assert not created.exists()
