package knowledge

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"math"
	"strconv"
	"sync"
	"testing"
)

type scriptedEmbeddingAdapter struct {
	spec       EmbeddingSpec
	embeddings []Embedding
	err        error
}

func (a scriptedEmbeddingAdapter) Spec() EmbeddingSpec { return a.spec }

func (a scriptedEmbeddingAdapter) Embed(context.Context, []Chunk) ([]Embedding, error) {
	return a.embeddings, a.err
}

type pointerEmbeddingAdapter struct{}

func (*pointerEmbeddingAdapter) Spec() EmbeddingSpec {
	return EmbeddingSpec{Adapter: "pointer", Model: "model", Revision: "v1", Dimensions: 1, Normalization: "none"}
}

func (*pointerEmbeddingAdapter) Embed(context.Context, []Chunk) ([]Embedding, error) { return nil, nil }

type driftingVectorStore struct {
	*memoryVectorStore
}

func (s *driftingVectorStore) CreateIndex(ctx context.Context, spec VectorIndexSpec) error {
	spec.Distance = DistanceDot
	return s.memoryVectorStore.CreateIndex(ctx, spec)
}

type emptyNameVectorStore struct {
	*memoryVectorStore
}

func (s *emptyNameVectorStore) Name() string { return "" }

type listOverrideStore struct {
	VectorStoreAdapter
	list func(context.Context, string, string, int) (VectorPage, error)
}

func (s listOverrideStore) List(ctx context.Context, index, after string, limit int) (VectorPage, error) {
	return s.list(ctx, index, after, limit)
}

type mutatingListStore struct {
	VectorStoreAdapter
	once   sync.Once
	mutate func()
}

func (s *mutatingListStore) List(ctx context.Context, index, after string, limit int) (VectorPage, error) {
	page, err := s.VectorStoreAdapter.List(ctx, index, after, limit)
	if err == nil {
		s.once.Do(s.mutate)
	}
	return page, err
}

func validEmbeddingSpec() EmbeddingSpec {
	return EmbeddingSpec{Adapter: "test", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}
}

func validIndexSpec(name string) VectorIndexSpec {
	return VectorIndexSpec{ContractVersion: VectorContractVersion, Name: name, Embedding: validEmbeddingSpec(), Distance: DistanceCosine}
}

func TestEmbeddingAndIndexSpecificationsRejectInvalidValues(t *testing.T) {
	embeddingTests := []struct {
		name string
		spec EmbeddingSpec
	}{
		{name: "missing adapter", spec: EmbeddingSpec{Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}},
		{name: "missing model", spec: EmbeddingSpec{Adapter: "test", Revision: "v1", Dimensions: 2, Normalization: "l2"}},
		{name: "missing revision", spec: EmbeddingSpec{Adapter: "test", Model: "model", Dimensions: 2, Normalization: "l2"}},
		{name: "invalid dimensions", spec: EmbeddingSpec{Adapter: "test", Model: "model", Revision: "v1", Normalization: "l2"}},
		{name: "invalid normalization", spec: EmbeddingSpec{Adapter: "test", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "unit"}},
	}
	for _, test := range embeddingTests {
		t.Run("embedding "+test.name, func(t *testing.T) {
			if err := test.spec.Validate(); err == nil {
				t.Fatal("invalid embedding specification was accepted")
			}
			if _, err := test.spec.Fingerprint(); err == nil {
				t.Fatal("invalid embedding specification was fingerprinted")
			}
		})
	}

	indexTests := []struct {
		name string
		spec VectorIndexSpec
	}{
		{name: "contract version", spec: VectorIndexSpec{Name: "index", ContractVersion: 99, Embedding: validEmbeddingSpec(), Distance: DistanceCosine}},
		{name: "missing name", spec: VectorIndexSpec{ContractVersion: VectorContractVersion, Embedding: validEmbeddingSpec(), Distance: DistanceCosine}},
		{name: "invalid embedding", spec: VectorIndexSpec{ContractVersion: VectorContractVersion, Name: "index", Distance: DistanceCosine}},
		{name: "distance", spec: VectorIndexSpec{ContractVersion: VectorContractVersion, Name: "index", Embedding: validEmbeddingSpec(), Distance: "manhattan"}},
	}
	for _, test := range indexTests {
		t.Run("index "+test.name, func(t *testing.T) {
			if err := test.spec.Validate(); err == nil {
				t.Fatal("invalid index specification was accepted")
			}
		})
	}
}

func TestBuildVectorRecordsRejectsInvalidBoundaries(t *testing.T) {
	validChunk := Chunk{ID: "chunk-a", SourceID: "source-a", ContentSHA256: "sha-a"}
	validAdapter := scriptedEmbeddingAdapter{spec: validEmbeddingSpec(), embeddings: []Embedding{{ChunkID: "chunk-a", Values: []float32{1, 0}}}}
	var typedNil *pointerEmbeddingAdapter

	tests := []struct {
		name    string
		ctx     context.Context
		adapter EmbeddingAdapter
		chunks  []Chunk
	}{
		{name: "nil context", adapter: validAdapter, chunks: []Chunk{validChunk}},
		{name: "nil adapter", ctx: context.Background(), chunks: []Chunk{validChunk}},
		{name: "typed nil adapter", ctx: context.Background(), adapter: typedNil, chunks: []Chunk{validChunk}},
		{name: "duplicate chunk", ctx: context.Background(), adapter: validAdapter, chunks: []Chunk{validChunk, validChunk}},
		{name: "negative ordinal", ctx: context.Background(), adapter: validAdapter, chunks: []Chunk{{ID: "chunk-a", SourceID: "source-a", ContentSHA256: "sha-a", Ordinal: -1}}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := BuildVectorRecords(test.ctx, test.adapter, test.chunks); err == nil {
				t.Fatal("invalid input was accepted")
			}
		})
	}
}

func TestBuildVectorRecordsRejectsBrokenAdapterResults(t *testing.T) {
	chunk := Chunk{ID: "chunk-a", SourceID: "source-a", ContentSHA256: "sha-a"}
	tests := []struct {
		name       string
		embeddings []Embedding
		err        error
	}{
		{name: "omitted", embeddings: nil},
		{name: "unknown", embeddings: []Embedding{{ChunkID: "other", Values: []float32{1, 0}}}},
		{name: "duplicate", embeddings: []Embedding{{ChunkID: "chunk-a", Values: []float32{1, 0}}, {ChunkID: "chunk-a", Values: []float32{1, 0}}}},
		{name: "nan", embeddings: []Embedding{{ChunkID: "chunk-a", Values: []float32{float32(math.NaN()), 0}}}},
		{name: "infinity", embeddings: []Embedding{{ChunkID: "chunk-a", Values: []float32{float32(math.Inf(1)), 0}}}},
		{name: "adapter error", err: errors.New("provider failed")},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			adapter := scriptedEmbeddingAdapter{spec: validEmbeddingSpec(), embeddings: test.embeddings, err: test.err}
			if _, err := BuildVectorRecords(context.Background(), adapter, []Chunk{chunk}); err == nil {
				t.Fatal("broken adapter result was accepted")
			}
		})
	}
}

func TestBuildVectorRecordsHonorsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	adapter := scriptedEmbeddingAdapter{spec: validEmbeddingSpec()}
	if _, err := BuildVectorRecords(ctx, adapter, nil); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
}

func TestAdapterRegistrySupportsZeroValueAndConcurrentRegistration(t *testing.T) {
	var registry AdapterRegistry
	const adapters = 64
	var wait sync.WaitGroup
	errorsFound := make(chan error, adapters*2)
	for i := 0; i < adapters; i++ {
		name := "embedding-" + strconv.Itoa(i)
		wait.Add(1)
		go func() {
			defer wait.Done()
			if err := registry.RegisterEmbedding(name, func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: validEmbeddingSpec()}, nil
			}); err != nil {
				errorsFound <- err
			}
		}()
		storeName := "store-" + strconv.Itoa(i)
		wait.Add(1)
		go func() {
			defer wait.Done()
			if err := registry.RegisterVectorStore(storeName, func(map[string]string) (VectorStoreAdapter, error) {
				return newMemoryVectorStore(storeName), nil
			}); err != nil {
				errorsFound <- err
			}
		}()
	}
	wait.Wait()
	close(errorsFound)
	for err := range errorsFound {
		t.Error(err)
	}
	if len(registry.embeddingFactories) != adapters || len(registry.vectorStoreFactories) != adapters {
		t.Fatalf("concurrent registrations were lost: embedding=%d store=%d", len(registry.embeddingFactories), len(registry.vectorStoreFactories))
	}
}

func TestAdapterRegistryRejectsInvalidRegistrationAndResolution(t *testing.T) {
	var nilRegistry *AdapterRegistry
	if err := nilRegistry.RegisterEmbedding("embed", func(map[string]string) (EmbeddingAdapter, error) { return nil, nil }); err == nil {
		t.Fatal("nil registry accepted an embedding registration")
	}
	if err := nilRegistry.RegisterVectorStore("store", func(map[string]string) (VectorStoreAdapter, error) { return nil, nil }); err == nil {
		t.Fatal("nil registry accepted a vector store registration")
	}
	if _, err := nilRegistry.Resolve(RetrievalConfig{}); err == nil {
		t.Fatal("nil registry resolved adapters")
	}

	registry := NewAdapterRegistry()
	validEmbeddingFactory := func(map[string]string) (EmbeddingAdapter, error) {
		return scriptedEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "embed", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}}, nil
	}
	validStoreFactory := func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil }
	if err := registry.RegisterEmbedding("", validEmbeddingFactory); err == nil {
		t.Fatal("blank embedding name was accepted")
	}
	if err := registry.RegisterEmbedding("embed", nil); err == nil {
		t.Fatal("nil embedding factory was accepted")
	}
	if err := registry.RegisterVectorStore("", validStoreFactory); err == nil {
		t.Fatal("blank vector store name was accepted")
	}
	if err := registry.RegisterVectorStore("store", nil); err == nil {
		t.Fatal("nil vector store factory was accepted")
	}
	if err := registry.RegisterEmbedding("embed", validEmbeddingFactory); err != nil {
		t.Fatal(err)
	}
	if err := registry.RegisterEmbedding("embed", validEmbeddingFactory); err == nil {
		t.Fatal("duplicate embedding registration was accepted")
	}
	if err := registry.RegisterVectorStore("store", validStoreFactory); err != nil {
		t.Fatal(err)
	}
	if err := registry.RegisterVectorStore("store", validStoreFactory); err == nil {
		t.Fatal("duplicate vector store registration was accepted")
	}

	invalidConfigs := []RetrievalConfig{
		{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "disabled"}, VectorStore: VectorStoreAdapterConfig{Adapter: "disabled"}},
		{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "disabled"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "missing"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "embed"}, VectorStore: VectorStoreAdapterConfig{Adapter: "missing"}},
	}
	for i, config := range invalidConfigs {
		if _, err := registry.Resolve(config); err == nil {
			t.Fatalf("invalid resolution config %d was accepted", i)
		}
	}
}

func TestAdapterRegistryRejectsFactoryErrorsAndInvalidStoreProducts(t *testing.T) {
	config := RetrievalConfig{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "embed"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}}
	tests := []struct {
		name      string
		embedding EmbeddingAdapterFactory
		store     VectorStoreAdapterFactory
	}{
		{
			name:      "embedding factory error",
			embedding: func(map[string]string) (EmbeddingAdapter, error) { return nil, errors.New("embedding failed") },
			store:     func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
		},
		{
			name: "invalid embedding specification",
			embedding: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "embed"}}, nil
			},
			store: func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
		},
		{
			name: "store factory error",
			embedding: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "embed", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}}, nil
			},
			store: func(map[string]string) (VectorStoreAdapter, error) { return nil, errors.New("store failed") },
		},
		{
			name: "nil store",
			embedding: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "embed", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}}, nil
			},
			store: func(map[string]string) (VectorStoreAdapter, error) { return nil, nil },
		},
		{
			name: "empty store name",
			embedding: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: EmbeddingSpec{Adapter: "embed", Model: "model", Revision: "v1", Dimensions: 2, Normalization: "l2"}}, nil
			},
			store: func(map[string]string) (VectorStoreAdapter, error) {
				return &emptyNameVectorStore{memoryVectorStore: newMemoryVectorStore("ignored")}, nil
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			registry := NewAdapterRegistry()
			if err := registry.RegisterEmbedding("embed", test.embedding); err != nil {
				t.Fatal(err)
			}
			if err := registry.RegisterVectorStore("store", test.store); err != nil {
				t.Fatal(err)
			}
			if _, err := registry.Resolve(config); err == nil {
				t.Fatal("invalid factory result was accepted")
			}
		})
	}
}

func TestAdapterRegistryRejectsInvalidFactoryProductsAndRuntimePolicy(t *testing.T) {
	var typedNil *pointerEmbeddingAdapter
	tests := []struct {
		name             string
		embeddingFactory EmbeddingAdapterFactory
		storeFactory     VectorStoreAdapterFactory
		config           RetrievalConfig
	}{
		{
			name:             "nil embedding",
			embeddingFactory: func(map[string]string) (EmbeddingAdapter, error) { return nil, nil },
			storeFactory:     func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
			config:           RetrievalConfig{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "embed"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		},
		{
			name:             "typed nil embedding",
			embeddingFactory: func(map[string]string) (EmbeddingAdapter, error) { return typedNil, nil },
			storeFactory:     func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
			config:           RetrievalConfig{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "embed"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		},
		{
			name: "persistent process",
			embeddingFactory: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: validEmbeddingSpec()}, nil
			},
			storeFactory: func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
			config:       RetrievalConfig{ExecutionMode: "on-demand", AllowPersistentProcess: true, Embedding: EmbeddingAdapterConfig{Adapter: "embed"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		},
		{
			name: "mismatched embedding identity",
			embeddingFactory: func(map[string]string) (EmbeddingAdapter, error) {
				return scriptedEmbeddingAdapter{spec: validEmbeddingSpec()}, nil
			},
			storeFactory: func(map[string]string) (VectorStoreAdapter, error) { return newMemoryVectorStore("store"), nil },
			config:       RetrievalConfig{ExecutionMode: "on-demand", Embedding: EmbeddingAdapterConfig{Adapter: "different"}, VectorStore: VectorStoreAdapterConfig{Adapter: "store"}},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			registry := NewAdapterRegistry()
			if err := registry.RegisterEmbedding(test.config.Embedding.Adapter, test.embeddingFactory); err != nil {
				t.Fatal(err)
			}
			if err := registry.RegisterVectorStore("store", test.storeFactory); err != nil {
				t.Fatal(err)
			}
			if _, err := registry.Resolve(test.config); err == nil {
				t.Fatal("invalid factory product or runtime policy was accepted")
			}
		})
	}
}

func TestCopyVectorIndexRejectsNilCanceledAndInvalidBatchInputs(t *testing.T) {
	store := newMemoryVectorStore("store")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	tests := []struct {
		name   string
		ctx    context.Context
		source VectorStoreAdapter
		target VectorStoreAdapter
		batch  int
	}{
		{name: "nil context", source: store, target: store, batch: 1},
		{name: "nil source", ctx: context.Background(), target: store, batch: 1},
		{name: "nil target", ctx: context.Background(), source: store, batch: 1},
		{name: "canceled", ctx: ctx, source: store, target: store, batch: 1},
		{name: "zero batch", ctx: context.Background(), source: store, target: store, batch: 0},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if _, err := CopyVectorIndex(test.ctx, test.source, test.target, "source", "target", test.batch); err == nil {
				t.Fatal("invalid migration input was accepted")
			}
		})
	}
}

func TestCopyVectorIndexRejectsTargetSpecificationDrift(t *testing.T) {
	ctx := context.Background()
	source := newMemoryVectorStore("source")
	if err := source.CreateIndex(ctx, validIndexSpec("source")); err != nil {
		t.Fatal(err)
	}
	target := &driftingVectorStore{memoryVectorStore: newMemoryVectorStore("target")}
	if report, err := CopyVectorIndex(ctx, source, target, "source", "target", 10); err == nil || report.Verified {
		t.Fatalf("target specification drift was accepted: report=%+v err=%v", report, err)
	}
}

func TestCopyVectorIndexRejectsSourceMutationDuringMigration(t *testing.T) {
	ctx := context.Background()
	base := newMemoryVectorStore("source")
	spec := validIndexSpec("source")
	if err := base.CreateIndex(ctx, spec); err != nil {
		t.Fatal(err)
	}
	if err := base.Upsert(ctx, spec.Name, []VectorRecord{
		{ID: "chunk-a", SourceID: "source", ContentSHA256: "old", Vector: []float32{1, 0}},
		{ID: "chunk-b", SourceID: "source", ContentSHA256: "sha-b", Vector: []float32{0, 1}},
	}); err != nil {
		t.Fatal(err)
	}
	source := &mutatingListStore{
		VectorStoreAdapter: base,
		mutate: func() {
			if err := base.Upsert(ctx, spec.Name, []VectorRecord{{ID: "chunk-a", SourceID: "source", ContentSHA256: "new", Vector: []float32{0.5, 0.5}}}); err != nil {
				t.Error(err)
			}
		},
	}
	target := newMemoryVectorStore("target")
	if report, err := CopyVectorIndex(ctx, source, target, "source", "target", 1); err == nil || report.Verified {
		t.Fatalf("source mutation was accepted: report=%+v err=%v", report, err)
	}
}

func TestCopyVectorIndexRejectsInvalidPaginationAndAllowsExplicitCleanup(t *testing.T) {
	ctx := context.Background()
	base := newMemoryVectorStore("source")
	if err := base.CreateIndex(ctx, validIndexSpec("source")); err != nil {
		t.Fatal(err)
	}
	source := listOverrideStore{
		VectorStoreAdapter: base,
		list: func(context.Context, string, string, int) (VectorPage, error) {
			return VectorPage{
				Records:    []VectorRecord{{ID: "chunk-b", SourceID: "source", ContentSHA256: "sha", Vector: []float32{1, 0}}},
				NextCursor: "wrong-cursor",
			}, nil
		},
	}
	target := newMemoryVectorStore("target")
	if report, err := CopyVectorIndex(ctx, source, target, "source", "target", 1); err == nil || report.Verified {
		t.Fatalf("invalid pagination was accepted: report=%+v err=%v", report, err)
	}
	if err := target.DeleteIndex(ctx, "target"); err != nil {
		t.Fatalf("partial target could not be explicitly cleaned up: %v", err)
	}
	if _, err := target.DescribeIndex(ctx, "target"); err == nil {
		t.Fatal("partial target remains after explicit cleanup")
	}
}

func TestCopyVectorIndexHandlesEmptyAndLargeIndexes(t *testing.T) {
	for _, size := range []int{0, 10_003} {
		t.Run(fmt.Sprintf("records-%d", size), func(t *testing.T) {
			ctx := context.Background()
			source := newMemoryVectorStore("source")
			target := newMemoryVectorStore("target")
			spec := validIndexSpec("source")
			if err := source.CreateIndex(ctx, spec); err != nil {
				t.Fatal(err)
			}
			records := make([]VectorRecord, 0, size)
			for i := 0; i < size; i++ {
				id := fmt.Sprintf("chunk-%06d", i)
				records = append(records, VectorRecord{ID: id, SourceID: "source", Ordinal: i, ContentSHA256: "sha-" + id, Metadata: map[string]string{"kind": "test"}, Vector: []float32{float32(i), float32(i + 1)}})
			}
			if err := source.Upsert(ctx, spec.Name, records); err != nil {
				t.Fatal(err)
			}
			report, err := CopyVectorIndex(ctx, source, target, "source", "target", 127)
			if err != nil {
				t.Fatal(err)
			}
			if !report.Verified || report.Records != int64(size) {
				t.Fatalf("unexpected report: %+v", report)
			}
		})
	}
}

func TestRecordDigestIsIndependentOfMetadataInsertionOrder(t *testing.T) {
	first := VectorRecord{ID: "chunk", SourceID: "source", ContentSHA256: "sha", Metadata: map[string]string{"a": "1", "b": "2"}, Vector: []float32{1, 2}}
	second := VectorRecord{ID: "chunk", SourceID: "source", ContentSHA256: "sha", Metadata: map[string]string{"b": "2", "a": "1"}, Vector: []float32{1, 2}}
	if digestRecord(first) != digestRecord(second) {
		t.Fatal("metadata insertion order changed the record digest")
	}
}

func FuzzRecordDigestMetadataOrder(f *testing.F) {
	f.Add("alpha", "one", "beta", "two", float32(1))
	f.Fuzz(func(t *testing.T, keyA, valueA, keyB, valueB string, vector float32) {
		if math.IsNaN(float64(vector)) || math.IsInf(float64(vector), 0) {
			t.Skip()
		}
		keyA = "a:" + keyA
		keyB = "b:" + keyB
		first := VectorRecord{ID: "chunk", SourceID: "source", ContentSHA256: "sha", Metadata: map[string]string{keyA: valueA, keyB: valueB}, Vector: []float32{vector}}
		second := VectorRecord{ID: "chunk", SourceID: "source", ContentSHA256: "sha", Metadata: map[string]string{keyB: valueB, keyA: valueA}, Vector: []float32{vector}}
		if digestRecord(first) != digestRecord(second) {
			t.Fatal("metadata insertion order changed the record digest")
		}
	})
}

func digestRecord(record VectorRecord) [sha256.Size]byte {
	digest := sha256.New()
	writeRecordDigest(digest, record)
	var result [sha256.Size]byte
	copy(result[:], digest.Sum(nil))
	return result
}
