# 저장소 표준

모든 Woon 편집 정본 저장소는 같은 제어 표면을 갖는다.

```text
README.md       사람이 읽는 진입점
woon.yaml       저장소 ID, 정책, 표준과 검증 목록
AGENTS.md       생성된 Codex 호환 지침
CLAUDE.md       생성된 Claude 지침
.cursor/rules/  생성된 Cursor 규칙
.github/        생성된 Copilot 지침과 repository workflow
docs/           오래 유지할 아키텍처와 운영 설명
```

domain 폴더는 실제로 필요할 때만 추가한다. 빈 framework 폴더와 복사된 정책 문서는 허용하지 않는다.

## 정본 소유권

- 공통 동작: `repo://core/policies/`, `repo://core/standards/`
- 저장소별 선택: 각 저장소의 `woon.yaml`
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
