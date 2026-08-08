package knowledge

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"unicode/utf8"
)

const streamingReadBufferBytes = 64 * 1024

func hashFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.CopyBuffer(digest, file, make([]byte, streamingReadBufferBytes)); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func countFileTokensUpTo(path string, limit int) (int, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer file.Close()
	reader := bufio.NewReaderSize(file, streamingReadBufferBytes)
	total := 0
	for {
		fragment, readErr := reader.ReadSlice('\n')
		total += countTokens(string(fragment))
		if limit > 0 && total > limit {
			return total, nil
		}
		if errors.Is(readErr, io.EOF) {
			return total, nil
		}
		if readErr != nil && !errors.Is(readErr, bufio.ErrBufferFull) {
			return 0, readErr
		}
	}
}

func isUTF8TextFile(path string) (bool, error) {
	file, err := os.Open(path)
	if err != nil {
		return false, err
	}
	defer file.Close()
	buffer := make([]byte, streamingReadBufferBytes)
	pending := make([]byte, 0, utf8.UTFMax)
	for {
		count, readErr := file.Read(buffer)
		data := append([]byte(nil), pending...)
		data = append(data, buffer[:count]...)
		pending = pending[:0]
		for len(data) > 0 {
			if data[0] == 0 {
				return false, nil
			}
			if !utf8.FullRune(data) {
				pending = append(pending, data...)
				break
			}
			r, size := utf8.DecodeRune(data)
			if r == utf8.RuneError && size == 1 {
				return false, nil
			}
			data = data[size:]
		}
		if errors.Is(readErr, io.EOF) {
			return len(pending) == 0, nil
		}
		if readErr != nil {
			return false, readErr
		}
	}
}

func walkFileChunks(ctx context.Context, sourceID, displayPath, filePath string, cfg ChunkingConfig, visit func(Chunk) error) error {
	file, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer file.Close()
	return walkReaderChunks(ctx, sourceID, displayPath, file, cfg, visit)
}

func walkReaderChunks(ctx context.Context, sourceID, path string, reader io.Reader, cfg ChunkingConfig, visit func(Chunk) error) error {
	if ctx == nil || visit == nil {
		return errors.New("context and chunk visitor are required")
	}
	builder := newStreamingChunkBuilder(sourceID, path, cfg, visit)
	if err := walkMarkdownBlocks(ctx, reader, cfg, builder.addBlock); err != nil {
		return err
	}
	return builder.finish()
}

type streamingChunkBuilder struct {
	sourceID  string
	path      string
	cfg       ChunkingConfig
	visit     func(Chunk) error
	current   []textBlock
	tokens    int
	ordinal   int
	pending   *Chunk
	newBlocks int
}

func newStreamingChunkBuilder(sourceID, path string, cfg ChunkingConfig, visit func(Chunk) error) *streamingChunkBuilder {
	return &streamingChunkBuilder{sourceID: sourceID, path: path, cfg: cfg, visit: visit}
}

func (b *streamingChunkBuilder) addBlock(block textBlock) error {
	for _, bounded := range splitOversizedBlocks([]textBlock{block}, b.cfg.MaxTokens) {
		blockTokens := countTokens(bounded.text)
		if len(b.current) > 0 && b.tokens+blockTokens > b.cfg.MaxTokens {
			if err := b.flush(); err != nil {
				return err
			}
			if b.tokens+blockTokens > b.cfg.MaxTokens {
				b.current = nil
				b.tokens = 0
			}
		}
		b.current = append(b.current, bounded)
		b.tokens += blockTokens
		b.newBlocks++
		if b.tokens >= b.cfg.TargetTokens {
			if err := b.flush(); err != nil {
				return err
			}
		}
	}
	return nil
}

func (b *streamingChunkBuilder) flush() error {
	if len(b.current) == 0 || b.newBlocks == 0 {
		return nil
	}
	group := append([]textBlock(nil), b.current...)
	textParts := make([]string, 0, len(group))
	for _, block := range group {
		if value := strings.TrimSpace(block.text); value != "" {
			textParts = append(textParts, value)
		}
	}
	value := strings.Join(textParts, "\n\n")
	if value != "" {
		contentSHA := digest([]byte(value))
		chunk := Chunk{
			ID: stableID("chunk", b.sourceID, fmt.Sprint(b.ordinal), contentSHA), SourceID: b.sourceID,
			Path: b.path, Ordinal: b.ordinal, HeadingPath: append([]string(nil), group[len(group)-1].headingPath...),
			StartOffset: group[0].start, EndOffset: group[len(group)-1].end, TokenCount: countTokens(value),
			Text: value, ContentSHA256: contentSHA, Metadata: map[string]string{"state": "active", "kind": "raw"},
		}
		b.ordinal++
		if b.pending != nil {
			b.pending.NextChunkID = chunk.ID
			chunk.PreviousChunkID = b.pending.ID
			populateChunkMetadata(b.pending)
			if err := b.visit(*b.pending); err != nil {
				return err
			}
		}
		b.pending = &chunk
	}

	overlap := make([]textBlock, 0)
	overlapTokens := 0
	for index := len(group) - 1; index >= 0 && overlapTokens < b.cfg.OverlapTokens; index-- {
		count := countTokens(group[index].text)
		if count >= b.cfg.TargetTokens {
			break
		}
		overlap = append([]textBlock{group[index]}, overlap...)
		overlapTokens += count
	}
	if len(overlap) == len(group) {
		overlap = nil
		overlapTokens = 0
	}
	b.current = overlap
	b.tokens = overlapTokens
	b.newBlocks = 0
	return nil
}

func (b *streamingChunkBuilder) finish() error {
	if err := b.flush(); err != nil {
		return err
	}
	if b.pending == nil {
		return nil
	}
	populateChunkMetadata(b.pending)
	return b.visit(*b.pending)
}

func populateChunkMetadata(chunk *Chunk) {
	chunk.Metadata["heading_path"] = strings.Join(chunk.HeadingPath, " / ")
	chunk.Metadata["previous_chunk_id"] = chunk.PreviousChunkID
	chunk.Metadata["next_chunk_id"] = chunk.NextChunkID
	chunk.Metadata["start_offset"] = fmt.Sprint(chunk.StartOffset)
	chunk.Metadata["end_offset"] = fmt.Sprint(chunk.EndOffset)
	chunk.Metadata["token_count"] = fmt.Sprint(chunk.TokenCount)
}

func walkMarkdownBlocks(ctx context.Context, input io.Reader, cfg ChunkingConfig, emit func(textBlock) error) error {
	reader := bufio.NewReaderSize(input, streamingReadBufferBytes)
	headings := make([]string, 0, 6)
	var block strings.Builder
	blockStart := 0
	blockEnd := 0
	blockHeadings := []string(nil)
	mode := ""
	offset := 0
	flush := func() error {
		if block.Len() == 0 {
			return nil
		}
		value := block.String()
		block.Reset()
		return emit(textBlock{text: value, start: blockStart, end: blockEnd, headingPath: append([]string(nil), blockHeadings...)})
	}
	appendFragment := func(fragment []byte) error {
		if block.Len() == 0 {
			blockStart = offset
			blockHeadings = compactStrings(headings)
		}
		block.Write(fragment)
		blockEnd = offset + len(fragment)
		offset += len(fragment)
		if countTokens(block.String()) >= cfg.MaxTokens {
			return flush()
		}
		return nil
	}
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		fragment, readErr := reader.ReadSlice('\n')
		completeLine := readErr == nil || errors.Is(readErr, io.EOF)
		trimmed := strings.TrimSpace(string(fragment))
		if completeLine {
			if level, title, ok := markdownHeading(trimmed); ok && mode != "code" {
				if err := flush(); err != nil {
					return err
				}
				if level <= len(headings) {
					headings = headings[:level-1]
				}
				for len(headings) < level-1 {
					headings = append(headings, "")
				}
				headings = append(headings, title)
				mode = "heading"
			}
			if cfg.PreserveCodeBlocks && isFence(trimmed) {
				if mode != "code" {
					if err := flush(); err != nil {
						return err
					}
					mode = "code"
				} else {
					if err := appendFragment(fragment); err != nil {
						return err
					}
					if err := flush(); err != nil {
						return err
					}
					mode = ""
					if errors.Is(readErr, io.EOF) {
						break
					}
					continue
				}
			} else if mode != "code" {
				lineMode := "paragraph"
				if trimmed == "" {
					lineMode = "blank"
				} else if cfg.PreserveTables && looksLikeTableLine(trimmed) {
					lineMode = "table"
				} else if _, _, ok := markdownHeading(trimmed); ok {
					lineMode = "heading"
				}
				if mode != "" && mode != lineMode {
					if err := flush(); err != nil {
						return err
					}
				}
				mode = lineMode
			}
		}
		if mode != "blank" || trimmed != "" {
			if err := appendFragment(fragment); err != nil {
				return err
			}
		} else {
			offset += len(fragment)
			if err := flush(); err != nil {
				return err
			}
			mode = ""
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil && !errors.Is(readErr, bufio.ErrBufferFull) {
			return readErr
		}
	}
	return flush()
}
