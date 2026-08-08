package app

import (
	"context"
	"fmt"
	"io"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/woonyong-kr/woon-core/internal/buildinfo"
	"github.com/woonyong-kr/woon-core/internal/contextdoc"
	"github.com/woonyong-kr/woon-core/internal/envsync"
	"github.com/woonyong-kr/woon-core/internal/knowledge"
	"github.com/woonyong-kr/woon-core/internal/registry"
	"github.com/woonyong-kr/woon-core/internal/skills"
	"github.com/woonyong-kr/woon-core/internal/workspace"
)

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
		fmt.Fprintln(stdout, buildinfo.Version)
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
	case "skills":
		return runSkills(opts, args[1:], stdout)
	case "knowledge":
		return runKnowledge(opts, args[1:], stdout)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func runKnowledge(opts options, args []string, out io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: woon knowledge <scan|process|index|search|status|context|link|trace|retire>")
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	repo, err := reg.Resolve(ws.Root, "knowledge")
	if err != nil {
		return err
	}
	switch args[0] {
	case "scan":
		if len(args) != 1 {
			return fmt.Errorf("usage: woon knowledge scan")
		}
		result, err := knowledge.Scan(repo)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nfiles: %d\nsources: %d\nreview_items: %d\n", result.Files, result.Sources, result.ReviewItems)
		return nil
	case "watch":
		cfg, err := knowledge.LoadConfig(repo)
		if err != nil {
			return err
		}
		if !cfg.Processing.AllowPersistentProcess {
			return fmt.Errorf("knowledge watch is disabled: persistent processes are not allowed; use woon knowledge process")
		}
		interval, err := knowledgeWatchInterval(args, time.Duration(cfg.PollSeconds)*time.Second)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: watching\ninterval: %s\n", interval)
		return knowledge.Watch(repo, interval, func(result knowledge.ScanResult) {
			fmt.Fprintf(out, "scan: files=%d sources=%d review_items=%d\n", result.Files, result.Sources, result.ReviewItems)
		})
	case "process":
		cfg, err := knowledge.LoadConfig(repo)
		if err != nil {
			return err
		}
		limit, err := knowledgeProcessLimit(args, cfg.Processing.BatchSize)
		if err != nil {
			return err
		}
		processor, err := knowledge.NewConfiguredProcessor(repo, cfg)
		if err != nil {
			return err
		}
		result, err := knowledge.ProcessPending(context.Background(), repo, processor, limit)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nscanned_files: %d\npending_sources: %d\ncreated_candidates: %d\nreview_items: %d\n", result.ScannedFiles, result.Pending, result.Created, result.ReviewItems)
		return nil
	case "index":
		if len(args) != 1 {
			return fmt.Errorf("usage: woon knowledge index")
		}
		registry, err := knowledge.NewDefaultAdapterRegistry(repo)
		if err != nil {
			return err
		}
		result, err := knowledge.IndexSources(context.Background(), repo, registry)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nindex: %s\nchunks: %d\nupserted: %d\ndeleted: %d\n", result.Index, result.Chunks, result.Upserted, result.Deleted)
		return nil
	case "search":
		query, limit, err := parseKnowledgeSearch(args[1:])
		if err != nil {
			return err
		}
		registry, err := knowledge.NewDefaultAdapterRegistry(repo)
		if err != nil {
			return err
		}
		results, err := knowledge.SearchSources(context.Background(), repo, registry, query, limit)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nmatches: %d\n", len(results))
		for _, result := range results {
			fmt.Fprintf(out, "- score: %.6f\n  source: %s\n  path: %s\n  ordinal: %d\n  context: %s\n  heading: %s\n  text: %s\n", result.Score, result.SourceID, result.Path, result.Ordinal, result.ContextKind, strings.Join(result.HeadingPath, " / "), strings.ReplaceAll(result.Text, "\n", " "))
		}
		return nil
	case "status":
		if len(args) != 1 {
			return fmt.Errorf("usage: woon knowledge status")
		}
		status, err := knowledge.GetStatus(repo)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nactive_sources: %d\nsanitized_sources: %d\nmissing_sources: %d\nquarantined_sources: %d\nretracted_sources: %d\nartifacts: %d\nreview_items: %d\n", status.ActiveSources, status.SanitizedSources, status.MissingSources, status.QuarantinedSources, status.RetractedSources, status.Artifacts, status.ReviewItems)
		return nil
	case "stage":
		if len(args) != 1 {
			return fmt.Errorf("usage: woon knowledge stage")
		}
		result, err := knowledge.StageKnowledgeChanges(context.Background(), repo)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nstaged_files: %d\nlfs_files: %d\nblocked_large_files: %d\n", result.StagedFiles, result.LFSFiles, len(result.BlockedLargeFiles))
		for _, path := range result.BlockedLargeFiles {
			fmt.Fprintf(out, "  blocked: %s\n", path)
		}
		return nil
	case "context":
		scope := ""
		if len(args) == 3 && args[1] == "--scope" {
			scope = args[2]
		} else if len(args) != 1 {
			return fmt.Errorf("usage: woon knowledge context [--scope <scope>]")
		}
		claims, err := knowledge.Context(repo, scope)
		if err != nil {
			return err
		}
		data, err := knowledge.EncodeContext(claims)
		if err != nil {
			return err
		}
		fmt.Fprintln(out, string(data))
		return nil
	case "link":
		artifactPath, kind, sourceIDs, err := parseKnowledgeLink(args[1:])
		if err != nil {
			return err
		}
		artifact, err := knowledge.Link(repo, artifactPath, kind, sourceIDs)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nartifact: %s\nstate: %s\nsources: %d\n", artifact.ID, artifact.State, len(artifact.SourceIDs))
		return nil
	case "trace":
		if len(args) != 2 {
			return fmt.Errorf("usage: woon knowledge trace <source-id-or-prefix>")
		}
		source, artifacts, err := knowledge.Trace(repo, args[1])
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nsource: %s\nstate: %s\n", source.ID, source.State)
		for _, path := range source.Paths {
			fmt.Fprintf(out, "  raw: %s\n", path)
		}
		for _, artifact := range artifacts {
			fmt.Fprintf(out, "  derived: %s [%s]\n", artifact.Path, artifact.State)
		}
		return nil
	case "retire":
		if len(args) != 4 || args[2] != "--reason" {
			return fmt.Errorf("usage: woon knowledge retire <source-id-or-prefix> --reason <text>")
		}
		source, artifacts, err := knowledge.Retire(repo, args[1], args[3])
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\nsource: %s\nstate: %s\naffected_artifacts: %d\nhard_deleted: 0\n", source.ID, source.State, len(artifacts))
		return nil
	default:
		return fmt.Errorf("unknown knowledge command %q", args[0])
	}
}

func parseKnowledgeSearch(args []string) (string, int, error) {
	limit := 5
	if len(args) == 0 {
		return "", 0, fmt.Errorf("usage: woon knowledge search <query> [--limit <count>]")
	}
	if len(args) >= 2 && args[len(args)-2] == "--limit" {
		parsed, err := strconv.Atoi(args[len(args)-1])
		if err != nil || parsed <= 0 || parsed > 50 {
			return "", 0, fmt.Errorf("search limit must be between 1 and 50")
		}
		limit = parsed
		args = args[:len(args)-2]
	}
	query := strings.TrimSpace(strings.Join(args, " "))
	if query == "" {
		return "", 0, fmt.Errorf("search query is required")
	}
	return query, limit, nil
}

func knowledgeProcessLimit(args []string, defaultLimit int) (int, error) {
	if len(args) == 1 {
		if defaultLimit <= 0 {
			return 0, fmt.Errorf("processing batch size must be positive")
		}
		return defaultLimit, nil
	}
	if len(args) != 3 || args[1] != "--limit" {
		return 0, fmt.Errorf("usage: woon knowledge process [--limit <count>]")
	}
	limit, err := strconv.Atoi(args[2])
	if err != nil || limit <= 0 {
		return 0, fmt.Errorf("process limit must be a positive integer")
	}
	if limit > defaultLimit {
		return 0, fmt.Errorf("process limit %d exceeds configured batch size %d", limit, defaultLimit)
	}
	return limit, nil
}

func knowledgeWatchInterval(args []string, defaultInterval time.Duration) (time.Duration, error) {
	interval := defaultInterval
	if len(args) == 3 && args[1] == "--interval" {
		parsed, err := time.ParseDuration(args[2])
		if err != nil {
			return 0, fmt.Errorf("parse watch interval: %w", err)
		}
		interval = parsed
	} else if len(args) != 1 {
		return 0, fmt.Errorf("usage: woon knowledge watch [--interval <duration>]")
	}
	if interval <= 0 {
		return 0, fmt.Errorf("watch interval must be positive")
	}
	return interval, nil
}

func parseKnowledgeLink(args []string) (string, string, []string, error) {
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return "", "", nil, fmt.Errorf("usage: woon knowledge link <artifact-path> --kind <kind> --source <source-id> [--source <source-id>...]")
	}
	artifactPath := args[0]
	kind := ""
	var sourceIDs []string
	for i := 1; i < len(args); i++ {
		if i+1 >= len(args) {
			return "", "", nil, fmt.Errorf("%s requires a value", args[i])
		}
		switch args[i] {
		case "--kind":
			kind = args[i+1]
		case "--source":
			sourceIDs = append(sourceIDs, args[i+1])
		default:
			return "", "", nil, fmt.Errorf("unknown link option %q", args[i])
		}
		i++
	}
	if kind == "" || len(sourceIDs) == 0 {
		return "", "", nil, fmt.Errorf("link requires --kind and at least one --source")
	}
	return artifactPath, kind, sourceIDs, nil
}

func runSkills(opts options, args []string, out io.Writer) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: woon skills <plan|validate|install|doctor> [--profile <names>] [--target <codex|claude>]")
	}
	profiles, target, remaining, err := parseSkillsOptions(args[1:])
	if err != nil {
		return err
	}
	if len(remaining) != 0 {
		return fmt.Errorf("unexpected skills arguments: %s", strings.Join(remaining, " "))
	}
	if args[0] == "doctor" {
		targets, err := skills.Doctor()
		if err != nil {
			return err
		}
		fmt.Fprintln(out, "status: ok")
		for _, name := range []string{"codex", "claude"} {
			fmt.Fprintf(out, "%s: %s\n", name, targets[name])
		}
		return nil
	}
	ws, reg, err := load(opts)
	if err != nil {
		return err
	}
	switch args[0] {
	case "validate":
		result, err := skills.Validate(ws.Root, reg, profiles)
		if err != nil {
			return err
		}
		printSkillsPlan(out, result)
		return nil
	case "plan":
		result, err := skills.Plan(ws.Root, reg, profiles, target)
		if err != nil {
			return err
		}
		printSkillsPlan(out, result)
		return nil
	case "install":
		result, err := skills.Install(ws.Root, reg, profiles, target)
		if err != nil {
			return err
		}
		fmt.Fprintf(out, "status: ok\ntarget: %s\ninstalled: %d\nupdated: %d\nretired: %d\nunchanged: %d\n", result.Target, result.Installed, result.Updated, result.Retired, result.Unchanged)
		if result.Backup != "" {
			fmt.Fprintf(out, "backup: %s\n", result.Backup)
		}
		return nil
	default:
		return fmt.Errorf("unknown skills command %q", args[0])
	}
}

func parseSkillsOptions(args []string) ([]string, string, []string, error) {
	var profiles []string
	var target string
	var remaining []string
	for index := 0; index < len(args); index++ {
		switch args[index] {
		case "--profile":
			if index+1 >= len(args) {
				return nil, "", nil, fmt.Errorf("--profile requires a comma-separated value")
			}
			for _, profile := range strings.Split(args[index+1], ",") {
				profile = strings.TrimSpace(profile)
				if profile != "" {
					profiles = append(profiles, profile)
				}
			}
			index++
		case "--target":
			if index+1 >= len(args) {
				return nil, "", nil, fmt.Errorf("--target requires codex or claude")
			}
			target = args[index+1]
			index++
		default:
			remaining = append(remaining, args[index])
		}
	}
	sort.Strings(profiles)
	return profiles, target, remaining, nil
}

func printSkillsPlan(out io.Writer, result skills.PlanResult) {
	fmt.Fprintf(out, "status: ok\nprofiles: %s\nskills: %d\n", strings.Join(result.Profiles, ","), len(result.Items))
	if result.Target != "" {
		fmt.Fprintf(out, "target: %s\n", result.Target)
	}
	for _, item := range result.Items {
		fmt.Fprintf(out, "  - %s: %s [%s]\n", item.Name, item.Action, strings.Join(item.Effects, ","))
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
  woon skills plan --profile <names> [--target <codex|claude>]
  woon skills validate --profile <names>
  woon skills install --profile <names> --target <codex|claude>
  woon skills doctor
  woon knowledge scan
  woon knowledge watch [--interval <duration>]
  woon knowledge status
  woon knowledge context [--scope <scope>]
  woon knowledge link <artifact-path> --kind <kind> --source <source-id>...
  woon knowledge trace <source-id-or-prefix>
  woon knowledge retire <source-id-or-prefix> --reason <text>
  woon env check [--target <macos|windows|linux>]
  woon version`)
}
