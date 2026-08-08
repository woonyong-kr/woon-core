package knowledge

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const automationBranchEnvironment = "WOON_KNOWLEDGE_AUTOMATION_BRANCH"

type AutomationRunResult struct {
	Skipped         bool
	ProcessReceipts []ProcessResult
	Index           IndexResult
	Status          Status
	Stage           StageResult
	Committed       bool
	Pushed          bool
	CommitSHA       string
}

// RunAutomation executes the complete one-shot knowledge workflow. It owns
// orchestration only; document processing, indexing, and staging remain in
// their existing domain functions.
func RunAutomation(ctx context.Context, repo string) (AutomationRunResult, error) {
	var result AutomationRunResult
	if ctx == nil {
		return result, errors.New("context is required")
	}
	cfg, err := LoadConfig(repo)
	if err != nil {
		return result, err
	}
	locked, release, err := acquireAutomationLock(repo)
	if err != nil {
		return result, err
	}
	if !locked {
		result.Skipped = true
		return result, nil
	}
	defer release()

	if err := verifyAutomationBranch(ctx, repo); err != nil {
		return result, err
	}
	processor, err := NewConfiguredProcessor(repo, cfg)
	if err != nil {
		return result, err
	}
	for {
		receipt, processErr := ProcessPending(ctx, repo, processor, cfg.Processing.BatchSize)
		if processErr != nil {
			return result, processErr
		}
		result.ProcessReceipts = append(result.ProcessReceipts, receipt)
		if receipt.Created == 0 || receipt.Pending <= receipt.Created {
			break
		}
	}

	registry, err := NewDefaultAdapterRegistry(repo)
	if err != nil {
		return result, err
	}
	result.Index, err = IndexSources(ctx, repo, registry)
	if err != nil {
		return result, err
	}
	result.Status, err = GetStatus(repo)
	if err != nil {
		return result, err
	}
	if cfg.Automation.RequirePrivateRemote {
		if err := verifyPrivateRemote(ctx, repo); err != nil {
			return result, err
		}
	}
	result.Stage, err = StageKnowledgeChanges(ctx, repo)
	if err != nil {
		return result, err
	}
	if result.Stage.StagedFiles == 0 || !cfg.Automation.AutoCommit {
		return result, nil
	}
	if _, err := runGit(ctx, repo, "commit", "-m", cfg.Automation.CommitMessage); err != nil {
		return result, err
	}
	result.Committed = true
	result.CommitSHA, err = runGit(ctx, repo, "rev-parse", "HEAD")
	if err != nil {
		return result, err
	}
	result.CommitSHA = strings.TrimSpace(result.CommitSHA)
	if cfg.Automation.AutoPush {
		if _, err := runGit(ctx, repo, "push", "origin", "HEAD"); err != nil {
			return result, err
		}
		result.Pushed = true
	}
	return result, nil
}

func acquireAutomationLock(repo string) (bool, func(), error) {
	name := "woon-knowledge-automation-" + digest([]byte(filepath.Clean(repo)))[:16] + ".lock"
	path := filepath.Join(os.TempDir(), name)
	if err := os.Mkdir(path, 0o700); err != nil {
		if errors.Is(err, os.ErrExist) {
			return false, func() {}, nil
		}
		return false, nil, fmt.Errorf("create automation lock: %w", err)
	}
	return true, func() { _ = os.Remove(path) }, nil
}

func verifyAutomationBranch(ctx context.Context, repo string) error {
	expected := strings.TrimSpace(os.Getenv(automationBranchEnvironment))
	if expected == "" {
		return nil
	}
	current, err := runGit(ctx, repo, "branch", "--show-current")
	if err != nil {
		return err
	}
	current = strings.TrimSpace(current)
	if current != expected {
		return fmt.Errorf("refusing to run on branch %s; expected %s", current, expected)
	}
	return nil
}

func verifyPrivateRemote(ctx context.Context, repo string) error {
	cmd := exec.CommandContext(ctx, "gh", "repo", "view", "--json", "isPrivate", "--jq", ".isPrivate")
	cmd.Dir = repo
	var output boundedBuffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("verify private knowledge remote: %w: %s", err, output.String())
	}
	if strings.TrimSpace(output.String()) != "true" {
		return errors.New("refusing to push because the knowledge repository is not private")
	}
	return nil
}
