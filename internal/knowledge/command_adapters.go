package knowledge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
)

type commandEmbeddingAdapter struct {
	command  string
	model    string
	cacheDir string
	spec     EmbeddingSpec
}

type commandVectorStore struct {
	command  string
	database string
}

func NewDefaultAdapterRegistry(repo string) (*AdapterRegistry, error) {
	registry := NewAdapterRegistry()
	if err := registry.RegisterEmbedding("fastembed", func(options map[string]string) (EmbeddingAdapter, error) {
		command, err := resolveCommand(options["command"])
		if err != nil {
			return nil, err
		}
		dimensions, err := strconv.Atoi(options["dimensions"])
		if err != nil || dimensions <= 0 {
			return nil, fmt.Errorf("fastembed dimensions must be positive")
		}
		cacheDir, err := resolveAdapterPath(repo, options["cache_dir"])
		if err != nil {
			return nil, err
		}
		adapter := &commandEmbeddingAdapter{
			command: command, model: strings.TrimSpace(options["model"]), cacheDir: cacheDir,
			spec: EmbeddingSpec{
				Adapter: "fastembed", Model: strings.TrimSpace(options["model"]),
				Revision: strings.TrimSpace(options["revision"]), Dimensions: dimensions,
				Normalization: strings.TrimSpace(options["normalization"]),
			},
		}
		if err := adapter.spec.Validate(); err != nil {
			return nil, err
		}
		return adapter, nil
	}); err != nil {
		return nil, err
	}
	if err := registry.RegisterVectorStore("lancedb", func(options map[string]string) (VectorStoreAdapter, error) {
		command, err := resolveCommand(options["command"])
		if err != nil {
			return nil, err
		}
		database, err := resolveAdapterPath(repo, options["path"])
		if err != nil {
			return nil, err
		}
		return &commandVectorStore{command: command, database: database}, nil
	}); err != nil {
		return nil, err
	}
	return registry, nil
}

func (a *commandEmbeddingAdapter) Spec() EmbeddingSpec { return a.spec }

func (a *commandEmbeddingAdapter) Embed(ctx context.Context, chunks []Chunk) ([]Embedding, error) {
	request := struct {
		Operation string  `json:"operation"`
		Model     string  `json:"model"`
		CacheDir  string  `json:"cache_dir"`
		Chunks    []Chunk `json:"chunks"`
	}{Operation: "embed", Model: a.model, CacheDir: a.cacheDir, Chunks: chunks}
	var response struct {
		Embeddings []Embedding `json:"embeddings"`
	}
	if err := runAdapterCommand(ctx, a.command, request, &response); err != nil {
		return nil, err
	}
	return response.Embeddings, nil
}

func (s *commandVectorStore) Name() string { return "lancedb" }

func (s *commandVectorStore) CreateIndex(ctx context.Context, spec VectorIndexSpec) error {
	return s.run(ctx, map[string]any{"operation": "create_index", "index": spec.Name, "spec": spec}, nil)
}

func (s *commandVectorStore) DeleteIndex(ctx context.Context, name string) error {
	return s.run(ctx, map[string]any{"operation": "delete_index", "index": name}, nil)
}

func (s *commandVectorStore) DescribeIndex(ctx context.Context, name string) (VectorIndexSpec, error) {
	var response struct {
		Spec VectorIndexSpec `json:"spec"`
	}
	err := s.run(ctx, map[string]any{"operation": "describe_index", "index": name}, &response)
	return response.Spec, err
}

func (s *commandVectorStore) Upsert(ctx context.Context, name string, records []VectorRecord) error {
	return s.run(ctx, map[string]any{"operation": "upsert", "index": name, "records": records}, nil)
}

func (s *commandVectorStore) Delete(ctx context.Context, name string, ids []string) error {
	return s.run(ctx, map[string]any{"operation": "delete_records", "index": name, "ids": ids}, nil)
}

func (s *commandVectorStore) List(ctx context.Context, name, after string, limit int) (VectorPage, error) {
	var page VectorPage
	err := s.run(ctx, map[string]any{"operation": "list_records", "index": name, "after": after, "limit": limit}, &page)
	return page, err
}

func (s *commandVectorStore) Search(ctx context.Context, name string, query VectorQuery) ([]VectorMatch, error) {
	var response struct {
		Matches []VectorMatch `json:"matches"`
	}
	err := s.run(ctx, map[string]any{"operation": "search", "index": name, "vector": query.Vector, "limit": query.Limit, "metadata": query.Metadata}, &response)
	return response.Matches, err
}

func (s *commandVectorStore) run(ctx context.Context, request map[string]any, response any) error {
	request["database"] = s.database
	return runAdapterCommand(ctx, s.command, request, response)
}

func runAdapterCommand(ctx context.Context, command string, request, response any) error {
	data, err := json.Marshal(request)
	if err != nil {
		return fmt.Errorf("encode adapter request: %w", err)
	}
	cmd := exec.CommandContext(ctx, command)
	cmd.Stdin = bytes.NewReader(data)
	var stdout bytes.Buffer
	var stderr boundedBuffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("run adapter command: %w: %s", err, stderr.String())
	}
	if response == nil {
		return nil
	}
	if err := json.Unmarshal(stdout.Bytes(), response); err != nil {
		return fmt.Errorf("parse adapter response: %w", err)
	}
	return nil
}

func resolveCommand(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", fmt.Errorf("adapter command is required")
	}
	if filepath.IsAbs(value) {
		return value, nil
	}
	resolved, err := exec.LookPath(value)
	if err != nil {
		return "", fmt.Errorf("find adapter command %q: %w", value, err)
	}
	return resolved, nil
}

func resolveAdapterPath(repo, value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", fmt.Errorf("adapter path is required")
	}
	if filepath.IsAbs(value) {
		return filepath.Clean(value), nil
	}
	return safePath(repo, value)
}
