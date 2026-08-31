# Docling document intake

`woon knowledge document-intake`는 외부 문서 한 파일을 local-only로 변환해 비정본 후보를 만든다. 결과는 `woon-knowledge/.local/woon-knowledge/document-intake/`에만 저장되고 `wiki/`, `catalog/llm-wiki/`, compiler receipt를 수정하지 않는다. Core의 deterministic 정제 다음에는 단일 Wiki writer가 기존 정본을 검색해 후보를 통합·중복·폐기 중 하나로 종결한다. 신원·privacy·중대한 주장처럼 사용자 선택이 결과를 바꾸는 경우만 `user-action-required`를 허용한다.

## 설치와 업데이트

개발 checkout의 project-scoped virtual environment에 고정된 extra를 설치한다. 전역 `pip`는 사용하지 않는다.

```bash
uv sync --extra documents --dev
uv run python -c 'from importlib.metadata import version; print(version("docling"))'
```

현재 lock은 `docling[rapidocr]==2.117.0`, RapidOCR의 `onnxruntime`, macOS의 한국어 Vision OCR을 위한 `ocrmac==1.0.1`을 고정한다. 업데이트는 공식 release, Python 지원 범위, lock diff를 확인한 뒤 `pyproject.toml`의 pin을 바꾸고 `uv lock`, focused test, 전체 lint/typecheck/test를 다시 실행한다. output·option·receipt 계약이 바뀌면 `DOCUMENT_INTAKE_SCHEMA_VERSION`, cleaning projection만 바뀌면 `DOCUMENT_PROJECTION_VERSION`을 올려 이전 산출물을 새 코드가 replay하지 않게 한다. `uv sync --no-extra documents`는 현재 environment에서 extra dependency를 제거하지만 lock과 adapter는 유지한다.

## 변환과 산출물

```bash
uv run woon knowledge document-intake \
  --source /path/to/document.docx \
  --source-locator approved-drop/document.docx \
  --vault /path/to/woon-knowledge
```

지원 입력은 `PDF`, `DOCX`, `PPTX`, `XLSX`, `HTML`, image(`PNG`, `JPEG`, `TIFF`, `BMP`, `WebP`)이며 `Markdown`, `AsciiDoc`, `CSV`도 같은 adapter가 처리한다. 우선순위는 model weight가 필요 없는 `DOCX/PPTX/XLSX/HTML`부터이고, PDF·image는 아래 model cache gate를 통과해야 한다.

후보 directory에는 다음 파일이 mode `0600`으로 생긴다. candidate와 별도로 locator별 observation receipt가 `observations/<candidate-id>/` 아래에 생긴다.

- `document.json`: hierarchy, reading order, table, picture, formula, code, provenance와 가능한 layout 좌표를 보존하는 lossless `DoclingDocument`
- `document.raw.md`: Docling이 내보낸 원본 Markdown projection. OCR과 reading order 대조용 증거이며 Wiki 본문으로 승격하지 않는다.
- `document.md`: page chrome, 정확한 중복, 아이콘 문자와 명백한 OCR 조각을 규칙 기반으로 제거한 정제 후보. 날짜·숫자·고유명사처럼 해석이 필요한 값은 고치지 않는다.
- `chunks.jsonl`: `HierarchicalChunker`의 heading/caption/item metadata가 있는 한 줄당 한 chunk. standalone image의 그림 내부 OCR이 누락되면 출처가 표시된 fallback chunk 하나를 추가한다.
- `quality.json`: 정제 변환 수, 글자 수, 의심 조각과 semantic curation 준비 상태
- `promotion.json`: 기존 검색과 중복 대조 뒤 source/archive 또는 compiler 경로에 넘길 body hash 계약
- `receipt.json`: adapter schema version, 원문 SHA-256, converter package versions, deterministic options, model cache manifest, 각 결과 hash와 승격 대기 상태
- `observations/<candidate-id>/<observation-id>.json`: 안전한 source locator와 candidate receipt hash를 묶는 immutable provenance

candidate ID는 adapter schema/projection version, 원문 content hash, 변환에 관여하는 Docling·OCR·ML package versions, OS·CPU·Python runtime, 변환 옵션과 model cache manifest로 결정된다. 동일 bytes와 옵션은 같은 runtime에서 locator가 달라도 후보 하나로 deduplicate하고 observation만 각각 남긴다. 동일 입력 재실행은 기존 결과 hash를 검증하고 `replayed: true`를 반환한다. 지원 형식 판정 뒤 model cache·dependency preflight와 변환이 실패하면 `failures/<failure-id>.json`만 쓰고 canonical file이나 compiler receipt는 건드리지 않는다.

## Model cache와 OCR

Docling의 PDF layout/table pipeline과 image OCR은 model artifact가 필요하다. Docling 기본 동작은 첫 실행 때 artifact를 다운로드할 수 있지만 Woon adapter는 이를 허용하지 않는다. 필요한 artifact를 명시적으로 미리 받은 뒤 그 directory를 전달한다. 이 다운로드는 공개 model weight 수신이며 private 원문 전송이 아니다.

```bash
mkdir -p /path/to/local-docling-models
uv run docling-tools models download layout tableformer rapidocr \
  --output-dir /path/to/local-docling-models

uv run woon knowledge document-intake \
  --source /path/to/document.pdf \
  --model-cache /path/to/local-docling-models \
  --ocr off \
  --vault /path/to/woon-knowledge
```

scanned PDF나 image text가 필요할 때 OCR을 명시한다. 영어·중국어 중심의 portable 경로는 `--ocr rapidocr`, macOS에서 한국어와 영어가 섞인 문서는 `--ocr ocrmac`을 사용한다. `ocrmac`은 Apple Vision의 accurate recognition을 사용하며 macOS가 아니면 fail closed한다. standalone image는 full-page OCR과 공식 `ImageFormatOption`을 사용한다. lossless JSON의 OCR이 Docling 기본 Markdown·chunk에서 생략되면 review projection에만 보강하고, 원문 해석이나 accepted claim으로 승격하지 않는다. adapter는 process-local lock 안에서 converter 생성부터 export·chunking까지 `enable_remote_services=false`, `allow_external_plugins=false`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`로 실행하고 model cache가 없으면 failure receipt와 함께 실패한다. 원문은 content hash가 일치하는 mode `0600` snapshot으로 변환하며 remote Docling service나 외부 LLM에 전송하지 않는다.

PDF/OCR release gate는 실제 local model cache를 명시해 생성한 scanned PDF의 OCR과 replay까지 실행한다.

```bash
WOON_DOCLING_MODEL_CACHE=/path/to/local-docling-models \
  uv run pytest -q tests/test_document_intake.py \
  -k scanned_pdf_with_local_rapidocr_cache
```

## 자동 종결과 승격 경계

semantic curator는 후보를 읽은 실행에서 기존 Wiki를 검색하고 아래 결과 중 하나를 local decision JSON으로 만든 뒤 Core에 기록한다.

```bash
uv run woon knowledge document-resolve \
  --decision /path/to/local-decision.json \
  --vault /path/to/woon-knowledge

uv run woon knowledge document-audit --vault /path/to/woon-knowledge
```

- `integrated`: 재사용 가치가 있는 의미만 기존 Wiki에 병합하고 원본 bytes를 `wiki/private/_sources/knowledge/`에 hash 그대로 보존한다.
- `duplicate`: 새 페이지를 만들지 않고 기존 정본과 보존 source에 연결한다.
- `discarded`: 데모·저가치·화면 chrome·범위 밖·사용 불가 추출을 visible 문서 없이 hidden receipt로 끝낸다.
- `user-action-required`: `consequential-claim`, `identity-conflict`, `privacy-boundary` 중 하나이며 질문 하나만 남긴다. 일반 OCR 잡음이나 정리 판단에는 쓰지 않는다.

`document-audit`은 미종결 후보, 사용자 판단 적체, receipt/source drift가 하나라도 있으면 실패한다. 이 실패는 유지보수 실행의 완료를 막는다.

## 제한

- Markdown은 검토용 projection이므로 layout 전체의 정본이 아니다. 구조 판단에는 `document.json`을 사용한다.
- `HierarchicalChunker`는 구조 기반이며 embedding tokenizer의 token limit을 보장하지 않는다. 검색 backend에 넣을 때 그 backend tokenizer로 별도 검증한다.
- OCR 오인식, PDF reading order, merged table cell, formula/code enrichment는 lossless raw와 대조해야 한다. 자동 정제는 명백한 chrome·정확한 중복·아이콘 잡음만 제거하며 사실을 추측해 고치지 않는다. code/formula/picture enrichment model은 기본으로 켜지 않는다.
- password-protected, 손상된 legacy Office, audio/video/XML 특수 포맷은 현재 adapter 범위 밖이다.
- PDF·image 성공 변환과 replay는 위 local model cache integration gate로 검증한다. semantic curator는 실제 업무 문서의 의미를 기존 정본에 통합하되 raw OCR을 Wiki에 복사하지 않는다.
- candidate는 accepted claim이 아니다. `promotion.json`의 body hash로 `$knowledge` 검색과 `$ingest`의 파일별 중복·privacy 검토를 묶고 terminal receipt를 남긴 뒤에만 source로 승격한다. compiler-backed page라면 이후에도 `$compile-knowledge`의 source → claim → page spec → compile/receipt audit 경로를 사용한다.

## Rollback

코드 rollback은 `pyproject.toml`의 `documents` extra, `uv.lock`, `document_intake.py`, CLI dispatch와 관련 test/docs 변경만 되돌린 뒤 `uv sync`로 environment를 맞춘다. runtime candidate는 `.local` 아래의 재생성 가능한 산출물이다. 삭제가 필요하면 candidate ID와 원문 hash를 먼저 확인하고, 사용자가 삭제를 명시적으로 승인한 경우에만 해당 candidate 또는 failure receipt 하나를 제거한다. `wiki/`, compiler catalog와 receipt는 rollback 대상이 아니다.
