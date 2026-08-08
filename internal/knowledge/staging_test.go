package knowledge

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestStageKnowledgeChangesExcludesRawSecret(t *testing.T) {
	repo := t.TempDir()
	runTestGit(t, repo, "init")
	runTestGit(t, repo, "config", "user.name", "Test")
	runTestGit(t, repo, "config", "user.email", "test@example.invalid")
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), basicKnowledgeConfig())
	credential := "ghp_" + strings.Repeat("B", 24)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "unsafe.md"), credential)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "safe.md"), "safe")
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	result, err := StageKnowledgeChanges(context.Background(), repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.StagedFiles < 3 {
		t.Fatalf("expected generated files and safe source to be staged: %+v", result)
	}
	staged := runTestGit(t, repo, "diff", "--cached", "--name-only")
	if strings.Contains(staged, "unsafe.md") {
		t.Fatalf("raw secret was staged:\n%s", staged)
	}
	if !strings.Contains(staged, "safe.md") || !strings.Contains(staged, "knowledge-ops/sanitized/") {
		t.Fatalf("approved paths were not staged:\n%s", staged)
	}
	ignored := runTestGit(t, repo, "check-ignore", "sources/imports/drop/unsafe.md")
	if !strings.Contains(ignored, "unsafe.md") {
		t.Fatalf("raw secret is not locally excluded: %s", ignored)
	}
	quarantined, err := filepath.Glob(filepath.Join(repo, ".knowledge-runtime", "quarantine", "src-*", "unsafe.md"))
	if err != nil || len(quarantined) != 1 {
		t.Fatalf("raw secret was not preserved in quarantine: %v err=%v", quarantined, err)
	}
}

func TestStageKnowledgeChangesRoutesLargeFileToLFS(t *testing.T) {
	if _, err := exec.LookPath("git-lfs"); err != nil {
		t.Skip("git-lfs is not installed")
	}
	repo := t.TempDir()
	runTestGit(t, repo, "init")
	runTestGit(t, repo, "config", "user.name", "Test")
	runTestGit(t, repo, "config", "user.email", "test@example.invalid")
	config := basicKnowledgeConfig() + "ingestion:\n  size_policy: {regular_git_max_mib: 1, large_file_strategy: git-lfs, text_processing: streaming, image_analysis_max_dimension: 2048, preserve_original: true}\n"
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), config)
	path := filepath.Join(repo, "sources", "imports", "drop", "large.bin")
	writeFixture(t, path, "large")
	if err := os.Truncate(path, 2*1024*1024); err != nil {
		t.Fatal(err)
	}
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	result, err := StageKnowledgeChanges(context.Background(), repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.LFSFiles != 1 || len(result.BlockedLargeFiles) != 0 {
		t.Fatalf("large file was not routed to LFS: %+v", result)
	}
	attribute := runTestGit(t, repo, "check-attr", "filter", "--", "sources/imports/drop/large.bin")
	if !strings.HasSuffix(strings.TrimSpace(attribute), ": lfs") {
		t.Fatalf("large file lacks LFS attribute: %s", attribute)
	}
}

func runTestGit(t *testing.T, repo string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", repo}, args...)...)
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s: %v: %s", strings.Join(args, " "), err, output)
	}
	return string(output)
}
