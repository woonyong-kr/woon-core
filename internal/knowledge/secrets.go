package knowledge

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"unicode/utf8"
)

type secretPattern struct {
	name             string
	pattern          *regexp.Regexp
	rotationRequired bool
}

func preserveQuarantinedSource(repo string, cfg Config, sourceID, originalPath string, data []byte) error {
	root, err := safePath(repo, cfg.Secrets.QuarantineRoot)
	if err != nil {
		return err
	}
	directory := filepath.Join(root, sourceID)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("create quarantine directory: %w", err)
	}
	path := filepath.Join(directory, filepath.Base(originalPath))
	if err := writeAtomic(path, data); err != nil {
		return fmt.Errorf("preserve quarantined source: %w", err)
	}
	if err := os.Chmod(path, 0o600); err != nil {
		return fmt.Errorf("protect quarantined source: %w", err)
	}
	return nil
}

var secretPatterns = []secretPattern{
	{"private-key", regexp.MustCompile(`(?s)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----`), true},
	{"aws-access-key", regexp.MustCompile(`(?:AKIA|ASIA)[0-9A-Z]{16}`), true},
	{"github-token", regexp.MustCompile(`gh[pousr]_[A-Za-z0-9]{20,}`), true},
	{"openai-key", regexp.MustCompile(`sk-[A-Za-z0-9_-]{20,}`), true},
	{"password-candidate", regexp.MustCompile(`(?i)(password|passwd|pwd)(\s*[:=]\s*)[^\s"']{8,}`), false},
}

var privateKeyHeader = regexp.MustCompile(`-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----`)

type secretSanitization struct {
	Data             []byte
	Findings         []string
	RotationRequired bool
}

func sanitizeSecrets(data []byte) (secretSanitization, bool) {
	if !utf8.Valid(data) || bytes.IndexByte(data, 0) >= 0 {
		return secretSanitization{}, false
	}
	result := append([]byte(nil), data...)
	hadPrivateKeyHeader := privateKeyHeader.Match(result)
	findings := map[string]bool{}
	rotation := false
	for _, candidate := range secretPatterns {
		result = candidate.pattern.ReplaceAllFunc(result, func(match []byte) []byte {
			findings[candidate.name] = true
			rotation = rotation || candidate.rotationRequired
			return []byte(fmt.Sprintf("[REDACTED_SECRET:%s:%s]", candidate.name, digest(match)[:12]))
		})
	}
	if len(findings) == 0 {
		if hadPrivateKeyHeader {
			return secretSanitization{}, false
		}
		return secretSanitization{Data: result}, true
	}
	names := make([]string, 0, len(findings))
	for name := range findings {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, candidate := range secretPatterns {
		if candidate.pattern.Match(result) {
			return secretSanitization{}, false
		}
	}
	return secretSanitization{Data: result, Findings: names, RotationRequired: rotation}, true
}

func readableSourcePath(source Source) string {
	if source.State == "sanitized" && source.SanitizedPath != "" {
		return source.SanitizedPath
	}
	if len(source.Paths) == 0 {
		return ""
	}
	return source.Paths[0]
}

func sourceIsAvailable(source Source) bool {
	return source.State == "active" || source.State == "sanitized"
}
