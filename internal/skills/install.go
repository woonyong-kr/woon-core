package skills

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/woonyong-kr/woon-core/internal/registry"
)

const installManifestName = ".woon-installed.json"

type installManifest struct {
	Version  int               `json:"version"`
	Profiles []string          `json:"profiles"`
	Skills   map[string]string `json:"skills"`
}

type PlanItem struct {
	Name    string
	Source  string
	Hash    string
	Effects []string
	Action  string
}

type PlanResult struct {
	Profiles []string
	Items    []PlanItem
	Target   string
}

type InstallResult struct {
	Installed int
	Updated   int
	Retired   int
	Unchanged int
	Target    string
	Backup    string
}

type movedSkill struct {
	live   string
	backup string
}

func Validate(root string, reg registry.Registry, profiles []string) (PlanResult, error) {
	resolved, err := loadResolved(root, reg, profiles)
	if err != nil {
		return PlanResult{}, err
	}
	if err := validateSources(resolved.RepoPath); err != nil {
		return PlanResult{}, err
	}
	if err := validateCatalog(resolved.RepoPath); err != nil {
		return PlanResult{}, err
	}
	result := PlanResult{Profiles: resolved.Profiles}
	for _, skill := range resolved.Skills {
		result.Items = append(result.Items, PlanItem{Name: skill.Name, Source: skill.Reference, Hash: skill.Hash, Effects: skill.Effects, Action: "selected"})
	}
	return result, nil
}

func Plan(root string, reg registry.Registry, profiles []string, targetName string) (PlanResult, error) {
	resolved, err := loadResolved(root, reg, profiles)
	if err != nil {
		return PlanResult{}, err
	}
	if err := validateSources(resolved.RepoPath); err != nil {
		return PlanResult{}, err
	}
	target := ""
	manifest := installManifest{Skills: map[string]string{}}
	if targetName != "" {
		target, err = targetPath(targetName)
		if err != nil {
			return PlanResult{}, err
		}
		manifest, err = readInstallManifest(target)
		if err != nil {
			return PlanResult{}, err
		}
	}
	result := PlanResult{Profiles: resolved.Profiles, Target: target}
	selected := map[string]bool{}
	for _, skill := range resolved.Skills {
		action := "selected"
		if target != "" {
			destination := filepath.Join(target, skill.Name)
			lockedHash, managed := manifest.Skills[skill.Name]
			actualHash, exists, hashErr := installedHash(destination)
			if hashErr != nil {
				return PlanResult{}, hashErr
			}
			switch {
			case !managed && exists:
				action = "blocked"
			case !managed:
				action = "install"
			case !exists:
				action = "repair"
			case lockedHash != skill.Hash || actualHash != skill.Hash:
				action = "update"
			default:
				action = "unchanged"
			}
		}
		selected[skill.Name] = true
		result.Items = append(result.Items, PlanItem{Name: skill.Name, Source: skill.Reference, Hash: skill.Hash, Effects: skill.Effects, Action: action})
	}
	for name, hash := range manifest.Skills {
		if !selected[name] {
			action := "retire"
			if _, exists, err := installedHash(filepath.Join(target, name)); err != nil {
				return PlanResult{}, err
			} else if !exists {
				action = "forget"
			}
			result.Items = append(result.Items, PlanItem{Name: name, Hash: hash, Action: action})
		}
	}
	sort.Slice(result.Items, func(i, j int) bool { return result.Items[i].Name < result.Items[j].Name })
	return result, nil
}

func Install(root string, reg registry.Registry, profiles []string, targetName string) (InstallResult, error) {
	if targetName == "" {
		return InstallResult{}, fmt.Errorf("skills install requires --target codex or claude")
	}
	resolved, err := loadResolved(root, reg, profiles)
	if err != nil {
		return InstallResult{}, err
	}
	plan, err := Plan(root, reg, profiles, targetName)
	if err != nil {
		return InstallResult{}, err
	}
	if err := os.MkdirAll(plan.Target, 0o755); err != nil {
		return InstallResult{}, err
	}
	for _, item := range plan.Items {
		if item.Action == "blocked" {
			return InstallResult{}, fmt.Errorf("refusing to overwrite unmanaged skill %s", filepath.Join(plan.Target, item.Name))
		}
	}
	staging, err := os.MkdirTemp(plan.Target, ".woon-staging-*")
	if err != nil {
		return InstallResult{}, err
	}
	defer os.RemoveAll(staging)
	backup := filepath.Join(plan.Target, ".woon-backups", time.Now().UTC().Format("20060102T150405.000000000Z"))
	byName := map[string]CatalogSkill{}
	for _, skill := range resolved.Skills {
		byName[skill.Name] = skill
	}
	for _, item := range plan.Items {
		if item.Action == "install" || item.Action == "update" || item.Action == "repair" {
			if err := copyDirectoryContents(byName[item.Name].Path, filepath.Join(staging, item.Name)); err != nil {
				return InstallResult{}, err
			}
		}
	}
	manifestPath := filepath.Join(plan.Target, installManifestName)
	previousManifest, manifestErr := os.ReadFile(manifestPath)
	manifestExisted := manifestErr == nil
	if manifestErr != nil && !errors.Is(manifestErr, os.ErrNotExist) {
		return InstallResult{}, manifestErr
	}
	var moved []movedSkill
	var installed []string
	rollback := func(cause error) error {
		var rollbackErrors []error
		for index := len(installed) - 1; index >= 0; index-- {
			if err := os.RemoveAll(installed[index]); err != nil {
				rollbackErrors = append(rollbackErrors, err)
			}
		}
		for index := len(moved) - 1; index >= 0; index-- {
			if err := os.Rename(moved[index].backup, moved[index].live); err != nil {
				rollbackErrors = append(rollbackErrors, err)
			}
		}
		if manifestExisted {
			if err := atomicWrite(manifestPath, previousManifest); err != nil {
				rollbackErrors = append(rollbackErrors, err)
			}
		} else if err := os.Remove(manifestPath); err != nil && !errors.Is(err, os.ErrNotExist) {
			rollbackErrors = append(rollbackErrors, err)
		}
		return errors.Join(append([]error{cause}, rollbackErrors...)...)
	}
	result := InstallResult{Target: plan.Target}
	for _, item := range plan.Items {
		destination := filepath.Join(plan.Target, item.Name)
		switch item.Action {
		case "unchanged":
			result.Unchanged++
		case "install", "update", "repair":
			if item.Action == "update" {
				backupPath := filepath.Join(backup, item.Name)
				if err := moveToBackup(destination, backupPath); err != nil {
					return result, rollback(err)
				}
				moved = append(moved, movedSkill{live: destination, backup: backupPath})
				result.Updated++
			} else if item.Action == "install" {
				result.Installed++
			} else {
				result.Updated++
			}
			if err := os.Rename(filepath.Join(staging, item.Name), destination); err != nil {
				return result, rollback(err)
			}
			installed = append(installed, destination)
		case "retire":
			backupPath := filepath.Join(backup, item.Name)
			if err := moveToBackup(destination, backupPath); err != nil {
				return result, rollback(err)
			}
			moved = append(moved, movedSkill{live: destination, backup: backupPath})
			result.Retired++
		case "forget":
			result.Retired++
		}
	}
	manifest := installManifest{Version: 1, Profiles: resolved.Profiles, Skills: map[string]string{}}
	for _, skill := range resolved.Skills {
		manifest.Skills[skill.Name] = skill.Hash
	}
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return result, err
	}
	if err := atomicWrite(manifestPath, append(data, '\n')); err != nil {
		return result, rollback(err)
	}
	if len(moved) > 0 {
		result.Backup = backup
	}
	return result, nil
}

func Doctor() (map[string]string, error) {
	result := map[string]string{}
	for _, target := range []string{"codex", "claude"} {
		path, err := targetPath(target)
		if err != nil {
			return nil, err
		}
		result[target] = path
	}
	return result, nil
}

func targetPath(target string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	switch target {
	case "codex":
		if override := os.Getenv("WOON_CODEX_SKILLS_HOME"); override != "" {
			return filepath.Abs(override)
		}
		return filepath.Join(home, ".codex", "skills"), nil
	case "claude":
		if override := os.Getenv("WOON_CLAUDE_SKILLS_HOME"); override != "" {
			return filepath.Abs(override)
		}
		return filepath.Join(home, ".claude", "skills"), nil
	default:
		return "", fmt.Errorf("unknown skills target %q", target)
	}
}

func readInstallManifest(target string) (installManifest, error) {
	manifest := installManifest{Version: 1, Skills: map[string]string{}}
	data, err := os.ReadFile(filepath.Join(target, installManifestName))
	if errors.Is(err, os.ErrNotExist) {
		return manifest, nil
	}
	if err != nil {
		return manifest, err
	}
	if err := json.Unmarshal(data, &manifest); err != nil {
		return manifest, err
	}
	if manifest.Version != 1 || manifest.Skills == nil {
		return manifest, fmt.Errorf("invalid install manifest in %s", target)
	}
	return manifest, nil
}

func copyDirectoryContents(source, destination string) error {
	return filepath.WalkDir(source, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		relative, err := filepath.Rel(source, path)
		if err != nil {
			return err
		}
		if relative == "." {
			return nil
		}
		if entry.IsDir() && (entry.Name() == ".git" || entry.Name() == "__pycache__") {
			return filepath.SkipDir
		}
		target := filepath.Join(destination, relative)
		if entry.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		mode := fs.FileMode(0o644)
		if info, err := entry.Info(); err == nil && info.Mode()&0o111 != 0 {
			mode = 0o755
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		return os.WriteFile(target, data, mode)
	})
}

func installedHash(path string) (string, bool, error) {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", true, fmt.Errorf("installed skill path is not a directory: %s", path)
	}
	hash, err := hashDirectory(path)
	return hash, true, err
}

func moveToBackup(source, destination string) error {
	if _, err := os.Stat(source); errors.Is(err, os.ErrNotExist) {
		return nil
	} else if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0o755); err != nil {
		return err
	}
	return os.Rename(source, destination)
}

func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".woon-manifest-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryPath, path)
}
