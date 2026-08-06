package envsync

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"github.com/woonyong-kr/woon-core/internal/registry"
)

type GenerateResult struct {
	Target    string
	Artifacts int
	Hash      string
}

type vscodeOutputBinding struct {
	Key     string `json:"key"`
	Command string `json:"command"`
	When    string `json:"when,omitempty"`
}

type xmlKeymap struct {
	XMLName xml.Name    `xml:"keymap"`
	Version string      `xml:"version,attr"`
	Name    string      `xml:"name,attr"`
	Parent  string      `xml:"parent,attr"`
	Actions []xmlAction `xml:"action"`
}

type xmlAction struct {
	ID        string        `xml:"id,attr"`
	Shortcuts []xmlShortcut `xml:"keyboard-shortcut"`
}

type xmlShortcut struct {
	First  string `xml:"first-keystroke,attr"`
	Second string `xml:"second-keystroke,attr,omitempty"`
	Remove string `xml:"remove,attr,omitempty"`
}

func Generate(root string, reg registry.Registry, target string) (GenerateResult, error) {
	repoPath, err := reg.Resolve(root, "env")
	if err != nil {
		return GenerateResult{}, err
	}
	artifacts, err := render(repoPath, target)
	if err != nil {
		return GenerateResult{}, err
	}
	second, err := render(repoPath, target)
	if err != nil {
		return GenerateResult{}, err
	}
	if !equalArtifacts(artifacts, second) {
		return GenerateResult{}, fmt.Errorf("generator is not deterministic for target %q", target)
	}
	manifest, aggregate, err := makeManifest(target, artifacts)
	if err != nil {
		return GenerateResult{}, err
	}
	artifacts["manifest.json"] = manifest
	outputRoot := filepath.Join(repoPath, "generated", target)
	for _, relative := range sortedKeys(artifacts) {
		if err := atomicWrite(filepath.Join(outputRoot, filepath.FromSlash(relative)), artifacts[relative]); err != nil {
			return GenerateResult{}, err
		}
	}
	return GenerateResult{Target: target, Artifacts: len(artifacts), Hash: aggregate}, nil
}

func Check(root string, reg registry.Registry, target string) (GenerateResult, error) {
	repoPath, err := reg.Resolve(root, "env")
	if err != nil {
		return GenerateResult{}, err
	}
	artifacts, err := render(repoPath, target)
	if err != nil {
		return GenerateResult{}, err
	}
	manifest, aggregate, err := makeManifest(target, artifacts)
	if err != nil {
		return GenerateResult{}, err
	}
	artifacts["manifest.json"] = manifest
	outputRoot := filepath.Join(repoPath, "generated", target)
	for _, relative := range sortedKeys(artifacts) {
		actual, err := os.ReadFile(filepath.Join(outputRoot, filepath.FromSlash(relative)))
		if err != nil {
			return GenerateResult{}, fmt.Errorf("read generated artifact %s: %w", relative, err)
		}
		if !bytes.Equal(actual, artifacts[relative]) {
			return GenerateResult{}, fmt.Errorf("generated artifact drift: %s", relative)
		}
	}
	if err := auditAdapterIsolation(repoPath); err != nil {
		return GenerateResult{}, err
	}
	return GenerateResult{Target: target, Artifacts: len(artifacts), Hash: aggregate}, nil
}

func equalArtifacts(left, right map[string][]byte) bool {
	if len(left) != len(right) {
		return false
	}
	for path, data := range left {
		if !bytes.Equal(data, right[path]) {
			return false
		}
	}
	return true
}

func auditAdapterIsolation(repoPath string) error {
	_, _, vsActions, _, jbActions, _, err := loadModel(repoPath, runtimeTargetForAudit())
	if err != nil {
		return err
	}
	identifiers := map[string]bool{}
	for _, bindings := range vsActions.Intents {
		for _, binding := range bindings {
			identifiers[binding.Command] = true
		}
	}
	for _, commands := range vsActions.RemoveConflicts {
		for _, command := range commands {
			identifiers[command] = true
		}
	}
	for _, actions := range jbActions.Intents {
		for _, action := range actions {
			identifiers[action] = true
		}
	}
	var violations []string
	err = filepath.WalkDir(repoPath, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			relative, _ := filepath.Rel(repoPath, path)
			relative = filepath.ToSlash(relative)
			if relative == ".git" || relative == "generated" || relative == "tests/fixtures" || strings.HasPrefix(relative, "adapters/") {
				return filepath.SkipDir
			}
			return nil
		}
		extension := strings.ToLower(filepath.Ext(path))
		if extension != ".go" && extension != ".py" && extension != ".sh" && extension != ".ps1" && extension != ".json" && extension != ".yaml" && extension != ".yml" && extension != ".xml" {
			return nil
		}
		data, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		for identifier := range identifiers {
			if len(identifier) >= 4 && bytes.Contains(data, []byte(identifier)) {
				relative, _ := filepath.Rel(repoPath, path)
				violations = append(violations, filepath.ToSlash(relative)+": "+identifier)
				break
			}
		}
		return nil
	})
	if err != nil {
		return err
	}
	if len(violations) > 0 {
		sort.Strings(violations)
		return fmt.Errorf("IDE action IDs must stay under adapters: %s", strings.Join(violations, ", "))
	}
	return nil
}

func runtimeTargetForAudit() string {
	if runtime.GOOS == "darwin" {
		return "macos"
	}
	return runtime.GOOS
}

func render(repoPath, target string) (map[string][]byte, error) {
	env, vs, vsActions, jb, jbActions, platformOverlay, err := loadModel(repoPath, target)
	if err != nil {
		return nil, err
	}
	settings, err := renderVSCodeSettings(repoPath, env, vs, platformOverlay)
	if err != nil {
		return nil, err
	}
	bindings, err := renderVSCodeBindings(env, vsActions)
	if err != nil {
		return nil, err
	}
	exts := make([]string, 0, len(env.Extensions))
	for _, logical := range env.Extensions {
		exts = append(exts, vs.Extensions[logical])
	}
	sort.Strings(exts)
	extensionBytes := []byte(strings.Join(exts, "\n") + "\n")
	keymap, err := renderJetBrainsKeymap(env, jb, jbActions, target)
	if err != nil {
		return nil, err
	}
	activeKeymap := []byte("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<application>\n  <component name=\"KeymapManager\">\n    <active_keymap name=\"" + jb.Keymap.Name + "\" />\n  </component>\n</application>\n")
	plugins := make([]string, 0, len(jb.Extensions))
	for _, logical := range env.Extensions {
		if plugin, ok := jb.Extensions[logical]; ok {
			plugins = append(plugins, plugin)
		}
	}
	sort.Strings(plugins)
	pluginBytes := []byte(strings.Join(plugins, "\n"))
	if len(plugins) > 0 {
		pluginBytes = append(pluginBytes, '\n')
	}
	return map[string][]byte{
		"vscode/settings.json":        settings,
		"vscode/keybindings.json":     bindings,
		"vscode/extensions.txt":       extensionBytes,
		"cursor/settings.json":        settings,
		"cursor/keybindings.json":     bindings,
		"cursor/extensions.txt":       extensionBytes,
		"jetbrains/keymap.xml":        keymap,
		"jetbrains/active-keymap.xml": activeKeymap,
		"jetbrains/plugins.txt":       pluginBytes,
	}, nil
}

func renderVSCodeSettings(repoPath string, env environment, adapter vscodeAdapter, platformOverlay overlay) ([]byte, error) {
	defaults, err := os.ReadFile(filepath.Join(repoPath, "adapters", "vscode", "defaults.json"))
	if err != nil {
		return nil, err
	}
	settings := map[string]any{}
	if err := json.Unmarshal(defaults, &settings); err != nil {
		return nil, fmt.Errorf("parse VS Code defaults: %w", err)
	}
	for source, rawTarget := range adapter.Settings {
		parts := strings.Split(source, ".")
		if len(parts) != 2 || parts[0] != "editor" {
			return nil, fmt.Errorf("unsupported common setting path %q", source)
		}
		value, ok := env.Editor[parts[1]]
		if !ok {
			return nil, fmt.Errorf("common setting %q is not declared", source)
		}
		target, ok := rawTarget.(string)
		if !ok {
			return nil, fmt.Errorf("adapter target for %q must be a string", source)
		}
		if target == "editor.rulers" {
			value = []any{value}
		}
		settings[target] = value
	}
	for _, languageName := range sortedKeys(env.Languages) {
		language := env.Languages[languageName]
		formatter, ok := adapter.FormatterIDs[language.Formatter]
		if !ok {
			return nil, fmt.Errorf("formatter %q has no VS Code mapping", language.Formatter)
		}
		for _, languageID := range adapter.LanguageIDs[languageName] {
			settings["["+languageID+"]"] = map[string]any{
				"editor.defaultFormatter": formatter,
				"editor.insertSpaces":     true,
				"editor.tabSize":          language.TabSize,
			}
		}
	}
	for key, value := range platformOverlay.AdapterSettings["vscode"] {
		settings[key] = value
	}
	return encodeJSON(settings)
}

func renderVSCodeBindings(env environment, actions vscodeActions) ([]byte, error) {
	var output []vscodeOutputBinding
	for _, intent := range sortedKeys(env.Keybindings) {
		key := env.Keybindings[intent]
		for _, binding := range actions.Intents[intent] {
			output = append(output, vscodeOutputBinding{Key: key, Command: binding.Command, When: binding.When})
		}
		for _, command := range actions.RemoveConflicts[intent] {
			output = append(output, vscodeOutputBinding{Key: key, Command: "-" + command})
		}
	}
	return encodeJSON(output)
}

func renderJetBrainsKeymap(env environment, adapter jetbrainsAdapter, actions jetbrainsActions, target string) ([]byte, error) {
	byAction := map[string][]xmlShortcut{}
	for _, intent := range sortedKeys(env.Keybindings) {
		key := jetbrainsKey(env.Keybindings[intent])
		if override := actions.KeyOverrides[intent]; override != "" {
			key = override
		}
		for _, action := range actions.Intents[intent] {
			byAction[action] = append(byAction[action], xmlShortcut{First: key})
		}
	}
	for action, keys := range actions.RemoveConflicts {
		for _, key := range keys {
			byAction[action] = append(byAction[action], xmlShortcut{First: key, Remove: "true"})
		}
	}
	for action, shortcuts := range actions.AdditionalShortcuts {
		for _, shortcut := range shortcuts {
			byAction[action] = append(byAction[action], xmlShortcut{First: shortcut.First, Second: shortcut.Second})
		}
	}
	ids := sortedKeys(byAction)
	output := xmlKeymap{Version: "1", Name: adapter.Keymap.Name, Parent: adapter.Keymap.Parents[target]}
	for _, id := range ids {
		shortcuts := byAction[id]
		sort.SliceStable(shortcuts, func(i, j int) bool {
			if shortcuts[i].First == shortcuts[j].First && shortcuts[i].Second == shortcuts[j].Second {
				return shortcuts[i].Remove < shortcuts[j].Remove
			}
			return shortcuts[i].First+shortcuts[i].Second < shortcuts[j].First+shortcuts[j].Second
		})
		output.Actions = append(output.Actions, xmlAction{ID: id, Shortcuts: shortcuts})
	}
	data, err := xml.MarshalIndent(output, "", "  ")
	if err != nil {
		return nil, err
	}
	return append([]byte(xml.Header), append(data, '\n')...), nil
}

func jetbrainsKey(key string) string {
	parts := strings.Split(strings.ToLower(key), "+")
	for index, part := range parts {
		switch part {
		case "cmd":
			parts[index] = "meta"
		case "[":
			parts[index] = "open_bracket"
		case "]":
			parts[index] = "close_bracket"
		}
	}
	return strings.Join(parts, " ")
}

func makeManifest(target string, artifacts map[string][]byte) ([]byte, string, error) {
	manifest := generatedManifest{Version: 1, Target: target, Files: map[string]string{}}
	aggregate := sha256.New()
	for _, path := range sortedKeys(artifacts) {
		digest := sha256.Sum256(artifacts[path])
		encoded := hex.EncodeToString(digest[:])
		manifest.Files[path] = encoded
		aggregate.Write([]byte(path))
		aggregate.Write(digest[:])
	}
	data, err := encodeJSON(manifest)
	if err != nil {
		return nil, "", err
	}
	return data, hex.EncodeToString(aggregate.Sum(nil)), nil
}

func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".woon-env-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, path)
}
