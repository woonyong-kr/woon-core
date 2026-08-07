package knowledge

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestFastEmbedLanceDBAdaptersEndToEnd(t *testing.T) {
	cacheDir := os.Getenv("WOON_VECTOR_INTEGRATION_CACHE")
	if cacheDir == "" {
		t.Skip("set WOON_VECTOR_INTEGRATION_CACHE to run provider integration")
	}
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
  embedding:
    adapter: fastembed
    options:
      command: woon-knowledge-vector
      model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
      revision: fastembed-0.8.0-mean-pooling
      dimensions: "384"
      normalization: l2
      cache_dir: `+cacheDir+`
  vector_store:
    adapter: lancedb
    options:
      command: woon-knowledge-vector
      path: .runtime/lancedb
      index: knowledge-v1
classification: {preserve_raw_paths: true, folder_names: hint-only, auto_move_raw: false, uncertain: review, allowed_types: [note]}
conflicts: {auto_merge_equivalent: true, different_value: review, retrieval: block-conflicted}
deletion: {missing_source: quarantine-dependents, explicit_retire: cascade-review, hard_delete: explicit-only, delete_derivatives: lineage-review}
`)
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "deployment.md"), "배포 전략은 rolling 방식이다. 새 버전을 순차적으로 교체한다.\n")
	writeFixture(t, filepath.Join(repo, "sources", "imports", "drop", "memory.md"), "가상 메모리는 주소 변환과 page table을 사용한다.\n")
	registry, err := NewDefaultAdapterRegistry(repo)
	if err != nil {
		t.Fatal(err)
	}
	indexed, err := IndexSources(context.Background(), repo, registry)
	if err != nil {
		t.Fatal(err)
	}
	if indexed.Chunks != 2 || indexed.Upserted != 2 {
		t.Fatalf("unexpected provider index result: %+v", indexed)
	}
	results, err := SearchSources(context.Background(), repo, registry, "rolling 배포", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(results) == 0 || results[0].Path != "sources/imports/drop/deployment.md" {
		t.Fatalf("semantic search did not return deployment source first: %+v", results)
	}
}
