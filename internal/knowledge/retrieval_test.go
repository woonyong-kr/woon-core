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

func TestSplitTextIsDeterministicAndBounded(t *testing.T) {
	text := strings.Repeat("가", 7) + "\n\n" + strings.Repeat("나", 7)
	first := splitText(text, 10)
	second := splitText(text, 10)
	if strings.Join(first, "|") != strings.Join(second, "|") {
		t.Fatalf("split changed between runs: %v vs %v", first, second)
	}
	for _, chunk := range first {
		if len([]rune(chunk)) > 10 {
			t.Fatalf("chunk exceeds bound: %q", chunk)
		}
	}
}
