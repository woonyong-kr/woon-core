# woon-core

경로에 종속되지 않는 Woon 개발 시스템의 통합 제어 도구다. 저장소를 탐색하고 `repo://` 참조를 해석하며 공통 정책과 표준을 AI별 지침으로 컴파일한다.

## 주요 기능

- 충돌을 허용하지 않는 workspace root 탐색
- 저장소 ID와 `repo://` URI 기반 경로 해석
- 공통 정책·코드·문서·폴더 표준의 단일 정본 관리
- Codex·Claude·Cursor·Copilot 지침의 결정적 생성과 drift 검사
- 운영 파일의 개인 절대경로와 토큰 예산 검사

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
```

root 후보가 서로 다르면 임의로 선택하지 않고 실패한다. `--root`, `WOON_HOME`, platform config, 상위 `.woon-root`, 기본 workspace 순서로 확인한다.

교차 저장소 참조는 안정적인 URI를 사용한다.

```text
repo://knowledge/wiki/os/page-fault.md
```

공유 registry에는 Git URL과 상대 폴더만 기록한다. 머신 경로는 local Woon config에만 저장하고 Git에 커밋하지 않는다.

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
