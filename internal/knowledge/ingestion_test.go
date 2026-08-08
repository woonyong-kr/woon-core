package knowledge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestKnowledgeIgnoreSkipsFilesAndDirectories(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), basicKnowledgeConfig())
	writeFixture(t, filepath.Join(repo, ".knowledgeignore"), "**/*.tmp\nprivate/**\n")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "keep.md"), "keep")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "copy.tmp"), "ignore")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "private", "secret.md"), "ignore")
	result, err := Scan(repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.Files != 1 || result.Sources != 1 {
		t.Fatalf("ignored sources were scanned: %+v", result)
	}
}

func TestSecretSourceCreatesSafeDerivativeWithoutBlockingOtherFiles(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), basicKnowledgeConfig())
	credential := "ghp_" + strings.Repeat("A", 24)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "unsafe.md"), "token="+credential+"\n설명은 유지한다.\n")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "safe.md"), "정상 문서다.\n")
	result, err := Scan(repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.Sources != 2 {
		t.Fatalf("safe source was blocked: %+v", result)
	}
	catalog, err := loadCatalog(repo, mustConfig(t, repo))
	if err != nil {
		t.Fatal(err)
	}
	var sanitized Source
	for _, source := range catalog.Sources {
		if source.State == "sanitized" {
			sanitized = source
		}
	}
	if sanitized.ID == "" || sanitized.SanitizedPath == "" || !sanitized.RotationRequired || len(sanitized.Findings) != 1 {
		t.Fatalf("secret receipt is incomplete: %+v", sanitized)
	}
	path, _ := safePath(repo, sanitized.SanitizedPath)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), credential) || !strings.Contains(string(data), "[REDACTED_SECRET:github-token:") || !strings.Contains(string(data), "설명은 유지한다") {
		t.Fatalf("unsafe or incomplete sanitized derivative: %s", data)
	}
}

func TestWaitForStableSourcesRequiresQuietTree(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), basicKnowledgeConfig())
	path := filepath.Join(repo, "sources", "imports", "drop", "note.md")
	writeFixture(t, path, "first")
	cfg, err := LoadConfig(repo)
	if err != nil {
		t.Fatal(err)
	}
	cfg.Ingestion.Stability = StabilityConfig{QuietSeconds: 1, CheckIntervalSeconds: 1, RequiredEqualChecks: 1, MaxWaitSeconds: 4}
	started := time.Now()
	if err := WaitForStableSources(context.Background(), repo, cfg); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) < time.Second {
		t.Fatal("stability check returned before the quiet period")
	}
	second := time.Now()
	if err := WaitForStableSources(context.Background(), repo, cfg); err != nil {
		t.Fatal(err)
	}
	if time.Since(second) >= time.Second {
		t.Fatal("unchanged inbox did not reuse its stability receipt")
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
}

func basicKnowledgeConfig() string {
	return `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`
}
