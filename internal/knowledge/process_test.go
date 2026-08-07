package knowledge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeDocumentProcessor struct {
	response ProcessResponse
	calls    int
}

func (p *fakeDocumentProcessor) Name() string  { return "fake" }
func (p *fakeDocumentProcessor) Model() string { return "fake-v1" }
func (p *fakeDocumentProcessor) Process(_ context.Context, request ProcessRequest) (ProcessResponse, error) {
	p.calls++
	return p.response, nil
}

func TestProcessPendingCreatesCandidateClaimsAndLineageOnce(t *testing.T) {
	repo := t.TempDir()
	writeProcessingFixture(t, repo)
	writeFixture(t, filepath.Join(repo, "config", "voice.md"), "결론부터 쓰고 근거와 추론을 구분한다.\n")
	writeFixture(t, filepath.Join(repo, "config", "candidate.schema.json"), `{}`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "rough.md"), "배포 방식은 rolling이다. 근거는 운영 기록이다.\n")
	if _, err := Scan(repo); err != nil {
		t.Fatal(err)
	}
	catalog, err := loadCatalog(repo, mustConfig(t, repo))
	if err != nil {
		t.Fatal(err)
	}
	sourceID := catalog.Sources[0].ID
	processor := &fakeDocumentProcessor{response: ProcessResponse{Documents: []ProcessedDocument{{
		SourceID: sourceID, Title: "배포 방식", Type: "decision", Scope: "global",
		Summary: "운영 기록에 따른 배포 방식을 정리한다.", Body: "확인된 배포 방식은 rolling이다.",
		Claims: []CandidateClaim{{Subject: "deployment", Predicate: "strategy", Value: "rolling", Scope: "global"}},
	}}}}
	result, err := ProcessPending(context.Background(), repo, processor, 10)
	if err != nil {
		t.Fatal(err)
	}
	if result.Created != 1 || processor.calls != 1 {
		t.Fatalf("unexpected first process result: %+v calls=%d", result, processor.calls)
	}
	candidatePath := filepath.Join(repo, "knowledge-ops", "candidates", sourceID+".md")
	data, err := os.ReadFile(candidatePath)
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	if !strings.Contains(text, "status: Candidate") || !strings.Contains(text, sourceID) || !strings.Contains(text, "generated_by: fake") {
		t.Fatalf("candidate lacks provenance: %s", text)
	}
	claims, err := loadClaims(repo, mustConfig(t, repo))
	if err != nil {
		t.Fatal(err)
	}
	if len(claims.Claims) != 1 || claims.Claims[0].Status != "candidate" || claims.Claims[0].SourceIDs[0] != sourceID {
		t.Fatalf("candidate claim lacks lineage: %+v", claims.Claims)
	}
	_, artifacts, err := Trace(repo, sourceID[:16])
	if err != nil || len(artifacts) != 1 || artifacts[0].State != "candidate" {
		t.Fatalf("candidate artifact is not traceable: artifacts=%+v err=%v", artifacts, err)
	}
	second, err := ProcessPending(context.Background(), repo, processor, 10)
	if err != nil {
		t.Fatal(err)
	}
	if second.Created != 0 || processor.calls != 1 {
		t.Fatalf("idempotent process invoked processor again: %+v calls=%d", second, processor.calls)
	}
}

func TestValidateProcessedDocumentsRejectsMissingAndUnsupportedResults(t *testing.T) {
	cfg := Config{Classification: ClassificationPolicy{AllowedTypes: []string{"note"}}}
	request := ProcessRequest{Documents: []ProcessDocument{{SourceID: "src-a"}}}
	tests := []ProcessedDocument{
		{SourceID: "src-other", Title: "t", Type: "note", Scope: "global", Summary: "s", Body: "b"},
		{SourceID: "src-a", Title: "t", Type: "unknown", Scope: "global", Summary: "s", Body: "b"},
		{SourceID: "src-a", Title: "", Type: "note", Scope: "global", Summary: "s", Body: "b"},
	}
	for _, document := range tests {
		if _, err := validateProcessedDocuments([]ProcessedDocument{document}, request, cfg); err == nil {
			t.Fatalf("invalid processed document was accepted: %+v", document)
		}
	}
}

func TestCodexCLIProcessorEndToEnd(t *testing.T) {
	schemaSource := os.Getenv("WOON_KNOWLEDGE_SCHEMA")
	if schemaSource == "" {
		t.Skip("set WOON_KNOWLEDGE_SCHEMA to run the Codex CLI integration test")
	}
	schema, err := os.ReadFile(schemaSource)
	if err != nil {
		t.Fatal(err)
	}
	repo := t.TempDir()
	writeProcessingFixture(t, repo)
	writeFixture(t, filepath.Join(repo, "config", "voice.md"), "결론을 먼저 쓰고 원문에 없는 사실은 추가하지 않는다.\n")
	writeFixture(t, filepath.Join(repo, "config", "candidate.schema.json"), string(schema))
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "rough.md"), "메모: 배포는 rolling 방식. 이 내용은 테스트 환경 기준이고 운영 환경은 확인하지 못함.\n")
	cfg, err := LoadConfig(repo)
	if err != nil {
		t.Fatal(err)
	}
	processor, err := NewConfiguredProcessor(repo, cfg)
	if err != nil {
		t.Fatal(err)
	}
	result, err := ProcessPending(context.Background(), repo, processor, 1)
	if err != nil {
		t.Fatal(err)
	}
	if result.Created != 1 {
		t.Fatalf("Codex CLI did not create a candidate: %+v", result)
	}
	candidates, err := filepath.Glob(filepath.Join(repo, "knowledge-ops", "candidates", "*.md"))
	if err != nil || len(candidates) != 1 {
		t.Fatalf("unexpected candidate files: %v err=%v", candidates, err)
	}
}

func writeProcessingFixture(t *testing.T, repo string) {
	t.Helper()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
processing:
  execution_mode: on-demand
  allow_persistent_process: false
  adapter: codex-cli
  model: gpt-5.6-terra
  reasoning_effort: medium
  batch_size: 10
  output_root: knowledge-ops/candidates
  voice_profile_path: config/voice.md
  schema_path: config/candidate.schema.json
  command_path: codex
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note, decision]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
}
