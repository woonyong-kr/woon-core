package skills

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/woonyong-kr/woon-core/internal/registry"
)

func TestInstallDetectsDriftAndRepairsMissingSkill(t *testing.T) {
	root, reg := skillsFixture(t)
	target := filepath.Join(t.TempDir(), "target with spaces", "codex")
	t.Setenv("WOON_CODEX_SKILLS_HOME", target)

	plan, err := Plan(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if got := plan.Items[0].Action; got != "install" {
		t.Fatalf("initial action = %q, want install", got)
	}
	if _, err := Install(root, reg, []string{"core"}, "codex"); err != nil {
		t.Fatal(err)
	}
	plan, err = Plan(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if got := plan.Items[0].Action; got != "unchanged" {
		t.Fatalf("installed action = %q, want unchanged", got)
	}

	skillFile := filepath.Join(target, "demo", "SKILL.md")
	if err := os.WriteFile(skillFile, []byte("drift\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	plan, err = Plan(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if got := plan.Items[0].Action; got != "update" {
		t.Fatalf("drift action = %q, want update", got)
	}
	result, err := Install(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if result.Backup == "" {
		t.Fatal("drift update did not retain a backup")
	}

	if err := os.RemoveAll(filepath.Join(target, "demo")); err != nil {
		t.Fatal(err)
	}
	plan, err = Plan(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if got := plan.Items[0].Action; got != "repair" {
		t.Fatalf("missing action = %q, want repair", got)
	}
	if _, err := Install(root, reg, []string{"core"}, "codex"); err != nil {
		t.Fatal(err)
	}
}

func TestInstallRefusesUnmanagedSkill(t *testing.T) {
	root, reg := skillsFixture(t)
	target := filepath.Join(t.TempDir(), "codex")
	t.Setenv("WOON_CODEX_SKILLS_HOME", target)
	if err := os.MkdirAll(filepath.Join(target, "demo"), 0o755); err != nil {
		t.Fatal(err)
	}

	plan, err := Plan(root, reg, []string{"core"}, "codex")
	if err != nil {
		t.Fatal(err)
	}
	if got := plan.Items[0].Action; got != "blocked" {
		t.Fatalf("unmanaged action = %q, want blocked", got)
	}
	if _, err := Install(root, reg, []string{"core"}, "codex"); err == nil {
		t.Fatal("install overwrote an unmanaged skill")
	}
}

func TestValidateRejectsMissingConflictMember(t *testing.T) {
	root, reg := skillsFixture(t)
	conflicts := filepath.Join(root, "woon-skills", "conflicts", "conflicts.yaml")
	writeFixture(t, conflicts, "version: 1\ngroups:\n  - id: stale\n    mode: exclusive\n    members: [personal/demo, personal/missing]\n")
	if _, err := Validate(root, reg, []string{"core"}); err == nil {
		t.Fatal("validation accepted a missing conflict member")
	}
}

func skillsFixture(t *testing.T) (string, registry.Registry) {
	t.Helper()
	root := filepath.Join(t.TempDir(), "workspace with spaces")
	repo := filepath.Join(root, "woon-skills")
	writeFixture(t, filepath.Join(repo, "profiles", "core.yaml"), "version: 1\nname: core\nmax_active: 20\nskills: [personal/demo]\n")
	writeFixture(t, filepath.Join(repo, "conflicts", "effects.yaml"), "version: 1\ndefault: [read]\nskills: {}\n")
	writeFixture(t, filepath.Join(repo, "conflicts", "conflicts.yaml"), "version: 1\ngroups: []\n")
	writeFixture(t, filepath.Join(repo, "lock", "sources.yaml"), "version: 1\norigins:\n  personal:\n    path: personal\n    policy: maintained\n")
	writeFixture(t, filepath.Join(repo, "personal", "demo", "SKILL.md"), "---\nname: demo\ndescription: Test skill.\n---\n\n# Demo\n")
	reg := registry.Registry{Version: 1, Repositories: map[string]registry.Repository{
		"skills": {Remote: "https://github.com/example/woon-skills.git", Directory: "woon-skills"},
	}}
	return root, reg
}

func writeFixture(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}
