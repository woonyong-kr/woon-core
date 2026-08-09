# 저장소 표준

모든 Woon 편집 정본 저장소는 같은 제어 표면을 갖는다.

```text
README.md       사람이 읽는 진입점
.woon/
└── repository.yaml  저장소 ID, 정책, 표준과 검증 목록
AGENTS.md       생성된 Codex 호환 지침
CLAUDE.md       생성된 Claude 지침
.github/        생성된 Copilot 지침과 repository workflow
docs/           오래 유지할 아키텍처와 운영 설명
src/            실행 코드가 있는 저장소의 Python package
tests/          실행 코드의 자동 검증
```

`src/`와 `tests/`는 실행 코드가 있는 저장소에만 둔다. 지식·설정·배포 산출물 저장소에는 억지로 만들지 않는다. domain 폴더는 실제로 필요할 때만 추가하며 빈 framework 폴더와 복사된 정책 문서는 허용하지 않는다.

## 폴더 이름

- 저장소와 일반 폴더: `kebab-case`
- Python package: `lower_snake_case`
- Java package: 소문자 계층형 이름(`org/example/service`)
- 도구 고정 경로: `.github`, `__pycache__`처럼 도구가 이름을 정하는 경로만 예외
- 서비스 고정 저장소: GitHub Pages의 `owner.github.io` 같은 output 저장소 이름만 예외이며 내부 폴더에는 일반 규칙 적용

`woon context check`는 등록된 모든 저장소를 순회하며 이 규칙을 검사한다. 표시용 문서 제목과 frontmatter 값은 폴더 이름이 아니므로 원문의 언어를 유지한다.

## 정본 소유권

- 공통 동작: `repo://core/policies/`, `repo://core/standards/`
- 저장소별 선택: 각 저장소의 `.woon/repository.yaml`
- 머신별 값: Git에서 제외한 `*.local.yaml`
- 생성 지침: compiler 출력이며 직접 편집 금지
- 저장소별 아키텍처: 필요할 때만 local `docs/architecture.md`

## 변경 gate

1. 파일을 rename하거나 제거하기 전에 참조를 검색한다.
2. 정본만 편집한다.
3. 같은 입력으로 생성물을 두 번 만들고 hash를 비교한다.
4. 선언된 format, lint, test와 integration check를 실행한다.
5. `woon context check <repo-id>`를 실행한다.
6. 최종 diff에서 관련 없는 변경과 하드코딩된 local 값을 확인한다.
