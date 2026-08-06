package registry

import (
	"path/filepath"
	"testing"
)

func TestResolveRepoURI(t *testing.T) {
	root := t.TempDir()
	reg := Registry{Version: 1, Repositories: map[string]Repository{
		"knowledge": {Remote: "https://github.com/example/knowledge.git", Directory: "woon-knowledge"},
	}}
	resolved, err := reg.Resolve(root, "repo://knowledge/wiki/os/page-fault.md")
	if err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(root, "woon-knowledge", "wiki", "os", "page-fault.md")
	if resolved != want {
		t.Fatalf("resolved = %q, want %q", resolved, want)
	}
}

func TestResolveRejectsEscape(t *testing.T) {
	reg := Registry{Version: 1, Repositories: map[string]Repository{
		"knowledge": {Remote: "https://github.com/example/knowledge.git", Directory: "woon-knowledge"},
	}}
	if _, err := reg.Resolve(t.TempDir(), "repo://knowledge/../secret"); err == nil {
		t.Fatal("expected escape error")
	}
}

func TestValidateRejectsAbsoluteDirectory(t *testing.T) {
	reg := Registry{Version: 1, Repositories: map[string]Repository{
		"bad": {Remote: "https://github.com/example/bad.git", Directory: "/absolute"},
	}}
	if err := reg.Validate(); err == nil {
		t.Fatal("expected validation error")
	}
}
