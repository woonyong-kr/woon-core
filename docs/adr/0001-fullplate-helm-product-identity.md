# ADR-0001: Fullplate: Helm 제품 식별자

- 상태: accepted
- 날짜: 2026-08-09
- 요구 ID: U-014, U-015, U-016
- 근거 ID: D-027, D-035, D-036
- 구현 commit: `woon-core@451fc11`
- supersedes: `JustPile` 가칭, 첫 앱 이름으로서의 `Gauntlet` working name

## 문제

첫 지식 앱의 사람이 보는 이름, CLI 경로, 저장소 이름과 Library 내부 namespace가 서로 다르게 정해지면 설치·문서·migration이 다시 갈라진다. 반대로 `Helm`을 단독 executable로 사용하면 Kubernetes Helm과 직접 충돌한다. 제품명 결정과 아직 소유하지 않은 공개 배포 식별자도 구분해야 한다.

## 비교 후보

| 후보 | 장점 | 기각 또는 제한 이유 |
|---|---|---|
| `JustPile` | 동작을 바로 연상 | 한국어에서 `Pile`과 `File`의 발음 구분이 약하고 제품군 확장이 어려움 |
| `Gauntlet` | 중세 장비 제품군과 일치 | 같은 이름의 AI·launcher·agent testing 제품과 CLI가 존재 |
| bare `Helm` | 짧고 지식·방향 제어 의미가 강함 | Kubernetes의 `helm` executable·Homebrew formula와 직접 충돌 |
| `Fullplate: Helm` | 제품군과 앱 역할을 함께 표현하고 CLI namespace로 충돌 회피 | `Fullplate`와 `Helm` 검색 충돌은 남으므로 공개 식별자별 검토 필요 |

## 비교 데이터와 재현 명령

- 조사 snapshot과 출처는 [설계 근거 원장 D-027](../fullplate-helm-evidence-ledger.md#d-027-naming-gate)에 보존한다.
- registry·domain 결과는 시점에 따라 바뀌므로 release 직전에 다시 확인한다.
- 법률상 상표 clearance는 자동 검색 결과로 대체하지 않는다.

## 결정

다음 식별자를 승인한다.

| 용도 | 값 |
|---|---|
| 공식 표시명 | `Fullplate: Helm` |
| 한국어 표기 | `풀플레이트: 헬름` |
| 짧은 앱 이름 | `Helm` |
| 설명 포함 UI | `Helm · 지식 정리` |
| 제품군 ID | `fullplate` |
| app ID | `helm` |
| CLI command surface | `fullplate helm <command>` |
| 목표 저장소 이름 | `fullplate-helm` |
| Library control namespace | `.fullplate/helm/` |
| Markdown managed marker | `fullplate-helm:*` |

bare `helm` executable·package·Homebrew formula는 만들지 않는다. 실제 command binary는 `fullplate`이고 `helm`은 그 subcommand다.

조직 계정, domain, desktop bundle ID, signing identity, Homebrew formula·cask·tap, npm·PyPI·winget 등 공개 registry namespace는 이 ADR이 승인하지 않는다. 각 소유권과 배포 경로가 확인된 뒤 별도 release ADR에서 확정한다.

## 선택 이유

- 사용자가 선택한 `Helm`을 유지하면서 `Fullplate` 제품군 관계를 이름 자체에 드러낸다.
- `fullplate helm`은 bare `helm`과 command path가 달라 같은 기기에 공존할 수 있다.
- 제품군 root와 app ID를 분리해 향후 `fullplate <app>` 형태로 확장할 수 있다.
- control namespace와 managed marker를 정해 임시 `.product/` 표기가 production schema에 남는 것을 막는다.

## 기각 이유

- 기존 후보의 조사 기록을 삭제하지 않는다. `JustPile`과 첫 앱으로서의 `Gauntlet`은 역사적 후보로만 남긴다.
- bare `Helm`은 표시명 축약에는 사용할 수 있지만 기술 식별자로는 허용하지 않는다.

## 위험과 완화

- `Fullplate`와 `Helm`의 기존 사업·software 용례 때문에 검색성과 상표 위험이 남는다. 공개 배포 식별자는 별도 gate에서 재검사한다.
- `fullplate helm --workspace <path>` compatibility entry point는 Woon workspace registry 없이 실행되지만 기존 `woon knowledge` application service와 repository schema를 사용한다. 독립 제품 추출이 끝났다고 주장하지 않는다.
- 제품 추출 시 기존 workspace를 즉시 이동하지 않는다. 먼저 read-only inventory와 backup을 만들고, `.product/`가 실제 존재하면 `.fullplate/helm/` dual-read → 변환 → digest 비교 → commit → old namespace retire 순서의 migration을 제공한다.
- 저장소 remote와 로컬 폴더 rename은 별도 작업이다. rename 전후 URL redirect, CI, release, skill, trigger 경로를 검증한다.

## 검증 상태

- 표시명·한국어 표기·제품군 관계: 사용자 승인
- command contract와 compatibility `fullplate` binary: 구현·로컬 실행 검증
- control namespace, 독립 workspace, desktop app, installer, migration: 미구현
- 공개 package·domain·상표: 미승인

## 다시 검토할 조건

- 공개 배포 식별자의 소유권을 확보할 수 없음
- 법률 검토에서 사용 불가 판정
- `fullplate` root executable의 운영체제·package manager 직접 충돌 발견
- 사용자가 superseding naming decision을 승인
