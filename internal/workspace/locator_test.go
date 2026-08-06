package workspace

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestDiscoverAcceptsSameRootFromMultipleSources(t *testing.T) {
	root := t.TempDir()
	config := t.TempDir()
	t.Setenv("XDG_CONFIG_HOME", config)
	t.Setenv("WOON_HOME", root)
	if err := os.WriteFile(filepath.Join(root, markerName), []byte("version: 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(root); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(previous) })

	ws, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	want, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	if ws.Root != want {
		t.Fatalf("root = %q, want %q", ws.Root, want)
	}
}

func TestDiscoverRejectsAmbiguousRoots(t *testing.T) {
	t.Setenv("XDG_CONFIG_HOME", t.TempDir())
	t.Setenv("WOON_HOME", t.TempDir())
	_, err := Discover(t.TempDir())
	if err == nil {
		t.Fatal("expected ambiguous roots error")
	}
}

func TestInitializePersistsPortableRoot(t *testing.T) {
	config := t.TempDir()
	configPath := filepath.Join(config, "woon", "config.yaml")
	if runtime.GOOS == "windows" {
		t.Setenv("APPDATA", config)
		configPath = filepath.Join(config, "Woon", "config.yaml")
	} else {
		t.Setenv("XDG_CONFIG_HOME", config)
	}
	root := filepath.Join(t.TempDir(), "path with spaces", "woon")
	initialized, err := Initialize(root)
	if err != nil {
		t.Fatal(err)
	}
	if initialized != root {
		t.Fatalf("root = %q, want %q", initialized, root)
	}
	if _, err := os.Stat(filepath.Join(root, markerName)); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) == 0 {
		t.Fatal("config is empty")
	}
}
