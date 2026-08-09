# woon-core

경로에 종속되지 않는 Python 기반 Woon 제어 도구다. 저장소와 AI 지침을 관리하고, private Markdown 정본을 MCP로 검색·갱신·복구한다.

## 주요 기능

- 충돌을 허용하지 않는 workspace root 탐색
- 저장소 ID와 `repo://` URI 기반 경로 해석
- 공통 정책·코드·문서·폴더 표준의 단일 정본 관리
- Codex·Claude·Copilot 지침의 결정적 생성과 drift 검사
- 운영 파일의 개인 절대경로와 토큰 예산 검사
- 한 개념당 Markdown 정본 한 편과 optimistic revision 검사
- 외부 corpus의 content-addressed catalog, 파일별 병합, resume-safe ledger
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
woon knowledge index --vault /path/to/woon-knowledge
woon knowledge search '검색어' --vault /path/to/woon-knowledge
woon knowledge source-plan --source /path/to/source --source-name source --vault /path/to/woon-knowledge
woon knowledge source-reconcile --source /path/to/source --source-name source --state merge-required --limit 1 --vault /path/to/woon-knowledge
woon knowledge source-audit --source /path/to/source --source-name source --vault /path/to/woon-knowledge
```

`skills eval-routing`은 같은 catalog·prompt·JSON schema로 Codex와 Claude를 각각 격리 실행합니다. 특정 실행기만 검사하려면 `--executor codex` 또는 `--executor claude`를 사용합니다. `installable: false`인 평가 전용 profile은 validate와 routing에는 사용할 수 있지만 target plan·install은 거부됩니다.

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

제공 도구는 정본 검색·전체 읽기·대화 병합·index rebuild·audit·Git history·확인된 복구다. 같은 개념의 블로그, 기술문서, AI 전용 변형은 생성하지 않는다.

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
