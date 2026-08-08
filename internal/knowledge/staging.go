package knowledge

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
)

type StageResult struct {
	StagedFiles       int
	LFSFiles          int
	BlockedLargeFiles []string
}

// StageKnowledgeChanges stages only catalog-approved inputs and generated
// lineage files. A quarantined or raw secret source is never passed to git add.
func StageKnowledgeChanges(ctx context.Context, repo string) (StageResult, error) {
	var result StageResult
	if ctx == nil {
		return result, errors.New("context is required")
	}
	if output, err := runGit(ctx, repo, "diff", "--cached", "--name-only"); err != nil {
		return result, err
	} else if strings.TrimSpace(output) != "" {
		return result, errors.New("refusing to reuse an existing Git staging area")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return result, err
	}
	catalog, err := loadCatalog(repo, cfg)
	if err != nil {
		return result, err
	}
	paths := map[string]bool{}
	for _, generated := range []string{cfg.CatalogPath, cfg.ReviewPath, cfg.ClaimsPath} {
		paths[generated] = true
	}
	for _, artifact := range catalog.Artifacts {
		paths[artifact.Path] = true
	}
	threshold := cfg.Ingestion.SizePolicy.RegularGitMaxMiB * 1024 * 1024
	for _, source := range catalog.Sources {
		if source.State == "sanitized" && source.SanitizedPath != "" {
			paths[source.SanitizedPath] = true
		}
		if source.State != "active" && source.State != "missing" && source.State != "retracted" {
			continue
		}
		for _, relative := range source.Paths {
			absolute, pathErr := safePath(repo, relative)
			if pathErr != nil {
				return result, pathErr
			}
			info, statErr := os.Stat(absolute)
			if errors.Is(statErr, os.ErrNotExist) {
				if trackedPath(ctx, repo, relative) {
					paths[relative] = true
				}
				continue
			}
			if statErr != nil {
				return result, statErr
			}
			if info.Size() > threshold {
				if err := ensureLFSTracked(ctx, repo, relative); err != nil {
					result.BlockedLargeFiles = append(result.BlockedLargeFiles, relative)
					continue
				}
				result.LFSFiles++
				paths[".gitattributes"] = true
			}
			paths[relative] = true
		}
	}
	ordered := make([]string, 0, len(paths))
	for relative := range paths {
		if _, pathErr := safePath(repo, relative); pathErr != nil {
			return result, pathErr
		}
		absolute := filepath.Join(repo, filepath.FromSlash(relative))
		if _, statErr := os.Stat(absolute); statErr == nil || trackedPath(ctx, repo, relative) {
			ordered = append(ordered, relative)
		}
	}
	sort.Strings(ordered)
	for start := 0; start < len(ordered); start += 100 {
		end := start + 100
		if end > len(ordered) {
			end = len(ordered)
		}
		args := append([]string{"add", "--"}, ordered[start:end]...)
		if _, err := runGit(ctx, repo, args...); err != nil {
			return result, err
		}
	}
	output, err := runGit(ctx, repo, "diff", "--cached", "--name-only")
	if err != nil {
		return result, err
	}
	if value := strings.TrimSpace(output); value != "" {
		result.StagedFiles = len(strings.Split(value, "\n"))
	}
	sort.Strings(result.BlockedLargeFiles)
	return result, nil
}

func ensureLFSTracked(ctx context.Context, repo, relative string) error {
	if _, err := exec.LookPath("git-lfs"); err != nil {
		return fmt.Errorf("git-lfs is unavailable: %w", err)
	}
	if _, err := runGit(ctx, repo, "lfs", "install", "--local"); err != nil {
		return err
	}
	if _, err := runGit(ctx, repo, "lfs", "track", "--filename", relative); err != nil {
		return err
	}
	output, err := runGit(ctx, repo, "check-attr", "filter", "--", relative)
	if err != nil {
		return err
	}
	if !strings.HasSuffix(strings.TrimSpace(output), ": lfs") {
		return fmt.Errorf("Git LFS attribute was not applied to %s", relative)
	}
	return nil
}

func trackedPath(ctx context.Context, repo, relative string) bool {
	cmd := exec.CommandContext(ctx, "git", "-C", repo, "ls-files", "--error-unmatch", "--", relative)
	return cmd.Run() == nil
}

func runGit(ctx context.Context, repo string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", repo}, args...)...)
	var output boundedBuffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s: %w: %s", strings.Join(args, " "), err, output.String())
	}
	return output.String(), nil
}
