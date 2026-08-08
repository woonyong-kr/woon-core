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
	Automation     AutomationConfig     `yaml:"automation"`
	InboxRoots     []string             `yaml:"inbox_roots"`
	CatalogPath    string               `yaml:"catalog_path"`
	ReviewPath     string               `yaml:"review_path"`
	ClaimsPath     string               `yaml:"claims_path"`
	MaxFileBytes   int64                `yaml:"max_file_bytes"`
	IgnoreNames    []string             `yaml:"ignore_names"`
	PollSeconds    int                  `yaml:"poll_seconds"`
	Ingestion      IngestionConfig      `yaml:"ingestion"`
	Chunking       ChunkingConfig       `yaml:"chunking"`
	Secrets        SecretPolicy         `yaml:"secrets"`
	Conflicts      ConflictPolicy       `yaml:"conflicts"`
	Deletion       DeletionPolicy       `yaml:"deletion"`
	Classification ClassificationPolicy `yaml:"classification"`
	Retrieval      RetrievalConfig      `yaml:"retrieval"`
	Processing     ProcessingConfig     `yaml:"processing"`
}

type AutomationConfig struct {
	Trigger              string   `yaml:"trigger"`
	Label                string   `yaml:"label"`
	WatchPaths           []string `yaml:"watch_paths"`
	ThrottleSeconds      int      `yaml:"throttle_seconds"`
	RunAtLoad            bool     `yaml:"run_at_load"`
	AutoCommit           bool     `yaml:"auto_commit"`
	AutoPush             bool     `yaml:"auto_push"`
	RequirePrivateRemote bool     `yaml:"require_private_remote"`
	CommitMessage        string   `yaml:"commit_message"`
}

type ProcessingConfig struct {
	ExecutionMode          string `yaml:"execution_mode"`
	AllowPersistentProcess bool   `yaml:"allow_persistent_process"`
	Adapter                string `yaml:"adapter"`
	Model                  string `yaml:"model"`
	ReasoningEffort        string `yaml:"reasoning_effort"`
	BatchSize              int    `yaml:"batch_size"`
	OutputRoot             string `yaml:"output_root"`
	VoiceProfilePath       string `yaml:"voice_profile_path"`
	SchemaPath             string `yaml:"schema_path"`
	CommandPath            string `yaml:"command_path"`
	StreamingThresholdMiB  int64  `yaml:"streaming_threshold_mib"`
	MapReduceFanIn         int    `yaml:"map_reduce_fan_in"`
}

type RetrievalConfig struct {
	ExecutionMode               string                   `yaml:"execution_mode"`
	AllowPersistentProcess      bool                     `yaml:"allow_persistent_process"`
	VectorCandidates            int                      `yaml:"vector_candidates"`
	RerankLimit                 int                      `yaml:"rerank_limit"`
	NeighborChunks              int                      `yaml:"neighbor_chunks"`
	ReadFullDocumentUnderTokens int                      `yaml:"read_full_document_under_tokens"`
	ReadFullDocumentMaxMiB      int64                    `yaml:"read_full_document_max_mib"`
	EmbeddingBatchSize          int                      `yaml:"embedding_batch_size"`
	Embedding                   EmbeddingAdapterConfig   `yaml:"embedding"`
	VectorStore                 VectorStoreAdapterConfig `yaml:"vector_store"`
}

type IngestionConfig struct {
	IgnoreFile          string          `yaml:"ignore_file"`
	WholeFileScanMaxMiB int64           `yaml:"whole_file_scan_max_mib"`
	Stability           StabilityConfig `yaml:"stability"`
	SizePolicy          SizePolicy      `yaml:"size_policy"`
}

type StabilityConfig struct {
	QuietSeconds                    int      `yaml:"quiet_seconds"`
	CheckIntervalSeconds            int      `yaml:"check_interval_seconds"`
	RequiredEqualChecks             int      `yaml:"required_equal_checks"`
	Compare                         []string `yaml:"compare"`
	VerifyHashBeforeAfterProcessing bool     `yaml:"verify_hash_before_after_processing"`
	MaxWaitSeconds                  int      `yaml:"max_wait_seconds"`
}

type SizePolicy struct {
	RegularGitMaxMiB          int64  `yaml:"regular_git_max_mib"`
	LargeFileStrategy         string `yaml:"large_file_strategy"`
	TextProcessing            string `yaml:"text_processing"`
	ImageAnalysisMaxDimension int    `yaml:"image_analysis_max_dimension"`
	PreserveOriginal          bool   `yaml:"preserve_original"`
}

type ChunkingConfig struct {
	Unit               string `yaml:"unit"`
	Tokenizer          string `yaml:"tokenizer"`
	TargetTokens       int    `yaml:"target_tokens"`
	MaxTokens          int    `yaml:"max_tokens"`
	OverlapTokens      int    `yaml:"overlap_tokens"`
	PreserveHeadings   bool   `yaml:"preserve_headings"`
	PreserveCodeBlocks bool   `yaml:"preserve_code_blocks"`
	PreserveTables     bool   `yaml:"preserve_tables"`
}

type SecretPolicy struct {
	QuarantineRoot    string `yaml:"quarantine_root"`
	SanitizedRoot     string `yaml:"sanitized_root"`
	ContinueSafeFiles bool   `yaml:"continue_safe_files"`
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
	SanitizedPath    string   `json:"sanitized_path,omitempty"`
	SanitizedSHA256  string   `json:"sanitized_sha256,omitempty"`
	RotationRequired bool     `json:"rotation_required,omitempty"`
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
	SanitizedSources   int
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
	cfg.applyDefaults()
	if err := cfg.validate(repo); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c *Config) applyDefaults() {
	if c.Automation.Trigger == "" {
		c.Automation.Trigger = "manual"
	}
	if c.Automation.Label == "" {
		c.Automation.Label = "org.woonyong.knowledge-automation"
	}
	if len(c.Automation.WatchPaths) == 0 {
		c.Automation.WatchPaths = append([]string(nil), c.InboxRoots...)
	}
	if c.Automation.ThrottleSeconds == 0 {
		c.Automation.ThrottleSeconds = 30
	}
	if c.Automation.CommitMessage == "" {
		c.Automation.CommitMessage = "chore: 새 지식 원본과 정제 결과를 기록"
	}
	if c.PollSeconds == 0 {
		c.PollSeconds = 5
	}
	if c.Ingestion.IgnoreFile == "" {
		c.Ingestion.IgnoreFile = ".knowledgeignore"
	}
	if c.Ingestion.WholeFileScanMaxMiB == 0 {
		c.Ingestion.WholeFileScanMaxMiB = 64
	}
	if c.Ingestion.SizePolicy.RegularGitMaxMiB == 0 {
		c.Ingestion.SizePolicy.RegularGitMaxMiB = 90
	}
	if c.Ingestion.SizePolicy.LargeFileStrategy == "" {
		c.Ingestion.SizePolicy.LargeFileStrategy = "git-lfs"
	}
	if c.Ingestion.SizePolicy.TextProcessing == "" {
		c.Ingestion.SizePolicy.TextProcessing = "streaming"
	}
	if c.Ingestion.SizePolicy.ImageAnalysisMaxDimension == 0 {
		c.Ingestion.SizePolicy.ImageAnalysisMaxDimension = 2048
	}
	if !c.Ingestion.SizePolicy.PreserveOriginal {
		c.Ingestion.SizePolicy.PreserveOriginal = true
	}
	if c.Chunking.Unit == "" {
		c.Chunking.Unit = "token"
	}
	if c.Chunking.Tokenizer == "" {
		c.Chunking.Tokenizer = "unicode-word-v1"
	}
	if c.Chunking.TargetTokens == 0 {
		c.Chunking.TargetTokens = 1000
	}
	if c.Chunking.MaxTokens == 0 {
		c.Chunking.MaxTokens = 1400
	}
	if c.Chunking.OverlapTokens == 0 {
		c.Chunking.OverlapTokens = 150
	}
	if c.Retrieval.VectorCandidates == 0 {
		c.Retrieval.VectorCandidates = 10
	}
	if c.Retrieval.RerankLimit == 0 {
		c.Retrieval.RerankLimit = 5
	}
	if c.Retrieval.NeighborChunks == 0 {
		c.Retrieval.NeighborChunks = 1
	}
	if c.Retrieval.ReadFullDocumentUnderTokens == 0 {
		c.Retrieval.ReadFullDocumentUnderTokens = 12000
	}
	if c.Retrieval.ReadFullDocumentMaxMiB == 0 {
		c.Retrieval.ReadFullDocumentMaxMiB = 64
	}
	if c.Retrieval.EmbeddingBatchSize == 0 {
		c.Retrieval.EmbeddingBatchSize = 64
	}
	if c.Processing.StreamingThresholdMiB == 0 {
		c.Processing.StreamingThresholdMiB = 64
	}
	if c.Processing.MapReduceFanIn == 0 {
		c.Processing.MapReduceFanIn = c.Processing.BatchSize
		if c.Processing.MapReduceFanIn == 0 {
			c.Processing.MapReduceFanIn = 10
		}
	}
	if c.Secrets.QuarantineRoot == "" {
		c.Secrets.QuarantineRoot = ".knowledge-runtime/quarantine"
	}
	if c.Secrets.SanitizedRoot == "" {
		c.Secrets.SanitizedRoot = "knowledge-ops/sanitized"
	}
	if !c.Secrets.ContinueSafeFiles {
		c.Secrets.ContinueSafeFiles = true
	}
}

func (c Config) validate(repo string) error {
	c.applyDefaults()
	if c.Version != 1 {
		return fmt.Errorf("unsupported knowledge config version %d", c.Version)
	}
	if len(c.InboxRoots) == 0 {
		return errors.New("knowledge config requires inbox_roots")
	}
	if c.Automation.Trigger != "manual" && c.Automation.Trigger != "macos-launchd" {
		return fmt.Errorf("unsupported automation trigger %q", c.Automation.Trigger)
	}
	if strings.TrimSpace(c.Automation.Label) == "" || strings.ContainsAny(c.Automation.Label, " /\\") {
		return errors.New("automation label must be a non-empty launchd-safe identifier")
	}
	if c.Automation.ThrottleSeconds <= 0 {
		return errors.New("automation throttle_seconds must be positive")
	}
	if c.Automation.AutoPush && !c.Automation.AutoCommit {
		return errors.New("automation auto_push requires auto_commit")
	}
	if c.Automation.AutoCommit && strings.TrimSpace(c.Automation.CommitMessage) == "" {
		return errors.New("automation auto_commit requires commit_message")
	}
	for _, path := range c.Automation.WatchPaths {
		if _, err := safePath(repo, path); err != nil {
			return err
		}
		if !contains(c.InboxRoots, path) {
			return fmt.Errorf("automation watch path %q must be an inbox_root", path)
		}
	}
	for _, path := range append(append([]string{}, c.InboxRoots...), c.CatalogPath, c.ReviewPath, c.ClaimsPath) {
		if _, err := safePath(repo, path); err != nil {
			return err
		}
	}
	if c.PollSeconds <= 0 {
		return errors.New("poll_seconds must be positive")
	}
	if c.Chunking.Unit != "token" || c.Chunking.Tokenizer != "unicode-word-v1" {
		return errors.New("chunking requires token unit and supported tokenizer unicode-word-v1")
	}
	if c.Chunking.TargetTokens <= 0 || c.Chunking.MaxTokens < c.Chunking.TargetTokens || c.Chunking.OverlapTokens < 0 || c.Chunking.OverlapTokens >= c.Chunking.TargetTokens {
		return errors.New("chunking token limits are invalid")
	}
	if c.Retrieval.VectorCandidates <= 0 || c.Retrieval.RerankLimit <= 0 || c.Retrieval.RerankLimit > c.Retrieval.VectorCandidates || c.Retrieval.NeighborChunks < 0 || c.Retrieval.ReadFullDocumentUnderTokens <= 0 || c.Retrieval.ReadFullDocumentMaxMiB <= 0 || c.Retrieval.EmbeddingBatchSize <= 0 {
		return errors.New("retrieval limits are invalid")
	}
	if c.Ingestion.Stability.QuietSeconds < 0 || c.Ingestion.Stability.CheckIntervalSeconds < 0 || c.Ingestion.Stability.RequiredEqualChecks < 0 || c.Ingestion.Stability.MaxWaitSeconds < 0 {
		return errors.New("ingestion stability values cannot be negative")
	}
	if c.Ingestion.Stability.QuietSeconds > 0 && (c.Ingestion.Stability.CheckIntervalSeconds <= 0 || c.Ingestion.Stability.RequiredEqualChecks <= 0) {
		return errors.New("enabled ingestion stability requires an interval and equal checks")
	}
	if c.Ingestion.Stability.QuietSeconds > 0 && (!contains(c.Ingestion.Stability.Compare, "size") || !contains(c.Ingestion.Stability.Compare, "modified_time") || !c.Ingestion.Stability.VerifyHashBeforeAfterProcessing) {
		return errors.New("enabled ingestion stability must compare size and modified_time and verify processing hashes")
	}
	if c.Ingestion.SizePolicy.RegularGitMaxMiB <= 0 || c.Ingestion.SizePolicy.LargeFileStrategy != "git-lfs" || c.Ingestion.SizePolicy.TextProcessing != "streaming" || c.Ingestion.SizePolicy.ImageAnalysisMaxDimension <= 0 || !c.Ingestion.SizePolicy.PreserveOriginal {
		return errors.New("size policy requires git-lfs, streaming text processing, and positive limits")
	}
	if c.Ingestion.WholeFileScanMaxMiB <= 0 || c.Processing.StreamingThresholdMiB <= 0 || c.Processing.MapReduceFanIn < 2 {
		return errors.New("streaming thresholds must be positive and map_reduce_fan_in must be at least 2")
	}
	for _, path := range []string{c.Ingestion.IgnoreFile, c.Secrets.QuarantineRoot, c.Secrets.SanitizedRoot} {
		if _, err := safePath(repo, path); err != nil {
			return err
		}
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
	if c.Processing.Adapter != "" && c.Processing.Adapter != "disabled" {
		if c.Processing.ExecutionMode != "on-demand" || c.Processing.AllowPersistentProcess {
			return errors.New("processing requires on-demand execution without persistent processes")
		}
		if c.Processing.Adapter != "codex-cli" {
			return fmt.Errorf("unsupported processing adapter %q", c.Processing.Adapter)
		}
		if strings.TrimSpace(c.Processing.Model) == "" || c.Processing.BatchSize <= 0 {
			return errors.New("processing model and positive batch_size are required")
		}
		for _, path := range []string{c.Processing.OutputRoot, c.Processing.VoiceProfilePath, c.Processing.SchemaPath} {
			if _, err := safePath(repo, path); err != nil {
				return err
			}
		}
		if filepath.IsAbs(c.Processing.CommandPath) {
			if info, err := os.Stat(c.Processing.CommandPath); err != nil || !info.Mode().IsRegular() {
				return fmt.Errorf("processing command_path is not a regular file: %q", c.Processing.CommandPath)
			}
		} else if strings.TrimSpace(c.Processing.CommandPath) == "" {
			return errors.New("processing command_path is required")
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

func writeYAML(path string, value any) error {
	data, err := yaml.Marshal(value)
	if err != nil {
		return err
	}
	return writeAtomic(path, data)
}

func writeAtomic(path string, data []byte) error {
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
