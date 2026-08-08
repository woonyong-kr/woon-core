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

type echoDocumentProcessor struct {
	calls   int
	maxDocs int
}

func (p *echoDocumentProcessor) Name() string  { return "echo" }
func (p *echoDocumentProcessor) Model() string { return "echo-v1" }
func (p *echoDocumentProcessor) Process(_ context.Context, request ProcessRequest) (ProcessResponse, error) {
	p.calls++
	if len(request.Documents) > p.maxDocs {
		p.maxDocs = len(request.Documents)
	}
	response := ProcessResponse{Documents: make([]ProcessedDocument, 0, len(request.Documents))}
	for _, document := range request.Documents {
		response.Documents = append(response.Documents, ProcessedDocument{
			SourceID: document.SourceID, Title: "계층 정리", Type: "note", Scope: "global",
			Summary: "부분 내용을 근거로 정리했다.", Body: "부분 내용이 계층적으로 통합되었다.",
		})
	}
	return response, nil
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

func TestProcessPendingUsesHierarchicalMapReduceForLargeText(t *testing.T) {
	repo := t.TempDir()
	writeProcessingFixture(t, repo)
	configPath := filepath.Join(repo, "config", "knowledge-workflow.yaml")
	config, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	config = append(config, []byte(`
chunking: {unit: token, tokenizer: unicode-word-v1, target_tokens: 20, max_tokens: 30, overlap_tokens: 3, preserve_headings: true, preserve_code_blocks: true, preserve_tables: true}
retrieval: {vector_candidates: 10, rerank_limit: 5, neighbor_chunks: 1, read_full_document_under_tokens: 50}
`)...)
	writeFixture(t, configPath, string(config))
	writeFixture(t, filepath.Join(repo, "config", "voice.md"), "원문 근거만 사용한다.\n")
	writeFixture(t, filepath.Join(repo, "config", "candidate.schema.json"), `{}`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "large.md"), "# 큰 문서\n\n"+strings.Repeat("장애 원인과 해결 근거를 기록한다.\n", 80))
	processor := &echoDocumentProcessor{}
	result, err := ProcessPending(context.Background(), repo, processor, 1)
	if err != nil {
		t.Fatal(err)
	}
	if result.Created != 1 || processor.calls < 3 || processor.maxDocs > 10 {
		t.Fatalf("hierarchical processing was not bounded: result=%+v calls=%d max_docs=%d", result, processor.calls, processor.maxDocs)
	}
	candidates, err := filepath.Glob(filepath.Join(repo, "knowledge-ops", "candidates", "src-*.md"))
	if err != nil || len(candidates) != 1 {
		t.Fatalf("final source candidate was not created: %v err=%v", candidates, err)
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

func TestCodexCLIHierarchicalEndToEnd(t *testing.T) {
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
	configPath := filepath.Join(repo, "config", "knowledge-workflow.yaml")
	config, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	config = append(config, []byte(`
chunking: {unit: token, tokenizer: unicode-word-v1, target_tokens: 1000, max_tokens: 1400, overlap_tokens: 150, preserve_headings: true, preserve_code_blocks: true, preserve_tables: true}
retrieval: {vector_candidates: 10, rerank_limit: 5, neighbor_chunks: 1, read_full_document_under_tokens: 50, read_full_document_max_mib: 64, embedding_batch_size: 64}
`)...)
	writeFixture(t, configPath, string(config))
	writeFixture(t, filepath.Join(repo, "config", "voice.md"), "결론을 먼저 쓰고 원문에 없는 사실은 추가하지 않는다.\n")
	writeFixture(t, filepath.Join(repo, "config", "candidate.schema.json"), string(schema))
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "large.md"), "# 장애 기록\n\n"+strings.Repeat("테스트 환경에서 연결 실패를 확인했고 재시도로 복구했다.\n", 20))
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
		t.Fatalf("Codex CLI hierarchical processing failed: %+v", result)
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
