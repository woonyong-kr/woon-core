package app

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRunFullplateShowsProductSuiteUsage(t *testing.T) {
	var output bytes.Buffer
	if err := RunFullplate(nil, &output, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	for _, expected := range []string{"Fullplate - local-first tool suite", "fullplate helm <command>", "helm    Turn unorganized material"} {
		if !strings.Contains(output.String(), expected) {
			t.Fatalf("Fullplate usage is missing %q:\n%s", expected, output.String())
		}
	}
}

func TestRunFullplateShowsHelmUsageWithoutLegacyCommandName(t *testing.T) {
	for _, args := range [][]string{{"helm"}, {"helm", "--help"}} {
		var output bytes.Buffer
		if err := RunFullplate(args, &output, &bytes.Buffer{}); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(output.String(), "Fullplate: Helm") || !strings.Contains(output.String(), "fullplate helm run") || !strings.Contains(output.String(), "fullplate helm automation") {
			t.Fatalf("Helm usage is incomplete:\n%s", output.String())
		}
		if strings.Contains(output.String(), "woon knowledge") {
			t.Fatalf("Helm usage leaked the legacy command name:\n%s", output.String())
		}
	}
}

func TestRunFullplateRejectsUnknownApp(t *testing.T) {
	err := RunFullplate([]string{"gauntlet"}, &bytes.Buffer{}, &bytes.Buffer{})
	if err == nil || !strings.Contains(err.Error(), `unknown Fullplate app "gauntlet"`) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestParseFullplateGlobalSeparatesDirectWorkspaceFromCommand(t *testing.T) {
	opts, workspace, args, err := parseFullplateGlobal([]string{"helm", "--workspace", "/knowledge", "status"})
	if err != nil {
		t.Fatal(err)
	}
	if opts.root != "" || workspace != "/knowledge" || strings.Join(args, " ") != "helm status" {
		t.Fatalf("unexpected parse result: opts=%+v workspace=%q args=%v", opts, workspace, args)
	}
}

func TestParseFullplateGlobalRejectsAmbiguousWorkspaceSelection(t *testing.T) {
	_, _, _, err := parseFullplateGlobal([]string{"--workspace", "/knowledge", "--root", "/woon", "helm", "status"})
	if err == nil || !strings.Contains(err.Error(), "cannot be used together") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestRunFullplateReadsKnowledgeRepositoryWithoutWoonRegistry(t *testing.T) {
	workspace := t.TempDir()
	configPath := filepath.Join(workspace, "config", "knowledge-workflow.yaml")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatal(err)
	}
	config := `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`
	if err := os.WriteFile(configPath, []byte(config), 0o644); err != nil {
		t.Fatal(err)
	}

	var output bytes.Buffer
	if err := RunFullplate([]string{"helm", "--workspace", workspace, "status"}, &output, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(output.String(), "status: ok") || !strings.Contains(output.String(), "active_sources: 0") {
		t.Fatalf("unexpected status output:\n%s", output.String())
	}
}
