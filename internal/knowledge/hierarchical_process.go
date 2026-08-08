package knowledge

import (
	"context"
	"fmt"
	"strings"
)

type hierarchicalProcessor struct {
	ctx       context.Context
	processor DocumentProcessor
	cfg       Config
	voice     string
	source    Source
	levels    [][]ProcessedDocument
	reduceSeq int
}

func processLargeSource(ctx context.Context, repo string, processor DocumentProcessor, cfg Config, voice string, source Source, readPath string) (ProcessedDocument, error) {
	absolute, err := safePath(repo, readPath)
	if err != nil {
		return ProcessedDocument{}, err
	}
	h := &hierarchicalProcessor{ctx: ctx, processor: processor, cfg: cfg, voice: voice, source: source}
	batch := make([]ProcessDocument, 0, cfg.Processing.BatchSize)
	flush := func() error {
		if len(batch) == 0 {
			return nil
		}
		request := ProcessRequest{VoiceProfile: voice, AllowedTypes: append([]string(nil), cfg.Classification.AllowedTypes...), Documents: append([]ProcessDocument(nil), batch...)}
		response, processErr := processor.Process(ctx, request)
		if processErr != nil {
			return processErr
		}
		validated, validateErr := validateProcessedDocuments(response.Documents, request, cfg)
		if validateErr != nil {
			return validateErr
		}
		for _, document := range validated {
			if addErr := h.add(0, document); addErr != nil {
				return addErr
			}
		}
		batch = batch[:0]
		return nil
	}
	err = walkFileChunks(ctx, source.ID, source.Paths[0], absolute, cfg.Chunking, func(chunk Chunk) error {
		batch = append(batch, ProcessDocument{
			SourceID: fmt.Sprintf("%s-chunk-%08d", source.ID, chunk.Ordinal), Path: source.Paths[0],
			InputHints: append(append([]string(nil), source.InputHints...), strings.Join(chunk.HeadingPath, " / ")),
			Text:       chunk.Text,
		})
		if len(batch) == cap(batch) {
			return flush()
		}
		return nil
	})
	if err != nil {
		return ProcessedDocument{}, err
	}
	if err := flush(); err != nil {
		return ProcessedDocument{}, err
	}
	partials := make([]ProcessedDocument, 0, len(h.levels)*cfg.Processing.MapReduceFanIn)
	for _, level := range h.levels {
		partials = append(partials, level...)
	}
	if len(partials) == 0 {
		return ProcessedDocument{}, fmt.Errorf("large source %s produced no chunks", source.ID)
	}
	for len(partials) > 1 {
		next := make([]ProcessedDocument, 0, (len(partials)+cfg.Processing.MapReduceFanIn-1)/cfg.Processing.MapReduceFanIn)
		for start := 0; start < len(partials); start += cfg.Processing.MapReduceFanIn {
			end := start + cfg.Processing.MapReduceFanIn
			if end > len(partials) {
				end = len(partials)
			}
			target := h.nextReduceID("merge")
			merged, mergeErr := h.reduce(target, partials[start:end])
			if mergeErr != nil {
				return ProcessedDocument{}, mergeErr
			}
			next = append(next, merged)
		}
		partials = next
	}
	return h.reduce(source.ID, partials)
}

func (h *hierarchicalProcessor) add(level int, document ProcessedDocument) error {
	for len(h.levels) <= level {
		h.levels = append(h.levels, nil)
	}
	h.levels[level] = append(h.levels[level], document)
	if len(h.levels[level]) < h.cfg.Processing.MapReduceFanIn {
		return nil
	}
	group := append([]ProcessedDocument(nil), h.levels[level]...)
	h.levels[level] = h.levels[level][:0]
	merged, err := h.reduce(h.nextReduceID(fmt.Sprintf("level-%d", level+1)), group)
	if err != nil {
		return err
	}
	return h.add(level+1, merged)
}

func (h *hierarchicalProcessor) nextReduceID(kind string) string {
	h.reduceSeq++
	return fmt.Sprintf("%s-%s-%08d", h.source.ID, kind, h.reduceSeq)
}

func (h *hierarchicalProcessor) reduce(targetID string, documents []ProcessedDocument) (ProcessedDocument, error) {
	var combined strings.Builder
	combined.WriteString("다음 부분 정리들을 하나의 원문 근거 기반 문서로 통합한다. 서로 다른 값을 임의로 선택하지 말고 불확실성으로 남긴다.\n")
	for index, document := range documents {
		fmt.Fprintf(&combined, "\n## 부분 %d: %s\n\n요약: %s\n\n%s\n", index+1, document.Title, document.Summary, document.Body)
		for _, claim := range document.Claims {
			fmt.Fprintf(&combined, "\n- claim: %s | %s | %s | %s", claim.Subject, claim.Predicate, claim.Value, claim.Scope)
		}
		for _, uncertainty := range document.Uncertainties {
			fmt.Fprintf(&combined, "\n- uncertainty: %s", uncertainty)
		}
	}
	request := ProcessRequest{
		VoiceProfile: h.voice, AllowedTypes: append([]string(nil), h.cfg.Classification.AllowedTypes...),
		Documents: []ProcessDocument{{SourceID: targetID, Path: h.source.Paths[0], InputHints: append(append([]string(nil), h.source.InputHints...), "hierarchical-map-reduce"), Text: combined.String()}},
	}
	response, err := h.processor.Process(h.ctx, request)
	if err != nil {
		return ProcessedDocument{}, err
	}
	validated, err := validateProcessedDocuments(response.Documents, request, h.cfg)
	if err != nil {
		return ProcessedDocument{}, err
	}
	return validated[0], nil
}
