package knowledge

import (
	"context"
	"fmt"
	"sort"
	"testing"
)

type fakeEmbeddingAdapter struct {
	spec       EmbeddingSpec
	embeddings map[string][]float32
}

func (a fakeEmbeddingAdapter) Spec() EmbeddingSpec { return a.spec }

func (a fakeEmbeddingAdapter) Embed(_ context.Context, chunks []Chunk) ([]Embedding, error) {
	result := make([]Embedding, 0, len(chunks))
	for _, chunk := range chunks {
		values, ok := a.embeddings[chunk.ID]
		if !ok {
			continue
		}
		result = append(result, Embedding{ChunkID: chunk.ID, Values: values})
	}
	return result, nil
}

type memoryVectorStore struct {
	name    string
	specs   map[string]VectorIndexSpec
	records map[string]map[string]VectorRecord
	corrupt bool
}

func newMemoryVectorStore(name string) *memoryVectorStore {
	return &memoryVectorStore{name: name, specs: map[string]VectorIndexSpec{}, records: map[string]map[string]VectorRecord{}}
}

func (s *memoryVectorStore) Name() string { return s.name }

func (s *memoryVectorStore) CreateIndex(_ context.Context, spec VectorIndexSpec) error {
	if _, exists := s.specs[spec.Name]; exists {
		return fmt.Errorf("index exists")
	}
	s.specs[spec.Name] = spec
	s.records[spec.Name] = map[string]VectorRecord{}
	return nil
}

func (s *memoryVectorStore) DeleteIndex(_ context.Context, name string) error {
	if _, exists := s.specs[name]; !exists {
		return fmt.Errorf("unknown index")
	}
	delete(s.specs, name)
	delete(s.records, name)
	return nil
}

func (s *memoryVectorStore) DescribeIndex(_ context.Context, name string) (VectorIndexSpec, error) {
	spec, exists := s.specs[name]
	if !exists {
		return VectorIndexSpec{}, fmt.Errorf("unknown index")
	}
	return spec, nil
}

func (s *memoryVectorStore) Upsert(_ context.Context, name string, records []VectorRecord) error {
	for _, record := range records {
		if s.corrupt && len(record.Vector) > 0 {
			record.Vector[0]++
		}
		s.records[name][record.ID] = record
	}
	return nil
}

func (s *memoryVectorStore) Delete(_ context.Context, name string, ids []string) error {
	for _, id := range ids {
		delete(s.records[name], id)
	}
	return nil
}

func (s *memoryVectorStore) List(_ context.Context, name, after string, limit int) (VectorPage, error) {
	ids := make([]string, 0, len(s.records[name]))
	for id := range s.records[name] {
		if id > after {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	if len(ids) > limit {
		ids = ids[:limit]
	}
	page := VectorPage{Records: make([]VectorRecord, 0, len(ids))}
	for _, id := range ids {
		page.Records = append(page.Records, s.records[name][id])
	}
	if len(page.Records) > 0 && len(s.records[name]) > len(page.Records) && page.Records[len(page.Records)-1].ID != lastRecordID(s.records[name]) {
		page.NextCursor = page.Records[len(page.Records)-1].ID
	}
	return page, nil
}

func (s *memoryVectorStore) Search(context.Context, string, VectorQuery) ([]VectorMatch, error) {
	return nil, nil
}

func lastRecordID(records map[string]VectorRecord) string {
	last := ""
	for id := range records {
		if id > last {
			last = id
		}
	}
	return last
}

func TestBuildVectorRecordsKeepsStableLineageAcrossEmbeddingAdapters(t *testing.T) {
	chunks := []Chunk{
		{ID: "chunk-b", SourceID: "src-b", Path: "b.md", Ordinal: 1, ContentSHA256: "sha-b", Text: "beta"},
		{ID: "chunk-a", SourceID: "src-a", Path: "a.md", Ordinal: 0, ContentSHA256: "sha-a", Text: "alpha"},
	}
	adapter := fakeEmbeddingAdapter{
		spec:       EmbeddingSpec{Adapter: "custom", Model: "mine", Revision: "v1", Dimensions: 2, Normalization: "l2"},
		embeddings: map[string][]float32{"chunk-a": {1, 0}, "chunk-b": {0, 1}},
	}
	records, err := BuildVectorRecords(context.Background(), adapter, chunks)
	if err != nil {
		t.Fatal(err)
	}
	if len(records) != 2 || records[0].ID != "chunk-a" || records[0].SourceID != "src-a" || records[1].ID != "chunk-b" {
		t.Fatalf("records lost stable identity or order: %+v", records)
	}
}

func TestBuildVectorRecordsRejectsInvalidAdapterOutput(t *testing.T) {
	adapter := fakeEmbeddingAdapter{
		spec:       EmbeddingSpec{Adapter: "broken", Model: "mine", Revision: "v1", Dimensions: 2, Normalization: "none"},
		embeddings: map[string][]float32{"chunk-a": {1}},
	}
	_, err := BuildVectorRecords(context.Background(), adapter, []Chunk{{ID: "chunk-a", SourceID: "src-a", ContentSHA256: "sha", Text: "alpha"}})
	if err == nil {
		t.Fatal("expected dimension mismatch")
	}
}

func TestCopyVectorIndexStreamsIntoNewAdapter(t *testing.T) {
	ctx := context.Background()
	spec := VectorIndexSpec{
		ContractVersion: VectorContractVersion,
		Name:            "source-v1",
		Embedding:       EmbeddingSpec{Adapter: "custom", Model: "mine", Revision: "v1", Dimensions: 2, Normalization: "l2"},
		Distance:        DistanceCosine,
	}
	source := newMemoryVectorStore("source-store")
	target := newMemoryVectorStore("target-store")
	if err := source.CreateIndex(ctx, spec); err != nil {
		t.Fatal(err)
	}
	records := []VectorRecord{
		{ID: "chunk-a", SourceID: "src-a", ContentSHA256: "sha-a", Vector: []float32{1, 0}},
		{ID: "chunk-b", SourceID: "src-b", ContentSHA256: "sha-b", Vector: []float32{0, 1}},
		{ID: "chunk-c", SourceID: "src-c", ContentSHA256: "sha-c", Vector: []float32{0.5, 0.5}},
	}
	if err := source.Upsert(ctx, spec.Name, records); err != nil {
		t.Fatal(err)
	}
	report, err := CopyVectorIndex(ctx, source, target, spec.Name, "target-v1", 2)
	if err != nil {
		t.Fatal(err)
	}
	if report.Records != 3 || report.DigestSHA256 == "" || report.EmbeddingFingerprint == "" || !report.Verified {
		t.Fatalf("unexpected migration report: %+v", report)
	}
	targetSpec, err := target.DescribeIndex(ctx, "target-v1")
	if err != nil {
		t.Fatal(err)
	}
	if targetSpec.Embedding != spec.Embedding || len(target.records["target-v1"]) != 3 {
		t.Fatalf("target index differs: spec=%+v records=%+v", targetSpec, target.records["target-v1"])
	}
}

func TestAdapterRegistryResolvesConfiguredImplementations(t *testing.T) {
	registry := NewAdapterRegistry()
	if err := registry.RegisterEmbedding("mine", func(options map[string]string) (EmbeddingAdapter, error) {
		if options["model"] != "v1" {
			return nil, fmt.Errorf("missing model option")
		}
		return fakeEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "mine", Model: "v1", Revision: "1", Dimensions: 2, Normalization: "l2"}}, nil
	}); err != nil {
		t.Fatal(err)
	}
	store := newMemoryVectorStore("mine-store")
	if err := registry.RegisterVectorStore("mine-store", func(options map[string]string) (VectorStoreAdapter, error) {
		if options["path"] != "index" {
			return nil, fmt.Errorf("missing path option")
		}
		return store, nil
	}); err != nil {
		t.Fatal(err)
	}
	adapters, err := registry.Resolve(RetrievalConfig{
		ExecutionMode: "on-demand",
		Embedding:     EmbeddingAdapterConfig{Adapter: "mine", Options: map[string]string{"model": "v1"}},
		VectorStore:   VectorStoreAdapterConfig{Adapter: "mine-store", Options: map[string]string{"path": "index"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if adapters.Embedding.Spec().Adapter != "mine" || adapters.VectorStore.Name() != "mine-store" {
		t.Fatalf("registry resolved wrong adapters: %+v", adapters)
	}
}

func TestRetrievalConfigRejectsPersistentProcesses(t *testing.T) {
	cfg := Config{
		Version:      1,
		InboxRoots:   []string{"sources/imports/drop"},
		CatalogPath:  "knowledge-ops/catalog.json",
		ReviewPath:   "knowledge-ops/review.json",
		ClaimsPath:   "knowledge-ops/claims.yaml",
		MaxFileBytes: 1,
		PollSeconds:  1,
		Conflicts: ConflictPolicy{
			AutoMergeEquivalent: true,
			DifferentValue:      "review",
			Retrieval:           "block-conflicted",
		},
		Deletion: DeletionPolicy{
			MissingSource:     "quarantine-dependents",
			ExplicitRetire:    "cascade-review",
			HardDelete:        "explicit-only",
			DeleteDerivatives: "lineage-review",
		},
		Classification: ClassificationPolicy{
			PreserveRawPaths: true,
			FolderNames:      "hint-only",
			Uncertain:        "review",
			AllowedTypes:     []string{"note"},
		},
		Retrieval: RetrievalConfig{
			ExecutionMode:          "on-demand",
			AllowPersistentProcess: true,
			Embedding:              EmbeddingAdapterConfig{Adapter: "custom"},
			VectorStore:            VectorStoreAdapterConfig{Adapter: "custom"},
		},
	}
	if err := cfg.validate(t.TempDir()); err == nil {
		t.Fatal("persistent retrieval process was accepted")
	}
	cfg.Retrieval.AllowPersistentProcess = false
	if err := cfg.validate(t.TempDir()); err != nil {
		t.Fatalf("on-demand retrieval config was rejected: %v", err)
	}
}

func TestCopyVectorIndexRejectsTargetDigestMismatch(t *testing.T) {
	ctx := context.Background()
	spec := VectorIndexSpec{
		ContractVersion: VectorContractVersion,
		Name:            "source-v1",
		Embedding:       EmbeddingSpec{Adapter: "custom", Model: "mine", Revision: "v1", Dimensions: 2, Normalization: "l2"},
		Distance:        DistanceCosine,
	}
	source := newMemoryVectorStore("source-store")
	target := newMemoryVectorStore("corrupting-store")
	target.corrupt = true
	if err := source.CreateIndex(ctx, spec); err != nil {
		t.Fatal(err)
	}
	if err := source.Upsert(ctx, spec.Name, []VectorRecord{{ID: "chunk-a", SourceID: "src-a", ContentSHA256: "sha-a", Vector: []float32{1, 0}}}); err != nil {
		t.Fatal(err)
	}
	report, err := CopyVectorIndex(ctx, source, target, spec.Name, "target-v1", 10)
	if err == nil || report.Verified {
		t.Fatalf("corrupt migration was accepted: report=%+v err=%v", report, err)
	}
}
