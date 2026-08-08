package knowledge

import (
	"context"
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

// chunkDocument uses deterministic Unicode word tokens. These are retrieval
// units, not provider billing tokens; the tokenizer name is persisted in config.
func chunkDocument(sourceID, path, text string, cfg ChunkingConfig) []Chunk {
	var chunks []Chunk
	_ = walkReaderChunks(context.Background(), sourceID, path, strings.NewReader(text), cfg, func(chunk Chunk) error {
		chunks = append(chunks, chunk)
		return nil
	})
	return chunks
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
