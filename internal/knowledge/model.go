package knowledge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

const (
	configRelativePath = "config/knowledge-workflow.yaml"
	catalogVersion     = 1
)

type Config struct {
	Version        int                  `yaml:"version"`
	InboxRoots     []string             `yaml:"inbox_roots"`
	CatalogPath    string               `yaml:"catalog_path"`
	ReviewPath     string               `yaml:"review_path"`
	ClaimsPath     string               `yaml:"claims_path"`
	MaxFileBytes   int64                `yaml:"max_file_bytes"`
	IgnoreNames    []string             `yaml:"ignore_names"`
	PollSeconds    int                  `yaml:"poll_seconds"`
	Conflicts      ConflictPolicy       `yaml:"conflicts"`
	Deletion       DeletionPolicy       `yaml:"deletion"`
	Classification ClassificationPolicy `yaml:"classification"`
	Retrieval      RetrievalConfig      `yaml:"retrieval"`
}

type RetrievalConfig struct {
	ExecutionMode          string                   `yaml:"execution_mode"`
	AllowPersistentProcess bool                     `yaml:"allow_persistent_process"`
	Embedding              EmbeddingAdapterConfig   `yaml:"embedding"`
	VectorStore            VectorStoreAdapterConfig `yaml:"vector_store"`
}

type EmbeddingAdapterConfig struct {
	Adapter string            `yaml:"adapter"`
	Options map[string]string `yaml:"options,omitempty"`
}

type VectorStoreAdapterConfig struct {
	Adapter string            `yaml:"adapter"`
	Options map[string]string `yaml:"options,omitempty"`
}

type ConflictPolicy struct {
	AutoMergeEquivalent bool   `yaml:"auto_merge_equivalent"`
	DifferentValue      string `yaml:"different_value"`
	Retrieval           string `yaml:"retrieval"`
}

type DeletionPolicy struct {
	MissingSource     string `yaml:"missing_source"`
	ExplicitRetire    string `yaml:"explicit_retire"`
	HardDelete        string `yaml:"hard_delete"`
	DeleteDerivatives string `yaml:"delete_derivatives"`
}

type ClassificationPolicy struct {
	PreserveRawPaths bool     `yaml:"preserve_raw_paths"`
	FolderNames      string   `yaml:"folder_names"`
	AutoMoveRaw      bool     `yaml:"auto_move_raw"`
	Uncertain        string   `yaml:"uncertain"`
	AllowedTypes     []string `yaml:"allowed_types"`
}

type Catalog struct {
	Version   int        `json:"version"`
	Sources   []Source   `json:"sources"`
	Artifacts []Artifact `json:"artifacts"`
}

type Source struct {
	ID               string   `json:"id"`
	SHA256           string   `json:"sha256"`
	NormalizedSHA256 string   `json:"normalized_sha256,omitempty"`
	Paths            []string `json:"paths"`
	MediaType        string   `json:"media_type,omitempty"`
	State            string   `json:"state"`
	Findings         []string `json:"findings,omitempty"`
	InputHints       []string `json:"input_hints,omitempty"`
	RetireReason     string   `json:"retire_reason,omitempty"`
}

type Artifact struct {
	ID        string   `json:"id"`
	Path      string   `json:"path"`
	Kind      string   `json:"kind"`
	SourceIDs []string `json:"source_ids"`
	State     string   `json:"state"`
}

type Review struct {
	Version int          `json:"version"`
	Items   []ReviewItem `json:"items"`
}

type ReviewItem struct {
	ID        string   `json:"id"`
	Kind      string   `json:"kind"`
	Summary   string   `json:"summary"`
	SourceIDs []string `json:"source_ids,omitempty"`
	ClaimIDs  []string `json:"claim_ids,omitempty"`
	Paths     []string `json:"paths,omitempty"`
}

type ClaimFile struct {
	Version int     `yaml:"version"`
	Claims  []Claim `yaml:"claims"`
}

type Claim struct {
	ID         string   `yaml:"id" json:"id"`
	Subject    string   `yaml:"subject" json:"subject"`
	Predicate  string   `yaml:"predicate" json:"predicate"`
	Value      string   `yaml:"value" json:"value"`
	Scope      string   `yaml:"scope" json:"scope"`
	ValidFrom  string   `yaml:"valid_from,omitempty" json:"valid_from,omitempty"`
	ValidUntil string   `yaml:"valid_until,omitempty" json:"valid_until,omitempty"`
	SourceIDs  []string `yaml:"source_ids" json:"source_ids"`
	Status     string   `yaml:"status" json:"status"`
	Supersedes []string `yaml:"supersedes,omitempty" json:"supersedes,omitempty"`
}

type Status struct {
	ActiveSources      int
	MissingSources     int
	QuarantinedSources int
	RetractedSources   int
	Artifacts          int
	ReviewItems        int
}

func LoadConfig(repo string) (Config, error) {
	path := filepath.Join(repo, filepath.FromSlash(configRelativePath))
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read knowledge config %s: %w", path, err)
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse knowledge config: %w", err)
	}
	if err := cfg.validate(repo); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) validate(repo string) error {
	if c.Version != 1 {
		return fmt.Errorf("unsupported knowledge config version %d", c.Version)
	}
	if len(c.InboxRoots) == 0 {
		return errors.New("knowledge config requires inbox_roots")
	}
	for _, path := range append(append([]string{}, c.InboxRoots...), c.CatalogPath, c.ReviewPath, c.ClaimsPath) {
		if _, err := safePath(repo, path); err != nil {
			return err
		}
	}
	if c.MaxFileBytes <= 0 {
		return errors.New("max_file_bytes must be positive")
	}
	if c.PollSeconds <= 0 {
		return errors.New("poll_seconds must be positive")
	}
	if !c.Conflicts.AutoMergeEquivalent || c.Conflicts.DifferentValue != "review" || c.Conflicts.Retrieval != "block-conflicted" {
		return errors.New("conflict policy must review different values and block conflicted retrieval")
	}
	if c.Deletion.MissingSource != "quarantine-dependents" || c.Deletion.ExplicitRetire != "cascade-review" || c.Deletion.HardDelete != "explicit-only" || c.Deletion.DeleteDerivatives != "lineage-review" {
		return errors.New("deletion policy must keep hard delete explicit and review derivatives by lineage")
	}
	if !c.Classification.PreserveRawPaths || c.Classification.FolderNames != "hint-only" || c.Classification.AutoMoveRaw || c.Classification.Uncertain != "review" || len(c.Classification.AllowedTypes) == 0 {
		return errors.New("classification must preserve raw paths, treat folder names as hints, and review uncertain results")
	}
	embeddingAdapter := c.Retrieval.Embedding.Adapter
	vectorStoreAdapter := c.Retrieval.VectorStore.Adapter
	if (embeddingAdapter == "") != (vectorStoreAdapter == "") {
		return errors.New("retrieval must configure both embedding and vector_store adapters")
	}
	if (embeddingAdapter == "disabled") != (vectorStoreAdapter == "disabled") {
		return errors.New("retrieval adapters must be disabled together")
	}
	if embeddingAdapter != "" {
		if c.Retrieval.ExecutionMode != "on-demand" {
			return errors.New("retrieval execution_mode must be on-demand")
		}
		if c.Retrieval.AllowPersistentProcess {
			return errors.New("retrieval persistent processes are not allowed")
		}
	}
	return nil
}

func safePath(root, relative string) (string, error) {
	if relative == "" || filepath.IsAbs(relative) {
		return "", fmt.Errorf("unsafe relative path %q", relative)
	}
	clean := filepath.Clean(filepath.FromSlash(relative))
	if clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path escapes knowledge repository: %q", relative)
	}
	return filepath.Join(root, clean), nil
}

func loadCatalog(repo string, cfg Config) (Catalog, error) {
	path, _ := safePath(repo, cfg.CatalogPath)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return Catalog{Version: catalogVersion}, nil
	}
	if err != nil {
		return Catalog{}, fmt.Errorf("read catalog: %w", err)
	}
	var catalog Catalog
	if err := json.Unmarshal(data, &catalog); err != nil {
		return Catalog{}, fmt.Errorf("parse catalog: %w", err)
	}
	if catalog.Version != catalogVersion {
		return Catalog{}, fmt.Errorf("unsupported catalog version %d", catalog.Version)
	}
	return catalog, nil
}

func loadClaims(repo string, cfg Config) (ClaimFile, error) {
	path, _ := safePath(repo, cfg.ClaimsPath)
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return ClaimFile{Version: 1}, nil
	}
	if err != nil {
		return ClaimFile{}, fmt.Errorf("read claims: %w", err)
	}
	var claims ClaimFile
	if err := yaml.Unmarshal(data, &claims); err != nil {
		return ClaimFile{}, fmt.Errorf("parse claims: %w", err)
	}
	if claims.Version != 1 {
		return ClaimFile{}, fmt.Errorf("unsupported claims version %d", claims.Version)
	}
	return claims, nil
}

func readJSONIfExists(path string, value any) error {
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, value); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	return nil
}

func writeJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".knowledge-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o644); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}

func digest(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func normalizedDigest(data []byte) string {
	if !utf8.Valid(data) || bytes.IndexByte(data, 0) >= 0 {
		return ""
	}
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	lines := strings.Split(text, "\n")
	for i := range lines {
		lines[i] = strings.TrimRight(lines[i], " \t")
	}
	return digest([]byte(strings.TrimSpace(strings.Join(lines, "\n"))))
}

func stableID(kind string, parts ...string) string {
	values := append([]string(nil), parts...)
	sort.Strings(values)
	return kind + "-" + digest([]byte(strings.Join(values, "\x00")))[:16]
}
