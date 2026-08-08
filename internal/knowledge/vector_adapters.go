package knowledge

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash"
	"math"
	"reflect"
	"sort"
	"strings"
	"sync"
)

const VectorContractVersion = 1

type DistanceMetric string

const (
	DistanceCosine DistanceMetric = "cosine"
	DistanceDot    DistanceMetric = "dot"
	DistanceL2     DistanceMetric = "l2"
)

// EmbeddingSpec identifies vector compatibility. Vectors may be copied between
// stores only when this specification is unchanged.
type EmbeddingSpec struct {
	Adapter       string `json:"adapter"`
	Model         string `json:"model"`
	Revision      string `json:"revision"`
	Dimensions    int    `json:"dimensions"`
	Normalization string `json:"normalization"`
}

func (s EmbeddingSpec) Validate() error {
	if strings.TrimSpace(s.Adapter) == "" {
		return fmt.Errorf("embedding adapter is required")
	}
	if strings.TrimSpace(s.Model) == "" {
		return fmt.Errorf("embedding model is required")
	}
	if strings.TrimSpace(s.Revision) == "" {
		return fmt.Errorf("embedding model revision is required")
	}
	if s.Dimensions <= 0 {
		return fmt.Errorf("embedding dimensions must be positive")
	}
	if s.Normalization != "none" && s.Normalization != "l2" {
		return fmt.Errorf("unsupported embedding normalization %q", s.Normalization)
	}
	return nil
}

func (s EmbeddingSpec) Fingerprint() (string, error) {
	if err := s.Validate(); err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(strings.Join([]string{s.Adapter, s.Model, s.Revision, fmt.Sprint(s.Dimensions), s.Normalization}, "\x00")))
	return hex.EncodeToString(sum[:]), nil
}

type Chunk struct {
	ID              string            `json:"id"`
	SourceID        string            `json:"source_id"`
	Path            string            `json:"path"`
	Ordinal         int               `json:"ordinal"`
	HeadingPath     []string          `json:"heading_path,omitempty"`
	PreviousChunkID string            `json:"previous_chunk_id,omitempty"`
	NextChunkID     string            `json:"next_chunk_id,omitempty"`
	StartOffset     int               `json:"start_offset"`
	EndOffset       int               `json:"end_offset"`
	TokenCount      int               `json:"token_count"`
	Text            string            `json:"text"`
	ContentSHA256   string            `json:"content_sha256"`
	Metadata        map[string]string `json:"metadata"`
}

type Embedding struct {
	ChunkID string    `json:"chunk_id"`
	Values  []float32 `json:"values"`
}

// EmbeddingAdapter converts canonical chunks into vectors. Implementations may
// call a local process or library, but must not change chunk identity.
type EmbeddingAdapter interface {
	Spec() EmbeddingSpec
	Embed(context.Context, []Chunk) ([]Embedding, error)
}

type EmbeddingAdapterFactory func(map[string]string) (EmbeddingAdapter, error)

type VectorRecord struct {
	ID            string            `json:"id"`
	SourceID      string            `json:"source_id"`
	Path          string            `json:"path"`
	Ordinal       int               `json:"ordinal"`
	ContentSHA256 string            `json:"content_sha256"`
	Metadata      map[string]string `json:"metadata"`
	Vector        []float32         `json:"vector"`
}

type VectorIndexSpec struct {
	ContractVersion int            `json:"contract_version"`
	Name            string         `json:"name"`
	Embedding       EmbeddingSpec  `json:"embedding"`
	Distance        DistanceMetric `json:"distance"`
}

func (s VectorIndexSpec) Validate() error {
	if s.ContractVersion != VectorContractVersion {
		return fmt.Errorf("unsupported vector contract version %d", s.ContractVersion)
	}
	if strings.TrimSpace(s.Name) == "" {
		return fmt.Errorf("vector index name is required")
	}
	if err := s.Embedding.Validate(); err != nil {
		return err
	}
	if s.Distance != DistanceCosine && s.Distance != DistanceDot && s.Distance != DistanceL2 {
		return fmt.Errorf("unsupported distance metric %q", s.Distance)
	}
	return nil
}

type VectorPage struct {
	Records    []VectorRecord `json:"records"`
	NextCursor string         `json:"next_cursor"`
}

type VectorQuery struct {
	Vector   []float32         `json:"vector"`
	Limit    int               `json:"limit"`
	Metadata map[string]string `json:"metadata"`
}

type VectorMatch struct {
	Record VectorRecord `json:"record"`
	Score  float32      `json:"score"`
}

// VectorStoreAdapter owns persistence and similarity search. CreateIndex must
// fail when the name exists, and List must return records in strictly
// increasing ID order so migrations are resumable.
type VectorStoreAdapter interface {
	Name() string
	CreateIndex(context.Context, VectorIndexSpec) error
	DeleteIndex(context.Context, string) error
	DescribeIndex(context.Context, string) (VectorIndexSpec, error)
	Upsert(context.Context, string, []VectorRecord) error
	Delete(context.Context, string, []string) error
	List(context.Context, string, string, int) (VectorPage, error)
	Search(context.Context, string, VectorQuery) ([]VectorMatch, error)
}

type VectorStoreAdapterFactory func(map[string]string) (VectorStoreAdapter, error)

type RetrievalAdapters struct {
	Embedding   EmbeddingAdapter
	VectorStore VectorStoreAdapter
}

// AdapterRegistry is the composition boundary. Provider packages register
// factories here; domain and migration code depend only on the two interfaces.
type AdapterRegistry struct {
	mu                   sync.RWMutex
	embeddingFactories   map[string]EmbeddingAdapterFactory
	vectorStoreFactories map[string]VectorStoreAdapterFactory
}

func NewAdapterRegistry() *AdapterRegistry {
	return &AdapterRegistry{
		embeddingFactories:   map[string]EmbeddingAdapterFactory{},
		vectorStoreFactories: map[string]VectorStoreAdapterFactory{},
	}
}

func (r *AdapterRegistry) RegisterEmbedding(name string, factory EmbeddingAdapterFactory) error {
	if r == nil {
		return fmt.Errorf("adapter registry is required")
	}
	name = strings.TrimSpace(name)
	if name == "" || factory == nil {
		return fmt.Errorf("embedding adapter name and factory are required")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.embeddingFactories == nil {
		r.embeddingFactories = map[string]EmbeddingAdapterFactory{}
	}
	if _, exists := r.embeddingFactories[name]; exists {
		return fmt.Errorf("embedding adapter %q is already registered", name)
	}
	r.embeddingFactories[name] = factory
	return nil
}

func (r *AdapterRegistry) RegisterVectorStore(name string, factory VectorStoreAdapterFactory) error {
	if r == nil {
		return fmt.Errorf("adapter registry is required")
	}
	name = strings.TrimSpace(name)
	if name == "" || factory == nil {
		return fmt.Errorf("vector store adapter name and factory are required")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.vectorStoreFactories == nil {
		r.vectorStoreFactories = map[string]VectorStoreAdapterFactory{}
	}
	if _, exists := r.vectorStoreFactories[name]; exists {
		return fmt.Errorf("vector store adapter %q is already registered", name)
	}
	r.vectorStoreFactories[name] = factory
	return nil
}

func (r *AdapterRegistry) Resolve(config RetrievalConfig) (RetrievalAdapters, error) {
	if r == nil {
		return RetrievalAdapters{}, fmt.Errorf("adapter registry is required")
	}
	if config.ExecutionMode != "on-demand" || config.AllowPersistentProcess {
		return RetrievalAdapters{}, fmt.Errorf("retrieval adapters require on-demand execution without persistent processes")
	}
	if config.Embedding.Adapter == "disabled" && config.VectorStore.Adapter == "disabled" {
		return RetrievalAdapters{}, fmt.Errorf("retrieval adapters are disabled")
	}
	if config.Embedding.Adapter == "disabled" || config.VectorStore.Adapter == "disabled" {
		return RetrievalAdapters{}, fmt.Errorf("embedding and vector store adapters must be enabled together")
	}
	r.mu.RLock()
	embeddingFactory, exists := r.embeddingFactories[config.Embedding.Adapter]
	if !exists {
		r.mu.RUnlock()
		return RetrievalAdapters{}, fmt.Errorf("embedding adapter %q is not registered", config.Embedding.Adapter)
	}
	vectorStoreFactory, exists := r.vectorStoreFactories[config.VectorStore.Adapter]
	r.mu.RUnlock()
	if !exists {
		return RetrievalAdapters{}, fmt.Errorf("vector store adapter %q is not registered", config.VectorStore.Adapter)
	}
	embedding, err := embeddingFactory(cloneMetadata(config.Embedding.Options))
	if err != nil {
		return RetrievalAdapters{}, fmt.Errorf("create embedding adapter %q: %w", config.Embedding.Adapter, err)
	}
	if isNilAdapter(embedding) {
		return RetrievalAdapters{}, fmt.Errorf("embedding adapter %q factory returned nil", config.Embedding.Adapter)
	}
	embeddingSpec := embedding.Spec()
	if err := embeddingSpec.Validate(); err != nil {
		return RetrievalAdapters{}, fmt.Errorf("embedding adapter %q has invalid specification: %w", config.Embedding.Adapter, err)
	}
	if embeddingSpec.Adapter != config.Embedding.Adapter {
		return RetrievalAdapters{}, fmt.Errorf("embedding adapter specification name %q does not match configured adapter %q", embeddingSpec.Adapter, config.Embedding.Adapter)
	}
	vectorStore, err := vectorStoreFactory(cloneMetadata(config.VectorStore.Options))
	if err != nil {
		return RetrievalAdapters{}, fmt.Errorf("create vector store adapter %q: %w", config.VectorStore.Adapter, err)
	}
	if isNilAdapter(vectorStore) {
		return RetrievalAdapters{}, fmt.Errorf("vector store adapter %q factory returned nil", config.VectorStore.Adapter)
	}
	if strings.TrimSpace(vectorStore.Name()) == "" {
		return RetrievalAdapters{}, fmt.Errorf("vector store adapter %q returned an empty name", config.VectorStore.Adapter)
	}
	return RetrievalAdapters{Embedding: embedding, VectorStore: vectorStore}, nil
}

func BuildVectorRecords(ctx context.Context, adapter EmbeddingAdapter, chunks []Chunk) ([]VectorRecord, error) {
	if ctx == nil {
		return nil, fmt.Errorf("context is required")
	}
	if err := ctx.Err(); err != nil {
		return nil, fmt.Errorf("embedding canceled: %w", err)
	}
	if isNilAdapter(adapter) {
		return nil, fmt.Errorf("embedding adapter is required")
	}
	spec := adapter.Spec()
	if err := spec.Validate(); err != nil {
		return nil, fmt.Errorf("validate embedding adapter: %w", err)
	}
	if err := validateChunks(chunks); err != nil {
		return nil, err
	}
	embeddings, err := adapter.Embed(ctx, cloneChunks(chunks))
	if err != nil {
		return nil, fmt.Errorf("embed chunks with %s: %w", spec.Adapter, err)
	}
	byChunk := make(map[string][]float32, len(embeddings))
	for _, embedding := range embeddings {
		if _, exists := byChunk[embedding.ChunkID]; exists {
			return nil, fmt.Errorf("embedding adapter returned duplicate chunk %q", embedding.ChunkID)
		}
		if err := validateVector(embedding.Values, spec.Dimensions); err != nil {
			return nil, fmt.Errorf("embedding for chunk %q: %w", embedding.ChunkID, err)
		}
		byChunk[embedding.ChunkID] = append([]float32(nil), embedding.Values...)
	}
	if len(byChunk) != len(chunks) {
		return nil, fmt.Errorf("embedding adapter returned %d vectors for %d chunks", len(byChunk), len(chunks))
	}
	records := make([]VectorRecord, 0, len(chunks))
	for _, chunk := range chunks {
		vector, exists := byChunk[chunk.ID]
		if !exists {
			return nil, fmt.Errorf("embedding adapter omitted chunk %q", chunk.ID)
		}
		records = append(records, VectorRecord{
			ID: chunk.ID, SourceID: chunk.SourceID, Path: chunk.Path, Ordinal: chunk.Ordinal,
			ContentSHA256: chunk.ContentSHA256, Metadata: cloneMetadata(chunk.Metadata), Vector: vector,
		})
	}
	sort.Slice(records, func(i, j int) bool { return records[i].ID < records[j].ID })
	return records, nil
}

type VectorMigrationReport struct {
	SourceAdapter        string `json:"source_adapter"`
	TargetAdapter        string `json:"target_adapter"`
	SourceIndex          string `json:"source_index"`
	TargetIndex          string `json:"target_index"`
	EmbeddingFingerprint string `json:"embedding_fingerprint"`
	Records              int64  `json:"records"`
	DigestSHA256         string `json:"digest_sha256"`
	Verified             bool   `json:"verified"`
}

// CopyVectorIndex copies compatible vectors into a newly created target index.
// It never deletes or activates either index; callers verify before switching.
func CopyVectorIndex(ctx context.Context, source, target VectorStoreAdapter, sourceIndex, targetIndex string, batchSize int) (VectorMigrationReport, error) {
	report := VectorMigrationReport{SourceIndex: sourceIndex, TargetIndex: targetIndex}
	if ctx == nil {
		return report, fmt.Errorf("context is required")
	}
	if err := ctx.Err(); err != nil {
		return report, fmt.Errorf("migration canceled: %w", err)
	}
	if isNilAdapter(source) || isNilAdapter(target) {
		return report, fmt.Errorf("source and target vector store adapters are required")
	}
	report.SourceAdapter = strings.TrimSpace(source.Name())
	report.TargetAdapter = strings.TrimSpace(target.Name())
	if report.SourceAdapter == "" || report.TargetAdapter == "" {
		return report, fmt.Errorf("source and target vector store adapter names are required")
	}
	if batchSize <= 0 {
		return report, fmt.Errorf("migration batch size must be positive")
	}
	spec, err := source.DescribeIndex(ctx, sourceIndex)
	if err != nil {
		return report, fmt.Errorf("describe source index: %w", err)
	}
	if err := spec.Validate(); err != nil {
		return report, fmt.Errorf("validate source index: %w", err)
	}
	if spec.Name != sourceIndex {
		return report, fmt.Errorf("source index description name %q does not match requested name %q", spec.Name, sourceIndex)
	}
	report.EmbeddingFingerprint, err = spec.Embedding.Fingerprint()
	if err != nil {
		return report, fmt.Errorf("fingerprint source embedding: %w", err)
	}
	spec.Name = targetIndex
	if err := target.CreateIndex(ctx, spec); err != nil {
		return report, fmt.Errorf("create target index: %w", err)
	}
	targetSpec, err := target.DescribeIndex(ctx, targetIndex)
	if err != nil {
		return report, fmt.Errorf("describe created target index: %w", err)
	}
	if targetSpec != spec {
		return report, fmt.Errorf("target index specification mismatch: got %+v want %+v", targetSpec, spec)
	}

	digest := sha256.New()
	cursor := ""
	for {
		if err := ctx.Err(); err != nil {
			return report, fmt.Errorf("migration canceled after %q: %w", cursor, err)
		}
		page, err := source.List(ctx, sourceIndex, cursor, batchSize)
		if err != nil {
			return report, fmt.Errorf("list source index after %q: %w", cursor, err)
		}
		if err := validateVectorPage(page, cursor, spec.Embedding.Dimensions); err != nil {
			return report, err
		}
		if len(page.Records) > 0 {
			if err := target.Upsert(ctx, targetIndex, cloneRecords(page.Records)); err != nil {
				return report, fmt.Errorf("upsert target index after %q: %w", cursor, err)
			}
			for _, record := range page.Records {
				writeRecordDigest(digest, record)
				report.Records++
			}
		}
		if page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
	}
	report.DigestSHA256 = hex.EncodeToString(digest.Sum(nil))
	targetRecords, targetDigest, err := digestVectorIndex(ctx, target, targetIndex, batchSize)
	if err != nil {
		return report, fmt.Errorf("verify target index: %w", err)
	}
	if targetRecords != report.Records || targetDigest != report.DigestSHA256 {
		return report, fmt.Errorf("target verification mismatch: records=%d digest=%s", targetRecords, targetDigest)
	}
	verifiedTargetSpec, err := target.DescribeIndex(ctx, targetIndex)
	if err != nil {
		return report, fmt.Errorf("describe verified target index: %w", err)
	}
	if verifiedTargetSpec != spec {
		return report, fmt.Errorf("target index specification changed during migration: got %+v want %+v", verifiedTargetSpec, spec)
	}
	sourceRecords, sourceDigest, err := digestVectorIndex(ctx, source, sourceIndex, batchSize)
	if err != nil {
		return report, fmt.Errorf("recheck source index: %w", err)
	}
	if sourceRecords != report.Records || sourceDigest != report.DigestSHA256 {
		return report, fmt.Errorf("source index changed during migration: records=%d digest=%s", sourceRecords, sourceDigest)
	}
	report.Verified = true
	return report, nil
}

func digestVectorIndex(ctx context.Context, store VectorStoreAdapter, index string, batchSize int) (int64, string, error) {
	spec, err := store.DescribeIndex(ctx, index)
	if err != nil {
		return 0, "", err
	}
	if err := spec.Validate(); err != nil {
		return 0, "", err
	}
	digest := sha256.New()
	var records int64
	cursor := ""
	for {
		if err := ctx.Err(); err != nil {
			return records, "", err
		}
		page, err := store.List(ctx, index, cursor, batchSize)
		if err != nil {
			return records, "", err
		}
		if err := validateVectorPage(page, cursor, spec.Embedding.Dimensions); err != nil {
			return records, "", err
		}
		for _, record := range page.Records {
			writeRecordDigest(digest, record)
			records++
		}
		if page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
	}
	return records, hex.EncodeToString(digest.Sum(nil)), nil
}

func validateChunks(chunks []Chunk) error {
	seen := make(map[string]bool, len(chunks))
	for _, chunk := range chunks {
		if strings.TrimSpace(chunk.ID) == "" || strings.TrimSpace(chunk.SourceID) == "" || strings.TrimSpace(chunk.ContentSHA256) == "" {
			return fmt.Errorf("chunk ID, source ID, and content SHA-256 are required")
		}
		if seen[chunk.ID] {
			return fmt.Errorf("duplicate chunk %q", chunk.ID)
		}
		if chunk.Ordinal < 0 {
			return fmt.Errorf("chunk %q has negative ordinal %d", chunk.ID, chunk.Ordinal)
		}
		seen[chunk.ID] = true
	}
	return nil
}

func validateVector(values []float32, dimensions int) error {
	if len(values) != dimensions {
		return fmt.Errorf("vector dimensions = %d, want %d", len(values), dimensions)
	}
	for _, value := range values {
		if math.IsNaN(float64(value)) || math.IsInf(float64(value), 0) {
			return fmt.Errorf("vector contains a non-finite value")
		}
	}
	return nil
}

func validateVectorPage(page VectorPage, after string, dimensions int) error {
	previous := after
	for _, record := range page.Records {
		if record.ID <= previous {
			return fmt.Errorf("vector adapter returned non-increasing record ID %q after %q", record.ID, previous)
		}
		if strings.TrimSpace(record.SourceID) == "" || strings.TrimSpace(record.ContentSHA256) == "" {
			return fmt.Errorf("vector record %q lacks lineage", record.ID)
		}
		if err := validateVector(record.Vector, dimensions); err != nil {
			return fmt.Errorf("vector record %q: %w", record.ID, err)
		}
		previous = record.ID
	}
	if page.NextCursor != "" {
		if len(page.Records) == 0 || page.NextCursor != page.Records[len(page.Records)-1].ID {
			return fmt.Errorf("vector adapter returned invalid next cursor %q", page.NextCursor)
		}
	}
	return nil
}

func cloneRecords(records []VectorRecord) []VectorRecord {
	result := make([]VectorRecord, len(records))
	for i, record := range records {
		result[i] = record
		result[i].Metadata = cloneMetadata(record.Metadata)
		result[i].Vector = append([]float32(nil), record.Vector...)
	}
	return result
}

func cloneChunks(chunks []Chunk) []Chunk {
	result := make([]Chunk, len(chunks))
	for i, chunk := range chunks {
		result[i] = chunk
		result[i].Metadata = cloneMetadata(chunk.Metadata)
	}
	return result
}

func cloneMetadata(metadata map[string]string) map[string]string {
	if metadata == nil {
		return nil
	}
	result := make(map[string]string, len(metadata))
	for key, value := range metadata {
		result[key] = value
	}
	return result
}

func writeRecordDigest(digest hash.Hash, record VectorRecord) {
	writeDigestString(digest, record.ID)
	writeDigestString(digest, record.SourceID)
	writeDigestString(digest, record.Path)
	_ = binary.Write(digest, binary.BigEndian, int64(record.Ordinal))
	writeDigestString(digest, record.ContentSHA256)
	keys := make([]string, 0, len(record.Metadata))
	for key := range record.Metadata {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	_ = binary.Write(digest, binary.BigEndian, uint64(len(keys)))
	for _, key := range keys {
		writeDigestString(digest, key)
		writeDigestString(digest, record.Metadata[key])
	}
	_ = binary.Write(digest, binary.BigEndian, uint64(len(record.Vector)))
	for _, value := range record.Vector {
		_ = binary.Write(digest, binary.BigEndian, math.Float32bits(value))
	}
}

func isNilAdapter(adapter any) bool {
	if adapter == nil {
		return true
	}
	value := reflect.ValueOf(adapter)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

func writeDigestString(digest hash.Hash, value string) {
	_ = binary.Write(digest, binary.BigEndian, uint64(len(value)))
	_, _ = digest.Write([]byte(value))
}
