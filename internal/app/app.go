package app

import (
	"fmt"
	"io"
	"runtime"
	"strings"

	"github.com/woonyong-kr/woon-core/internal/contextdoc"
	"github.com/woonyong-kr/woon-core/internal/envsync"
	"github.com/woonyong-kr/woon-core/internal/registry"
	"github.com/woonyong-kr/woon-core/internal/workspace"
)

const version = "0.1.0"

type options struct {
	root string
}

func Run(rawArgs []string, stdout, stderr io.Writer) error {
	opts, args, err := parseGlobal(rawArgs)
	if err != nil {
		return err
	}
	if len(args) == 0 {
		printUsage(stdout)
		return nil
	}

	switch args[0] {
	case "version", "--version", "-v":
		fmt.Fprintln(stdout, version)
		return nil
	case "help", "--help", "-h":
		printUsage(stdout)
		return nil
	case "init":
		return runInit(opts, args[1:], stdout)
	case "doctor":
		return runDoctor(opts, stdout)
	case "resolve":
		return runResolve(opts, args[1:], stdout)
	case "repo":
		return runRepo(opts, args[1:], stdout)
	case "context":
		return runContext(opts, args[1:], stdout)
	case "env":
		return runEnv(opts, args[1:], stdout)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func runEnv(opts options, args []string, out io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: woon env <doctor|plan|generate|check|apply|verify> [--all] [--target <os>]")
	}
	target, remaining, err := parseTarget(args[1:])
	if err != nil {
		return err
	}
	if len(remaining) == 1 && remaining[0] == "--all" {
		remaining = nil
	}
	if len(remaining) != 0 {
		return fmt.Errorf("unexpected env arguments: %s", strings.Join(remaining, " "))
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	switch args[0] {
	case "doctor":
		statuses, err := envsync.Doctor(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\ninstallations: %d\n", target, len(statuses))
		for _, status := range statuses {
			fmt.Fprintf(out, "  - name: %s\n    path: %s\n    running: %t\n", status.Name, status.Path, status.Running)
			if status.ExtensionCommand != "" {
				fmt.Fprintf(out, "    extension_command: %s\n    extension_command_available: %t\n", status.ExtensionCommand, status.CommandAvailable)
			}
		}
		return nil
	case "plan":
		result, err := envsync.Plan(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\noperations: %d\nchanges: %d\n", result.Target, len(result.Operations), result.Changes)
		for _, operation := range result.Operations {
			if operation.Changed {
				fmt.Fprintf(out, "  - %s/%s: %s\n", operation.Target, operation.Kind, operation.Destination)
			}
		}
		return nil
	case "generate":
		result, err := envsync.Generate(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\nartifacts: %d\nhash: %s\n", result.Target, result.Artifacts, result.Hash)
		return nil
	case "check":
		result, err := envsync.Check(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\nartifacts: %d\nhash: %s\n", result.Target, result.Artifacts, result.Hash)
		return nil
	case "apply":
		result, err := envsync.Apply(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\napplied: %d\n", result.Target, result.Applied)
		if result.BackupPath != "" {
			fmt.Fprintf(out, "backup: %s\n", result.BackupPath)
		}
		return nil
	case "verify":
		result, err := envsync.Verify(ws.Root, reg, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\nverified: %d\n", result.Target, len(result.Operations))
		return nil
	default:
		return fmt.Errorf("unknown env command %q", args[0])
	}
}

func parseTarget(args []string) (string, []string, error) {
	target := runtime.GOOS
	switch target {
	case "darwin":
		target = "macos"
	case "windows", "linux":
	default:
		return "", nil, fmt.Errorf("unsupported runtime OS %q", runtime.GOOS)
	}
	remaining := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		if args[i] != "--target" {
			remaining = append(remaining, args[i])
			continue
		}
		if i+1 >= len(args) {
			return "", nil, fmt.Errorf("--target requires macos, windows, or linux")
		}
		target = args[i+1]
		i++
	}
	if target != "macos" && target != "windows" && target != "linux" {
		return "", nil, fmt.Errorf("unsupported target %q", target)
	}
	return target, remaining, nil
}

func parseGlobal(args []string) (options, []string, error) {
	var opts options
	clean := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		if args[i] != "--root" {
			clean = append(clean, args[i])
			continue
		}
		if i+1 >= len(args) || strings.TrimSpace(args[i+1]) == "" {
			return opts, nil, fmt.Errorf("--root requires a path")
		}
		opts.root = args[i+1]
		i++
	}
	return opts, clean, nil
}

func runInit(opts options, args []string, out io.Writer) error {
	if len(args) != 0 {
		return fmt.Errorf("init takes no positional arguments")
	}
	if opts.root == "" {
		return fmt.Errorf("init requires --root")
	}
	root, err := workspace.Initialize(opts.root)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "status: initialized\nroot: %s\nnext_actions:\n  - woon doctor\n  - woon repo sync\n", root)
	return nil
}

func runDoctor(opts options, out io.Writer) error {
	ws, err := workspace.Discover(opts.root)
	if err != nil {
		return err
	}
	reg, err := registry.Load(ws.Root)
	if err != nil {
		return err
	}
	missing := reg.Missing(ws.Root)
	fmt.Fprintf(out, "status: ok\nroot: %s\nsource: %s\nrepositories: %d\nmissing: %d\n", ws.Root, ws.Source, len(reg.Repositories), len(missing))
	for _, id := range missing {
		fmt.Fprintf(out, "  - %s\n", id)
	}
	return nil
}

func runResolve(opts options, args []string, out io.Writer) error {
	if len(args) != 1 {
		return fmt.Errorf("resolve requires one repository ID or repo URI")
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	resolved, err := reg.Resolve(ws.Root, args[0])
	if err != nil {
		return err
	}
	fmt.Fprintln(out, resolved)
	return nil
}

func runRepo(opts options, args []string, out io.Writer) error {
	if len(args) != 1 || args[0] != "sync" {
		return fmt.Errorf("usage: woon repo sync")
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	result, err := reg.Sync(ws.Root)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "status: ok\ncloned: %d\nexisting: %d\n", result.Cloned, result.Existing)
	return nil
}

func runContext(opts options, args []string, out io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: woon context <generate|check> [--all|repo-id]")
	}
	all, ids, err := parseTargets(args[1:])
	if err != nil {
		return err
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	compiler, err := contextdoc.New(ws.Root, reg)
	if err != nil {
		return err
	}
	switch args[0] {
	case "generate":
		result, err := compiler.Generate(all, ids)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nrepositories: %d\nartifacts: %d\n", result.Repositories, result.Artifacts)
		return nil
	case "check":
		result, err := compiler.Check(all, ids)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nrepositories: %d\nartifacts: %d\npath_violations: 0\n", result.Repositories, result.Artifacts)
		return nil
	default:
		return fmt.Errorf("unknown context command %q", args[0])
	}
}

func parseTargets(args []string) (bool, []string, error) {
	if len(args) == 0 || (len(args) == 1 && args[0] == "--all") {
		return true, nil, nil
	}
	for _, arg := range args {
		if strings.HasPrefix(arg, "-") {
			return false, nil, fmt.Errorf("unknown option %q", arg)
		}
	}
	return false, args, nil
}

func load(opts options) (workspace.Workspace, registry.Registry, error) {
	ws, err := workspace.Discover(opts.root)
	if err != nil {
		return workspace.Workspace{}, registry.Registry{}, err
	}
	reg, err := registry.Load(ws.Root)
	return ws, reg, err
}

func printUsage(out io.Writer) {
	fmt.Fprintln(out, `woon - deterministic control plane for the Woon development system

Usage:
  woon init --root <path>
  woon doctor [--root <path>]
  woon repo sync [--root <path>]
  woon resolve <repo-id|repo://id/path> [--root <path>]
  woon context generate [--all|repo-id...] [--root <path>]
  woon context check [--all|repo-id...] [--root <path>]
  woon env generate [--target <macos|windows|linux>]
  woon env doctor [--all]
  woon env plan [--all]
  woon env apply [--all]
  woon env verify [--all]
  woon env check [--target <macos|windows|linux>]
  woon version`)
}
