package knowledge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestGetStatusCountsCatalogAndReviewStates(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	cfg := mustConfig(t, repo)
	catalogPath, err := safePath(repo, cfg.CatalogPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := writeJSON(catalogPath, Catalog{
		Version: catalogVersion,
		Sources: []Source{
			{ID: "active", State: "active"},
			{ID: "missing", State: "missing"},
			{ID: "quarantined", State: "quarantined"},
			{ID: "retracted", State: "retracted"},
		},
		Artifacts: []Artifact{{ID: "artifact"}},
	}); err != nil {
		t.Fatal(err)
	}
	reviewPath, err := safePath(repo, cfg.ReviewPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := writeJSON(reviewPath, Review{Version: 1, Items: []ReviewItem{{ID: "one"}, {ID: "two"}}}); err != nil {
		t.Fatal(err)
	}
	status, err := GetStatus(repo)
	if err != nil {
		t.Fatal(err)
	}
	if status.ActiveSources != 1 || status.MissingSources != 1 || status.QuarantinedSources != 1 || status.RetractedSources != 1 || status.Artifacts != 1 || status.ReviewItems != 2 {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestEncodeContextProducesVersionedJSON(t *testing.T) {
	data, err := EncodeContext([]Claim{{ID: "claim", Subject: "subject", Predicate: "is", Value: "value", Scope: "global"}})
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Version int     `json:"version"`
		Claims  []Claim `json:"claims"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Version != 1 || len(envelope.Claims) != 1 || envelope.Claims[0].ID != "claim" {
		t.Fatalf("unexpected context envelope: %+v", envelope)
	}
}

func TestScanDetectsDuplicatesConflictsAndMissingLineage(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts:
  auto_merge_equivalent: true
  different_value: review
  retrieval: block-conflicted
deletion:
  missing_source: quarantine-dependents
  explicit_retire: cascade-review
  hard_delete: explicit-only
  delete_derivatives: lineage-review
`)
	firstPath := filepath.Join(repo, "sources", "imports", "drop", "first.md")
	secondPath := filepath.Join(repo, "sources", "imports", "drop", "rough", "code", "copy.md")
	writeFixture(t, firstPath, "alpha\n")
	writeFixture(t, secondPath, "alpha\n")

	result, err := Scan(repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.Files != 2 || result.Sources != 1 || result.ReviewItems != 1 {
		t.Fatalf("unexpected first scan: %+v", result)
	}
	catalog, err := loadCatalog(repo, mustConfig(t, repo))
	if err != nil {
		t.Fatal(err)
	}
	sourceID := catalog.Sources[0].ID
	if len(catalog.Sources[0].InputHints) != 1 || catalog.Sources[0].InputHints[0] != "rough/code" {
		t.Fatalf("nested folder was not preserved as a hint: %+v", catalog.Sources[0].InputHints)
	}
	artifactPath := filepath.Join(repo, "wiki", "alpha.md")
	writeFixture(t, artifactPath, "derived")
	if _, err := Link(repo, "wiki/alpha.md", "wiki", []string{sourceID}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(firstPath); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(secondPath); err != nil {
		t.Fatal(err)
	}
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	source, artifacts, err := Trace(repo, sourceID)
	if err != nil {
		t.Fatal(err)
	}
	if source.State != "missing" || len(artifacts) != 1 || artifacts[0].State != "review-required" {
		t.Fatalf("missing source did not quarantine lineage: source=%+v artifacts=%+v", source, artifacts)
	}
}

func TestClaimConflictRequiresReviewUnlessSuperseded(t *testing.T) {
	sources := []Source{{ID: "src-a", State: "active"}, {ID: "src-b", State: "active"}}
	claims := []Claim{
		{ID: "old", Subject: "deploy", Predicate: "strategy", Value: "blue-green", Scope: "repo:x", ValidFrom: "2026-01-01", SourceIDs: []string{"src-a"}, Status: "active"},
		{ID: "new", Subject: "deploy", Predicate: "strategy", Value: "rolling", Scope: "repo:x", ValidFrom: "2026-01-01", SourceIDs: []string{"src-b"}, Status: "candidate"},
	}
	items := evaluateClaims(claims, sources)
	if len(items) != 1 || items[0].Kind != "claim-conflict" {
		t.Fatalf("expected conflict review, got %+v", items)
	}
	claims[1].Supersedes = []string{"old"}
	if items := evaluateClaims(claims, sources); len(items) != 0 {
		t.Fatalf("explicit supersedes should resolve conflict, got %+v", items)
	}
}

func TestClaimWithoutSourceIsInvalid(t *testing.T) {
	items := evaluateClaims([]Claim{{ID: "unsupported", Subject: "x", Predicate: "is", Value: "y", Scope: "global", Status: "active"}}, nil)
	if len(items) != 1 || items[0].Kind != "invalid-claim" {
		t.Fatalf("unsupported claim should be invalid: %+v", items)
	}
}

func TestContextMergesEquivalentClaimsAndEvidence(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "a.md"), "a")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "b.md"), "b")
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	catalog, _ := loadCatalog(repo, mustConfig(t, repo))
	writeFixture(t, filepath.Join(repo, "knowledge-ops", "claims.yaml"), `version: 1
claims:
  - {id: a-claim, subject: build, predicate: command, value: make, scope: global, source_ids: [`+catalog.Sources[0].ID+`], status: active}
  - {id: b-claim, subject: Build, predicate: command, value: " make ", scope: global, source_ids: [`+catalog.Sources[1].ID+`], status: active}
`)
	claims, err := Context(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(claims) != 1 || len(claims[0].SourceIDs) != 2 || claims[0].ID != "a-claim" {
		t.Fatalf("equivalent claims were not merged deterministically: %+v", claims)
	}
}

func TestContextExcludesConflictedAndUnavailableClaims(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "a.md"), "a")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "b.md"), "b")
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	catalog, _ := loadCatalog(repo, mustConfig(t, repo))
	writeFixture(t, filepath.Join(repo, "knowledge-ops", "claims.yaml"), `version: 1
claims:
  - {id: stable, subject: build, predicate: command, value: make, scope: global, source_ids: [`+catalog.Sources[0].ID+`], status: active}
  - {id: conflict-a, subject: deploy, predicate: strategy, value: rolling, scope: repo:x, source_ids: [`+catalog.Sources[0].ID+`], status: active}
  - {id: conflict-b, subject: deploy, predicate: strategy, value: blue-green, scope: repo:x, source_ids: [`+catalog.Sources[1].ID+`], status: active}
`)
	claims, err := Context(repo, "repo:x")
	if err != nil {
		t.Fatal(err)
	}
	if len(claims) != 1 || claims[0].ID != "stable" {
		t.Fatalf("context leaked conflicted claims: %+v", claims)
	}
}

func TestRetireDoesNotHardDelete(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	rawPath := filepath.Join(repo, "sources", "imports", "drop", "raw.md")
	writeFixture(t, rawPath, "raw")
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	catalog, _ := loadCatalog(repo, mustConfig(t, repo))
	artifactPath := filepath.Join(repo, "wiki", "derived.md")
	writeFixture(t, artifactPath, "derived")
	if _, err := Link(repo, "wiki/derived.md", "wiki", []string{catalog.Sources[0].ID}); err != nil {
		t.Fatal(err)
	}
	_, affected, err := Retire(repo, catalog.Sources[0].ID, "no longer needed")
	if err != nil {
		t.Fatal(err)
	}
	if len(affected) != 1 {
		t.Fatalf("affected artifacts = %d", len(affected))
	}
	if _, err := os.Stat(rawPath); err != nil {
		t.Fatalf("raw source was deleted: %v", err)
	}
	if _, err := os.Stat(artifactPath); err != nil {
		t.Fatalf("derived artifact was deleted: %v", err)
	}
}

func mustConfig(t *testing.T, repo string) Config {
	t.Helper()
	cfg, err := LoadConfig(repo)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
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
