# woon-core

경로에 종속되지 않는 Python 기반 Woon 제어 도구다. 저장소와 AI 지침을 관리하고, private Markdown 정본을 MCP로 검색·갱신·복구한다.

실제 운영 대상은 macOS다. Apple Calendar·Obsidian 로컬 자동화와 POSIX 파일 권한
계약은 macOS에서 검증하며, Linux는 전체 회귀 테스트를 위한 동일 POSIX 환경으로
사용한다. Windows는 패키지 설치·정적 검사·timezone 데이터·빌드 호환성까지만
지원하고, EventKit과 POSIX 권한 동작은 지원 대상으로 주장하지 않는다.

## 주요 기능

- 충돌을 허용하지 않는 workspace root 탐색
- 저장소 ID와 `repo://` URI 기반 경로 해석
- 공통 정책·코드·문서·폴더 표준의 단일 정본 관리
- Codex·Claude·Copilot 지침의 결정적 생성과 drift 검사
- 운영 파일의 개인 절대경로와 토큰 예산 검사
- 한 개념당 Markdown 정본 한 편과 optimistic revision 검사
- 외부 corpus의 content-addressed catalog, 파일별 병합, resume-safe ledger
- Docling 기반 local-only 문서 변환, deterministic 정제와 terminal resolution receipt
- source·claim·page spec에서 receipt가 있는 LLM Wiki를 결정론적으로 컴파일
- 교체 가능한 document, search, history port와 local stdio MCP

## 설치

Python 3.12 이상과 `uv`를 사용한다. GitHub 저장소에서 CLI와 MCP를 설치한다.

```bash
uv tool install git+https://github.com/woonyong-kr/woon-core.git
```

개발 checkout에서는 `uv sync --all-extras --dev`를 사용한다.

## 사용법

```bash
woon init --root /path/to/woon
woon doctor
woon repo sync
woon context generate --all
woon context check --all
woon skills validate --profile core
woon skills plan --profile core,python --target codex
woon skills eval-routing --executor all --repeat 3
woon knowledge compile-audit --vault /path/to/woon-knowledge
woon knowledge compile --vault /path/to/woon-knowledge
woon knowledge book-intake-audit --manifest official-books --vault /path/to/woon-knowledge
woon knowledge book-coverage-audit --vault /path/to/woon-knowledge
woon knowledge book-promote --input /path/to/verified-book-pages.json --vault /path/to/woon-knowledge
woon knowledge book-promote-retire --input /path/to/atomic-book-update.json --vault /path/to/woon-knowledge
woon knowledge book-rights-demote --input /path/to/book-rights-demotion.json --vault /path/to/woon-knowledge
woon knowledge apply-compiled-transaction --input /path/to/catalog-transaction.json --vault /path/to/woon-knowledge
woon knowledge index --vault /path/to/woon-knowledge
woon knowledge search '검색어' --vault /path/to/woon-knowledge
woon knowledge learning-checkpoint --canonical-id personal/topic --unit '현재 범위' \
  --status partial --evidence '실제 실행 근거' --unstable '남은 오류' \
  --next-question '다음에 자료 없이 답할 질문' --recorded-on 2026-08-29 \
  --expected-revision <현재-revision> --vault /path/to/woon-knowledge
woon knowledge source-plan --source /path/to/source --source-name source --vault /path/to/woon-knowledge
woon knowledge source-audit --source /path/to/source --source-name source --vault /path/to/woon-knowledge
woon knowledge document-intake --source /path/to/document.docx --vault /path/to/woon-knowledge
woon knowledge document-resolve --decision /path/to/decision.json --vault /path/to/woon-knowledge
woon knowledge document-audit --vault /path/to/woon-knowledge
woon career create --id company-role-2026 --company Company --role Role --jd /path/to/jd.pdf --vault /path/to/woon-knowledge
woon career analyze --id company-role-2026 --vault /path/to/woon-knowledge
woon career context --id company-role-2026 --vault /path/to/woon-knowledge
```

`book-promote`와 `book-promote-retire` 입력은 현재 `payload_schema_version`과
hash-pinned `book_contract`, 명시적 `coverage_manifest.mode`를 반드시 포함한다. 전권이
schema v2로 검증된 경우에만 `replace`를 사용한다. 한 장만 검증됐고 기존 전권 manifest의
나머지 장이 아직 legacy·pending이면 `merge-scope`를 사용한다. 이 모드는 전권 manifest의
경로와 SHA-256을 고정한 채 `catalog/book-coverage-scopes/<book>/<scope>.json`만 원자적으로
생성·갱신한다. 전권 파일은 byte-for-byte 유지되고, scoped audit 0건만 해당 장의 완료
근거가 된다. 전권 audit는 나머지 장을 `pending_books`로 계속 보고하며 책 전체 완료로
승격하지 않는다. `book-promote-retire`에서 `apply: false`는 revision·hash·scope 경계를
검사하는 read-only preflight이고, 동일 payload를 `apply: true`로 바꿔야 실제 writer가
compiler·tree·scope fragment·검색 index를 하나의 rollback 경계에서 갱신한다.
`book-promote`와 `book-promote-retire`는 같은 optional `staged_assets` 계약을 사용한다.
이미 존재하는 archive 경로는 staged SHA-256과 현재 bytes가 동일할 때만 idempotent하게
재사용하며, 서로 다른 bytes로의 암묵적 덮어쓰기는 거부한다. 새 asset을 설치한 뒤 후속
검증이 실패하면 transaction snapshot에서 원래 asset 상태까지 복원한다.
퇴역 page의 기존 도판을 새 archive 경로로 옮겨야 할 때만
`retirement_image_replacements`에 page별 `old_target: new_target`을 명시한다. 이 예외는
현재·대체 coverage inventory와 실제 archive SHA-256이 모두 일치하고, 기존 본문에서
정확히 한 번 나타나는 Markdown image target만 바뀔 때 허용된다. 일반 본문 차이는 계속
거부한다.

```json
{
  "mode": "merge-scope",
  "relative_path": "catalog/book-coverage-scopes/book-slug/chapter-02.json",
  "expected_sha256": null,
  "base_relative_path": "catalog/book-coverage/book-slug.json",
  "base_expected_sha256": "<current-full-manifest-sha256>",
  "scope_root_id": "books/book-slug/chapter-02",
  "replacement": {
    "schema_version": 2,
    "book_id": "books/book-slug",
    "coverage_scope": {
      "root_id": "books/book-slug/chapter-02",
      "base_relative_path": "catalog/book-coverage/book-slug.json",
      "base_sha256": "<current-full-manifest-sha256>"
    }
  }
}
```

schema v5 이하 payload와 `coverage_manifest.mode`가 없는 implicit full replacement는
fail-closed한다. 기존 payload를 hash만 바꿔 재사용하지 말고 현재 contract로 다시 생성한다.
현재 book contract는 원문의 claim·example·caution·figure·code를 semantic unit으로
전수 inventory하고 stable locator·source hash·exact-one leaf assignment를 요구한다. non-code는
실제 reader body의 unique exact span 또는 hash-pinned figure delivery를 가리켜야 하며, node별
coverage count는 모든 element assignment에서 파생된다. count-only manifest나 과거 schema의
payload는 승격 전에 거부된다.
source structure inventory는 저자·역자 서문과 소개, 본문, 부록, 참고문헌, 찾아보기를
원문 순서로 전수 분류하며 의미 있는 front/back matter와 부록을 canonical leaf로 요구한다.
Markdown 본문의 설명 표식은 밝은 원형 숫자 `①`부터 `⑩`까지 사용하며, 낮은 대비의
검은 원형 숫자 `❶`부터 `❿`까지가 남은 payload는 승격 전에 거부한다. 원본 figure asset은
변형하지 않는다.
번호 section은 descendant wrapper가 아니라 Map H2 group이고, 퇴역 wrapper prose는 첫 terminal
leaf의 exact relocated span evidence로 보존한다.
승격할 기존 page, coverage manifest, retire할 wrapper의 현재 revision·hash를 다시 확인한 뒤
compiler input·generated output·coverage manifest·검색 index를 하나의 rollback 경계에서
갱신한다. 본문과 coverage를 따로 쓰거나 `book-promote`와 retirement를 분리해 중간 tree를
노출하지 않는다.

`apply-compiled-transaction` 입력은 `apply`, `expected_revisions`, `sources_upsert`,
`claims_upsert`, `pages_upsert`, `curations_upsert`만 받는다. `apply`는 `true`여야 하고,
기존 page는 현재 Markdown SHA-256, 신규 page는 `null` revision과 실제 부재를 요구한다.
명령은 compiler catalog 입력, generated output, compile audit와 검색 index를 하나의 exclusive
lock과 rollback 경계에서 갱신하며 source·claim ID의 비동일 충돌을 거부한다.

## 지원 파이프라인

`woon career`는 지원 하나를 `wiki/personal/career/applications/<id>.md` 한 편에서 관리한다. JD와 PDF는 private source로 hash를 보존하며, 별도 JSON tracker나 context 저장소를 만들지 않는다.

- `analyze`: JD 문장과 기존 Wiki를 대조하되 결과를 사람 검토 전 후보로만 둔다.
- `evaluate`: 사람이 검토한 `verified`·`adjacent`·`gap` 판정과 Wiki 근거를 기록한다.
- `approve-draft` → `attach-pdf --kind draft` → `mark-reviewed` → `mark-ready`: 명시 확인을 거쳐 초안을 제출 가능 상태로 올린다.
- `attach-pdf --kind submitted --confirmed true`: 검증된 실제 제출 PDF 복사와 지원 상태 변경을 같은 잠금·복구 경계에서 수행한다.
- `outcome --confirmed true`: 제출 뒤 면접·합격·불합격·철회·종료 결과를 기록한다.
- `context`: 현재 Wiki 검색 결과를 제한된 크기로 조립해 출력할 뿐 저장하지 않는다.

PDF 렌더러는 각 문서 저장소가 소유한다. Career pipeline은 렌더러가 만든 PDF를 읽어 페이지와 hash를 검증한 뒤 지원 기록과 함께 보존하며, 자동 지원·메일 전송·공개 게시를 수행하지 않는다.

`skills eval-routing`은 같은 catalog·prompt·JSON schema로 Codex와 Claude를 각각 격리 실행합니다. 특정 실행기만 검사하려면 `--executor codex` 또는 `--executor claude`를 사용합니다. `installable: false`인 평가 전용 profile은 validate와 routing에는 사용할 수 있지만 target plan·install은 거부됩니다.

문서 변환 설치·model cache·privacy·rollback 운영 계약은 [Docling document intake](docs/docling-document-intake.md)를 따른다. 동일 bytes는 content candidate 하나로 합치고 locator별 observation을 분리한다. 변환 결과는 Wiki가 아니며 기존 정본 검색과 의미 정리를 거쳐 `integrated`, `duplicate`, `discarded`, 예외적인 `user-action-required` 중 하나의 terminal receipt로 끝나야 한다. 현재 권한으로 판단 가능한 중복·저가치·명확한 통합을 Review 적체로 남기지 않는다.

스킬 설치 경로는 Woon 전용 직접 경로가 가장 우선합니다. 설정하지 않으면 각 executor의 표준 home 아래 `skills`를 사용하고, 표준 home도 없으면 사용자 기본 경로를 사용합니다.

| target | 직접 경로 | executor home | 기본 경로 |
|---|---|---|---|
| Codex | `WOON_CODEX_SKILLS_HOME` | `CODEX_HOME/skills` | `~/.codex/skills` |
| Claude | `WOON_CLAUDE_SKILLS_HOME` | `CLAUDE_CONFIG_DIR/skills` | `~/.claude/skills` |

격리된 plan·install 검증은 임시 executor home을 명시합니다.

```bash
CODEX_HOME=/tmp/woon-codex-eval woon skills plan --profile learning --target codex
CLAUDE_CONFIG_DIR=/tmp/woon-claude-eval woon skills plan --profile learning --target claude
```

root 후보가 서로 다르면 임의로 선택하지 않고 실패한다. `--root`, `WOON_HOME`, platform config, 상위 `.woon-root`, 기본 workspace 순서로 확인한다.

교차 저장소 참조는 안정적인 URI를 사용한다.

```text
repo://knowledge/wiki/os/page-fault.md
```

공유 registry에는 Git URL과 상대 폴더만 기록한다. 머신 경로는 local Woon config에만 저장하고 Git에 커밋하지 않는다.

## 정본 지식 MCP

`woon-knowledge-mcp`는 client가 stdio 연결을 유지하는 동안에만 실행되며 background daemon을 만들지 않는다. `WOON_KNOWLEDGE_ROOT`에 private vault를 지정한다.

Codex에는 설치된 실행 파일과 vault를 한 번 등록한다.

```bash
codex mcp add woon-knowledge \
  --env WOON_KNOWLEDGE_ROOT=/path/to/woon-knowledge \
  -- "$(uv tool dir --bin)/woon-knowledge-mcp"
```

등록 뒤 새 Codex 작업에서 `woon_knowledge_search`를 사용할 수 있다. 설정 확인과 제거는 각각 `codex mcp get woon-knowledge`, `codex mcp remove woon-knowledge`다.

제공 도구는 정본 검색·전체 읽기·대화 병합·학습 체크포인트·LLM Wiki compile/receipt audit·index rebuild·audit·Git history·확인된 복구다. 학습 체크포인트는 최신 revision을 요구하며 compiler-owned page는 새 curated source·claim·receipt로, 그 밖의 canonical page는 YAML을 보존하는 body writer로 갱신한다. 같은 개념의 블로그, 기술문서, AI 전용 변형은 생성하지 않는다.

IDE 설정은 같은 바이너리에서 관리한다.

```bash
woon env doctor --all
woon env plan --all
woon env generate
woon env apply --all
woon env verify --all
```

## 저장소 구성

- `woon-core`: policy and orchestration
- `woon-skills`: skill catalog, profiles, locks, and conflicts
- `woon-env`: deterministic IDE configuration
- `woon-knowledge`: private durable knowledge
- `woon-site`: private publishing source
- `woonyong-kr`: generated GitHub profile output
- `woonyong-kr.github.io`: protected Pages output

두 출력 저장소는 GitHub가 요구하는 이름을 유지하며 rename 대상이 아니다.

## 기여

변경 전 [저장소 표준](docs/repository-standard.md)을 확인한다. `uv run ruff check src tests`, `uv run mypy src`, `uv run pytest`, `woon context check core`를 모두 통과해야 한다.
