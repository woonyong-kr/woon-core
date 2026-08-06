package envsync

import (
	"os"
	"path/filepath"
	"testing"
)

func TestJetBrainsKeyUsesSemanticTokens(t *testing.T) {
	for input, want := range map[string]string{
		"cmd+t":         "meta t",
		"ctrl+alt+left": "ctrl alt left",
		"alt+[":         "alt open_bracket",
		"shift+alt+]":   "shift alt close_bracket",
	} {
		if got := jetbrainsKey(input); got != want {
			t.Errorf("jetbrainsKey(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestJetBrainsKeymapComparisonIgnoresActionOrder(t *testing.T) {
	left := []byte(`<keymap version="1" name="Woon" parent="Mac OS X"><action id="Back"><keyboard-shortcut first-keystroke="ctrl minus"/></action><action id="Forward"><keyboard-shortcut first-keystroke="shift ctrl minus"/></action></keymap>`)
	right := []byte(`<keymap name="Woon" parent="Mac OS X" version="1"><action id="Forward"><keyboard-shortcut first-keystroke="shift ctrl minus" /></action><action id="Back"><keyboard-shortcut first-keystroke="ctrl minus" /></action></keymap>`)
	if !equalJetBrainsKeymaps(left, right) {
		t.Fatal("semantically equal keymaps were treated as different")
	}
}

func TestJSONComparisonIgnoresFormattingAndKeyOrder(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "expected.json")
	destination := filepath.Join(dir, "actual.json")
	mustWriteFile(t, source, []byte(`{"a":1,"b":[2,3]}`))
	mustWriteFile(t, destination, []byte("{\n  \"b\": [2, 3],\n  \"a\": 1\n}\n"))
	changed, err := differsSemantically("vscode/settings.json", source, destination)
	if err != nil {
		t.Fatal(err)
	}
	if changed {
		t.Fatal("semantically equal JSON was treated as different")
	}
}

func TestRollbackRestoresExistingAndRemovesCreatedFiles(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "existing.json")
	created := filepath.Join(dir, "created.json")
	backup := filepath.Join(dir, "backup")
	mustWriteFile(t, existing, []byte("before"))
	record, err := backupDestination(backup, 0, existing)
	if err != nil {
		t.Fatal(err)
	}
	createdRecord, err := backupDestination(backup, 1, created)
	if err != nil {
		t.Fatal(err)
	}
	mustWriteFile(t, existing, []byte("after"))
	mustWriteFile(t, created, []byte("created"))
	if err := rollback([]backupRecord{record, createdRecord}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(existing)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "before" {
		t.Fatalf("restored content = %q", data)
	}
	if _, err := os.Stat(created); !os.IsNotExist(err) {
		t.Fatalf("created file still exists: %v", err)
	}
}

func mustWriteFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}
