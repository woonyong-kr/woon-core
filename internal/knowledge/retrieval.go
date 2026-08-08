package knowledge

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
	"unicode/utf8"
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
	chunks, err := sourceChunks(repo, cfg)
	if err != nil {
		return result, err
	}
	result.Chunks = len(chunks)
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
	pending := make([]Chunk, 0, len(chunks))
	expected := make(map[string]bool, len(chunks))
	for _, chunk := range chunks {
		expected[chunk.ID] = true
		record, exists := existingByID[chunk.ID]
		if !exists || record.ContentSHA256 != chunk.ContentSHA256 {
			pending = append(pending, chunk)
		}
	}
	var records []VectorRecord
	if len(pending) > 0 {
		records, err = BuildVectorRecords(ctx, adapters.Embedding, pending)
		if err != nil {
			return result, err
		}
	}
	if len(records) > 0 {
		if err := adapters.VectorStore.Upsert(ctx, indexName, records); err != nil {
			return result, fmt.Errorf("upsert vector records: %w", err)
		}
	}
	result.Upserted = len(records)
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
	chunks, err := sourceChunks(repo, cfg)
	if err != nil {
		return nil, err
	}
	byID := make(map[string]Chunk, len(chunks))
	for _, chunk := range chunks {
		byID[chunk.ID] = chunk
	}
	chunksBySource := make(map[string][]Chunk)
	for _, chunk := range chunks {
		chunksBySource[chunk.SourceID] = append(chunksBySource[chunk.SourceID], chunk)
	}
	for sourceID := range chunksBySource {
		sort.Slice(chunksBySource[sourceID], func(i, j int) bool { return chunksBySource[sourceID][i].Ordinal < chunksBySource[sourceID][j].Ordinal })
	}
	type rankedMatch struct {
		chunk Chunk
		score float32
	}
	queryTerms := tokenTerms(query)
	ranked := make([]rankedMatch, 0, len(matches))
	seenContent := map[string]bool{}
	for _, match := range matches {
		chunk, exists := byID[match.Record.ID]
		if !exists || seenContent[chunk.ContentSHA256] {
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
		text, kind := expandChunkContext(repo, match.chunk, chunksBySource[match.chunk.SourceID], cfg)
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

func sourceChunks(repo string, cfg Config) ([]Chunk, error) {
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return nil, err
	}
	var chunks []Chunk
	for _, source := range catalog.Sources {
		if source.State != "active" || len(source.Paths) == 0 {
			continue
		}
		path, _ := safePath(repo, source.Paths[0])
		data, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read source for indexing %s: %w", source.ID, err)
		}
		if !utf8.Valid(data) || strings.IndexByte(string(data), 0) >= 0 {
			continue
		}
		chunks = append(chunks, chunkDocument(source.ID, source.Paths[0], string(data), cfg.Chunking)...)
	}
	sortChunksByOrdinal(chunks)
	return chunks, nil
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

func expandChunkContext(repo string, hit Chunk, sourceChunks []Chunk, cfg Config) (string, string) {
	total := 0
	for _, chunk := range sourceChunks {
		total += chunk.TokenCount
	}
	if total <= cfg.Retrieval.ReadFullDocumentUnderTokens {
		path, err := safePath(repo, hit.Path)
		if err == nil {
			if data, readErr := os.ReadFile(path); readErr == nil {
				return string(data), "full-document"
			}
		}
	}
	selected := map[int]bool{}
	for i, chunk := range sourceChunks {
		if sameHeadingPath(chunk.HeadingPath, hit.HeadingPath) {
			selected[i] = true
		}
		if chunk.ID == hit.ID {
			for offset := -cfg.Retrieval.NeighborChunks; offset <= cfg.Retrieval.NeighborChunks; offset++ {
				if i+offset >= 0 && i+offset < len(sourceChunks) {
					selected[i+offset] = true
				}
			}
		}
	}
	indices := make([]int, 0, len(selected))
	for index := range selected {
		indices = append(indices, index)
	}
	sort.Ints(indices)
	parts := make([]string, 0, len(indices))
	used := 0
	for _, index := range indices {
		chunk := sourceChunks[index]
		if used > 0 && used+chunk.TokenCount > cfg.Retrieval.ReadFullDocumentUnderTokens {
			break
		}
		if len(parts) == 0 || parts[len(parts)-1] != chunk.Text {
			parts = append(parts, chunk.Text)
		}
		used += chunk.TokenCount
	}
	return strings.Join(parts, "\n\n"), "section"
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
