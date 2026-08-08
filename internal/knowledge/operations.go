package knowledge

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

func GetStatus(repo string) (Status, error) {
	cfg, err := LoadConfig(repo)
	if err != nil {
		return Status{}, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return Status{}, err
	}
	var status Status
	for _, source := range catalog.Sources {
		switch source.State {
		case "active":
			status.ActiveSources++
		case "sanitized":
			status.SanitizedSources++
		case "missing":
			status.MissingSources++
		case "quarantined":
			status.QuarantinedSources++
		case "retracted":
			status.RetractedSources++
		}
	}
	status.Artifacts = len(catalog.Artifacts)
	reviewPath, _ := safePath(repo, cfg.ReviewPath)
	var review Review
	if err := readJSONIfExists(reviewPath, &review); err != nil {
		return Status{}, err
	}
	status.ReviewItems = len(review.Items)
	return status, nil
}

func Link(repo, artifactPath, kind string, sourceIDs []string) (Artifact, error) {
	cfg, err := LoadConfig(repo)
	if err != nil {
		return Artifact{}, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return Artifact{}, err
	}
	if !contains([]string{"wiki", "blog", "portfolio", "llm-index", "skill", "other"}, kind) {
		return Artifact{}, fmt.Errorf("unsupported artifact kind %q", kind)
	}
	abs, err := safePath(repo, artifactPath)
	if err != nil {
		return Artifact{}, err
	}
	info, err := os.Lstat(abs)
	if err != nil {
		return Artifact{}, fmt.Errorf("artifact does not exist: %s", artifactPath)
	}
	if !info.Mode().IsRegular() {
		return Artifact{}, fmt.Errorf("artifact must be a regular file: %s", artifactPath)
	}
	resolvedRepo, err := filepath.EvalSymlinks(repo)
	if err != nil {
		return Artifact{}, fmt.Errorf("resolve knowledge repository: %w", err)
	}
	resolvedArtifact, err := filepath.EvalSymlinks(abs)
	if err != nil {
		return Artifact{}, fmt.Errorf("resolve artifact: %w", err)
	}
	relativeResolved, err := filepath.Rel(resolvedRepo, resolvedArtifact)
	if err != nil || relativeResolved == ".." || strings.HasPrefix(relativeResolved, ".."+string(filepath.Separator)) {
		return Artifact{}, fmt.Errorf("artifact resolves outside knowledge repository: %s", artifactPath)
	}
	known := map[string]bool{}
	for _, source := range catalog.Sources {
		known[source.ID] = true
	}
	for _, sourceID := range sourceIDs {
		if !known[sourceID] {
			return Artifact{}, fmt.Errorf("unknown source %q", sourceID)
		}
	}
	if len(sourceIDs) == 0 {
		return Artifact{}, fmt.Errorf("link requires at least one source")
	}
	sourceIDs = uniqueSorted(sourceIDs)
	artifact := Artifact{ID: "artifact-" + digest([]byte(filepath.ToSlash(filepath.Clean(artifactPath)))), Path: filepath.ToSlash(filepath.Clean(artifactPath)), Kind: kind, SourceIDs: sourceIDs, State: "candidate"}
	replaced := false
	for i := range catalog.Artifacts {
		if catalog.Artifacts[i].ID == artifact.ID {
			catalog.Artifacts[i] = artifact
			replaced = true
			break
		}
	}
	if !replaced {
		catalog.Artifacts = append(catalog.Artifacts, artifact)
	}
	applyArtifactAvailability(&catalog)
	catalogPath, _ := safePath(repo, cfg.CatalogPath)
	if err := writeJSON(catalogPath, catalog); err != nil {
		return Artifact{}, err
	}
	for _, candidate := range catalog.Artifacts {
		if candidate.ID == artifact.ID {
			return candidate, nil
		}
	}
	return artifact, nil
}

func Retire(repo, sourceReference, reason string) (Source, []Artifact, error) {
	if strings.TrimSpace(reason) == "" {
		return Source{}, nil, fmt.Errorf("retire requires a reason")
	}
	if len(sourceReference) < 12 {
		return Source{}, nil, fmt.Errorf("source prefix must contain at least 12 characters")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return Source{}, nil, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return Source{}, nil, err
	}
	index := -1
	for i, source := range catalog.Sources {
		if source.ID == sourceReference || strings.HasPrefix(source.ID, sourceReference) {
			if index != -1 {
				return Source{}, nil, fmt.Errorf("ambiguous source prefix %q", sourceReference)
			}
			index = i
		}
	}
	if index == -1 {
		return Source{}, nil, fmt.Errorf("unknown source %q", sourceReference)
	}
	catalog.Sources[index].State = "retracted"
	catalog.Sources[index].RetireReason = reason
	var affected []Artifact
	for i := range catalog.Artifacts {
		if contains(catalog.Artifacts[i].SourceIDs, catalog.Sources[index].ID) {
			catalog.Artifacts[i].State = "review-required"
			affected = append(affected, catalog.Artifacts[i])
		}
	}
	catalogPath, _ := safePath(repo, cfg.CatalogPath)
	if err := writeJSON(catalogPath, catalog); err != nil {
		return Source{}, nil, err
	}
	if _, err := Scan(repo); err != nil {
		return Source{}, nil, fmt.Errorf("refresh review after retire: %w", err)
	}
	return catalog.Sources[index], affected, nil
}

func Trace(repo, sourceReference string) (Source, []Artifact, error) {
	if len(sourceReference) < 12 {
		return Source{}, nil, fmt.Errorf("source prefix must contain at least 12 characters")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return Source{}, nil, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return Source{}, nil, err
	}
	var matches []Source
	for _, source := range catalog.Sources {
		if source.ID == sourceReference || strings.HasPrefix(source.ID, sourceReference) {
			matches = append(matches, source)
		}
	}
	if len(matches) != 1 {
		return Source{}, nil, fmt.Errorf("source reference %q matched %d sources", sourceReference, len(matches))
	}
	var artifacts []Artifact
	for _, artifact := range catalog.Artifacts {
		if contains(artifact.SourceIDs, matches[0].ID) {
			artifacts = append(artifacts, artifact)
		}
	}
	return matches[0], artifacts, nil
}

func Context(repo, scope string) ([]Claim, error) {
	cfg, err := LoadConfig(repo)
	if err != nil {
		return nil, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return nil, err
	}
	claimFile, err := loadClaims(repo, cfg)
	if err != nil {
		return nil, err
	}
	blocked := map[string]bool{}
	for _, item := range evaluateClaims(claimFile.Claims, catalog.Sources) {
		if item.Kind != "claim-conflict" && item.Kind != "claim-source-unavailable" && item.Kind != "invalid-claim" {
			continue
		}
		for _, claimID := range item.ClaimIDs {
			blocked[claimID] = true
		}
	}
	for _, claim := range claimFile.Claims {
		if claim.Status != "active" || blocked[claim.ID] {
			continue
		}
		for _, supersededID := range claim.Supersedes {
			blocked[supersededID] = true
		}
	}
	equivalent := map[string]Claim{}
	for _, claim := range claimFile.Claims {
		if claim.Status != "active" || blocked[claim.ID] {
			continue
		}
		if scope != "" && claim.Scope != "global" && claim.Scope != scope {
			continue
		}
		key := normalize(claim.Subject) + "\x00" + normalize(claim.Predicate) + "\x00" + normalize(claim.Value) + "\x00" + normalize(claim.Scope) + "\x00" + claim.ValidFrom + "\x00" + claim.ValidUntil
		current, exists := equivalent[key]
		if !exists || claim.ID < current.ID {
			claim.SourceIDs = uniqueSorted(append(claim.SourceIDs, current.SourceIDs...))
			claim.Supersedes = uniqueSorted(append(claim.Supersedes, current.Supersedes...))
			equivalent[key] = claim
		} else {
			current.SourceIDs = uniqueSorted(append(current.SourceIDs, claim.SourceIDs...))
			current.Supersedes = uniqueSorted(append(current.Supersedes, claim.Supersedes...))
			equivalent[key] = current
		}
	}
	result := make([]Claim, 0, len(equivalent))
	for _, claim := range equivalent {
		result = append(result, claim)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
	return result, nil
}

func EncodeContext(claims []Claim) ([]byte, error) {
	return json.MarshalIndent(struct {
		Version int     `json:"version"`
		Claims  []Claim `json:"claims"`
	}{Version: 1, Claims: claims}, "", "  ")
}

func uniqueSorted(values []string) []string {
	seen := map[string]bool{}
	result := make([]string, 0, len(values))
	for _, value := range values {
		if !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	sort.Strings(result)
	return result
}

func Watch(repo string, interval time.Duration, onScan func(ScanResult)) error {
	if interval <= 0 {
		return fmt.Errorf("watch interval must be positive")
	}
	for {
		result, err := Scan(repo)
		if err != nil {
			return err
		}
		onScan(result)
		time.Sleep(interval)
	}
}
