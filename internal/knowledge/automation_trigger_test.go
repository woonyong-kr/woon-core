package knowledge

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

type recordingAutomationCommandRunner struct {
	calls []string
}

func (r *recordingAutomationCommandRunner) Run(_ context.Context, name string, args ...string) (string, error) {
	r.calls = append(r.calls, name+" "+strings.Join(args, " "))
	if name == "launchctl" && len(args) > 0 && args[0] == "print" {
		return "state = not running\nruns = 7\nlast exit code = 0\nkeepalive = 0\n", nil
	}
	return "", nil
}

func TestMacOSLaunchdTriggerInstallsCoreExecutableAsOneShot(t *testing.T) {
	home := t.TempDir()
	runner := &recordingAutomationCommandRunner{}
	adapter := macOSLaunchdTrigger{
		environment: triggerEnvironment{goos: "darwin", home: home, uid: "501"},
		runner:      runner,
	}
	spec := automationTriggerSpec{
		Kind: "macos-launchd", Label: "org.example.knowledge",
		WorkspaceRoot: "/workspace/woon", KnowledgeRepo: "/workspace/woon/knowledge",
		Executable: "/usr/local/bin/woon", WatchPaths: []string{"/workspace/woon/knowledge/drop"},
		ThrottleSeconds: 30, RunAtLoad: true, Branch: "feature/knowledge",
		PathEnvironment: "/usr/local/bin:/usr/bin:/bin",
	}
	status, err := adapter.Install(context.Background(), spec)
	if err != nil {
		t.Fatal(err)
	}
	if !status.Installed || !status.Enabled || status.KeepAlive || status.Runs != 7 || status.State != "not running" {
		t.Fatalf("unexpected launchd status: %+v", status)
	}
	data, err := os.ReadFile(filepath.Join(home, "Library", "LaunchAgents", spec.Label+".plist"))
	if err != nil {
		t.Fatal(err)
	}
	plist := string(data)
	for _, expected := range []string{
		"<string>/usr/local/bin/woon</string>",
		"<string>knowledge</string>",
		"<string>automation</string>",
		"<string>run</string>",
		"<key>KeepAlive</key><false/>",
		"<key>RunAtLoad</key><true/>",
		"<string>feature/knowledge</string>",
	} {
		if !strings.Contains(plist, expected) {
			t.Fatalf("generated plist is missing %q:\n%s", expected, plist)
		}
	}
	joined := strings.Join(runner.calls, "\n")
	for _, expected := range []string{"launchctl bootout gui/501/org.example.knowledge", "launchctl enable gui/501/org.example.knowledge", "launchctl bootstrap gui/501", "launchctl print gui/501/org.example.knowledge"} {
		if !strings.Contains(joined, expected) {
			t.Fatalf("launchctl call is missing %q:\n%s", expected, joined)
		}
	}
}

func TestManualTriggerHasNoPersistentRegistration(t *testing.T) {
	adapter := manualTrigger{}
	spec := automationTriggerSpec{Kind: "manual", Label: "org.example.knowledge"}
	status, err := adapter.Install(context.Background(), spec)
	if err != nil {
		t.Fatal(err)
	}
	if status.State != "manual-ready" || !status.Enabled || status.KeepAlive || status.PlistPath != "" {
		t.Fatalf("unexpected manual trigger status: %+v", status)
	}
}

func TestAutomationConfigRejectsPushWithoutCommit(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
automation:
  trigger: manual
  auto_commit: false
  auto_push: true
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	if _, err := LoadConfig(repo); err == nil || !strings.Contains(err.Error(), "auto_push requires auto_commit") {
		t.Fatalf("push without commit was not rejected: %v", err)
	}
}

func TestAutomationPathExcludesUnrelatedSessionDirectories(t *testing.T) {
	t.Setenv("PATH", strings.Join([]string{
		filepath.Join(t.TempDir(), "codex-session"),
		filepath.Dir(mustLookPath(t, "git")),
	}, string(os.PathListSeparator)))
	for _, command := range []string{"codex", "woon-knowledge-vector", "gh"} {
		directory := filepath.Join(t.TempDir(), command)
		writeFixture(t, filepath.Join(directory, command), "#!/bin/sh\n")
		if err := os.Chmod(filepath.Join(directory, command), 0o755); err != nil {
			t.Fatal(err)
		}
		t.Setenv("PATH", directory+string(os.PathListSeparator)+os.Getenv("PATH"))
	}
	path, err := buildAutomationPath("/stable/bin/woon")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(path, "codex-session") || !strings.HasPrefix(path, "/stable/bin") {
		t.Fatalf("automation path is not stable: %s", path)
	}
}

func mustLookPath(t *testing.T, command string) string {
	t.Helper()
	path, err := exec.LookPath(command)
	if err != nil {
		t.Fatal(err)
	}
	return path
}
