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
	SourceID string
	Path     string
	Ordinal  int
	Score    float32
	Text     string
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
	matches, err := adapters.VectorStore.Search(ctx, indexName, VectorQuery{Vector: embeddings[0].Values, Limit: limit, Metadata: map[string]string{"state": "active"}})
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
	results := make([]SearchResult, 0, len(matches))
	for _, match := range matches {
		chunk, exists := byID[match.Record.ID]
		if !exists {
			continue
		}
		results = append(results, SearchResult{
			SourceID: chunk.SourceID, Path: chunk.Path, Ordinal: chunk.Ordinal,
			Score: match.Score, Text: chunk.Text,
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
		parts := splitText(string(data), 1800)
		for ordinal, text := range parts {
			contentSHA := digest([]byte(text))
			chunks = append(chunks, Chunk{
				ID:       stableID("chunk", source.ID, fmt.Sprint(ordinal), contentSHA),
				SourceID: source.ID, Path: source.Paths[0], Ordinal: ordinal, Text: text,
				ContentSHA256: contentSHA, Metadata: map[string]string{"state": "active", "kind": "raw"},
			})
		}
	}
	sort.Slice(chunks, func(i, j int) bool { return chunks[i].ID < chunks[j].ID })
	return chunks, nil
}

func splitText(text string, maxRunes int) []string {
	text = strings.TrimSpace(strings.ReplaceAll(text, "\r\n", "\n"))
	if text == "" {
		return nil
	}
	paragraphs := strings.Split(text, "\n\n")
	var result []string
	var current strings.Builder
	flush := func() {
		value := strings.TrimSpace(current.String())
		if value != "" {
			result = append(result, value)
		}
		current.Reset()
	}
	for _, paragraph := range paragraphs {
		paragraph = strings.TrimSpace(paragraph)
		for len([]rune(paragraph)) > maxRunes {
			flush()
			runes := []rune(paragraph)
			result = append(result, strings.TrimSpace(string(runes[:maxRunes])))
			paragraph = strings.TrimSpace(string(runes[maxRunes:]))
		}
		separator := 0
		if current.Len() > 0 {
			separator = 2
		}
		if len([]rune(current.String()))+separator+len([]rune(paragraph)) > maxRunes {
			flush()
		}
		if current.Len() > 0 {
			current.WriteString("\n\n")
		}
		current.WriteString(paragraph)
	}
	flush()
	return result
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
