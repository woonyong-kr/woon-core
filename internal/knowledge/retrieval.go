package knowledge

import (
	"context"
	"fmt"
	"io"
	"os"
	"sort"
	"strconv"
	"strings"
)

type IndexResult struct {
	Index    string
	Chunks   int
	Upserted int
	Deleted  int
}

type SearchResult struct {
	SourceID    string
	Path        string
	Ordinal     int
	Score       float32
	Text        string
	HeadingPath []string
	ContextKind string
}

func IndexSources(ctx context.Context, repo string, registry *AdapterRegistry) (IndexResult, error) {
	var result IndexResult
	if _, err := Scan(repo); err != nil {
		return result, err
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return result, err
	}
	adapters, err := registry.Resolve(cfg.Retrieval)
	if err != nil {
		return result, err
	}
	indexName := strings.TrimSpace(cfg.Retrieval.VectorStore.Options["index"])
	if indexName == "" {
		return result, fmt.Errorf("vector_store index option is required")
	}
	result.Index = indexName
	spec := VectorIndexSpec{
		ContractVersion: VectorContractVersion, Name: indexName,
		Embedding: adapters.Embedding.Spec(), Distance: DistanceCosine,
	}
	currentSpec, describeErr := adapters.VectorStore.DescribeIndex(ctx, indexName)
	if describeErr != nil {
		if err := adapters.VectorStore.CreateIndex(ctx, spec); err != nil {
			return result, fmt.Errorf("create vector index after describe failed (%v): %w", describeErr, err)
		}
	} else if currentSpec != spec {
		return result, fmt.Errorf("vector index specification changed; create a new index and migrate instead: got %+v want %+v", currentSpec, spec)
	}
	existing, err := listVectorRecords(ctx, adapters.VectorStore, indexName)
	if err != nil {
		return result, err
	}
	existingByID := make(map[string]VectorRecord, len(existing))
	for _, record := range existing {
		existingByID[record.ID] = record
	}
	expected := make(map[string]bool)
	pending := make([]Chunk, 0, 64)
	flushPending := func() error {
		if len(pending) == 0 {
			return nil
		}
		records, buildErr := BuildVectorRecords(ctx, adapters.Embedding, pending)
		if buildErr != nil {
			return buildErr
		}
		if err := adapters.VectorStore.Upsert(ctx, indexName, records); err != nil {
			return fmt.Errorf("upsert vector records: %w", err)
		}
		result.Upserted += len(records)
		pending = pending[:0]
		return nil
	}
	result.Chunks = 0
	if err := walkSourceChunks(ctx, repo, cfg, func(chunk Chunk) error {
		result.Chunks++
		expected[chunk.ID] = true
		record, exists := existingByID[chunk.ID]
		if exists && record.ContentSHA256 == chunk.ContentSHA256 {
			return nil
		}
		pending = append(pending, chunk)
		if len(pending) == cap(pending) {
			return flushPending()
		}
		return nil
	}); err != nil {
		return result, err
	}
	if err := flushPending(); err != nil {
		return result, err
	}
	stale := make([]string, 0)
	for _, record := range existing {
		if !expected[record.ID] {
			stale = append(stale, record.ID)
		}
	}
	if len(stale) > 0 {
		if err := adapters.VectorStore.Delete(ctx, indexName, stale); err != nil {
			return result, fmt.Errorf("delete stale vector records: %w", err)
		}
	}
	result.Deleted = len(stale)
	return result, nil
}

func SearchSources(ctx context.Context, repo string, registry *AdapterRegistry, query string, limit int) ([]SearchResult, error) {
	query = strings.TrimSpace(query)
	if query == "" || limit <= 0 {
		return nil, fmt.Errorf("search query and positive limit are required")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return nil, err
	}
	adapters, err := registry.Resolve(cfg.Retrieval)
	if err != nil {
		return nil, err
	}
	indexName := strings.TrimSpace(cfg.Retrieval.VectorStore.Options["index"])
	queryChunk := Chunk{ID: "query-" + digest([]byte(query)), SourceID: "query", ContentSHA256: digest([]byte(query)), Text: query}
	embeddings, err := adapters.Embedding.Embed(ctx, []Chunk{queryChunk})
	if err != nil {
		return nil, fmt.Errorf("embed search query: %w", err)
	}
	if len(embeddings) != 1 || embeddings[0].ChunkID != queryChunk.ID {
		return nil, fmt.Errorf("embedding adapter returned an invalid query vector")
	}
	if err := validateVector(embeddings[0].Values, adapters.Embedding.Spec().Dimensions); err != nil {
		return nil, err
	}
	candidateLimit := cfg.Retrieval.VectorCandidates
	if candidateLimit < limit {
		candidateLimit = limit
	}
	matches, err := adapters.VectorStore.Search(ctx, indexName, VectorQuery{Vector: embeddings[0].Values, Limit: candidateLimit, Metadata: map[string]string{"state": "active"}})
	if err != nil {
		return nil, fmt.Errorf("search vector index: %w", err)
	}
	type rankedMatch struct {
		chunk Chunk
		score float32
	}
	queryTerms := tokenTerms(query)
	ranked := make([]rankedMatch, 0, len(matches))
	seenContent := map[string]bool{}
	for _, match := range matches {
		chunk, readErr := chunkFromVectorRecord(repo, match.Record)
		if readErr != nil || seenContent[chunk.ContentSHA256] {
			continue
		}
		seenContent[chunk.ContentSHA256] = true
		ranked = append(ranked, rankedMatch{chunk: chunk, score: match.Score + lexicalBoost(queryTerms, chunk.Text)})
	}
	sort.SliceStable(ranked, func(i, j int) bool { return ranked[i].score > ranked[j].score })
	resultLimit := limit
	if resultLimit > cfg.Retrieval.RerankLimit {
		resultLimit = cfg.Retrieval.RerankLimit
	}
	results := make([]SearchResult, 0, resultLimit)
	seenContext := map[string]bool{}
	for _, match := range ranked {
		if len(results) >= resultLimit {
			break
		}
		text, kind, expandErr := expandChunkContextStreaming(ctx, repo, match.chunk, cfg)
		if expandErr != nil {
			continue
		}
		key := match.chunk.SourceID + "\x00" + kind + "\x00" + strings.Join(match.chunk.HeadingPath, "\x00")
		if seenContext[key] {
			continue
		}
		seenContext[key] = true
		results = append(results, SearchResult{
			SourceID: match.chunk.SourceID, Path: match.chunk.Path, Ordinal: match.chunk.Ordinal,
			Score: match.score, Text: text, HeadingPath: append([]string(nil), match.chunk.HeadingPath...), ContextKind: kind,
		})
	}
	return results, nil
}

func chunkFromVectorRecord(repo string, record VectorRecord) (Chunk, error) {
	start, err := strconv.Atoi(record.Metadata["start_offset"])
	if err != nil || start < 0 {
		return Chunk{}, fmt.Errorf("invalid start offset for %s", record.ID)
	}
	end, err := strconv.Atoi(record.Metadata["end_offset"])
	if err != nil || end <= start {
		return Chunk{}, fmt.Errorf("invalid end offset for %s", record.ID)
	}
	readPath := record.Metadata["read_path"]
	if readPath == "" {
		readPath = record.Path
	}
	absolute, err := safePath(repo, readPath)
	if err != nil {
		return Chunk{}, err
	}
	file, err := os.Open(absolute)
	if err != nil {
		return Chunk{}, err
	}
	defer file.Close()
	data := make([]byte, end-start)
	count, err := file.ReadAt(data, int64(start))
	if err != nil && err != io.EOF {
		return Chunk{}, err
	}
	data = data[:count]
	tokenCount, _ := strconv.Atoi(record.Metadata["token_count"])
	return Chunk{
		ID: record.ID, SourceID: record.SourceID, Path: record.Path, Ordinal: record.Ordinal,
		HeadingPath: splitHeadingPath(record.Metadata["heading_path"]), PreviousChunkID: record.Metadata["previous_chunk_id"],
		NextChunkID: record.Metadata["next_chunk_id"], StartOffset: start, EndOffset: end, TokenCount: tokenCount,
		Text: strings.TrimSpace(string(data)), ContentSHA256: record.ContentSHA256, Metadata: cloneMetadata(record.Metadata),
	}, nil
}

func splitHeadingPath(value string) []string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return strings.Split(value, " / ")
}

func walkSourceChunks(ctx context.Context, repo string, cfg Config, visit func(Chunk) error) error {
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return err
	}
	for _, source := range catalog.Sources {
		if !sourceIsAvailable(source) || len(source.Paths) == 0 {
			continue
		}
		readPath := readableSourcePath(source)
		path, _ := safePath(repo, readPath)
		isText, textErr := isUTF8TextFile(path)
		if textErr != nil {
			return textErr
		}
		if !isText {
			continue
		}
		err = walkFileChunks(ctx, source.ID, source.Paths[0], path, cfg.Chunking, func(chunk Chunk) error {
			chunk.Metadata["read_path"] = readPath
			if source.State == "sanitized" {
				chunk.Metadata["kind"] = "sanitized"
			}
			return visit(chunk)
		})
		if err != nil {
			return fmt.Errorf("stream source for indexing %s: %w", source.ID, err)
		}
	}
	return nil
}

func tokenTerms(text string) map[string]bool {
	terms := map[string]bool{}
	for _, span := range unicodeTokenSpans(strings.ToLower(text)) {
		terms[strings.ToLower(text)[span.start:span.end]] = true
	}
	return terms
}

func lexicalBoost(queryTerms map[string]bool, text string) float32 {
	if len(queryTerms) == 0 {
		return 0
	}
	matched := map[string]bool{}
	lower := strings.ToLower(text)
	for _, span := range unicodeTokenSpans(lower) {
		term := lower[span.start:span.end]
		if queryTerms[term] {
			matched[term] = true
		}
	}
	return float32(len(matched)) / float32(len(queryTerms)) * 0.05
}

func expandChunkContextStreaming(ctx context.Context, repo string, hit Chunk, cfg Config) (string, string, error) {
	readPath := hit.Metadata["read_path"]
	if readPath == "" {
		readPath = hit.Path
	}
	path, err := safePath(repo, readPath)
	if err != nil {
		return "", "", err
	}
	total, err := countFileTokensUpTo(path, cfg.Retrieval.ReadFullDocumentUnderTokens)
	if err != nil {
		return "", "", err
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", "", err
	}
	if total <= cfg.Retrieval.ReadFullDocumentUnderTokens && info.Size() <= 64*1024*1024 {
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return "", "", readErr
		}
		return string(data), "full-document", nil
	}
	parts := make([]string, 0)
	used := 0
	err = walkFileChunks(ctx, hit.SourceID, hit.Path, path, cfg.Chunking, func(chunk Chunk) error {
		inSection := sameHeadingPath(chunk.HeadingPath, hit.HeadingPath)
		isNeighbor := chunk.Ordinal >= hit.Ordinal-cfg.Retrieval.NeighborChunks && chunk.Ordinal <= hit.Ordinal+cfg.Retrieval.NeighborChunks
		if !inSection && !isNeighbor {
			return nil
		}
		if used > 0 && used+chunk.TokenCount > cfg.Retrieval.ReadFullDocumentUnderTokens {
			return nil
		}
		parts = append(parts, chunk.Text)
		used += chunk.TokenCount
		return nil
	})
	if err != nil {
		return "", "", err
	}
	return strings.Join(parts, "\n\n"), "section", nil
}

func sameHeadingPath(left, right []string) bool {
	return strings.Join(left, "\x00") == strings.Join(right, "\x00")
}

func listVectorRecords(ctx context.Context, store VectorStoreAdapter, index string) ([]VectorRecord, error) {
	var records []VectorRecord
	cursor := ""
	for {
		page, err := store.List(ctx, index, cursor, 500)
		if err != nil {
			return nil, fmt.Errorf("list vector records: %w", err)
		}
		records = append(records, page.Records...)
		if page.NextCursor == "" {
			break
		}
		cursor = page.NextCursor
	}
	return records, nil
}
