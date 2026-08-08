package knowledge

import (
	"bytes"
	"fmt"
	"io/fs"
	"mime"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"
)

type ScanResult struct {
	Files       int
	Sources     int
	ReviewItems int
}

func Scan(repo string) (ScanResult, error) {
	cfg, err := LoadConfig(repo)
	if err != nil {
		return ScanResult{}, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return ScanResult{}, err
	}

	previous := make(map[string]Source, len(catalog.Sources))
	for _, source := range catalog.Sources {
		previous[source.ID] = source
	}
	found := make(map[string]Source)
	ignored := make(map[string]bool, len(cfg.IgnoreNames))
	for _, name := range cfg.IgnoreNames {
		ignored[name] = true
	}
	ignore, err := loadKnowledgeIgnore(repo, cfg)
	if err != nil {
		return ScanResult{}, err
	}
	var items []ReviewItem
	files := 0

	for _, relativeRoot := range cfg.InboxRoots {
		root, _ := safePath(repo, relativeRoot)
		if err := os.MkdirAll(root, 0o755); err != nil {
			return ScanResult{}, fmt.Errorf("create inbox %s: %w", root, err)
		}
		err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			if path == root {
				return nil
			}
			relativeToRoot, err := filepath.Rel(root, path)
			if err != nil {
				return err
			}
			if ignored[entry.Name()] || ignore.matches(relativeToRoot) {
				if entry.IsDir() {
					return filepath.SkipDir
				}
				return nil
			}
			if entry.IsDir() {
				return nil
			}
			info, err := entry.Info()
			if err != nil {
				return err
			}
			relative, err := filepath.Rel(repo, path)
			if err != nil {
				return err
			}
			relative = filepath.ToSlash(relative)
			if info.Mode()&os.ModeSymlink != 0 {
				items = append(items, newReview("unsupported-link", "심볼릭 링크는 원본으로 수집하지 않음", nil, []string{relative}, nil))
				return nil
			}
			if info.Size() > cfg.Ingestion.WholeFileScanMaxMiB*1024*1024 {
				analysis, analyzeErr := analyzeLargeSource(repo, path)
				if analyzeErr != nil {
					items = append(items, newReview("streaming-source-error", "대용량 원본을 streaming 검사하지 못해 해당 파일만 보류함", nil, []string{relative}, nil))
					return nil
				}
				if analysis.SanitizedTemp != "" {
					defer os.Remove(analysis.SanitizedTemp)
				}
				files++
				if recordErr := recordStreamedSource(repo, cfg, root, path, relative, analysis, previous, found); recordErr != nil {
					return recordErr
				}
				return nil
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return err
			}
			files++
			sha := digest(data)
			id := "src-" + sha
			source, exists := found[id]
			if !exists {
				source = Source{
					ID: id, SHA256: sha, NormalizedSHA256: normalizedDigest(data),
					MediaType: mime.TypeByExtension(strings.ToLower(filepath.Ext(path))), State: "active",
				}
				if old, ok := previous[id]; ok && old.State == "retracted" {
					source.State = old.State
					source.RetireReason = old.RetireReason
				}
			}
			source.Paths = append(source.Paths, relative)
			textSource := utf8.Valid(data) && bytes.IndexByte(data, 0) < 0
			if sanitized, safe := sanitizeSecrets(data); textSource && safe && len(sanitized.Findings) > 0 && source.State != "retracted" {
				source.Findings = append(source.Findings, sanitized.Findings...)
				source.State = "sanitized"
				source.RotationRequired = sanitized.RotationRequired
				source.SanitizedPath = filepath.ToSlash(filepath.Join(cfg.Secrets.SanitizedRoot, source.ID+".txt"))
				source.SanitizedSHA256 = digest(sanitized.Data)
				sanitizedPath, pathErr := safePath(repo, source.SanitizedPath)
				if pathErr != nil {
					return pathErr
				}
				if writeErr := writeAtomic(sanitizedPath, sanitized.Data); writeErr != nil {
					return fmt.Errorf("write sanitized source %s: %w", source.ID, writeErr)
				}
				if preserveErr := preserveQuarantinedSource(repo, cfg, source.ID, path, data); preserveErr != nil {
					return preserveErr
				}
			} else if textSource && !safe && source.State != "retracted" {
				source.State = "quarantined"
				if preserveErr := preserveQuarantinedSource(repo, cfg, source.ID, path, data); preserveErr != nil {
					return preserveErr
				}
			} else if !textSource && source.State != "retracted" {
				for _, candidate := range secretPatterns {
					if candidate.pattern.Match(data) {
						source.Findings = appendUnique(source.Findings, candidate.name)
					}
				}
				if len(source.Findings) > 0 {
					source.State = "quarantined"
					if preserveErr := preserveQuarantinedSource(repo, cfg, source.ID, path, data); preserveErr != nil {
						return preserveErr
					}
				}
			}
			hintPath, err := filepath.Rel(root, filepath.Dir(path))
			if err != nil {
				return err
			}
			if hintPath != "." {
				source.InputHints = appendUnique(source.InputHints, filepath.ToSlash(hintPath))
			}
			found[id] = source
			return nil
		})
		if err != nil {
			return ScanResult{}, fmt.Errorf("scan inbox %s: %w", relativeRoot, err)
		}
	}

	for id, old := range previous {
		if _, ok := found[id]; ok {
			continue
		}
		if old.State != "retracted" {
			old.State = "missing"
		}
		found[id] = old
	}

	catalog.Sources = make([]Source, 0, len(found))
	if catalog.Artifacts == nil {
		catalog.Artifacts = []Artifact{}
	}
	for _, source := range found {
		sort.Strings(source.Paths)
		sort.Strings(source.Findings)
		sort.Strings(source.InputHints)
		catalog.Sources = append(catalog.Sources, source)
		if len(source.Paths) > 1 {
			items = append(items, newReview("exact-duplicate", "동일한 bytes의 원본이 여러 경로에 있음", []string{source.ID}, source.Paths, nil))
		}
		if source.State == "sanitized" {
			items = append(items, newReview("secret-redacted", "비밀 값 후보를 원본에서 격리하고 안전한 정제본만 처리함; 실제 credential은 폐기·재발급해야 함", []string{source.ID}, append(append([]string(nil), source.Paths...), source.SanitizedPath), nil))
		}
		if source.State == "quarantined" {
			items = append(items, newReview("secret-detected", "안전하게 정제할 수 없어 해당 파일만 수집과 검색을 차단함", []string{source.ID}, source.Paths, nil))
		}
		if source.State == "missing" {
			paths := append([]string(nil), source.Paths...)
			for _, artifact := range catalog.Artifacts {
				if contains(artifact.SourceIDs, source.ID) {
					paths = append(paths, artifact.Path)
				}
			}
			items = append(items, newReview("source-missing", "원본이 사라져 연결된 가공물의 사용을 중지하고 삭제 여부를 검토해야 함", []string{source.ID}, paths, nil))
		}
		if source.State == "retracted" {
			paths := append([]string(nil), source.Paths...)
			for _, artifact := range catalog.Artifacts {
				if contains(artifact.SourceIDs, source.ID) {
					paths = append(paths, artifact.Path)
				}
			}
			items = append(items, newReview("source-retracted", "불필요하다고 표시한 원본과 연결된 가공물의 보존 또는 삭제를 검토해야 함", []string{source.ID}, paths, nil))
		}
		if source.NormalizedSHA256 == "" && source.State == "active" {
			items = append(items, newReview("unsupported-text-source", "UTF-8 텍스트가 아닌 원본은 자동 정제하지 않음", []string{source.ID}, source.Paths, nil))
		}
	}
	sort.Slice(catalog.Sources, func(i, j int) bool { return catalog.Sources[i].ID < catalog.Sources[j].ID })

	normalizedGroups := map[string][]Source{}
	for _, source := range catalog.Sources {
		if source.NormalizedSHA256 != "" && source.State != "missing" && source.State != "retracted" {
			normalizedGroups[source.NormalizedSHA256] = append(normalizedGroups[source.NormalizedSHA256], source)
		}
	}
	for _, group := range normalizedGroups {
		if len(group) < 2 {
			continue
		}
		ids, paths := []string{}, []string{}
		for _, source := range group {
			ids = append(ids, source.ID)
			paths = append(paths, source.Paths...)
		}
		items = append(items, newReview("normalized-duplicate", "공백과 줄바꿈을 제외하면 같은 원본 후보임", ids, paths, nil))
	}

	claims, err := loadClaims(repo, cfg)
	if err != nil {
		return ScanResult{}, err
	}
	items = append(items, evaluateClaims(claims.Claims, catalog.Sources)...)
	applyArtifactAvailability(&catalog)
	sort.Slice(catalog.Artifacts, func(i, j int) bool { return catalog.Artifacts[i].ID < catalog.Artifacts[j].ID })
	items = deduplicateReviews(items)

	catalogPath, _ := safePath(repo, cfg.CatalogPath)
	reviewPath, _ := safePath(repo, cfg.ReviewPath)
	if err := writeJSON(catalogPath, catalog); err != nil {
		return ScanResult{}, fmt.Errorf("write catalog: %w", err)
	}
	if err := writeJSON(reviewPath, Review{Version: 1, Items: items}); err != nil {
		return ScanResult{}, fmt.Errorf("write review: %w", err)
	}
	return ScanResult{Files: files, Sources: len(catalog.Sources), ReviewItems: len(items)}, nil
}

func recordStreamedSource(repo string, cfg Config, inboxRoot, path, relative string, analysis streamedSourceAnalysis, previous, found map[string]Source) error {
	id := "src-" + analysis.SHA256
	source, exists := found[id]
	if !exists {
		source = Source{
			ID: id, SHA256: analysis.SHA256, NormalizedSHA256: analysis.NormalizedSHA256,
			MediaType: mime.TypeByExtension(strings.ToLower(filepath.Ext(path))), State: "active",
		}
		if old, ok := previous[id]; ok && old.State == "retracted" {
			source.State = old.State
			source.RetireReason = old.RetireReason
		}
	}
	source.Paths = append(source.Paths, relative)
	if len(analysis.Findings) > 0 && source.State != "retracted" {
		source.Findings = append(source.Findings, analysis.Findings...)
		source.RotationRequired = analysis.RotationRequired
		quarantineRoot, err := safePath(repo, cfg.Secrets.QuarantineRoot)
		if err != nil {
			return err
		}
		if err := copyFilePrivate(path, filepath.Join(quarantineRoot, id, filepath.Base(path))); err != nil {
			return fmt.Errorf("preserve large quarantined source: %w", err)
		}
		if analysis.SanitizedTemp != "" {
			source.State = "sanitized"
			source.SanitizedPath = filepath.ToSlash(filepath.Join(cfg.Secrets.SanitizedRoot, id+".txt"))
			target, err := safePath(repo, source.SanitizedPath)
			if err != nil {
				return err
			}
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Rename(analysis.SanitizedTemp, target); err != nil {
				return err
			}
			if err := os.Chmod(target, 0o644); err != nil {
				return err
			}
			sanitizedHash, err := hashFile(target)
			if err != nil {
				return err
			}
			source.SanitizedSHA256 = sanitizedHash
		} else {
			source.State = "quarantined"
		}
	}
	hintPath, err := filepath.Rel(inboxRoot, filepath.Dir(path))
	if err != nil {
		return err
	}
	if hintPath != "." {
		source.InputHints = appendUnique(source.InputHints, filepath.ToSlash(hintPath))
	}
	found[id] = source
	return nil
}

func newReview(kind, summary string, sourceIDs, paths, claimIDs []string) ReviewItem {
	sort.Strings(sourceIDs)
	sort.Strings(paths)
	sort.Strings(claimIDs)
	parts := append(append(append([]string{}, sourceIDs...), paths...), claimIDs...)
	return ReviewItem{ID: stableID(kind, parts...), Kind: kind, Summary: summary, SourceIDs: sourceIDs, Paths: paths, ClaimIDs: claimIDs}
}

func deduplicateReviews(items []ReviewItem) []ReviewItem {
	byID := make(map[string]ReviewItem, len(items))
	for _, item := range items {
		byID[item.ID] = item
	}
	result := make([]ReviewItem, 0, len(byID))
	for _, item := range byID {
		result = append(result, item)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
	return result
}

func appendUnique(values []string, value string) []string {
	if !contains(values, value) {
		return append(values, value)
	}
	return values
}

func contains(values []string, value string) bool {
	for _, candidate := range values {
		if candidate == value {
			return true
		}
	}
	return false
}

func applyArtifactAvailability(catalog *Catalog) {
	states := make(map[string]string, len(catalog.Sources))
	for _, source := range catalog.Sources {
		states[source.ID] = source.State
	}
	for i := range catalog.Artifacts {
		if catalog.Artifacts[i].State == "retracted" {
			continue
		}
		available := true
		for _, sourceID := range catalog.Artifacts[i].SourceIDs {
			if states[sourceID] != "active" && states[sourceID] != "sanitized" {
				available = false
				break
			}
		}
		if available && catalog.Artifacts[i].State == "review-required" {
			catalog.Artifacts[i].State = "candidate"
		} else if !available {
			catalog.Artifacts[i].State = "review-required"
		}
	}
}
