# woon-core

경로에 종속되지 않는 Woon 개발 시스템의 통합 제어 도구다. 저장소를 탐색하고 `repo://` 참조를 해석하며 공통 정책과 표준을 AI별 지침으로 컴파일한다.

## 주요 기능

- 충돌을 허용하지 않는 workspace root 탐색
- 저장소 ID와 `repo://` URI 기반 경로 해석
- 공통 정책·코드·문서·폴더 표준의 단일 정본 관리
- Codex·Claude·Cursor·Copilot 지침의 결정적 생성과 drift 검사
- 운영 파일의 개인 절대경로와 토큰 예산 검사
- 지식 수집·정제·색인과 교체 가능한 embedding·vector store adapter
- manual·macOS launchd trigger를 통한 비상주 one-shot 자동화

## 설치

GitHub Releases에서 운영체제에 맞는 바이너리와 `SHA256SUMS`를 내려받는다. release binary는 Go가 없어도 실행된다.

소스에서 설치하려면 다음 명령을 사용한다.

```bash
go install github.com/woonyong-kr/woon-core/cmd/woon@latest
```

## 사용법

```bash
woon init --root /path/to/woon
woon doctor
woon repo sync
woon context generate --all
woon context check --all
woon knowledge automation install
woon knowledge automation status
woon knowledge automation run
woon knowledge status
```

root 후보가 서로 다르면 임의로 선택하지 않고 실패한다. `--root`, `WOON_HOME`, platform config, 상위 `.woon-root`, 기본 workspace 순서로 확인한다.

교차 저장소 참조는 안정적인 URI를 사용한다.

```text
repo://knowledge/wiki/os/page-fault.md
```

공유 registry에는 Git URL과 상대 폴더만 기록한다. 머신 경로는 local Woon config에만 저장하고 Git에 커밋하지 않는다.

`woon knowledge`는 `woon-knowledge/config/knowledge-workflow.yaml`을 읽어 원본을 정제·색인한다. `woon-core`가 실행 코드와 trigger adapter를 소유하고 `woon-knowledge`는 private 원본·사용자 설정·산출물만 소유한다. `woon-skills`는 Codex용 사용 절차이며 실행 시 필수 dependency가 아니다.

현재 구현을 독립 제품 `Fullplate: Helm`으로 전환할 때의 정본, Projection, 삭제, 검색 정확도, token budget과 migration 방향은 [제품·아키텍처 설계](docs/fullplate-helm-product-architecture.md)에 정의한다. 상태·transaction·삭제·복구의 규범 동작은 [정확성 계약](docs/fullplate-helm-correctness-contract.md), 선택의 출처·비교 데이터·검증 상태는 [설계 근거 원장](docs/fullplate-helm-evidence-ledger.md), 이름과 기술 식별자는 [ADR-0001](docs/adr/0001-fullplate-helm-product-identity.md)에 남긴다.

`fullplate helm` compatibility CLI는 기존 `woon knowledge` application service를 같은 프로세스에서 호출한다. `--workspace`로 지식 저장소를 직접 열면 Woon workspace registry 없이 실행할 수 있다. 다만 현재 저장소 형식은 기존 `woon-knowledge` schema이며 새 정본 schema·데스크톱 앱까지 구현됐다는 뜻은 아니다.

```bash
go build -o "$HOME/.local/bin/fullplate" ./cmd/fullplate
fullplate helm --help
fullplate helm --workspace <knowledge-repository> status
fullplate helm --workspace <knowledge-repository> run
```

자동 실행은 같은 바이너리로 관리한다.

```bash
fullplate helm --workspace <knowledge-repository> automation install
fullplate helm --workspace <knowledge-repository> automation status

# 기존 호환 명령
woon knowledge automation install
woon knowledge automation status
woon knowledge automation disable
woon knowledge automation enable
woon knowledge automation uninstall
```

`run`은 manual one-shot 명령이다. macOS에서는 `automation install`이 사용자 LaunchAgent를 생성하지만 `KeepAlive`를 사용하지 않으며, drop 폴더 변경 시 설치에 사용한 binary의 `run` 명령을 한 번 실행하고 종료한다. 현재 Fullplate 등록은 `fullplate --workspace <knowledge-repository> helm run`을 사용한다. Linux·Windows trigger는 실제 필요가 생기기 전까지 구현하지 않는다.

원본 hash와 가공물 계보는 Git 정본에 남고 LLM Wiki는 선택형 adapter이므로, 데스크톱 앱이나 Codex skill 없이도 수집·중복 검사·충돌 차단·삭제 검토가 동작한다.

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

변경 전 [저장소 표준](docs/repository-standard.md)을 확인한다. `go test ./...`, `go vet ./...`, `woon context check core`를 모두 통과해야 한다.
