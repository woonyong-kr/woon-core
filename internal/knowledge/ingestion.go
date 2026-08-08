package knowledge

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	gitignore "github.com/sabhiram/go-gitignore"
)

type knowledgeIgnore struct {
	matcher *gitignore.GitIgnore
}

func loadKnowledgeIgnore(repo string, cfg Config) (knowledgeIgnore, error) {
	path, err := safePath(repo, cfg.Ingestion.IgnoreFile)
	if err != nil {
		return knowledgeIgnore{}, err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return knowledgeIgnore{}, nil
	}
	if err != nil {
		return knowledgeIgnore{}, fmt.Errorf("read knowledge ignore file: %w", err)
	}
	lines := strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n")
	return knowledgeIgnore{matcher: gitignore.CompileIgnoreLines(lines...)}, nil
}

func (i knowledgeIgnore) matches(relative string) bool {
	return i.matcher != nil && i.matcher.MatchesPath(filepath.ToSlash(relative))
}

type fileSnapshot struct {
	Path             string
	Size             int64
	ModifiedUnixNano int64
}

type stabilityCache struct {
	Files []fileSnapshot `json:"files"`
}

// WaitForStableSources waits for the complete inbox tree, not only one file,
// to remain unchanged. It returns immediately when stability is disabled or
// the inbox is empty.
func WaitForStableSources(ctx context.Context, repo string, cfg Config) error {
	policy := cfg.Ingestion.Stability
	if policy.QuietSeconds == 0 {
		return nil
	}
	previous, err := snapshotInbox(repo, cfg)
	if err != nil {
		return err
	}
	if len(previous) == 0 {
		return nil
	}
	cachePath := filepath.Join(repo, ".knowledge-runtime", "stability.json")
	var cache stabilityCache
	if err := readJSONIfExists(cachePath, &cache); err != nil {
		return err
	}
	if reflect.DeepEqual(cache.Files, previous) {
		return nil
	}
	equalChecks := 0
	lastChanged := time.Now()
	started := time.Now()
	ticker := time.NewTicker(time.Duration(policy.CheckIntervalSeconds) * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			current, err := snapshotInbox(repo, cfg)
			if err != nil {
				return err
			}
			if reflect.DeepEqual(previous, current) {
				equalChecks++
			} else {
				previous = current
				equalChecks = 0
				lastChanged = time.Now()
			}
			quiet := time.Since(lastChanged) >= time.Duration(policy.QuietSeconds)*time.Second
			if equalChecks >= policy.RequiredEqualChecks && quiet {
				return writeJSON(cachePath, stabilityCache{Files: current})
			}
			if policy.MaxWaitSeconds > 0 && time.Since(started) >= time.Duration(policy.MaxWaitSeconds)*time.Second {
				return fmt.Errorf("inbox did not stabilize within %d seconds", policy.MaxWaitSeconds)
			}
		}
	}
}

func snapshotInbox(repo string, cfg Config) ([]fileSnapshot, error) {
	ignore, err := loadKnowledgeIgnore(repo, cfg)
	if err != nil {
		return nil, err
	}
	ignoredNames := make(map[string]bool, len(cfg.IgnoreNames))
	for _, name := range cfg.IgnoreNames {
		ignoredNames[name] = true
	}
	var snapshots []fileSnapshot
	for _, relativeRoot := range cfg.InboxRoots {
		root, err := safePath(repo, relativeRoot)
		if err != nil {
			return nil, err
		}
		if err := os.MkdirAll(root, 0o755); err != nil {
			return nil, err
		}
		err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
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
			if ignoredNames[entry.Name()] || ignore.matches(relativeToRoot) {
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
			snapshots = append(snapshots, fileSnapshot{Path: filepath.ToSlash(relative), Size: info.Size(), ModifiedUnixNano: info.ModTime().UnixNano()})
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Slice(snapshots, func(i, j int) bool { return snapshots[i].Path < snapshots[j].Path })
	return snapshots, nil
}
