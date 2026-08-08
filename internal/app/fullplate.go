package app

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/woonyong-kr/woon-core/internal/buildinfo"
)

const fullplateHelmCommand = "fullplate helm"

// RunFullplate exposes the Helm product command without duplicating the
// existing knowledge application service.
func RunFullplate(rawArgs []string, stdout, stderr io.Writer) error {
	opts, workspacePath, args, err := parseFullplateGlobal(rawArgs)
	if err != nil {
		return err
	}
	if len(args) == 0 {
		printFullplateUsage(stdout)
		return nil
	}

	switch args[0] {
	case "version", "--version", "-v":
		fmt.Fprintln(stdout, buildinfo.Version)
		return nil
	case "help", "--help", "-h":
		printFullplateUsage(stdout)
		return nil
	case "helm":
		if len(args) == 1 || (len(args) == 2 && isHelpArgument(args[1])) {
			printFullplateHelmUsage(stdout)
			return nil
		}
		if workspacePath != "" {
			workspace, err := resolveFullplateWorkspace(workspacePath)
			if err != nil {
				return err
			}
			invocationArguments := []string{"--workspace", workspace, "helm"}
			return runKnowledgeRepository(workspace, workspace, args[1:], stdout, fullplateHelmCommand, invocationArguments)
		}
		return runKnowledge(opts, args[1:], stdout, fullplateHelmCommand, []string{"helm"})
	default:
		return fmt.Errorf("unknown Fullplate app %q", args[0])
	}
}

func parseFullplateGlobal(rawArgs []string) (options, string, []string, error) {
	var workspacePath string
	remaining := make([]string, 0, len(rawArgs))
	for index := 0; index < len(rawArgs); index++ {
		if rawArgs[index] != "--workspace" {
			remaining = append(remaining, rawArgs[index])
			continue
		}
		if workspacePath != "" {
			return options{}, "", nil, fmt.Errorf("--workspace may be specified only once")
		}
		if index+1 >= len(rawArgs) || strings.TrimSpace(rawArgs[index+1]) == "" {
			return options{}, "", nil, fmt.Errorf("--workspace requires a path")
		}
		workspacePath = rawArgs[index+1]
		index++
	}
	opts, args, err := parseGlobal(remaining)
	if err != nil {
		return options{}, "", nil, err
	}
	if workspacePath != "" && opts.root != "" {
		return options{}, "", nil, fmt.Errorf("--workspace and legacy --root cannot be used together")
	}
	return opts, workspacePath, args, nil
}

func resolveFullplateWorkspace(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("resolve Helm workspace path: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("resolve Helm workspace: %w", err)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", fmt.Errorf("inspect Helm workspace: %w", err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("Helm workspace must be a directory: %s", resolved)
	}
	return resolved, nil
}

func isHelpArgument(argument string) bool {
	return argument == "help" || argument == "--help" || argument == "-h"
}

func printFullplateUsage(out io.Writer) {
	fmt.Fprintln(out, `Fullplate - local-first tool suite

Usage:
  fullplate helm <command> [options]
  fullplate version

Apps:
  helm    Turn unorganized material into a synchronized Markdown library`)
}

func printFullplateHelmUsage(out io.Writer) {
	fmt.Fprintln(out, `Fullplate: Helm - local-first knowledge compiler

Usage:
  fullplate helm run
  fullplate helm scan
  fullplate helm process [--limit <count>]
  fullplate helm index
  fullplate helm search <query> [--limit <count>]
  fullplate helm status
  fullplate helm context [--scope <scope>]
  fullplate helm link <artifact-path> --kind <kind> --source <source-id>...
  fullplate helm trace <source-id-or-prefix>
  fullplate helm retire <source-id-or-prefix> --reason <text>
  fullplate helm automation <run|install|status|enable|disable|uninstall>

Global options:
  --workspace <path>    Open a knowledge repository directly
  --root <path>         Legacy Woon workspace compatibility`)
}
