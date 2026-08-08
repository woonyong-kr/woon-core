package knowledge

import (
	"bufio"
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type repeatedPatternReader struct {
	pattern []byte
	remain  int64
	offset  int
}

func (r *repeatedPatternReader) Read(target []byte) (int, error) {
	if r.remain == 0 {
		return 0, io.EOF
	}
	count := len(target)
	if int64(count) > r.remain {
		count = int(r.remain)
	}
	for index := 0; index < count; index++ {
		target[index] = r.pattern[r.offset]
		r.offset = (r.offset + 1) % len(r.pattern)
	}
	r.remain -= int64(count)
	return count, nil
}

func TestWalkReaderChunksKeepsLargeInputBoundedAndLinked(t *testing.T) {
	reader := &repeatedPatternReader{pattern: []byte("# 제목\n\n근거가 있는 긴 문장이다.\n\n"), remain: 16 * 1024 * 1024}
	cfg := ChunkingConfig{Unit: "token", Tokenizer: "unicode-word-v1", TargetTokens: 1000, MaxTokens: 1400, OverlapTokens: 150, PreserveHeadings: true, PreserveCodeBlocks: true, PreserveTables: true}
	count := 0
	maxTextBytes := 0
	previousID := ""
	err := walkReaderChunks(context.Background(), "src-large", "large.md", reader, cfg, func(chunk Chunk) error {
		count++
		if len(chunk.Text) > maxTextBytes {
			maxTextBytes = len(chunk.Text)
		}
		if chunk.TokenCount > cfg.MaxTokens {
			t.Fatalf("chunk exceeded token bound: %d", chunk.TokenCount)
		}
		if previousID != "" && chunk.PreviousChunkID != previousID {
			t.Fatalf("chunk lineage is broken: previous=%s chunk=%+v", previousID, chunk)
		}
		previousID = chunk.ID
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if count < 100 || maxTextBytes > 256*1024 {
		t.Fatalf("large stream was not bounded: chunks=%d max_text_bytes=%d", count, maxTextBytes)
	}
}

func TestAnalyzeLargeSourceRedactsSecretWithoutWholeFileResult(t *testing.T) {
	repo := t.TempDir()
	path := filepath.Join(repo, "large.txt")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := bufio.NewWriterSize(file, 64*1024)
	for index := 0; index < 100000; index++ {
		if _, err := writer.WriteString("일반 근거 문장이다.\n"); err != nil {
			t.Fatal(err)
		}
	}
	credential := "ghp_" + strings.Repeat("C", 24)
	if _, err := writer.WriteString("token=" + credential + "\n"); err != nil {
		t.Fatal(err)
	}
	if err := writer.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	analysis, err := analyzeLargeSource(repo, path)
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(analysis.SanitizedTemp)
	if !analysis.Text || analysis.SHA256 == "" || analysis.NormalizedSHA256 == "" || len(analysis.Findings) != 1 || analysis.SanitizedTemp == "" {
		t.Fatalf("large source analysis is incomplete: %+v", analysis)
	}
	sanitized, err := os.ReadFile(analysis.SanitizedTemp)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(sanitized), credential) || !strings.Contains(string(sanitized), "[REDACTED_SECRET:github-token:") {
		t.Fatal("large sanitized source leaked or lost its placeholder")
	}
}

func TestScanRoutesSourceAboveMemoryBoundThroughStreamingAnalysis(t *testing.T) {
	if testing.Short() {
		t.Skip("large streaming integration fixture")
	}
	repo := t.TempDir()
	writeFixture(t, filepath.Join(repo, "config", "knowledge-workflow.yaml"), basicKnowledgeConfig())
	path := filepath.Join(repo, "sources", "imports", "drop", "large.txt")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	pattern := []byte("streaming evidence line\n")
	size := int64((defaultWholeFileScanThresholdBytes/len(pattern))+1) * int64(len(pattern))
	reader := &repeatedPatternReader{pattern: pattern, remain: size}
	if _, err := io.Copy(file, reader); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	result, err := Scan(repo)
	if err != nil {
		t.Fatal(err)
	}
	if result.Files != 1 || result.Sources != 1 {
		t.Fatalf("large streamed source was not cataloged: %+v", result)
	}
	catalog, err := loadCatalog(repo, mustConfig(t, repo))
	if err != nil {
		t.Fatal(err)
	}
	if catalog.Sources[0].NormalizedSHA256 == "" || catalog.Sources[0].State != "active" {
		t.Fatalf("large text source analysis is incomplete: %+v", catalog.Sources[0])
	}
}
