package envsync

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

type environment struct {
	Version     int                 `yaml:"version"`
	Editor      map[string]any      `yaml:"editor"`
	Languages   map[string]language `yaml:"languages"`
	Keybindings map[string]string   `yaml:"keybindings"`
	Extensions  []string            `yaml:"extensions"`
}

type language struct {
	Formatter string `yaml:"formatter"`
	TabSize   int    `yaml:"tab_size"`
}

type vscodeAdapter struct {
	Version      int                 `yaml:"version"`
	Extends      string              `yaml:"extends,omitempty"`
	Settings     map[string]any      `yaml:"settings"`
	FormatterIDs map[string]string   `yaml:"formatter_ids"`
	LanguageIDs  map[string][]string `yaml:"language_ids"`
	Extensions   map[string]string   `yaml:"extensions"`
}

type vscodeActions struct {
	Version         int                        `yaml:"version"`
	Intents         map[string][]vscodeBinding `yaml:"intents"`
	RemoveConflicts map[string][]string        `yaml:"remove_conflicts"`
}

type vscodeBinding struct {
	Command string `yaml:"command"`
	When    string `yaml:"when,omitempty"`
}

type jetbrainsAdapter struct {
	Version    int               `yaml:"version"`
	Keymap     jetbrainsKeymap   `yaml:"keymap"`
	Extensions map[string]string `yaml:"extensions"`
}

type jetbrainsKeymap struct {
	Name    string            `yaml:"name"`
	Parents map[string]string `yaml:"parents"`
}

type jetbrainsActions struct {
	Version             int                                 `yaml:"version"`
	KeyOverrides        map[string]string                   `yaml:"key_overrides,omitempty"`
	Intents             map[string][]string                 `yaml:"intents"`
	RemoveConflicts     map[string][]string                 `yaml:"remove_conflicts"`
	AdditionalShortcuts map[string][]jetbrainsShortcutInput `yaml:"additional_shortcuts,omitempty"`
}

type jetbrainsShortcutInput struct {
	First  string `yaml:"first"`
	Second string `yaml:"second,omitempty"`
}

type overlay struct {
	Version         int                       `yaml:"version"`
	AdapterSettings map[string]map[string]any `yaml:"adapter_settings"`
}

type generatedManifest struct {
	Version int               `json:"version"`
	Target  string            `json:"target"`
	Files   map[string]string `json:"files"`
}

func loadModel(repoPath, target string) (environment, vscodeAdapter, vscodeActions, jetbrainsAdapter, jetbrainsActions, overlay, error) {
	env, err := loadYAML[environment](filepath.Join(repoPath, "config", "env.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	vs, err := loadYAML[vscodeAdapter](filepath.Join(repoPath, "adapters", "vscode", "adapter.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	cursor, err := loadYAML[vscodeAdapter](filepath.Join(repoPath, "adapters", "cursor", "adapter.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	if cursor.Version != 1 || cursor.Extends != "vscode" {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, fmt.Errorf("Cursor adapter must extend vscode schema version 1")
	}
	vsActions, err := loadYAML[vscodeActions](filepath.Join(repoPath, "adapters", "vscode", "actions.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	jb, err := loadYAML[jetbrainsAdapter](filepath.Join(repoPath, "adapters", "jetbrains", "adapter.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	jbActions, err := loadYAML[jetbrainsActions](filepath.Join(repoPath, "adapters", "jetbrains", "actions.yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	platformOverlay, err := loadYAML[overlay](filepath.Join(repoPath, "overlays", target+".yaml"))
	if err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	if err := validateModel(env, vs, vsActions, jb, jbActions, platformOverlay, target); err != nil {
		return environment{}, vscodeAdapter{}, vscodeActions{}, jetbrainsAdapter{}, jetbrainsActions{}, overlay{}, err
	}
	return env, vs, vsActions, jb, jbActions, platformOverlay, nil
}

func validateModel(env environment, vs vscodeAdapter, vsActions vscodeActions, jb jetbrainsAdapter, jbActions jetbrainsActions, platformOverlay overlay, target string) error {
	if env.Version != 1 || vs.Version != 1 || vsActions.Version != 1 || jb.Version != 1 || jbActions.Version != 1 || platformOverlay.Version != 1 {
		return fmt.Errorf("all environment schemas must use version 1")
	}
	if _, ok := jb.Keymap.Parents[target]; !ok {
		return fmt.Errorf("JetBrains parent keymap missing for target %q", target)
	}
	commonTargets := map[string]bool{}
	for source, rawTarget := range vs.Settings {
		parts := strings.Split(source, ".")
		if len(parts) != 2 || parts[0] != "editor" {
			return fmt.Errorf("unsupported common setting path %q", source)
		}
		if _, ok := env.Editor[parts[1]]; !ok {
			return fmt.Errorf("adapter maps undeclared common setting %q", source)
		}
		targetName, ok := rawTarget.(string)
		if !ok || targetName == "" {
			return fmt.Errorf("adapter target for %q must be a string", source)
		}
		commonTargets[targetName] = true
	}
	if len(env.Editor) != len(vs.Settings) {
		return fmt.Errorf("every editor setting must have exactly one VS Code adapter mapping")
	}
	for setting := range platformOverlay.AdapterSettings["vscode"] {
		if commonTargets[setting] {
			return fmt.Errorf("platform overlay may not override common setting %q", setting)
		}
	}
	for name, language := range env.Languages {
		if language.TabSize <= 0 || language.Formatter == "" {
			return fmt.Errorf("language %q requires formatter and positive tab_size", name)
		}
		if len(vs.LanguageIDs[name]) == 0 {
			return fmt.Errorf("language %q has no VS Code language ID", name)
		}
		if _, ok := vs.FormatterIDs[language.Formatter]; !ok {
			return fmt.Errorf("formatter %q has no VS Code mapping", language.Formatter)
		}
	}
	for intent := range env.Keybindings {
		if len(vsActions.Intents[intent]) == 0 {
			return fmt.Errorf("VS Code action mapping missing for intent %q", intent)
		}
		if len(jbActions.Intents[intent]) == 0 {
			return fmt.Errorf("JetBrains action mapping missing for intent %q", intent)
		}
	}
	for name := range vsActions.Intents {
		if _, ok := env.Keybindings[name]; !ok {
			return fmt.Errorf("VS Code adapter has undeclared intent %q", name)
		}
	}
	for name := range jbActions.Intents {
		if _, ok := env.Keybindings[name]; !ok {
			return fmt.Errorf("JetBrains adapter has undeclared intent %q", name)
		}
	}
	for name := range jbActions.KeyOverrides {
		if _, ok := env.Keybindings[name]; !ok {
			return fmt.Errorf("JetBrains adapter overrides undeclared intent %q", name)
		}
	}
	seen := map[string]bool{}
	for _, name := range env.Extensions {
		if seen[name] {
			return fmt.Errorf("duplicate extension %q", name)
		}
		seen[name] = true
		if _, ok := vs.Extensions[name]; !ok {
			return fmt.Errorf("VS Code extension mapping missing for %q", name)
		}
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

func encodeJSON(value any) ([]byte, error) {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}
