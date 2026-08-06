package skills

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/woonyong-kr/woon-core/internal/registry"
	"gopkg.in/yaml.v3"
)

type profile struct {
	Version   int      `yaml:"version"`
	Name      string   `yaml:"name"`
	Extends   []string `yaml:"extends,omitempty"`
	MaxActive int      `yaml:"max_active"`
	Skills    []string `yaml:"skills"`
}

type conflictFile struct {
	Version int             `yaml:"version"`
	Groups  []conflictGroup `yaml:"groups"`
}

type conflictGroup struct {
	ID             string   `yaml:"id"`
	Mode           string   `yaml:"mode"`
	Members        []string `yaml:"members"`
	Preferred      string   `yaml:"preferred,omitempty"`
	RequiredOption string   `yaml:"required_option,omitempty"`
}

type effectsFile struct {
	Version int                 `yaml:"version"`
	Default []string            `yaml:"default"`
	Skills  map[string][]string `yaml:"skills"`
}

type sourcesFile struct {
	Version int                     `yaml:"version"`
	Origins map[string]sourceOrigin `yaml:"origins"`
}

type sourceOrigin struct {
	Path         string `yaml:"path"`
	Policy       string `yaml:"policy,omitempty"`
	Upstream     string `yaml:"upstream,omitempty"`
	Commit       string `yaml:"commit,omitempty"`
	License      string `yaml:"license,omitempty"`
	UpdatePolicy string `yaml:"update_policy,omitempty"`
}

type frontmatter struct {
	Name        string `yaml:"name"`
	Description string `yaml:"description"`
	License     string `yaml:"license,omitempty"`
	Origin      string `yaml:"origin,omitempty"`
}

type CatalogSkill struct {
	Reference   string
	Name        string
	Description string
	Path        string
	Hash        string
	Effects     []string
}

type resolved struct {
	RepoPath string
	Profiles []string
	Skills   []CatalogSkill
}

var allowedEffects = map[string]bool{
	"read": true, "write": true, "process": true, "network": true,
	"commit": true, "push": true, "merge": true, "release": true, "delete": true,
}

func loadResolved(root string, reg registry.Registry, requested []string) (resolved, error) {
	repoPath, err := reg.Resolve(root, "skills")
	if err != nil {
		return resolved{}, err
	}
	if len(requested) == 0 {
		requested = []string{"core"}
	}
	profileNames, references, maxActive, err := resolveProfiles(repoPath, requested)
	if err != nil {
		return resolved{}, err
	}
	if len(references) > maxActive {
		return resolved{}, fmt.Errorf("resolved profile has %d skills, budget is %d", len(references), maxActive)
	}
	effects, err := loadYAML[effectsFile](filepath.Join(repoPath, "conflicts", "effects.yaml"))
	if err != nil {
		return resolved{}, err
	}
	if effects.Version != 1 {
		return resolved{}, fmt.Errorf("unsupported effects schema version %d", effects.Version)
	}
	for reference, declared := range effects.Skills {
		if err := validateEffects(reference, declared); err != nil {
			return resolved{}, err
		}
	}
	if err := validateEffects("default", effects.Default); err != nil {
		return resolved{}, err
	}
	var catalog []CatalogSkill
	seenNames := map[string]string{}
	for _, reference := range references {
		path, err := safeSkillPath(repoPath, reference)
		if err != nil {
			return resolved{}, err
		}
		metadata, err := readFrontmatter(filepath.Join(path, "SKILL.md"))
		if err != nil {
			return resolved{}, fmt.Errorf("%s: %w", reference, err)
		}
		if metadata.Name != filepath.Base(reference) {
			return resolved{}, fmt.Errorf("%s frontmatter name %q does not match directory", reference, metadata.Name)
		}
		if countRunes(metadata.Description) > 180 {
			return resolved{}, fmt.Errorf("%s description exceeds 180 characters", reference)
		}
		if previous, duplicate := seenNames[metadata.Name]; duplicate {
			return resolved{}, fmt.Errorf("duplicate active skill name %q: %s and %s", metadata.Name, previous, reference)
		}
		seenNames[metadata.Name] = reference
		hash, err := hashDirectory(path)
		if err != nil {
			return resolved{}, err
		}
		declaredEffects := effects.Default
		if override, ok := effects.Skills[reference]; ok {
			declaredEffects = override
		}
		catalog = append(catalog, CatalogSkill{Reference: reference, Name: metadata.Name, Description: metadata.Description, Path: path, Hash: hash, Effects: append([]string(nil), declaredEffects...)})
	}
	if err := validateConflicts(repoPath, references, map[string]string{}); err != nil {
		return resolved{}, err
	}
	return resolved{RepoPath: repoPath, Profiles: profileNames, Skills: catalog}, nil
}

func resolveProfiles(repoPath string, requested []string) ([]string, []string, int, error) {
	profiles := map[string]profile{}
	visiting := map[string]bool{}
	selectedProfiles := map[string]bool{}
	selectedSkills := map[string]bool{}
	maxActive := 1 << 30
	var visit func(string) error
	visit = func(name string) error {
		if visiting[name] {
			return fmt.Errorf("profile cycle at %q", name)
		}
		if selectedProfiles[name] {
			return nil
		}
		item, ok := profiles[name]
		if !ok {
			loaded, err := loadYAML[profile](filepath.Join(repoPath, "profiles", name+".yaml"))
			if err != nil {
				return err
			}
			if loaded.Version != 1 || loaded.Name != name || loaded.MaxActive <= 0 {
				return fmt.Errorf("invalid profile %q", name)
			}
			profiles[name] = loaded
			item = loaded
		}
		visiting[name] = true
		for _, parent := range item.Extends {
			if err := visit(parent); err != nil {
				return err
			}
		}
		delete(visiting, name)
		selectedProfiles[name] = true
		if item.MaxActive < maxActive {
			maxActive = item.MaxActive
		}
		for _, reference := range item.Skills {
			selectedSkills[reference] = true
		}
		return nil
	}
	for _, name := range requested {
		if err := visit(name); err != nil {
			return nil, nil, 0, err
		}
	}
	profileNames := sortedKeys(selectedProfiles)
	references := sortedKeys(selectedSkills)
	return profileNames, references, maxActive, nil
}

func validateConflicts(repoPath string, active []string, options map[string]string) error {
	conflicts, err := loadYAML[conflictFile](filepath.Join(repoPath, "conflicts", "conflicts.yaml"))
	if err != nil {
		return err
	}
	if conflicts.Version != 1 {
		return fmt.Errorf("unsupported conflicts schema version %d", conflicts.Version)
	}
	activeSet := map[string]bool{}
	for _, reference := range active {
		activeSet[reference] = true
	}
	for _, group := range conflicts.Groups {
		var matched []string
		for _, member := range group.Members {
			if activeSet[member] {
				matched = append(matched, member)
			}
		}
		if len(matched) < 2 {
			continue
		}
		switch group.Mode {
		case "exclusive":
			return fmt.Errorf("profile conflict %q: %s", group.ID, strings.Join(matched, ", "))
		case "explicit-policy":
			if options[group.RequiredOption] == "" {
				return fmt.Errorf("profile conflict %q requires option %q", group.ID, group.RequiredOption)
			}
		default:
			return fmt.Errorf("unknown conflict mode %q", group.Mode)
		}
	}
	return nil
}

func validateSources(repoPath string) error {
	sources, err := loadYAML[sourcesFile](filepath.Join(repoPath, "lock", "sources.yaml"))
	if err != nil {
		return err
	}
	if sources.Version != 1 {
		return fmt.Errorf("unsupported sources schema version %d", sources.Version)
	}
	for name, origin := range sources.Origins {
		if filepath.IsAbs(origin.Path) || strings.Contains(filepath.ToSlash(origin.Path), "../") {
			return fmt.Errorf("origin %q has unsafe path", name)
		}
		if _, err := os.Stat(filepath.Join(repoPath, origin.Path)); err != nil {
			return fmt.Errorf("origin %q: %w", name, err)
		}
		if origin.Upstream != "" && (len(origin.Commit) != 40 || origin.UpdatePolicy != "review-pr") {
			return fmt.Errorf("vendor origin %q requires a commit lock and review-pr policy", name)
		}
	}
	return nil
}

func readFrontmatter(path string) (frontmatter, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return frontmatter{}, err
	}
	if !bytes.HasPrefix(data, []byte("---\n")) && !bytes.HasPrefix(data, []byte("---\r\n")) {
		return frontmatter{}, fmt.Errorf("missing YAML frontmatter")
	}
	normalized := bytes.ReplaceAll(data, []byte("\r\n"), []byte("\n"))
	end := bytes.Index(normalized[4:], []byte("\n---\n"))
	if end < 0 {
		return frontmatter{}, fmt.Errorf("unterminated YAML frontmatter")
	}
	var metadata frontmatter
	if err := yaml.Unmarshal(normalized[4:4+end], &metadata); err != nil {
		return frontmatter{}, fmt.Errorf("parse frontmatter: %w", err)
	}
	if metadata.Name == "" || strings.TrimSpace(metadata.Description) == "" {
		return frontmatter{}, fmt.Errorf("frontmatter requires name and description")
	}
	return metadata, nil
}

func safeSkillPath(repoPath, reference string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(reference))
	if filepath.IsAbs(clean) || clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("unsafe skill reference %q", reference)
	}
	path := filepath.Join(repoPath, clean)
	if _, err := os.Stat(filepath.Join(path, "SKILL.md")); err != nil {
		return "", fmt.Errorf("skill %q: %w", reference, err)
	}
	return path, nil
}

func hashDirectory(root string) (string, error) {
	hasher := sha256.New()
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if entry.Name() == "__pycache__" || entry.Name() == ".git" {
				return filepath.SkipDir
			}
			return nil
		}
		relative, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		hasher.Write([]byte(filepath.ToSlash(relative)))
		hasher.Write([]byte{0})
		hasher.Write(data)
		hasher.Write([]byte{0})
		return nil
	})
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

func validateEffects(reference string, effects []string) error {
	seen := map[string]bool{}
	for _, effect := range effects {
		if !allowedEffects[effect] {
			return fmt.Errorf("%s declares unknown side effect %q", reference, effect)
		}
		if seen[effect] {
			return fmt.Errorf("%s declares duplicate side effect %q", reference, effect)
		}
		seen[effect] = true
	}
	return nil
}

func loadYAML[T any](path string) (T, error) {
	var value T
	data, err := os.ReadFile(path)
	if err != nil {
		return value, fmt.Errorf("read %s: %w", path, err)
	}
	decoder := yaml.NewDecoder(bytes.NewReader(data))
	decoder.KnownFields(true)
	if err := decoder.Decode(&value); err != nil {
		return value, fmt.Errorf("parse %s: %w", path, err)
	}
	return value, nil
}

func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func countRunes(value string) int {
	return len([]rune(strings.TrimSpace(value)))
}
