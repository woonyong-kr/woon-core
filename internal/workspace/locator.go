package workspace

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

const markerName = ".woon-root"

type Workspace struct {
	Root   string
	Source string
}

type localConfig struct {
	Root string `yaml:"root"`
}

func Discover(cliRoot string) (Workspace, error) {
	type candidate struct {
		root   string
		source string
	}
	var candidates []candidate
	add := func(root, source string) {
		if strings.TrimSpace(root) != "" {
			candidates = append(candidates, candidate{root: root, source: source})
		}
	}

	add(cliRoot, "--root")
	add(os.Getenv("WOON_HOME"), "WOON_HOME")
	if configured, err := readConfiguredRoot(); err != nil {
		return Workspace{}, err
	} else {
		add(configured, "config")
	}
	if marker, err := findMarker(); err != nil {
		return Workspace{}, err
	} else {
		add(marker, markerName)
	}

	if len(candidates) == 0 {
		home, err := os.UserHomeDir()
		if err != nil {
			return Workspace{}, fmt.Errorf("resolve home directory: %w", err)
		}
		add(filepath.Join(home, "workspace", "woon"), "default")
	}

	resolved := make(map[string][]string)
	for _, c := range candidates {
		root, err := canonical(c.root)
		if err != nil {
			return Workspace{}, fmt.Errorf("resolve %s: %w", c.source, err)
		}
		resolved[root] = append(resolved[root], c.source)
	}
	if len(resolved) != 1 {
		keys := make([]string, 0, len(resolved))
		for root, sources := range resolved {
			keys = append(keys, fmt.Sprintf("%s (%s)", root, strings.Join(sources, ", ")))
		}
		sort.Strings(keys)
		return Workspace{}, fmt.Errorf("ambiguous workspace roots: %s", strings.Join(keys, "; "))
	}
	for root, sources := range resolved {
		return Workspace{Root: root, Source: strings.Join(sources, "+")}, nil
	}
	return Workspace{}, errors.New("workspace discovery failed")
}

func Initialize(path string) (string, error) {
	root, err := canonical(path)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", fmt.Errorf("create workspace: %w", err)
	}
	marker := filepath.Join(root, markerName)
	if err := os.WriteFile(marker, []byte("version: 1\n"), 0o644); err != nil {
		return "", fmt.Errorf("write workspace marker: %w", err)
	}
	configPath, err := platformConfigPath()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(configPath), 0o700); err != nil {
		return "", fmt.Errorf("create config directory: %w", err)
	}
	data, err := yaml.Marshal(localConfig{Root: root})
	if err != nil {
		return "", fmt.Errorf("encode config: %w", err)
	}
	if err := atomicWrite(configPath, data, 0o600); err != nil {
		return "", err
	}
	return root, nil
}

func readConfiguredRoot() (string, error) {
	path, err := platformConfigPath()
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", fmt.Errorf("read %s: %w", path, err)
	}
	var cfg localConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return "", fmt.Errorf("parse %s: %w", path, err)
	}
	return cfg.Root, nil
}

func platformConfigPath() (string, error) {
	if runtime.GOOS == "windows" {
		base := os.Getenv("APPDATA")
		if base == "" {
			return "", errors.New("APPDATA is not set")
		}
		return filepath.Join(base, "Woon", "config.yaml"), nil
	}
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, "woon", "config.yaml"), nil
}

func findMarker() (string, error) {
	current, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for {
		if _, err := os.Stat(filepath.Join(current, markerName)); err == nil {
			return current, nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", nil
		}
		current = parent
	}
}

func canonical(path string) (string, error) {
	if strings.HasPrefix(path, "~"+string(filepath.Separator)) {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		path = filepath.Join(home, strings.TrimPrefix(path, "~"+string(filepath.Separator)))
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	abs = filepath.Clean(abs)
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return filepath.Clean(resolved), nil
	}
	return abs, nil
}

func atomicWrite(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".woon-config-*")
	if err != nil {
		return fmt.Errorf("create temporary config: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("replace config: %w", err)
	}
	return nil
}
