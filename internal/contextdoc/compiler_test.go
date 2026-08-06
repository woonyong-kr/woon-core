package contextdoc

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/woonyong-kr/woon-core/internal/registry"
)

func TestGenerateThenCheckIsDeterministic(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, "woon-core")
	repo := filepath.Join(root, "target")
	mustWrite(t, filepath.Join(core, "config", "budgets.yaml"), "global_bootstrap_bytes: 2048\nrepository_instruction_bytes: 6144\n")
	mustWrite(t, filepath.Join(core, "policies", "safety.yaml"), "id: safety\nrules:\n  - Verify before success.\n")
	mustWrite(t, filepath.Join(core, "standards", "code.yaml"), "id: code\nrules:\n  - Keep code clear.\n")
	mustWrite(t, filepath.Join(core, "templates", "context", "instruction.md.tmpl"), "{{.Header}}\n{{.Manifest.ID}} {{.Target}}\n{{range .Policies}}{{range .Rules}}- {{.}}\n{{end}}{{end}}")
	mustWrite(t, filepath.Join(repo, "woon.yaml"), "id: target\nprofiles: [core]\nrequired_policies: [safety]\nrequired_standards: [code]\nrequired_checks: [unit]\n")
	reg := registry.Registry{Version: 1, Repositories: map[string]registry.Repository{
		"core":   {Remote: "https://github.com/example/core.git", Directory: "woon-core"},
		"target": {Remote: "https://github.com/example/target.git", Directory: "target"},
	}}
	compiler, err := New(root, reg)
	if err != nil {
		t.Fatal(err)
	}
	first, err := compiler.Generate(false, []string{"target"})
	if err != nil {
		t.Fatal(err)
	}
	if first.Artifacts != 4 {
		t.Fatalf("artifacts = %d, want 4", first.Artifacts)
	}
	before, err := os.ReadFile(filepath.Join(repo, "AGENTS.md"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := compiler.Generate(false, []string{"target"}); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(filepath.Join(repo, "AGENTS.md"))
	if err != nil {
		t.Fatal(err)
	}
	if string(before) != string(after) {
		t.Fatal("same input generated different output")
	}
	if _, err := compiler.Check(false, []string{"target"}); err != nil {
		t.Fatal(err)
	}
}

func TestGenerateRefusesUnmanagedInstruction(t *testing.T) {
	root := t.TempDir()
	core := filepath.Join(root, "woon-core")
	repo := filepath.Join(root, "target")
	mustWrite(t, filepath.Join(core, "config", "budgets.yaml"), "global_bootstrap_bytes: 2048\nrepository_instruction_bytes: 6144\n")
	mustWrite(t, filepath.Join(core, "policies", "safety.yaml"), "id: safety\nrules: [verify]\n")
	mustWrite(t, filepath.Join(core, "standards", "code.yaml"), "id: code\nrules: [keep-code-clear]\n")
	mustWrite(t, filepath.Join(core, "templates", "context", "instruction.md.tmpl"), "{{.Header}}\n{{.Manifest.ID}}\n")
	mustWrite(t, filepath.Join(repo, "woon.yaml"), "id: target\nprofiles: [core]\nrequired_policies: [safety]\nrequired_standards: [code]\nrequired_checks: [unit]\n")
	mustWrite(t, filepath.Join(repo, "AGENTS.md"), "human-owned\n")
	reg := registry.Registry{Version: 1, Repositories: map[string]registry.Repository{
		"core":   {Remote: "https://github.com/example/core.git", Directory: "woon-core"},
		"target": {Remote: "https://github.com/example/target.git", Directory: "target"},
	}}
	compiler, err := New(root, reg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := compiler.Generate(false, []string{"target"}); err == nil {
		t.Fatal("expected unmanaged file protection")
	}
}

func TestAuditPathsRejectsUnixAndWindowsHomes(t *testing.T) {
	for name, content := range map[string]string{
		"unix":    "/" + "Users/example/project",
		"windows": `C:` + `\Users\example\project`,
	} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			mustWrite(t, filepath.Join(root, "config.yaml"), "path: "+content+"\n")
			if err := auditPaths(root); err == nil {
				t.Fatal("expected absolute path violation")
			}
		})
	}
}

func mustWrite(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
