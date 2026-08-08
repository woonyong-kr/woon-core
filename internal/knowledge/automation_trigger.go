package knowledge

import (
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
)

type AutomationTriggerStatus struct {
	Kind         string
	Label        string
	State        string
	Installed    bool
	Enabled      bool
	KeepAlive    bool
	Runs         int
	LastExitCode int
	PlistPath    string
}

type automationTriggerSpec struct {
	Kind                string
	Label               string
	WorkspaceRoot       string
	KnowledgeRepo       string
	Executable          string
	InvocationArguments []string
	WatchPaths          []string
	ThrottleSeconds     int
	RunAtLoad           bool
	Branch              string
	PathEnvironment     string
}

type automationTrigger interface {
	Install(context.Context, automationTriggerSpec) (AutomationTriggerStatus, error)
	Status(context.Context, automationTriggerSpec) (AutomationTriggerStatus, error)
	Enable(context.Context, automationTriggerSpec) (AutomationTriggerStatus, error)
	Disable(context.Context, automationTriggerSpec) (AutomationTriggerStatus, error)
	Uninstall(context.Context, automationTriggerSpec) (AutomationTriggerStatus, error)
}

type automationCommandRunner interface {
	Run(context.Context, string, ...string) (string, error)
}

type execAutomationCommandRunner struct {
	directory string
}

func (r execAutomationCommandRunner) Run(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = r.directory
	var output boundedBuffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Run(); err != nil {
		return output.String(), fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, output.String())
	}
	return output.String(), nil
}

type triggerEnvironment struct {
	goos string
	home string
	uid  string
}

func currentTriggerEnvironment() (triggerEnvironment, error) {
	current, err := user.Current()
	if err != nil {
		return triggerEnvironment{}, fmt.Errorf("resolve current user: %w", err)
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return triggerEnvironment{}, fmt.Errorf("resolve user home: %w", err)
	}
	return triggerEnvironment{goos: runtime.GOOS, home: home, uid: current.Uid}, nil
}

func InstallAutomation(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string) (AutomationTriggerStatus, error) {
	return changeAutomationTrigger(ctx, workspaceRoot, repo, executable, invocationArguments, "install")
}

func GetAutomationStatus(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string) (AutomationTriggerStatus, error) {
	return changeAutomationTrigger(ctx, workspaceRoot, repo, executable, invocationArguments, "status")
}

func EnableAutomation(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string) (AutomationTriggerStatus, error) {
	return changeAutomationTrigger(ctx, workspaceRoot, repo, executable, invocationArguments, "enable")
}

func DisableAutomation(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string) (AutomationTriggerStatus, error) {
	return changeAutomationTrigger(ctx, workspaceRoot, repo, executable, invocationArguments, "disable")
}

func UninstallAutomation(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string) (AutomationTriggerStatus, error) {
	return changeAutomationTrigger(ctx, workspaceRoot, repo, executable, invocationArguments, "uninstall")
}

func changeAutomationTrigger(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string, action string) (AutomationTriggerStatus, error) {
	var status AutomationTriggerStatus
	if ctx == nil {
		return status, errors.New("context is required")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return status, err
	}
	spec, err := buildAutomationTriggerSpec(ctx, workspaceRoot, repo, executable, invocationArguments, cfg)
	if err != nil {
		return status, err
	}
	environment, err := currentTriggerEnvironment()
	if err != nil {
		return status, err
	}
	adapter, err := newAutomationTrigger(spec.Kind, environment, execAutomationCommandRunner{directory: repo})
	if err != nil {
		return status, err
	}
	switch action {
	case "install":
		return adapter.Install(ctx, spec)
	case "status":
		return adapter.Status(ctx, spec)
	case "enable":
		return adapter.Enable(ctx, spec)
	case "disable":
		return adapter.Disable(ctx, spec)
	case "uninstall":
		return adapter.Uninstall(ctx, spec)
	default:
		return status, fmt.Errorf("unsupported automation action %q", action)
	}
}

func buildAutomationTriggerSpec(ctx context.Context, workspaceRoot, repo, executable string, invocationArguments []string, cfg Config) (automationTriggerSpec, error) {
	if strings.TrimSpace(executable) == "" || !filepath.IsAbs(executable) {
		return automationTriggerSpec{}, errors.New("automation executable must be an absolute path")
	}
	if len(invocationArguments) == 0 {
		return automationTriggerSpec{}, errors.New("automation invocation arguments are required")
	}
	for _, argument := range invocationArguments {
		if strings.TrimSpace(argument) == "" {
			return automationTriggerSpec{}, errors.New("automation invocation arguments must not be blank")
		}
	}
	branch, err := runGit(ctx, repo, "branch", "--show-current")
	if err != nil {
		return automationTriggerSpec{}, err
	}
	branch = strings.TrimSpace(branch)
	if branch == "" {
		return automationTriggerSpec{}, errors.New("cannot install knowledge automation from a detached HEAD")
	}
	pathEnvironment, err := buildAutomationPath(executable)
	if err != nil {
		return automationTriggerSpec{}, err
	}
	watchPaths := make([]string, 0, len(cfg.Automation.WatchPaths))
	for _, relative := range cfg.Automation.WatchPaths {
		absolute, pathErr := safePath(repo, relative)
		if pathErr != nil {
			return automationTriggerSpec{}, pathErr
		}
		if err := os.MkdirAll(absolute, 0o755); err != nil {
			return automationTriggerSpec{}, fmt.Errorf("create automation watch path: %w", err)
		}
		watchPaths = append(watchPaths, absolute)
	}
	return automationTriggerSpec{
		Kind: cfg.Automation.Trigger, Label: cfg.Automation.Label,
		WorkspaceRoot: workspaceRoot, KnowledgeRepo: repo, Executable: executable,
		InvocationArguments: append([]string(nil), invocationArguments...),
		WatchPaths:          watchPaths, ThrottleSeconds: cfg.Automation.ThrottleSeconds,
		RunAtLoad: cfg.Automation.RunAtLoad, Branch: branch,
		PathEnvironment: pathEnvironment,
	}, nil
}

func buildAutomationPath(executable string) (string, error) {
	directories := []string{filepath.Dir(executable)}
	for _, command := range []string{"codex", "woon-knowledge-vector", "gh", "git"} {
		path, err := exec.LookPath(command)
		if err != nil {
			return "", fmt.Errorf("find required automation command %q: %w", command, err)
		}
		directories = append(directories, filepath.Dir(path))
	}
	directories = append(directories, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
	seen := map[string]bool{}
	unique := directories[:0]
	for _, directory := range directories {
		directory = filepath.Clean(directory)
		if directory == "." || seen[directory] {
			continue
		}
		seen[directory] = true
		unique = append(unique, directory)
	}
	return strings.Join(unique, string(os.PathListSeparator)), nil
}

func newAutomationTrigger(kind string, environment triggerEnvironment, runner automationCommandRunner) (automationTrigger, error) {
	switch kind {
	case "manual":
		return manualTrigger{}, nil
	case "macos-launchd":
		if environment.goos != "darwin" {
			return nil, fmt.Errorf("macos-launchd trigger requires macOS, current OS is %s", environment.goos)
		}
		return macOSLaunchdTrigger{environment: environment, runner: runner}, nil
	default:
		return nil, fmt.Errorf("unsupported automation trigger %q", kind)
	}
}

type manualTrigger struct{}

func (manualTrigger) ready(spec automationTriggerSpec) AutomationTriggerStatus {
	return AutomationTriggerStatus{Kind: spec.Kind, Label: spec.Label, State: "manual-ready", Installed: true, Enabled: true}
}

func (m manualTrigger) Install(_ context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	return m.ready(spec), nil
}
func (m manualTrigger) Status(_ context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	return m.ready(spec), nil
}
func (m manualTrigger) Enable(_ context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	return m.ready(spec), nil
}
func (m manualTrigger) Disable(_ context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	status := m.ready(spec)
	status.Enabled = false
	status.State = "manual-only"
	return status, nil
}
func (m manualTrigger) Uninstall(_ context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	status := m.ready(spec)
	status.Installed = false
	status.Enabled = false
	status.State = "manual-only"
	return status, nil
}

type macOSLaunchdTrigger struct {
	environment triggerEnvironment
	runner      automationCommandRunner
}

func (m macOSLaunchdTrigger) Install(ctx context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	plistPath := m.plistPath(spec)
	logDirectory := filepath.Join(m.environment.home, "Library", "Logs", "woon-knowledge")
	if err := os.MkdirAll(filepath.Dir(plistPath), 0o755); err != nil {
		return AutomationTriggerStatus{}, err
	}
	if err := os.MkdirAll(logDirectory, 0o755); err != nil {
		return AutomationTriggerStatus{}, err
	}
	data, err := renderLaunchAgentPlist(spec, logDirectory)
	if err != nil {
		return AutomationTriggerStatus{}, err
	}
	if err := writeAtomicFile(plistPath, data, 0o644); err != nil {
		return AutomationTriggerStatus{}, err
	}
	_, _ = m.runner.Run(ctx, "launchctl", "bootout", m.service(spec))
	if _, err := m.runner.Run(ctx, "launchctl", "enable", m.service(spec)); err != nil {
		return AutomationTriggerStatus{}, err
	}
	if _, err := m.runner.Run(ctx, "launchctl", "bootstrap", m.domain(), plistPath); err != nil {
		return AutomationTriggerStatus{}, err
	}
	return m.Status(ctx, spec)
}

func (m macOSLaunchdTrigger) Status(ctx context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	status := AutomationTriggerStatus{Kind: spec.Kind, Label: spec.Label, PlistPath: m.plistPath(spec)}
	if _, err := os.Stat(status.PlistPath); err == nil {
		status.Installed = true
	} else if !errors.Is(err, os.ErrNotExist) {
		return status, err
	}
	output, err := m.runner.Run(ctx, "launchctl", "print", m.service(spec))
	if err != nil {
		status.State = "not-loaded"
		return status, nil
	}
	status.Enabled = true
	status.State = launchctlStringValue(output, "state")
	status.Runs = launchctlIntValue(output, "runs")
	status.LastExitCode = launchctlIntValue(output, "last exit code")
	status.KeepAlive = launchctlIntValue(output, "keepalive") != 0
	return status, nil
}

func (m macOSLaunchdTrigger) Enable(ctx context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	if _, err := os.Stat(m.plistPath(spec)); err != nil {
		return AutomationTriggerStatus{}, errors.New("automation is not installed")
	}
	if _, err := m.runner.Run(ctx, "launchctl", "enable", m.service(spec)); err != nil {
		return AutomationTriggerStatus{}, err
	}
	status, err := m.Status(ctx, spec)
	if err != nil || status.Enabled {
		return status, err
	}
	if _, err := m.runner.Run(ctx, "launchctl", "bootstrap", m.domain(), m.plistPath(spec)); err != nil {
		return AutomationTriggerStatus{}, err
	}
	return m.Status(ctx, spec)
}

func (m macOSLaunchdTrigger) Disable(ctx context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	if _, err := m.runner.Run(ctx, "launchctl", "disable", m.service(spec)); err != nil {
		return AutomationTriggerStatus{}, err
	}
	_, _ = m.runner.Run(ctx, "launchctl", "bootout", m.service(spec))
	return m.Status(ctx, spec)
}

func (m macOSLaunchdTrigger) Uninstall(ctx context.Context, spec automationTriggerSpec) (AutomationTriggerStatus, error) {
	_, _ = m.runner.Run(ctx, "launchctl", "bootout", m.service(spec))
	_, _ = m.runner.Run(ctx, "launchctl", "enable", m.service(spec))
	if err := os.Remove(m.plistPath(spec)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return AutomationTriggerStatus{}, err
	}
	return m.Status(ctx, spec)
}

func (m macOSLaunchdTrigger) domain() string { return "gui/" + m.environment.uid }
func (m macOSLaunchdTrigger) service(spec automationTriggerSpec) string {
	return m.domain() + "/" + spec.Label
}
func (m macOSLaunchdTrigger) plistPath(spec automationTriggerSpec) string {
	return filepath.Join(m.environment.home, "Library", "LaunchAgents", spec.Label+".plist")
}

func renderLaunchAgentPlist(spec automationTriggerSpec, logDirectory string) ([]byte, error) {
	if len(spec.WatchPaths) == 0 {
		return nil, errors.New("launchd trigger requires at least one watch path")
	}
	stringsByKey := []struct {
		key   string
		value string
	}{
		{"Label", spec.Label},
		{"ProcessType", "Background"},
		{"StandardErrorPath", filepath.Join(logDirectory, "automation.err.log")},
		{"StandardOutPath", filepath.Join(logDirectory, "automation.out.log")},
	}
	var output strings.Builder
	output.WriteString(`<?xml version="1.0" encoding="UTF-8"?>` + "\n")
	output.WriteString(`<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">` + "\n")
	output.WriteString(`<plist version="1.0"><dict>` + "\n")
	for _, item := range stringsByKey {
		writePlistString(&output, item.key, item.value)
	}
	programArguments := []string{spec.Executable}
	programArguments = append(programArguments, spec.InvocationArguments...)
	programArguments = append(programArguments, "run")
	writePlistArray(&output, "ProgramArguments", programArguments)
	writePlistArray(&output, "WatchPaths", spec.WatchPaths)
	output.WriteString("<key>EnvironmentVariables</key><dict>\n")
	writePlistString(&output, "PATH", spec.PathEnvironment)
	writePlistString(&output, automationBranchEnvironment, spec.Branch)
	output.WriteString("</dict>\n")
	writePlistBool(&output, "KeepAlive", false)
	writePlistBool(&output, "RunAtLoad", spec.RunAtLoad)
	fmt.Fprintf(&output, "<key>ThrottleInterval</key><integer>%d</integer>\n", spec.ThrottleSeconds)
	output.WriteString("</dict></plist>\n")
	return []byte(output.String()), nil
}

func writePlistString(output *strings.Builder, key, value string) {
	fmt.Fprintf(output, "<key>%s</key><string>%s</string>\n", escapeXML(key), escapeXML(value))
}

func writePlistArray(output *strings.Builder, key string, values []string) {
	fmt.Fprintf(output, "<key>%s</key><array>\n", escapeXML(key))
	for _, value := range values {
		fmt.Fprintf(output, "<string>%s</string>\n", escapeXML(value))
	}
	output.WriteString("</array>\n")
}

func writePlistBool(output *strings.Builder, key string, value bool) {
	if value {
		fmt.Fprintf(output, "<key>%s</key><true/>\n", escapeXML(key))
		return
	}
	fmt.Fprintf(output, "<key>%s</key><false/>\n", escapeXML(key))
}

func escapeXML(value string) string {
	var output bytes.Buffer
	_ = xml.EscapeText(&output, []byte(value))
	return output.String()
}

func writeAtomicFile(path string, data []byte, mode os.FileMode) error {
	temporary := path + ".tmp"
	if err := os.WriteFile(temporary, data, mode); err != nil {
		return err
	}
	if err := os.Chmod(temporary, mode); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	if err := os.Rename(temporary, path); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func launchctlStringValue(output, key string) string {
	prefix := key + " = "
	for _, line := range strings.Split(output, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, prefix) {
			return strings.TrimSpace(strings.TrimPrefix(trimmed, prefix))
		}
	}
	return "unknown"
}

func launchctlIntValue(output, key string) int {
	value, _ := strconv.Atoi(launchctlStringValue(output, key))
	return value
}
