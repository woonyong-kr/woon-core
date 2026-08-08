package knowledge

import (
	"fmt"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

type textBlock struct {
	text        string
	start       int
	end         int
	headingPath []string
}

type tokenSpan struct {
	start int
	end   int
}

type markdownLine struct {
	start int
	end   int
	text  string
}

// chunkDocument uses deterministic Unicode word tokens. These are retrieval
// units, not provider billing tokens; the tokenizer name is persisted in config.
func chunkDocument(sourceID, path, text string, cfg ChunkingConfig) []Chunk {
	blocks := markdownBlocks(text, cfg)
	blocks = splitOversizedBlocks(blocks, cfg.MaxTokens)
	if len(blocks) == 0 {
		return nil
	}

	var groups [][]textBlock
	for cursor := 0; cursor < len(blocks); {
		end := cursor
		tokens := 0
		for end < len(blocks) {
			count := countTokens(blocks[end].text)
			if end > cursor && tokens+count > cfg.MaxTokens {
				break
			}
			tokens += count
			end++
			if tokens >= cfg.TargetTokens {
				break
			}
		}
		if end == cursor {
			end++
		}
		groups = append(groups, append([]textBlock(nil), blocks[cursor:end]...))
		if end == len(blocks) {
			break
		}
		overlap := 0
		next := end
		for next > cursor && overlap < cfg.OverlapTokens {
			next--
			overlap += countTokens(blocks[next].text)
		}
		if next == cursor {
			next = end
		}
		cursor = next
	}

	chunks := make([]Chunk, 0, len(groups))
	for ordinal, group := range groups {
		start := group[0].start
		end := group[len(group)-1].end
		value := strings.TrimSpace(text[start:end])
		if value == "" {
			continue
		}
		contentSHA := digest([]byte(value))
		chunk := Chunk{
			ID: stableID("chunk", sourceID, fmt.Sprint(ordinal), contentSHA), SourceID: sourceID,
			Path: path, Ordinal: ordinal, HeadingPath: append([]string(nil), group[len(group)-1].headingPath...),
			StartOffset: start, EndOffset: end, TokenCount: countTokens(value), Text: value,
			ContentSHA256: contentSHA, Metadata: map[string]string{"state": "active", "kind": "raw"},
		}
		chunks = append(chunks, chunk)
	}
	for i := range chunks {
		if i > 0 {
			chunks[i].PreviousChunkID = chunks[i-1].ID
		}
		if i+1 < len(chunks) {
			chunks[i].NextChunkID = chunks[i+1].ID
		}
		chunks[i].Metadata["heading_path"] = strings.Join(chunks[i].HeadingPath, " / ")
		chunks[i].Metadata["previous_chunk_id"] = chunks[i].PreviousChunkID
		chunks[i].Metadata["next_chunk_id"] = chunks[i].NextChunkID
		chunks[i].Metadata["start_offset"] = fmt.Sprint(chunks[i].StartOffset)
		chunks[i].Metadata["end_offset"] = fmt.Sprint(chunks[i].EndOffset)
		chunks[i].Metadata["token_count"] = fmt.Sprint(chunks[i].TokenCount)
	}
	return chunks
}

func markdownBlocks(text string, cfg ChunkingConfig) []textBlock {
	if strings.TrimSpace(text) == "" {
		return nil
	}
	var lines []markdownLine
	for start := 0; start < len(text); {
		end := strings.IndexByte(text[start:], '\n')
		if end < 0 {
			end = len(text)
		} else {
			end += start + 1
		}
		lines = append(lines, markdownLine{start: start, end: end, text: text[start:end]})
		start = end
	}
	if len(lines) == 0 {
		lines = append(lines, markdownLine{0, len(text), text})
	}

	var blocks []textBlock
	headings := make([]string, 0, 6)
	for i := 0; i < len(lines); {
		trimmed := strings.TrimSpace(lines[i].text)
		if level, title, ok := markdownHeading(trimmed); ok {
			if level <= len(headings) {
				headings = headings[:level-1]
			}
			for len(headings) < level-1 {
				headings = append(headings, "")
			}
			headings = append(headings, title)
			blocks = append(blocks, textBlock{text: lines[i].text, start: lines[i].start, end: lines[i].end, headingPath: compactStrings(headings)})
			i++
			continue
		}
		if cfg.PreserveCodeBlocks && isFence(trimmed) {
			start := i
			i++
			for i < len(lines) {
				closed := isFence(strings.TrimSpace(lines[i].text))
				i++
				if closed {
					break
				}
			}
			blocks = append(blocks, blockFromLines(lines, start, i, headings))
			continue
		}
		if cfg.PreserveTables && looksLikeTableLine(trimmed) {
			start := i
			for i < len(lines) && looksLikeTableLine(strings.TrimSpace(lines[i].text)) {
				i++
			}
			blocks = append(blocks, blockFromLines(lines, start, i, headings))
			continue
		}
		if trimmed == "" {
			i++
			continue
		}
		start := i
		for i < len(lines) {
			value := strings.TrimSpace(lines[i].text)
			if value == "" || isFence(value) || looksLikeTableLine(value) {
				break
			}
			if _, _, ok := markdownHeading(value); ok && i > start {
				break
			}
			i++
		}
		blocks = append(blocks, blockFromLines(lines, start, i, headings))
	}
	return blocks
}

func blockFromLines(lines []markdownLine, start, end int, headings []string) textBlock {
	first := lines[start]
	last := lines[end-1]
	var value strings.Builder
	for _, line := range lines[start:end] {
		value.WriteString(line.text)
	}
	return textBlock{text: value.String(), start: first.start, end: last.end, headingPath: compactStrings(headings)}
}

func markdownHeading(line string) (int, string, bool) {
	level := 0
	for level < len(line) && level < 6 && line[level] == '#' {
		level++
	}
	if level == 0 || level >= len(line) || line[level] != ' ' {
		return 0, "", false
	}
	return level, strings.TrimSpace(line[level+1:]), true
}

func isFence(line string) bool {
	return strings.HasPrefix(line, "```") || strings.HasPrefix(line, "~~~")
}

func looksLikeTableLine(line string) bool {
	return strings.Count(line, "|") >= 2 && !strings.HasPrefix(line, "#")
}

func compactStrings(values []string) []string {
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value != "" {
			result = append(result, value)
		}
	}
	return result
}

func splitOversizedBlocks(blocks []textBlock, maxTokens int) []textBlock {
	var result []textBlock
	for _, block := range blocks {
		spans := unicodeTokenSpans(block.text)
		if len(spans) <= maxTokens {
			result = append(result, block)
			continue
		}
		for start := 0; start < len(spans); start += maxTokens {
			end := start + maxTokens
			if end > len(spans) {
				end = len(spans)
			}
			localStart := spans[start].start
			localEnd := spans[end-1].end
			result = append(result, textBlock{
				text: block.text[localStart:localEnd], start: block.start + localStart,
				end: block.start + localEnd, headingPath: append([]string(nil), block.headingPath...),
			})
		}
	}
	return result
}

func countTokens(text string) int { return len(unicodeTokenSpans(text)) }

func unicodeTokenSpans(text string) []tokenSpan {
	var spans []tokenSpan
	wordStart := -1
	flush := func(end int) {
		if wordStart >= 0 {
			spans = append(spans, tokenSpan{wordStart, end})
			wordStart = -1
		}
	}
	for offset := 0; offset < len(text); {
		r, size := utf8.DecodeRuneInString(text[offset:])
		if unicode.IsSpace(r) {
			flush(offset)
		} else if isCJK(r) || unicode.IsPunct(r) || unicode.IsSymbol(r) {
			flush(offset)
			spans = append(spans, tokenSpan{offset, offset + size})
		} else if wordStart < 0 {
			wordStart = offset
		}
		offset += size
	}
	flush(len(text))
	return spans
}

func isCJK(r rune) bool {
	return unicode.In(r, unicode.Han, unicode.Hangul, unicode.Hiragana, unicode.Katakana)
}

func sortChunksByOrdinal(chunks []Chunk) {
	sort.Slice(chunks, func(i, j int) bool {
		if chunks[i].SourceID == chunks[j].SourceID {
			return chunks[i].Ordinal < chunks[j].Ordinal
		}
		return chunks[i].SourceID < chunks[j].SourceID
	})
}
