package knowledge

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode"
)

const defaultWholeFileScanThresholdBytes = 64 * 1024 * 1024

type streamedSourceAnalysis struct {
	SHA256           string
	NormalizedSHA256 string
	Findings         []string
	RotationRequired bool
	SanitizedTemp    string
	Text             bool
}

func analyzeLargeSource(repo, path string) (streamedSourceAnalysis, error) {
	var result streamedSourceAnalysis
	sha, err := hashFile(path)
	if err != nil {
		return result, err
	}
	result.SHA256 = sha
	result.Text, err = isUTF8TextFile(path)
	if err != nil {
		return result, err
	}
	if !result.Text {
		result.Findings, result.RotationRequired, err = scanSecretFindings(path)
		return result, err
	}
	result.NormalizedSHA256, err = normalizedFileDigest(repo, path)
	if err != nil {
		return result, err
	}
	result.SanitizedTemp, result.Findings, result.RotationRequired, err = sanitizeTextFile(repo, path)
	return result, err
}

func normalizedFileDigest(repo, path string) (string, error) {
	runtime := filepath.Join(repo, ".knowledge-runtime", "tmp")
	if err := os.MkdirAll(runtime, 0o700); err != nil {
		return "", err
	}
	temporary, err := os.CreateTemp(runtime, "normalized-*")
	if err != nil {
		return "", err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	defer temporary.Close()
	input, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer input.Close()
	reader := bufio.NewReaderSize(input, streamingReadBufferBytes)
	wrote := false
	pendingBlank := 0
	for {
		line, readErr := reader.ReadString('\n')
		line = strings.TrimSuffix(line, "\n")
		line = strings.TrimSuffix(line, "\r")
		line = strings.TrimRight(line, " \t")
		if line == "" {
			if wrote {
				pendingBlank++
			}
		} else {
			if wrote {
				for count := 0; count <= pendingBlank; count++ {
					if _, err := temporary.WriteString("\n"); err != nil {
						return "", err
					}
				}
			}
			if !wrote {
				line = strings.TrimLeftFunc(line, unicode.IsSpace)
			}
			if _, err := temporary.WriteString(line); err != nil {
				return "", err
			}
			wrote = true
			pendingBlank = 0
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return "", readErr
		}
	}
	if err := temporary.Close(); err != nil {
		return "", err
	}
	return hashFile(temporaryPath)
}

func sanitizeTextFile(repo, path string) (string, []string, bool, error) {
	runtime := filepath.Join(repo, ".knowledge-runtime", "tmp")
	if err := os.MkdirAll(runtime, 0o700); err != nil {
		return "", nil, false, err
	}
	output, err := os.CreateTemp(runtime, "sanitized-*")
	if err != nil {
		return "", nil, false, err
	}
	outputPath := output.Name()
	cleanup := true
	defer func() {
		output.Close()
		if cleanup {
			os.Remove(outputPath)
		}
	}()
	input, err := os.Open(path)
	if err != nil {
		return "", nil, false, err
	}
	defer input.Close()
	reader := bufio.NewReaderSize(input, streamingReadBufferBytes)
	findings := map[string]bool{}
	rotation := false
	inPrivateKey := false
	privateDigest := sha256.New()
	for {
		line, readErr := reader.ReadString('\n')
		lineBytes := []byte(line)
		if inPrivateKey {
			privateDigest.Write(lineBytes)
			if strings.Contains(line, "-----END") && strings.Contains(line, "PRIVATE KEY-----") {
				placeholder := fmt.Sprintf("[REDACTED_SECRET:private-key:%s]\n", hex.EncodeToString(privateDigest.Sum(nil))[:12])
				if _, err := output.WriteString(placeholder); err != nil {
					return "", nil, false, err
				}
				inPrivateKey = false
				privateDigest.Reset()
			}
		} else if privateKeyBlockPattern.Match(lineBytes) {
			findings["private-key"] = true
			rotation = true
			lineBytes = privateKeyBlockPattern.ReplaceAllFunc(lineBytes, func(match []byte) []byte {
				return []byte(fmt.Sprintf("[REDACTED_SECRET:private-key:%s]", digest(match)[:12]))
			})
			if _, err := output.Write(lineBytes); err != nil {
				return "", nil, false, err
			}
		} else if privateKeyHeader.Match(lineBytes) {
			findings["private-key"] = true
			rotation = true
			inPrivateKey = true
			privateDigest.Write(lineBytes)
		} else {
			for _, candidate := range secretPatterns {
				if candidate.name == "private-key" {
					continue
				}
				lineBytes = candidate.pattern.ReplaceAllFunc(lineBytes, func(match []byte) []byte {
					findings[candidate.name] = true
					rotation = rotation || candidate.rotationRequired
					return []byte(fmt.Sprintf("[REDACTED_SECRET:%s:%s]", candidate.name, digest(match)[:12]))
				})
			}
			if _, err := output.Write(lineBytes); err != nil {
				return "", nil, false, err
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return "", nil, false, readErr
		}
	}
	if inPrivateKey {
		return "", nil, false, errors.New("unterminated private key block")
	}
	if err := output.Chmod(0o600); err != nil {
		return "", nil, false, err
	}
	if err := output.Close(); err != nil {
		return "", nil, false, err
	}
	names := make([]string, 0, len(findings))
	for name := range findings {
		names = append(names, name)
	}
	sort.Strings(names)
	if len(names) == 0 {
		os.Remove(outputPath)
		cleanup = false
		return "", nil, false, nil
	}
	cleanup = false
	return outputPath, names, rotation, nil
}

func scanSecretFindings(path string) ([]string, bool, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, false, err
	}
	defer file.Close()
	buffer := make([]byte, 1024*1024)
	carry := make([]byte, 0, 4096)
	findings := map[string]bool{}
	rotation := false
	for {
		count, readErr := file.Read(buffer)
		window := append(append([]byte(nil), carry...), buffer[:count]...)
		for _, candidate := range secretPatterns {
			if candidate.pattern.Match(window) || (candidate.name == "private-key" && privateKeyHeader.Match(window)) {
				findings[candidate.name] = true
				rotation = rotation || candidate.rotationRequired
			}
		}
		if len(window) > cap(carry) {
			carry = append(carry[:0], window[len(window)-cap(carry):]...)
		} else {
			carry = append(carry[:0], window...)
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return nil, false, readErr
		}
	}
	names := make([]string, 0, len(findings))
	for name := range findings {
		names = append(names, name)
	}
	sort.Strings(names)
	return names, rotation, nil
}

func copyFilePrivate(sourcePath, targetPath string) error {
	input, err := os.Open(sourcePath)
	if err != nil {
		return err
	}
	defer input.Close()
	if err := os.MkdirAll(filepath.Dir(targetPath), 0o700); err != nil {
		return err
	}
	output, err := os.OpenFile(targetPath, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	if _, err := io.CopyBuffer(output, input, make([]byte, streamingReadBufferBytes)); err != nil {
		output.Close()
		return err
	}
	return output.Close()
}
