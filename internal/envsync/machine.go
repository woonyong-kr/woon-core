package envsync

import (
	"bytes"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"time"

	"github.com/woonyong-kr/woon-core/internal/registry"
)

type installationConfig struct {
	Version int                           `yaml:"version"`
	Targets map[string]installationTarget `yaml:"targets"`
}

type installationTarget struct {
	ProcessPatterns        map[string][]string          `yaml:"process_patterns"`
	ConfigPaths            map[string][]string          `yaml:"config_paths"`
	RequireChild           string                       `yaml:"require_child,omitempty"`
	Files                  map[string]string            `yaml:"files"`
	PlatformFiles          map[string]map[string]string `yaml:"platform_files,omitempty"`
	ExtensionArtifact      string                       `yaml:"extension_artifact,omitempty"`
	ExtensionCommands      map[string]string            `yaml:"extension_commands,omitempty"`
	ExtensionListArgs      []string                     `yaml:"extension_list_args,omitempty"`
	ExtensionInstallArgs   []string                     `yaml:"extension_install_args,omitempty"`
	ExtensionUninstallArgs []string                     `yaml:"extension_uninstall_args,omitempty"`
}

type TargetStatus struct {
	Name             string
	Path             string
	Running          bool
	ExtensionCommand string
	CommandAvailable bool
}

type Operation struct {
	Kind        string
	Target      string
	Artifact    string
	Source      string
	Destination string
	Changed     bool
}

type PlanResult struct {
	Target     string
	Operations []Operation
	Changes    int
}

type ApplyResult struct {
	Target     string
	Applied    int
	BackupPath string
}

type discoveredTarget struct {
	name                   string
	path                   string
	processPatterns        []string
	files                  map[string]string
	extensionArtifact      string
	extensionCommand       string
	extensionListArgs      []string
	extensionInstallArgs   []string
	extensionUninstallArgs []string
}

type backupRecord struct {
	destination string
	backup      string
	existed     bool
}

type installedExtension struct {
	command       string
	uninstallArgs []string
	id            string
}

type activeKeymapDocument struct {
	Components []activeKeymapComponent `xml:"component"`
}

type activeKeymapComponent struct {
	Name   string            `xml:"name,attr"`
	Active activeKeymapEntry `xml:"active_keymap"`
}

type activeKeymapEntry struct {
	Name string `xml:"name,attr"`
}

func Doctor(root string, reg registry.Registry, target string) ([]TargetStatus, error) {
	discovered, err := discover(root, reg, target)
	if err != nil {
		return nil, err
	}
	statuses := make([]TargetStatus, 0, len(discovered))
	for _, item := range discovered {
		running, err := anyProcessRunning(item.processPatterns)
		if err != nil {
			return nil, err
		}
		available := true
		if item.extensionCommand != "" {
			_, err := exec.LookPath(item.extensionCommand)
			available = err == nil
		}
		statuses = append(statuses, TargetStatus{Name: item.name, Path: item.path, Running: running, ExtensionCommand: item.extensionCommand, CommandAvailable: available})
	}
	return statuses, nil
}

func Plan(root string, reg registry.Registry, target string) (PlanResult, error) {
	if _, err := Check(root, reg, target); err != nil {
		return PlanResult{}, fmt.Errorf("generated artifacts are not current: %w", err)
	}
	repoPath, err := reg.Resolve(root, "env")
	if err != nil {
		return PlanResult{}, err
	}
	discovered, err := discover(root, reg, target)
	if err != nil {
		return PlanResult{}, err
	}
	result := PlanResult{Target: target}
	for _, item := range discovered {
		for _, artifact := range sortedKeys(item.files) {
			source := filepath.Join(repoPath, "generated", target, filepath.FromSlash(artifact))
			destination := filepath.Join(item.path, filepath.FromSlash(item.files[artifact]))
			changed, err := differsSemantically(artifact, source, destination)
			if err != nil {
				return result, err
			}
			result.Operations = append(result.Operations, Operation{
				Kind: "file", Target: item.name, Artifact: artifact, Source: source, Destination: destination, Changed: changed,
			})
			if changed {
				result.Changes++
			}
		}
		if item.extensionArtifact != "" {
			if item.extensionCommand == "" {
				return result, fmt.Errorf("extension command is not configured for %s on %s", item.name, target)
			}
			installed, err := listExtensions(item.extensionCommand, item.extensionListArgs)
			if err != nil {
				return result, fmt.Errorf("list %s extensions: %w", item.name, err)
			}
			source := filepath.Join(repoPath, "generated", target, filepath.FromSlash(item.extensionArtifact))
			required, err := readLines(source)
			if err != nil {
				return result, err
			}
			for _, id := range required {
				changed := !installed[strings.ToLower(id)]
				result.Operations = append(result.Operations, Operation{Kind: "extension", Target: item.name, Artifact: item.extensionArtifact, Source: source, Destination: id, Changed: changed})
				if changed {
					result.Changes++
				}
			}
		}
	}
	sort.Slice(result.Operations, func(i, j int) bool {
		if result.Operations[i].Target == result.Operations[j].Target {
			if result.Operations[i].Kind == result.Operations[j].Kind {
				return result.Operations[i].Destination < result.Operations[j].Destination
			}
			return result.Operations[i].Kind < result.Operations[j].Kind
		}
		return result.Operations[i].Target < result.Operations[j].Target
	})
	return result, nil
}

func Apply(root string, reg registry.Registry, target string) (ApplyResult, error) {
	if target != runtimeTarget() {
		return ApplyResult{}, fmt.Errorf("apply target %q does not match current OS %q", target, runtimeTarget())
	}
	statuses, err := Doctor(root, reg, target)
	if err != nil {
		return ApplyResult{}, err
	}
	for _, status := range statuses {
		if status.Running {
			return ApplyResult{}, fmt.Errorf("%s is running; close the IDE before apply", status.Name)
		}
	}
	plan, err := Plan(root, reg, target)
	if err != nil {
		return ApplyResult{}, err
	}
	if plan.Changes == 0 {
		return ApplyResult{Target: target}, nil
	}
	repoPath, err := reg.Resolve(root, "env")
	if err != nil {
		return ApplyResult{}, err
	}
	backupRoot := filepath.Join(repoPath, "backups", time.Now().UTC().Format("20060102T150405.000000000Z"))
	var records []backupRecord
	var installed []installedExtension
	for index, operation := range plan.Operations {
		if !operation.Changed || operation.Kind != "file" {
			continue
		}
		record, err := backupDestination(backupRoot, index, operation.Destination)
		if err != nil {
			rollback(records)
			return ApplyResult{}, fmt.Errorf("backup %s: %w", operation.Destination, err)
		}
		records = append(records, record)
	}
	discovered, err := discover(root, reg, target)
	if err != nil {
		rollback(records)
		return ApplyResult{}, err
	}
	byName := map[string]discoveredTarget{}
	for _, item := range discovered {
		byName[item.name] = item
	}
	for _, operation := range plan.Operations {
		if !operation.Changed || operation.Kind != "extension" {
			continue
		}
		item := byName[operation.Target]
		args := substituteID(item.extensionInstallArgs, operation.Destination)
		command := exec.Command(item.extensionCommand, args...)
		if output, commandErr := command.CombinedOutput(); commandErr != nil {
			rollback(records)
			rollbackExtensions(installed)
			return ApplyResult{}, fmt.Errorf("install extension %s for %s: %w: %s", operation.Destination, operation.Target, commandErr, strings.TrimSpace(string(output)))
		}
		installed = append(installed, installedExtension{command: item.extensionCommand, uninstallArgs: item.extensionUninstallArgs, id: operation.Destination})
	}
	for _, operation := range plan.Operations {
		if !operation.Changed || operation.Kind != "file" {
			continue
		}
		data, err := os.ReadFile(operation.Source)
		if err != nil {
			rollback(records)
			rollbackExtensions(installed)
			return ApplyResult{}, err
		}
		if err := atomicWrite(operation.Destination, data); err != nil {
			rollback(records)
			rollbackExtensions(installed)
			return ApplyResult{}, fmt.Errorf("apply %s: %w", operation.Destination, err)
		}
	}
	if _, err := Verify(root, reg, target); err != nil {
		fileRollbackErr := rollback(records)
		extensionRollbackErr := rollbackExtensions(installed)
		if fileRollbackErr != nil || extensionRollbackErr != nil {
			return ApplyResult{}, fmt.Errorf("verify failed: %v; file rollback: %v; extension rollback: %v", err, fileRollbackErr, extensionRollbackErr)
		}
		return ApplyResult{}, fmt.Errorf("verify failed and changes were rolled back: %w", err)
	}
	return ApplyResult{Target: target, Applied: len(records) + len(installed), BackupPath: backupRoot}, nil
}

func Verify(root string, reg registry.Registry, target string) (PlanResult, error) {
	plan, err := Plan(root, reg, target)
	if err != nil {
		return PlanResult{}, err
	}
	if plan.Changes != 0 {
		var paths []string
		for _, operation := range plan.Operations {
			if operation.Changed {
				paths = append(paths, operation.Destination)
			}
		}
		return plan, fmt.Errorf("semantic verification failed for %d files: %s", plan.Changes, strings.Join(paths, ", "))
	}
	return plan, nil
}

func discover(root string, reg registry.Registry, target string) ([]discoveredTarget, error) {
	repoPath, err := reg.Resolve(root, "env")
	if err != nil {
		return nil, err
	}
	config, err := loadYAML[installationConfig](filepath.Join(repoPath, "adapters", "installations.yaml"))
	if err != nil {
		return nil, err
	}
	if config.Version != 1 {
		return nil, fmt.Errorf("unsupported installations version %d", config.Version)
	}
	var result []discoveredTarget
	for _, name := range sortedKeys(config.Targets) {
		definition := config.Targets[name]
		patterns, ok := definition.ConfigPaths[target]
		if !ok {
			return nil, fmt.Errorf("target %q has no %s config path", name, target)
		}
		files := cloneStringMap(definition.Files)
		for artifact, destination := range definition.PlatformFiles[target] {
			if existing, duplicate := files[artifact]; duplicate && existing == destination {
				continue
			}
			files[artifact+"#platform"] = destination
		}
		for _, pattern := range patterns {
			expanded, err := expandPath(pattern)
			if err != nil {
				return nil, err
			}
			matches, err := filepath.Glob(expanded)
			if err != nil {
				return nil, err
			}
			if !hasGlob(pattern) {
				matches = []string{expanded}
			}
			for _, path := range matches {
				if definition.RequireChild != "" {
					if info, err := os.Stat(filepath.Join(path, definition.RequireChild)); err != nil || !info.IsDir() {
						continue
					}
				}
				if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
					continue
				} else if err != nil {
					return nil, err
				}
				result = append(result, discoveredTarget{
					name: name, path: path, processPatterns: definition.ProcessPatterns[target], files: files,
					extensionArtifact: definition.ExtensionArtifact, extensionCommand: definition.ExtensionCommands[target],
					extensionListArgs: definition.ExtensionListArgs, extensionInstallArgs: definition.ExtensionInstallArgs,
					extensionUninstallArgs: definition.ExtensionUninstallArgs,
				})
			}
		}
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].name == result[j].name {
			return result[i].path < result[j].path
		}
		return result[i].name < result[j].name
	})
	return result, nil
}

func differsSemantically(artifact, source, destination string) (bool, error) {
	expected, err := os.ReadFile(sourceArtifactPath(artifact, source))
	if err != nil {
		return false, err
	}
	actual, err := os.ReadFile(destination)
	if errors.Is(err, os.ErrNotExist) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	artifact = strings.TrimSuffix(artifact, "#platform")
	switch {
	case strings.HasSuffix(artifact, ".json"):
		var expectedValue, actualValue any
		if err := json.Unmarshal(expected, &expectedValue); err != nil {
			return false, err
		}
		if err := json.Unmarshal(actual, &actualValue); err != nil {
			return true, nil
		}
		return !reflect.DeepEqual(expectedValue, actualValue), nil
	case artifact == "jetbrains/keymap.xml":
		return !equalJetBrainsKeymaps(expected, actual), nil
	case artifact == "jetbrains/active-keymap.xml":
		return !equalActiveKeymaps(expected, actual), nil
	default:
		return !bytes.Equal(expected, actual), nil
	}
}

func sourceArtifactPath(artifact, source string) string {
	if strings.HasSuffix(artifact, "#platform") {
		return strings.TrimSuffix(source, "#platform")
	}
	return source
}

func equalJetBrainsKeymaps(left, right []byte) bool {
	var a, b xmlKeymap
	if xml.Unmarshal(left, &a) != nil || xml.Unmarshal(right, &b) != nil {
		return false
	}
	return reflect.DeepEqual(normalizeKeymap(a), normalizeKeymap(b))
}

func normalizeKeymap(value xmlKeymap) xmlKeymap {
	for index := range value.Actions {
		sort.Slice(value.Actions[index].Shortcuts, func(i, j int) bool {
			left := value.Actions[index].Shortcuts[i]
			right := value.Actions[index].Shortcuts[j]
			return left.First+left.Remove < right.First+right.Remove
		})
	}
	sort.Slice(value.Actions, func(i, j int) bool { return value.Actions[i].ID < value.Actions[j].ID })
	return value
}

func equalActiveKeymaps(left, right []byte) bool {
	var a, b activeKeymapDocument
	if xml.Unmarshal(left, &a) != nil || xml.Unmarshal(right, &b) != nil {
		return false
	}
	return activeName(a) != "" && activeName(a) == activeName(b)
}

func activeName(document activeKeymapDocument) string {
	for _, component := range document.Components {
		if component.Name == "KeymapManager" {
			return component.Active.Name
		}
	}
	return ""
}

func backupDestination(root string, index int, destination string) (backupRecord, error) {
	record := backupRecord{destination: destination, backup: filepath.Join(root, fmt.Sprintf("%04d", index))}
	data, err := os.ReadFile(destination)
	if errors.Is(err, os.ErrNotExist) {
		return record, nil
	}
	if err != nil {
		return record, err
	}
	record.existed = true
	if err := atomicWrite(record.backup, data); err != nil {
		return record, err
	}
	return record, nil
}

func rollback(records []backupRecord) error {
	var failures []string
	for index := len(records) - 1; index >= 0; index-- {
		record := records[index]
		if record.existed {
			data, err := os.ReadFile(record.backup)
			if err == nil {
				err = atomicWrite(record.destination, data)
			}
			if err != nil {
				failures = append(failures, err.Error())
			}
			continue
		}
		if err := os.Remove(record.destination); err != nil && !errors.Is(err, os.ErrNotExist) {
			failures = append(failures, err.Error())
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("%s", strings.Join(failures, "; "))
	}
	return nil
}

func expandPath(value string) (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	value = strings.ReplaceAll(value, "%HOME%", home)
	if strings.Contains(value, "%APPDATA%") {
		appData := os.Getenv("APPDATA")
		if appData == "" {
			return "", fmt.Errorf("APPDATA is required for path %q", value)
		}
		value = strings.ReplaceAll(value, "%APPDATA%", appData)
	}
	return filepath.Clean(value), nil
}

func anyProcessRunning(names []string) (bool, error) {
	for _, name := range names {
		var command *exec.Cmd
		if runtime.GOOS == "windows" {
			command = exec.Command("tasklist", "/FI", "IMAGENAME eq "+name)
		} else {
			command = exec.Command("pgrep", "-f", name)
		}
		output, err := command.Output()
		if err == nil && len(bytes.TrimSpace(output)) > 0 {
			if runtime.GOOS != "windows" || bytes.Contains(bytes.ToLower(output), bytes.ToLower([]byte(name))) {
				return true, nil
			}
		}
		var exitError *exec.ExitError
		if err != nil && !errors.As(err, &exitError) {
			return false, err
		}
	}
	return false, nil
}

func listExtensions(command string, args []string) (map[string]bool, error) {
	if _, err := exec.LookPath(command); err != nil {
		return nil, err
	}
	output, err := exec.Command(command, args...).Output()
	if err != nil {
		return nil, err
	}
	result := map[string]bool{}
	for _, line := range strings.Split(string(output), "\n") {
		id := strings.TrimSpace(line)
		if id == "" {
			continue
		}
		if at := strings.IndexByte(id, '@'); at >= 0 {
			id = id[:at]
		}
		result[strings.ToLower(id)] = true
	}
	return result, nil
}

func readLines(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var result []string
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			result = append(result, line)
		}
	}
	return result, nil
}

func substituteID(args []string, id string) []string {
	result := make([]string, len(args))
	for index, arg := range args {
		result[index] = strings.ReplaceAll(arg, "%ID%", id)
	}
	return result
}

func rollbackExtensions(records []installedExtension) error {
	var failures []string
	for index := len(records) - 1; index >= 0; index-- {
		record := records[index]
		args := substituteID(record.uninstallArgs, record.id)
		if output, err := exec.Command(record.command, args...).CombinedOutput(); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v: %s", record.id, err, strings.TrimSpace(string(output))))
		}
	}
	if len(failures) > 0 {
		return fmt.Errorf("%s", strings.Join(failures, "; "))
	}
	return nil
}

func runtimeTarget() string {
	if runtime.GOOS == "darwin" {
		return "macos"
	}
	return runtime.GOOS
}

func cloneStringMap(source map[string]string) map[string]string {
	result := make(map[string]string, len(source))
	for key, value := range source {
		result[key] = value
	}
	return result
}

func hasGlob(value string) bool {
	return strings.ContainsAny(value, "*?[")
}
