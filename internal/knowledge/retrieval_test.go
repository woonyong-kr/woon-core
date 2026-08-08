package knowledge

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type deterministicEmbeddingAdapter struct{}

func (deterministicEmbeddingAdapter) Spec() EmbeddingSpec {
	return EmbeddingSpec{Adapter: "test-embedding", Model: "fixture", Revision: "v1", Dimensions: 2, Normalization: "l2"}
}

type batchingEmbeddingAdapter struct {
	calls int
	max   int
}

func (*batchingEmbeddingAdapter) Spec() EmbeddingSpec {
	return EmbeddingSpec{Adapter: "batch-embedding", Model: "fixture", Revision: "v1", Dimensions: 2, Normalization: "l2"}
}

func (a *batchingEmbeddingAdapter) Embed(_ context.Context, chunks []Chunk) ([]Embedding, error) {
	a.calls++
	if len(chunks) > a.max {
		a.max = len(chunks)
	}
	result := make([]Embedding, 0, len(chunks))
	for _, chunk := range chunks {
		result = append(result, Embedding{ChunkID: chunk.ID, Values: []float32{1, 0}})
	}
	return result, nil
}

func (deterministicEmbeddingAdapter) Embed(_ context.Context, chunks []Chunk) ([]Embedding, error) {
	result := make([]Embedding, 0, len(chunks))
	for _, chunk := range chunks {
		result = append(result, Embedding{ChunkID: chunk.ID, Values: []float32{1, 0}})
	}
	return result, nil
}

func TestIndexSourcesUpsertsAndDeletesBySourceLineage(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
max_file_bytes: 1048576
ignore_names: [.DS_Store]
poll_seconds: 5
retrieval:
  execution_mode: on-demand
  allow_persistent_process: false
  embedding: {adapter: test-embedding}
  vector_store: {adapter: test-store, options: {index: knowledge-v1}}
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	rawPath := filepath.Join(repo, "sources", "imports", "drop", "note.md")
	writeFixture(t, rawPath, "첫 문단이다.\n\n두 번째 문단이다.\n")
	store := newMemoryVectorStore("test-store")
	registry := NewAdapterRegistry()
	if err := registry.RegisterEmbedding("test-embedding", func(map[string]string) (EmbeddingAdapter, error) {
		return deterministicEmbeddingAdapter{}, nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := registry.RegisterVectorStore("test-store", func(map[string]string) (VectorStoreAdapter, error) {
		return store, nil
	}); err != nil {
		t.Fatal(err)
	}
	first, err := IndexSources(context.Background(), repo, registry)
	if err != nil {
		t.Fatal(err)
	}
	if first.Chunks != 1 || first.Upserted != 1 || len(store.records["knowledge-v1"]) != 1 {
		t.Fatalf("unexpected first index result: %+v records=%d", first, len(store.records["knowledge-v1"]))
	}
	unchanged, err := IndexSources(context.Background(), repo, registry)
	if err != nil {
		t.Fatal(err)
	}
	if unchanged.Chunks != 1 || unchanged.Upserted != 0 || unchanged.Deleted != 0 {
		t.Fatalf("unchanged source was indexed again: %+v", unchanged)
	}
	if err := os.Remove(rawPath); err != nil {
		t.Fatal(err)
	}
	second, err := IndexSources(context.Background(), repo, registry)
	if err != nil {
		t.Fatal(err)
	}
	if second.Chunks != 0 || second.Deleted != 1 || len(store.records["knowledge-v1"]) != 0 {
		t.Fatalf("stale vector was not deleted: %+v records=%d", second, len(store.records["knowledge-v1"]))
	}
}

func TestChunkDocumentIsDeterministicBoundedAndLinked(t *testing.T) {
	text := "# 제목\n\n" + strings.Repeat("가", 7) + "\n\n```go\nvalue := 1\n```\n\n" + strings.Repeat("나", 7)
	cfg := ChunkingConfig{Unit: "token", Tokenizer: "unicode-word-v1", TargetTokens: 8, MaxTokens: 10, OverlapTokens: 2, PreserveHeadings: true, PreserveCodeBlocks: true, PreserveTables: true}
	first := chunkDocument("src-a", "note.md", text, cfg)
	second := chunkDocument("src-a", "note.md", text, cfg)
	if len(first) < 2 || len(first) != len(second) {
		t.Fatalf("unexpected deterministic chunk count: %d vs %d", len(first), len(second))
	}
	for i, chunk := range first {
		if chunk.ID != second[i].ID || chunk.TokenCount > cfg.MaxTokens {
			t.Fatalf("chunk changed or exceeded limit: %+v", chunk)
		}
		if chunk.StartOffset < 0 || chunk.EndOffset > len(text) || chunk.StartOffset >= chunk.EndOffset {
			t.Fatalf("invalid offsets: %+v", chunk)
		}
		if i > 0 && chunk.PreviousChunkID != first[i-1].ID {
			t.Fatalf("missing previous link: %+v", chunk)
		}
		if i+1 < len(first) && chunk.NextChunkID != first[i+1].ID {
			t.Fatalf("missing next link: %+v", chunk)
		}
	}
}

func TestIndexSourcesEmbedsStreamingBatches(t *testing.T) {
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), `version: 1
inbox_roots: [sources/imports/drop]
catalog_path: knowledge-ops/catalog.json
review_path: knowledge-ops/review.json
claims_path: knowledge-ops/claims.yaml
poll_seconds: 5
chunking: {unit: token, tokenizer: unicode-word-v1, target_tokens: 10, max_tokens: 12, overlap_tokens: 1, preserve_headings: true, preserve_code_blocks: true, preserve_tables: true}
retrieval:
  execution_mode: on-demand
  allow_persistent_process: false
  vector_candidates: 10
  rerank_limit: 5
  neighbor_chunks: 1
  read_full_document_under_tokens: 50
  embedding: {adapter: batch-embedding}
  vector_store: {adapter: test-store, options: {index: knowledge-v1}}
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "large.md"), strings.Repeat("한 줄의 근거 문장이다.\n\n", 900))
	adapter := &batchingEmbeddingAdapter{}
	store := newMemoryVectorStore("test-store")
	registry := NewAdapterRegistry()
	if err := registry.RegisterEmbedding("batch-embedding", func(map[string]string) (EmbeddingAdapter, error) { return adapter, nil }); err != nil {
		t.Fatal(err)
	}
	if err := registry.RegisterVectorStore("test-store", func(map[string]string) (VectorStoreAdapter, error) { return store, nil }); err != nil {
		t.Fatal(err)
	}
	result, err := IndexSources(context.Background(), repo, registry)
	if err != nil {
		t.Fatal(err)
	}
	if result.Chunks <= 64 || adapter.calls < 2 || adapter.max > 64 || result.Upserted != result.Chunks {
		t.Fatalf("index was not streamed in bounded batches: result=%+v calls=%d max=%d", result, adapter.calls, adapter.max)
	}
}
