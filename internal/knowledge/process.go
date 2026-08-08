package knowledge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

type ProcessDocument struct {
	SourceID   string   `json:"source_id"`
	Path       string   `json:"path"`
	InputHints []string `json:"input_hints"`
	Text       string   `json:"text"`
}

type ProcessRequest struct {
	VoiceProfile string            `json:"voice_profile"`
	AllowedTypes []string          `json:"allowed_types"`
	Documents    []ProcessDocument `json:"documents"`
}

type CandidateClaim struct {
	Subject    string   `json:"subject"`
	Predicate  string   `json:"predicate"`
	Value      string   `json:"value"`
	Scope      string   `json:"scope"`
	ValidFrom  string   `json:"valid_from"`
	ValidUntil string   `json:"valid_until"`
	Supersedes []string `json:"supersedes"`
}

type ProcessedDocument struct {
	SourceID      string           `json:"source_id"`
	Title         string           `json:"title"`
	Type          string           `json:"type"`
	Scope         string           `json:"scope"`
	Summary       string           `json:"summary"`
	Body          string           `json:"body"`
	Claims        []CandidateClaim `json:"claims"`
	Uncertainties []string         `json:"uncertainties"`
}

type ProcessResponse struct {
	Documents []ProcessedDocument `json:"documents"`
}

type DocumentProcessor interface {
	Name() string
	Model() string
	Process(context.Context, ProcessRequest) (ProcessResponse, error)
}

type ProcessResult struct {
	ScannedFiles int
	Pending      int
	Created      int
	ReviewItems  int
}

type CodexCLIProcessor struct {
	commandPath     string
	model           string
	reasoningEffort string
	schemaPath      string
}

func NewConfiguredProcessor(repo string, cfg Config) (DocumentProcessor, error) {
	if cfg.Processing.Adapter == "" || cfg.Processing.Adapter == "disabled" {
		return nil, fmt.Errorf("document processing adapter is disabled")
	}
	if cfg.Processing.Adapter != "codex-cli" {
		return nil, fmt.Errorf("unsupported document processing adapter %q", cfg.Processing.Adapter)
	}
	schemaPath, err := safePath(repo, cfg.Processing.SchemaPath)
	if err != nil {
		return nil, err
	}
	commandPath := cfg.Processing.CommandPath
	if !filepath.IsAbs(commandPath) {
		commandPath, err = exec.LookPath(commandPath)
		if err != nil {
			return nil, fmt.Errorf("find processing command %q: %w", cfg.Processing.CommandPath, err)
		}
	}
	return &CodexCLIProcessor{
		commandPath: commandPath, model: cfg.Processing.Model,
		reasoningEffort: cfg.Processing.ReasoningEffort, schemaPath: schemaPath,
	}, nil
}

func (p *CodexCLIProcessor) Name() string  { return "codex-cli" }
func (p *CodexCLIProcessor) Model() string { return p.model }

func (p *CodexCLIProcessor) Process(ctx context.Context, request ProcessRequest) (ProcessResponse, error) {
	var response ProcessResponse
	if p == nil || strings.TrimSpace(p.commandPath) == "" || strings.TrimSpace(p.model) == "" {
		return response, fmt.Errorf("configured Codex CLI processor is required")
	}
	input, err := json.Marshal(request)
	if err != nil {
		return response, fmt.Errorf("encode processing request: %w", err)
	}
	workdir, err := os.MkdirTemp("", "woon-knowledge-codex-*")
	if err != nil {
		return response, fmt.Errorf("create isolated Codex workdir: %w", err)
	}
	defer os.RemoveAll(workdir)
	outputPath := filepath.Join(workdir, "response.json")
	args := []string{
		"exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
		"--sandbox", "read-only", "--model", p.model,
		"--skip-git-repo-check", "--cd", workdir,
		"--output-schema", p.schemaPath, "--output-last-message", outputPath,
	}
	if strings.TrimSpace(p.reasoningEffort) != "" {
		args = append(args, "--config", fmt.Sprintf("model_reasoning_effort=%q", p.reasoningEffort))
	}
	args = append(args, `입력 JSON의 각 문서를 한국어 지식 candidate로 정리하라. 원문 사실을 보존하고 추론을 사실처럼 추가하지 않는다. voice_profile을 모든 문서에 적용한다. 서로 다른 내용을 임의로 하나로 합치거나 최신이라고 선택하지 않는다. 불확실한 내용은 uncertainties에 기록한다. claims는 원문이 직접 뒷받침하는 최소 사실만 추출한다. 입력과 같은 source_id를 정확히 한 번 반환하고 JSON Schema만 따른다.`)

	cmd := exec.CommandContext(ctx, p.commandPath, args...)
	cmd.Dir = workdir
	cmd.Stdin = bytes.NewReader(input)
	var diagnostics boundedBuffer
	cmd.Stdout = &diagnostics
	cmd.Stderr = &diagnostics
	if err := cmd.Run(); err != nil {
		return response, fmt.Errorf("run Codex CLI processor: %w: %s", err, diagnostics.String())
	}
	data, err := os.ReadFile(outputPath)
	if err != nil {
		return response, fmt.Errorf("read Codex CLI response: %w", err)
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return response, fmt.Errorf("parse Codex CLI response: %w", err)
	}
	return response, nil
}

type boundedBuffer struct {
	data []byte
}

func (b *boundedBuffer) Write(data []byte) (int, error) {
	const limit = 8192
	b.data = append(b.data, data...)
	if len(b.data) > limit {
		b.data = append([]byte(nil), b.data[len(b.data)-limit:]...)
	}
	return len(data), nil
}

func (b *boundedBuffer) String() string { return strings.TrimSpace(string(b.data)) }

func ProcessPending(ctx context.Context, repo string, processor DocumentProcessor, limit int) (ProcessResult, error) {
	var result ProcessResult
	if ctx == nil || processor == nil {
		return result, fmt.Errorf("context and document processor are required")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return result, err
	}
	if err := WaitForStableSources(ctx, repo, cfg); err != nil {
		return result, err
	}
	scan, err := Scan(repo)
	if err != nil {
		return result, err
	}
	result.ScannedFiles = scan.Files
	if limit <= 0 || limit > cfg.Processing.BatchSize {
		limit = cfg.Processing.BatchSize
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return result, err
	}
	linked := map[string]bool{}
	for _, artifact := range catalog.Artifacts {
		for _, sourceID := range artifact.SourceIDs {
			linked[sourceID] = true
		}
	}
	voicePath, _ := safePath(repo, cfg.Processing.VoiceProfilePath)
	voice, err := os.ReadFile(voicePath)
	if err != nil {
		return result, fmt.Errorf("read voice profile: %w", err)
	}
	request := ProcessRequest{VoiceProfile: string(voice), AllowedTypes: append([]string(nil), cfg.Classification.AllowedTypes...)}
	inputHashes := map[string]string{}
	inputPaths := map[string]string{}
	var hierarchicalDocuments []ProcessedDocument
	selectedSources := 0
	for _, source := range catalog.Sources {
		if !sourceIsAvailable(source) || linked[source.ID] {
			continue
		}
		result.Pending++
		if selectedSources >= limit || len(source.Paths) == 0 {
			continue
		}
		selectedSources++
		readPath := readableSourcePath(source)
		path, _ := safePath(repo, readPath)
		isText, textErr := isUTF8TextFile(path)
		if textErr != nil {
			return result, fmt.Errorf("inspect source %s: %w", source.ID, textErr)
		}
		if !isText {
			continue
		}
		beforeHash, hashErr := hashFile(path)
		if hashErr != nil {
			return result, fmt.Errorf("hash source %s: %w", source.ID, hashErr)
		}
		inputHashes[source.ID] = beforeHash
		inputPaths[source.ID] = readPath
		tokens, countErr := countFileTokensUpTo(path, cfg.Retrieval.ReadFullDocumentUnderTokens)
		if countErr != nil {
			return result, fmt.Errorf("count source tokens %s: %w", source.ID, countErr)
		}
		info, statErr := os.Stat(path)
		if statErr != nil {
			return result, statErr
		}
		if tokens > cfg.Retrieval.ReadFullDocumentUnderTokens || info.Size() > cfg.Processing.StreamingThresholdMiB*1024*1024 {
			document, processErr := processLargeSource(ctx, repo, processor, cfg, string(voice), source, readPath)
			if processErr != nil {
				return result, fmt.Errorf("hierarchical process source %s: %w", source.ID, processErr)
			}
			hierarchicalDocuments = append(hierarchicalDocuments, document)
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return result, fmt.Errorf("read source %s: %w", source.ID, err)
		}
		if !utf8.Valid(data) || bytes.IndexByte(data, 0) >= 0 {
			continue
		}
		request.Documents = append(request.Documents, ProcessDocument{
			SourceID: source.ID, Path: source.Paths[0], InputHints: append([]string(nil), source.InputHints...), Text: string(data),
		})
	}
	if len(request.Documents) == 0 && len(hierarchicalDocuments) == 0 {
		result.ReviewItems = scan.ReviewItems
		return result, nil
	}
	response := ProcessResponse{}
	if len(request.Documents) > 0 {
		response, err = processor.Process(ctx, request)
		if err != nil {
			return result, err
		}
	}
	if cfg.Ingestion.Stability.VerifyHashBeforeAfterProcessing {
		for sourceID, readPath := range inputPaths {
			path, pathErr := safePath(repo, readPath)
			if pathErr != nil {
				return result, pathErr
			}
			afterHash, readErr := hashFile(path)
			if readErr != nil || afterHash != inputHashes[sourceID] {
				return result, fmt.Errorf("source %s changed during processing; discard result and retry", sourceID)
			}
		}
	}
	validated := append([]ProcessedDocument(nil), hierarchicalDocuments...)
	if len(request.Documents) > 0 {
		smallDocuments, validateErr := validateProcessedDocuments(response.Documents, request, cfg)
		if validateErr != nil {
			return result, validateErr
		}
		validated = append(validated, smallDocuments...)
	}
	claimFile, err := loadClaims(repo, cfg)
	if err != nil {
		return result, err
	}
	claimByID := make(map[string]Claim, len(claimFile.Claims))
	for _, claim := range claimFile.Claims {
		claimByID[claim.ID] = claim
	}
	outputRoot, _ := safePath(repo, cfg.Processing.OutputRoot)
	for _, document := range validated {
		relativePath := filepath.ToSlash(filepath.Join(cfg.Processing.OutputRoot, document.SourceID+".md"))
		outputPath := filepath.Join(outputRoot, document.SourceID+".md")
		rendered, err := renderCandidate(document, processor)
		if err != nil {
			return result, err
		}
		for _, pattern := range secretPatterns {
			if pattern.pattern.Match(rendered) {
				return result, fmt.Errorf("generated candidate %s contains secret pattern %s", document.SourceID, pattern.name)
			}
		}
		if err := writeAtomic(outputPath, rendered); err != nil {
			return result, fmt.Errorf("write candidate %s: %w", document.SourceID, err)
		}
		artifact := Artifact{
			ID: "artifact-" + digest([]byte(relativePath)), Path: relativePath, Kind: "wiki",
			SourceIDs: []string{document.SourceID}, State: "candidate",
		}
		catalog.Artifacts = append(catalog.Artifacts, artifact)
		for _, candidate := range document.Claims {
			claim := Claim{
				Subject: strings.TrimSpace(candidate.Subject), Predicate: strings.TrimSpace(candidate.Predicate),
				Value: strings.TrimSpace(candidate.Value), Scope: strings.TrimSpace(candidate.Scope),
				ValidFrom: strings.TrimSpace(candidate.ValidFrom), ValidUntil: strings.TrimSpace(candidate.ValidUntil),
				SourceIDs: []string{document.SourceID}, Status: "candidate", Supersedes: uniqueSorted(candidate.Supersedes),
			}
			claim.ID = stableID("claim", claim.Subject, claim.Predicate, claim.Value, claim.Scope, claim.ValidFrom, claim.ValidUntil, document.SourceID)
			claimByID[claim.ID] = claim
		}
		result.Created++
	}
	claimFile.Claims = claimFile.Claims[:0]
	for _, claim := range claimByID {
		claimFile.Claims = append(claimFile.Claims, claim)
	}
	sort.Slice(claimFile.Claims, func(i, j int) bool { return claimFile.Claims[i].ID < claimFile.Claims[j].ID })
	sort.Slice(catalog.Artifacts, func(i, j int) bool { return catalog.Artifacts[i].ID < catalog.Artifacts[j].ID })
	claimsPath, _ := safePath(repo, cfg.ClaimsPath)
	catalogPath, _ := safePath(repo, cfg.CatalogPath)
	if err := writeYAML(claimsPath, claimFile); err != nil {
		return result, fmt.Errorf("write candidate claims: %w", err)
	}
	if err := writeJSON(catalogPath, catalog); err != nil {
		return result, fmt.Errorf("write candidate lineage: %w", err)
	}
	finalScan, err := Scan(repo)
	if err != nil {
		return result, err
	}
	result.ReviewItems = finalScan.ReviewItems
	return result, nil
}

func validateProcessedDocuments(documents []ProcessedDocument, request ProcessRequest, cfg Config) ([]ProcessedDocument, error) {
	expected := make(map[string]bool, len(request.Documents))
	for _, document := range request.Documents {
		expected[document.SourceID] = true
	}
	seen := map[string]bool{}
	for i := range documents {
		document := &documents[i]
		if !expected[document.SourceID] || seen[document.SourceID] {
			return nil, fmt.Errorf("processor returned unknown or duplicate source %q", document.SourceID)
		}
		seen[document.SourceID] = true
		document.Title = strings.TrimSpace(document.Title)
		document.Type = strings.TrimSpace(document.Type)
		document.Scope = strings.TrimSpace(document.Scope)
		document.Summary = strings.TrimSpace(document.Summary)
		document.Body = strings.TrimSpace(document.Body)
		if document.Title == "" || document.Scope == "" || document.Summary == "" || document.Body == "" {
			return nil, fmt.Errorf("processor returned incomplete document for %s", document.SourceID)
		}
		if !contains(cfg.Classification.AllowedTypes, document.Type) {
			return nil, fmt.Errorf("processor returned unsupported type %q for %s", document.Type, document.SourceID)
		}
		for _, claim := range document.Claims {
			if strings.TrimSpace(claim.Subject) == "" || strings.TrimSpace(claim.Predicate) == "" || strings.TrimSpace(claim.Value) == "" || strings.TrimSpace(claim.Scope) == "" {
				return nil, fmt.Errorf("processor returned incomplete claim for %s", document.SourceID)
			}
		}
	}
	if len(seen) != len(expected) {
		return nil, fmt.Errorf("processor returned %d documents for %d sources", len(seen), len(expected))
	}
	sort.Slice(documents, func(i, j int) bool { return documents[i].SourceID < documents[j].SourceID })
	return documents, nil
}

func renderCandidate(document ProcessedDocument, processor DocumentProcessor) ([]byte, error) {
	frontmatter := struct {
		Type           string   `yaml:"type"`
		Title          string   `yaml:"title"`
		Status         string   `yaml:"status"`
		Scope          string   `yaml:"scope"`
		SourceIDs      []string `yaml:"source_ids"`
		GeneratedBy    string   `yaml:"generated_by"`
		GeneratedModel string   `yaml:"generated_model"`
	}{
		Type: document.Type, Title: document.Title, Status: "Candidate", Scope: document.Scope,
		SourceIDs: []string{document.SourceID}, GeneratedBy: processor.Name(), GeneratedModel: processor.Model(),
	}
	header, err := yaml.Marshal(frontmatter)
	if err != nil {
		return nil, err
	}
	var output strings.Builder
	output.WriteString("---\n")
	output.Write(header)
	output.WriteString("---\n\n# ")
	output.WriteString(document.Title)
	output.WriteString("\n\n")
	output.WriteString(document.Body)
	output.WriteString("\n\n## 정리 메모\n\n")
	output.WriteString(document.Summary)
	if len(document.Uncertainties) > 0 {
		output.WriteString("\n\n## 확인 필요\n")
		for _, uncertainty := range document.Uncertainties {
			output.WriteString("\n- ")
			output.WriteString(strings.TrimSpace(uncertainty))
		}
	}
	output.WriteByte('\n')
	return []byte(output.String()), nil
}
