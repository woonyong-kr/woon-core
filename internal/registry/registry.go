package registry

import (
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

const registryRelativePath = "woon-core/registry/repositories.yaml"

type Repository struct {
	Remote    string `yaml:"remote"`
	Directory string `yaml:"directory"`
	Role      string `yaml:"role,omitempty"`
	Output    bool   `yaml:"output,omitempty"`
}

type Registry struct {
	Version      int                   `yaml:"version"`
	Repositories map[string]Repository `yaml:"repositories"`
}

type SyncResult struct {
	Cloned   int
	Existing int
}

func Load(root string) (Registry, error) {
	path := filepath.Join(root, filepath.FromSlash(registryRelativePath))
	data, err := os.ReadFile(path)
	if err != nil {
		return Registry{}, fmt.Errorf("read registry %s: %w", path, err)
	}
	var reg Registry
	if err := yaml.Unmarshal(data, &reg); err != nil {
		return Registry{}, fmt.Errorf("parse registry: %w", err)
	}
	if err := reg.Validate(); err != nil {
		return Registry{}, err
	}
	return reg, nil
}

func (r Registry) Validate() error {
	if r.Version != 1 {
		return fmt.Errorf("unsupported registry version %d", r.Version)
	}
	seen := map[string]string{}
	for id, repo := range r.Repositories {
		if id == "" || repo.Directory == "" || repo.Remote == "" {
			return fmt.Errorf("repository %q requires remote and directory", id)
		}
		if filepath.IsAbs(repo.Directory) || filepath.Clean(repo.Directory) != repo.Directory || strings.Contains(repo.Directory, "..") {
			return fmt.Errorf("repository %q has unsafe directory %q", id, repo.Directory)
		}
		if previous, ok := seen[repo.Directory]; ok {
			return fmt.Errorf("repositories %q and %q share directory %q", previous, id, repo.Directory)
		}
		seen[repo.Directory] = id
		parsed, err := url.Parse(repo.Remote)
		if err != nil || parsed.Scheme != "https" || parsed.Host != "github.com" {
			return fmt.Errorf("repository %q has unsupported remote %q", id, repo.Remote)
		}
	}
	return nil
}

func (r Registry) Resolve(root, reference string) (string, error) {
	id, relative, err := parseReference(reference)
	if err != nil {
		return "", err
	}
	repo, ok := r.Repositories[id]
	if !ok {
		return "", fmt.Errorf("unknown repository %q", id)
	}
	base := filepath.Join(root, repo.Directory)
	resolved := filepath.Join(base, filepath.FromSlash(relative))
	if resolved != base && !strings.HasPrefix(resolved, base+string(filepath.Separator)) {
		return "", fmt.Errorf("reference escapes repository %q", id)
	}
	return resolved, nil
}

func (r Registry) Missing(root string) []string {
	var missing []string
	for id, repo := range r.Repositories {
		if _, err := os.Stat(filepath.Join(root, repo.Directory)); errors.Is(err, os.ErrNotExist) {
			missing = append(missing, id)
		}
	}
	sort.Strings(missing)
	return missing
}

func (r Registry) Sync(root string) (SyncResult, error) {
	var result SyncResult
	ids := make([]string, 0, len(r.Repositories))
	for id := range r.Repositories {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		repo := r.Repositories[id]
		target := filepath.Join(root, repo.Directory)
		if _, err := os.Stat(target); err == nil {
			if _, err := os.Stat(filepath.Join(target, ".git")); err != nil {
				return result, fmt.Errorf("%s exists but is not a Git checkout", target)
			}
			result.Existing++
			continue
		} else if !errors.Is(err, os.ErrNotExist) {
			return result, err
		}
		cmd := exec.Command("git", "clone", "--", repo.Remote, target)
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			return result, fmt.Errorf("clone %s: %w", id, err)
		}
		result.Cloned++
	}
	return result, nil
}

func parseReference(reference string) (string, string, error) {
	if !strings.HasPrefix(reference, "repo://") {
		if strings.Contains(reference, "/") || reference == "" {
			return "", "", fmt.Errorf("invalid repository ID %q", reference)
		}
		return reference, "", nil
	}
	rest := strings.TrimPrefix(reference, "repo://")
	parts := strings.SplitN(rest, "/", 2)
	if parts[0] == "" {
		return "", "", fmt.Errorf("repo URI requires an ID")
	}
	relative := ""
	if len(parts) == 2 {
		relative = filepath.ToSlash(filepath.Clean(parts[1]))
		if relative == "." {
			relative = ""
		}
		if relative == ".." || strings.HasPrefix(relative, "../") {
			return "", "", fmt.Errorf("repo URI may not escape its repository")
		}
	}
	return parts[0], relative, nil
}
