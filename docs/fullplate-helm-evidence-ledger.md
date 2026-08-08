# Fullplate: Helm 설계 근거 원장

상태: 살아 있는 근거 원장
기준일: 2026-08-09
제품명: `Fullplate: Helm` (`풀플레이트: 헬름`)
설계 문서: [제품·아키텍처 설계](fullplate-helm-product-architecture.md)
규범 명세: [정확성 계약](fullplate-helm-correctness-contract.md)
로컬 기준 commit: `woon-core@451fc11`

## 1. 이 문서의 목적

이 원장은 제품이 **왜 이런 구조가 되었는지**, 무엇을 비교했고 무엇은 아직 비교하지 않았는지를 보존한다. 설계 결론만 남기지 않고 다음 계보를 연결한다.

```text
사용자 문제
→ 검토한 대안
→ 사용한 코드·제품·논문·실험 데이터
→ 선택과 기각 이유
→ 검증 수준
→ 다시 검토할 조건
→ 구현 commit·release
```

근거가 없는 선호와 측정된 결과를 섞지 않는다. 아직 local benchmark가 없는 선택은 `provisional`로 표시한다.

## 2. 근거 상태

| 상태 | 의미 |
|---|---|
| `observed` | 현재 로컬 코드·설정·명령 결과에서 직접 확인 |
| `published` | 원 논문·공식 문서·공식 저장소에서 확인 |
| `user-constraint` | 사용자가 제품에 요구한 동작·제약 |
| `inferred` | 여러 근거에서 도출했으나 직접 측정하지 않음 |
| `benchmarked` | versioned fixture와 재현 명령으로 비교 완료 |
| `provisional` | 방향은 선택했지만 adapter·수치의 실험이 남음 |
| `rejected` | 비교 후 현재 범위에서 채택하지 않음 |

`published`는 이 제품 환경에서 더 좋다는 뜻이 아니다. 공개 연구 결과가 존재한다는 뜻일 뿐이며 기본 adapter 선택에는 `benchmarked`가 필요하다.

결정 상태는 evidence 상태와 분리한다.

| 결정 상태 | 의미 |
|---|---|
| `proposed` | 아직 제품 계약으로 승인하지 않음 |
| `accepted` | 구현이 따라야 할 계약으로 승인 |
| `superseded` | 새 결정이 대체함 |
| `rejected` | 현재 범위에서 채택하지 않음 |

`accepted`는 구현·benchmark 완료를 뜻하지 않는다. 결정 표의 `검증 상태`가 `specified-unimplemented`이면 문서 계약만 존재하고 code·test는 아직 없다.

## 3. 사용자 요구 근거

대화 원문 전체를 제품 저장소에 복제하지 않는다. private 대화의 식별자와 승인된 요구 요약만 남기고, 공개 문서에는 개인 내용을 제거한다.

| 요구 ID | 출처 | 승인된 요구 | 설계 연결 |
|---|---|---|---|
| U-001 | Codex task `019fdb7c-8559-7e01-84b4-3092db1b34d6` | 아무렇게나 넣은 파일·폴더를 자동 정리 | Pile, one-shot trigger, Extractor |
| U-002 | 같은 task | 정제 Markdown을 사람이 보는 정본으로 사용 | Library canonical model |
| U-003 | 같은 task | Wiki·기술 문서·블로그 외 임의 출력 유형 지원 | Projection Type registry |
| U-004 | 같은 task | 사용자의 문체와 유형별 표현을 일관되게 적용 | Core Voice + type overlay + gold samples |
| U-005 | 같은 task | 처리된 원본은 복구 기간 후 삭제 | Recovery lifecycle, raw Git 제외 |
| U-006 | 같은 task | 정본 수정·삭제를 모든 관련 출력에 동기화 | Git diff + semantic diff + lineage |
| U-007 | 같은 task | Projection 수정은 목적별 편집과 전역 수정을 구분 | type-local edit, explicit canonical intent |
| U-008 | 같은 task | 로그는 무한히 늘지 않음 | retention과 compaction |
| U-009 | 같은 task | 상주 프로세스 금지, 실행 후 종료 | one-shot trigger와 durable queue |
| U-010 | 같은 task | embedding과 vector 저장·검색을 교체 가능한 adapter로 분리 | 독립 port와 migration contract |
| U-011 | 같은 task | 앱이 없어도 자동화 동작 | shared application service, CLI/OS trigger |
| U-012 | 같은 task | 설계·선택·비교 근거를 모두 아카이빙 | 이 Evidence Ledger와 ADR/benchmark 계보 |
| U-013 | 같은 task | 논리적 모순·미정의 실패 상태 없이 구현 가능한 명세 | 정확성 계약, 독립 상태 기계, correctness property |
| U-014 | 같은 task | 이전 가칭을 제품명으로 확정하지 않고 다른 이름 검토 | naming release gate, namespace 미확정 |
| U-015 | 같은 task | 미래 앱을 중세 갑옷 부품·무기·방패 이름으로 묶고 `Fullplate` 제품군으로 운영 | 제품군 brand architecture, namespaced distribution |
| U-016 | 같은 task | 첫 지식 앱의 표시명을 `Fullplate: Helm`으로 확정하고 제품군 아래에서 설정 | naming ADR, `fullplate helm`, `.fullplate/helm/` |

요구가 바뀌면 기존 행을 덮어쓰지 않고 새 요구 ID와 `supersedes` 관계를 추가한다.

## 4. 현재 구현에서 확인한 증거

다음은 위 commit에서 직접 확인한 사실이다.

| 증거 ID | 코드 위치 | 확인한 사실 | 영향 |
|---|---|---|---|
| C-001 | `internal/knowledge/model.go:24` | config가 automation, ingestion, chunking, retrieval, processing을 한 구조에 포함 | 새 제품에서는 관심사별 policy 파일과 schema 필요 |
| C-002 | `internal/knowledge/model.go:64` | 공통 voice profile 경로가 하나뿐 | 유형별 overlay와 반례·평가 version 부재 |
| C-003 | `internal/knowledge/model.go:100` | `PreserveOriginal` 필드 존재 | 원본 임시 lifecycle과 모델 불일치 |
| C-004 | `internal/knowledge/model.go:281` | false여도 default 적용 시 true로 강제 | 현재 설정만으로 원본 삭제 정책 구현 불가 |
| C-005 | `internal/knowledge/model.go:290` | 1000/1400/150 token 수치가 default | 측정 기반 adaptive 정책이 아님 |
| C-006 | `internal/knowledge/model.go:299` | vector 10, rerank 5가 고정 default | query complexity와 token budget 반영 없음 |
| C-007 | `internal/knowledge/model.go:376` | `unicode-word-v1`만 허용 | provider context/billing token과 다름 |
| C-008 | `internal/knowledge/model.go:394` | Git LFS·원본 보존이 안전 불변식으로 검증 | 사용자가 원하는 raw purge와 충돌 |
| C-009 | `internal/knowledge/model.go:430` | processing adapter는 `codex-cli`만 허용 | port 교체 범위가 아직 좁음 |
| C-010 | `internal/knowledge/process.go:193` | 한 voice profile을 모든 source에 적용 | 출력 목적별 문체 분리 불가 |
| C-011 | `internal/knowledge/process.go:310` | 생성 artifact kind가 `wiki`로 고정 | Projection Type 범용화 필요 |
| C-012 | `internal/knowledge/operations.go:56` | 허용 artifact kind가 코드 문자열 목록 | 사용자 정의 type 불가 |
| C-013 | `internal/knowledge/operations.go:147` | source retire가 연결 artifact를 review-required로 변경 | Recovery 삭제와 지식 삭제 의미 혼동 |
| C-014 | `internal/knowledge/retrieval.go:239` | index 입력이 active raw source | canonical-only retrieval이 아님 |
| C-015 | `internal/knowledge/retrieval.go:279` | vector 후보에 lexical boost만 더함 | 독립 sparse 후보가 없는 가짜 hybrid |
| C-016 | `internal/knowledge/retrieval.go:311` | 짧은 문서는 전체, 긴 문서는 section·이웃 확장 | 보존 가치가 있는 현재 구현 |
| C-017 | `internal/knowledge/staging.go:48` | active 원본을 Git/LFS stage | 원본 실제 삭제와 저장소 경량화 불가 |
| C-018 | `internal/knowledge/staging.go:27` | 기존 staging area가 있으면 거부 | 사용자 변경 보호를 위해 유지 |
| C-019 | `internal/knowledge/automation_trigger.go:351` | stdout/err가 고정 log 파일 | rotation이 없어 무한 증가 가능 |
| C-020 | `internal/knowledge/automation_trigger.go:367` | launchd `KeepAlive=false` | 비상주 요구와 일치해 유지 |
| C-021 | `cmd/fullplate/main.go`, `internal/app/fullplate.go` | `fullplate helm --workspace <path>`가 Woon registry 없이 기존 knowledge application service를 호출 | 실행 경계는 독립했지만 legacy repository schema는 남음 |
| C-022 | `internal/knowledge/automation_trigger.go` | launchd ProgramArguments가 entry point별 invocation과 정식 `run` command를 보존 | `woon knowledge run`과 `fullplate helm run` 모두 one-shot 등록 가능 |

줄 번호는 기준 commit에 종속된다. 이후에는 ADR과 구현 commit을 연결해 파일 이동에도 계보가 유지되게 한다.

## 5. 핵심 결정 원장

| 결정 ID | 결정 | 결정 상태 | 검증 상태 | 주 근거 | 다시 검토할 조건 |
|---|---|---|---|---|---|
| D-001 | Library Markdown을 전역 의미의 사람이 편집하는 정본으로 사용 | superseded | specified | U-002, Basic Memory, Engram, Git/Obsidian 요구 | D-018, D-021이 권위 범위를 분리함 |
| D-002 | Pile 원본은 Git에 넣지 않고 Recovery 후 삭제 | accepted | specified-unimplemented | U-005, C-004, C-017 | 규제·감사상 영구 원본 보존 workspace 필요 |
| D-003 | 출력은 범용 Projection Type으로 정의 | accepted | specified-unimplemented | U-003, Pandoc, Quarto, DITA | 형식 간 공통 contract가 실제로 불가능한 사례 발생 |
| D-004 | Git diff + Markdown AST diff + lineage로 동기화 | accepted | specified-unimplemented | U-006, W3C PROV-O, incremental view maintenance | Git 없는 동등한 revision adapter가 통과 |
| D-005 | 앱과 CLI가 같은 application service 사용 | accepted | specified-unimplemented | U-011 | 없음. 제품 불변식 |
| D-006 | trigger는 one-shot, 요청은 durable queue에 기록 | accepted | specified-unimplemented | U-009, CodeAlmanac lifecycle | 사용자가 daemon profile을 명시 승인 |
| D-007 | current-state DB와 embedding/vector/lexical index 분리 | accepted | specified-unimplemented | U-010, C-014, rebuildable index 원칙 | 단일 backend가 contract test와 migration을 모두 더 잘 충족함을 입증 |
| D-008 | 검색 기본형은 canonical BM25 + dense + RRF + rerank | accepted | provisional | RRF, DPR, SPLADE, ColBERTv2, BEIR, C-015 | 개인 gold set benchmark 결과에 따라 adapter·fusion 변경 |
| D-009 | 고정 top-k 대신 budget-aware set selection | accepted | provisional | Adaptive-k, SetR, Lost in the Middle | 단순 fixed-k가 같은 budget에서 더 높은 quality를 보임 |
| D-010 | 구조 보존 section/passage를 baseline으로 사용 | accepted | provisional | Dense X, Late Chunking, Stronger Baselines for RAG | proposition/RAPTOR가 비용 포함 benchmark에서 우위 |
| D-011 | 압축은 extractive selection부터 시작 | accepted | specified-unimplemented | RECOMP, LLMLingua 계열, source fidelity 요구 | faithful compression adapter가 gold set을 통과 |
| D-012 | 작은 모델 초안·강한 모델 검증의 routing | accepted | provisional | U-009의 비용 요구, FrugalGPT, RouteLLM, Speculative RAG | 실제 model pair별 quality/cost benchmark 후 확정 |
| D-013 | PDF·Office extractor는 Docling을 첫 비교 후보로 사용 | accepted | provisional | Docling 기능 범위, Unstructured 대안 | 설치 크기·한국어 OCR·표 fixture 비교 후 확정 |
| D-014 | Mermaid source를 Markdown에 보존 | accepted | specified-unimplemented | Markdown 정본 요구, 재현 가능한 diff | 출력 플랫폼이 Mermaid를 지원하지 않으면 파생 SVG만 추가 |
| D-015 | AGPL 제품은 아이디어 참고만 하고 코드 복사 금지 | accepted | published | Basic Memory·Khoj·Reor license | 제품 license가 AGPL-compatible로 명시 변경 |
| D-016 | Review는 fingerprint로 집계하고 stale item 자동 해소 | accepted | specified-unimplemented | 검토함 과다라는 사용자 문제 | 중요 사건 누락이 실제 테스트에서 발견됨 |
| D-017 | 앱·CLI·OS trigger는 같은 binary contract와 state를 사용 | accepted | specified-unimplemented | U-009, U-011, cross-platform 요구 | 없음. 제품 불변식 |
| D-018 | 전역 의미·출력별 편집·제어 상태·민감 기준의 권위를 분리 | accepted | specified-unimplemented | U-002, U-004, U-006, Projection 수동 편집 모순 | 모든 권위를 한 형식으로 손실 없이 표현하는 schema가 입증됨 |
| D-019 | canonical Git commit을 commit point로 하고 다른 target은 durable outbox saga로 수렴 | accepted | specified-unimplemented | U-006, Git·DB·다중 repository의 transaction 경계 | 모든 target이 동일 transactional store 안에 존재하는 단일 workspace profile |
| D-020 | Library local Git은 필수, remote와 push는 선택 | accepted | specified-unimplemented | U-006, revision·semantic diff·recovery 요구 | Git 없는 동등한 durable revision adapter가 contract test 통과 |
| D-021 | Projection 수동 편집을 versioned overlay로 수입 | accepted | specified-unimplemented | U-007, 수동 편집 보호와 재생성 가능성의 모순 | 사용자가 Projection을 절대 수정하지 않는 read-only profile |
| D-022 | retire와 secure erase를 분리하고 Git·remote 삭제 한계를 표시 | accepted | specified-unimplemented | U-005, U-006, Git object model | content history를 저장하지 않는 workspace profile |
| D-023 | ingest·retention·projection·publication을 독립 상태 기계로 관리 | accepted | specified-unimplemented | U-005, U-006, 부분 실패 정합성 | 단일 상태가 모든 orthogonal failure를 손실 없이 표현함이 입증됨 |
| D-024 | 입력 disposition과 CanonicalAsset 승격으로 raw purge와 재현성 조정 | accepted | specified-unimplemented | U-005, PDF page·image 사용 요구 | 모든 input raw를 영구 보존하는 명시적 workspace |
| D-025 | stale index보다 current control deny filter가 우선 | accepted | specified-unimplemented | 삭제 즉시성, U-006, eventual index update | index가 canonical commit과 원자적으로 갱신되는 backend |
| D-026 | update는 signed metadata, expiry, rollback protection을 검증 | accepted | specified-unimplemented | 자동 update 요구, TUF | OS package manager가 동일 보장을 전부 제공하고 app self-update를 제거 |
| D-027 | 공개 제품명·CLI·package namespace는 naming clearance 전 확정하지 않음 | accepted | specified-unimplemented | U-014 | 사용자가 이름과 collision 검토를 승인 |
| D-028 | Core Voice와 type overlay를 versioned gold eval로 관리 | accepted | specified-unimplemented | U-004, C-002, C-010 | deterministic style transform이 같은 사용자 만족도를 입증 |
| D-029 | Runtime log는 category와 global hard cap을 함께 적용 | accepted | specified-unimplemented | U-008, C-019 | 외부 log backend가 더 엄격한 동일 contract를 제공 |
| D-030 | requirement→decision→clause→property→scenario→test 계보를 release gate로 사용 | accepted | specified-unimplemented | U-012, U-013 | 더 간단한 schema가 누락 없이 역추적됨을 입증 |
| D-031 | source content·secret·adapter output을 untrusted data로 격리하고 capability를 OS 경계에서 강제 | accepted | specified-unimplemented | U-001, secret 격리 요구, OWASP prompt injection | 동등 이상의 non-model authorization 경계가 contract test 통과 |
| D-032 | working-tree materialization은 preserved compare-exchange를 사용하고 bulk delete는 invariant guard로 차단 | accepted | specified-unimplemented | U-006, F-002, F-013, F-016 | 사용자 bytes 보존과 대량 오삭제 방지를 더 단순한 primitive가 입증 |
| D-033 | identity label correction·profile retire/erase·content erase를 독립 lifecycle로 관리 | accepted | specified-unimplemented | 인물 사용자 교정 요구, privacy boundary | identity 기능을 제품 범위에서 제거 |
| D-034 | duplicate·scope/valid-time 병존·factual conflict를 claim tuple과 별도 상태로 분류 | accepted | specified-unimplemented | 동일 키워드의 다른 내용 질문, RAG 사실성 요구 | 더 단순한 모델이 같은 conflict recall을 입증 |
| D-035 | 중세 장비명을 개별 앱 working name으로, `Fullplate`를 제품군 working name으로 사용 | accepted | specified-unimplemented | U-015 | 공개 naming clearance 또는 사용자가 브랜드 방향 변경 |
| D-036 | 첫 앱 표시명은 `Fullplate: Helm`, command surface는 `fullplate helm`, control namespace는 `.fullplate/helm/`으로 사용하고 bare `helm`은 금지 | accepted | compatibility-entrypoint-implemented | U-014, U-015, U-016, D-027, C-021, C-022 | superseding naming ADR 또는 공개 식별자 검토에서 치명적 충돌 확인 |

## 6. 결정별 비교 근거

### D-001. Markdown 정본

비교 후보:

| 후보 | 장점 | 실패 모드 | 선택 |
|---|---|---|---|
| raw 원본 정본 | 최대 fidelity | 사용자가 정리된 원본을 정본으로 쓰려는 요구와 충돌, 삭제 불가 | 기각 |
| DB row 정본 | query·transaction 편리 | Obsidian·Git·사람 편집성이 떨어지고 vendor lock-in | 기각 |
| vector DB 정본 | 의미 검색 빠름 | 원문 복구·diff·수정·삭제 의미를 표현하지 못함 | 기각 |
| Markdown Library | Git diff, Obsidian, 이식성, 사람 편집 | 구조 제약과 metadata schema 필요 | 채택 |

사용 근거:

- 사용자 요구 U-002, U-005, U-011
- [Basic Memory](https://github.com/basicmachines-co/basic-memory)와 [Engram](https://github.com/semantic-craft/engram)의 Markdown source-of-truth 패턴
- [Pandoc AST](https://pandoc.org/filters.html)를 통한 구조적 diff·변환 가능성

아직 없는 데이터: 10,000 Markdown에서 AST diff·index incremental latency. Phase 0 benchmark가 필요하다.

### D-003. Projection Type

비교 후보:

- `wiki`, `blog`, `technical-doc`별 코드 분기: 빠르지만 새 유형마다 코드·migration이 필요해 기각.
- 자유 prompt 파일: 유연하지만 schema·삭제·동기화 계약을 검증할 수 없어 기각.
- versioned Projection Type: content contract, voice overlay, layout, sync, publisher를 schema로 검증할 수 있어 채택.

사용 근거:

- [Quarto profiles](https://quarto.org/docs/projects/profiles.html)의 같은 source·다른 profile
- [Pandoc](https://pandoc.org/)의 reader → AST → writer
- [DITA](https://www.oasis-open.org/standard/dita/)의 재사용 가능한 topic type
- 현재 hardcoding C-011, C-012

### D-006. One-shot trigger와 durable queue

비교 후보:

| 후보 | 자원 | 이벤트 유실 | 플랫폼성 | 판단 |
|---|---:|---:|---:|---|
| 상주 daemon | 지속 사용 | 낮음 | 별도 서비스 구현 | 사용자 제약으로 기각 |
| 단순 OS event 실행 | idle 시 0 | 동시 이벤트에서 가능 | OS adapter 필요 | queue 없이 불충분 |
| OS trigger + durable generation queue | idle 시 0 | transaction으로 방지 | adapter 교체 가능 | 채택 |
| 수동 실행만 | idle 시 0 | 자동화 없음 | 최고 | fallback으로 유지 |

현재 KeepAlive=false는 C-020으로 확인했다. durable queue의 lost wake-up 처리는 아직 구현되지 않았으므로 accepted design이지 completed implementation이 아니다.

### D-008~D-010. 검색 정확도

비교 대상:

| 방식 | 강점 | 약점 | 현재 판단 |
|---|---|---|---|
| BM25 only | exact term, 설명 가능, CPU 효율 | 표현이 다른 의미 검색 약함 | 반드시 baseline |
| dense only | paraphrase·의미 검색 | 정확 이름·숫자·희귀어 누락 가능 | 단독 기본값 기각 |
| BM25 + dense + RRF | 상호 보완, score normalization 불필요 | index 두 개 유지 | 기본 후보 |
| learned sparse SPLADE | lexical 설명력과 expansion | model·index 비용 | optional benchmark |
| ColBERT late interaction | token-level 정밀도 | 저장 공간·검색 복잡도 | optional benchmark |
| GraphRAG | global·multi-hop | 구축 token·latency·복잡도 | 초기 기본값 기각 |

논문이 보고한 수치는 서로 다른 dataset·model·hardware에 기반하므로 한 표에서 직접 우열 숫자로 합치지 않는다. 제품의 선택 데이터는 같은 corpus, query, token budget, hardware에서 다시 측정한다.

필수 비교 실험:

```text
BM25
dense-current
BM25 + dense + RRF
BM25 + dense + RRF + cross-encoder
구조 보존 DOS RAG
optional: late chunking, proposition, RAPTOR, ColBERT
```

측정값은 Recall@10, nDCG@10, conflict-pair recall, negative rejection, p95 latency, input token, index bytes다.

### D-009. Token budget

고정 `top-k=5`와 긴 context 전체 투입을 같은 token budget에서 비교해야 한다.

- [Adaptive-k](https://aclanthology.org/2025.emnlp-main.1017/)은 query별 passage 수 조절의 근거다.
- [SetR](https://aclanthology.org/2025.acl-long.861/)은 개별 순위가 아닌 근거 집합 coverage의 근거다.
- [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/)은 더 긴 context가 항상 더 좋지 않다는 근거다.
- [Stronger Baselines for RAG](https://aclanthology.org/2025.emnlp-main.1656/)은 복잡한 pipeline이 단순 구조 보존 baseline보다 반드시 낫지 않다는 반대 근거다.

따라서 초기 숫자 6,000/12,000 token은 제품 정답이 아니라 benchmark 시작점이다.

### D-012. 모델 routing

후보는 `terra only`, `sol only`, `terra draft → sol verify`, task classifier routing이다. 현재 사용자가 빠른 모델과 강한 모델을 역할별로 쓰길 원한다는 요구와 연구 근거는 있지만, 모델별 API 가격·latency·quality 데이터는 아직 이 원장에 없다. 그러므로 `provisional`이다.

확정에는 다음 데이터가 필요하다.

- 동일 50-query gold set
- 동일 prompt/profile version
- input/output token과 wall time
- unsupported claim과 style pass rate
- retry·schema failure rate
- 공개 가능한 model ID와 평가일

### D-013. 문서 extractor

Docling과 Unstructured의 기능 목록만 비교했으며 실제 한국어 PDF fixture benchmark는 아직 없다. 다음 자료로 비교해야 한다.

- text PDF, scan PDF, 2단 layout, 표, 코드, 이미지 caption, 한글·영문 혼합 각 10개 이상
- page·bounding box·reading order 정확도
- OCR character error rate
- table cell F1
- cold start와 p95 처리 시간
- peak RSS, 설치 bytes, 오프라인 동작
- 라이선스와 model download provenance

### D-018~D-025. 권위·transaction·삭제 정합성

기존 초안에는 세 가지 모순이 있었다.

1. Projection을 재생성 가능하다고 하면서 직접 수동 편집을 파일에만 남겼다.
2. Runtime DB를 재생성 가능하다고 하면서 raw purge 뒤 복구할 수 없는 lineage를 DB에만 둘 수 있었다.
3. Library, 여러 Git repository, vector index, 외부 publisher를 단일 transaction처럼 서술했다.

해결은 [정확성 계약](fullplate-helm-correctness-contract.md)으로 규범화했다.

- Library는 전역 의미, ProjectionOverlay는 type-local 의미의 권위다.
- Git-tracked current control manifest가 current lineage와 suppression의 durable 원장이다.
- local canonical Git commit이 commit point이며 다른 target은 durable outbox와 idempotent receipt로 수렴한다.
- retire는 현재 상태 제거, secure erase는 Git history와 remote까지 열거하는 별도 destructive plan이다.
- stale index에서도 current deny filter가 retracted·conflicted·suppressed·superseded claim을 먼저 차단한다.

[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)과 [WAL](https://www.sqlite.org/wal.html)은 local operational transaction의 근거이지 Git·filesystem·remote 전체를 묶는 분산 transaction의 근거가 아니다. [Git의 alternate index](https://git-scm.com/docs/git)와 [conditional ref update](https://git-scm.com/docs/git-update-ref), [commit-tree](https://git-scm.com/docs/git-commit-tree)는 사용자 staging을 재사용하지 않고 예상 tree와 조건부 commit을 만드는 구현 근거다. 실제 working tree crash recovery는 CP-002·CP-003 fault injection으로 검증해야 한다.

### D-026. 업데이트 무결성

[The Update Framework specification](https://theupdateframework.github.io/specification/latest/)은 단순 file hash뿐 아니라 rollback, freeze, mix-and-match, wrong target 공격을 다룬다. 제품 updater는 TUF-compatible implementation을 채택하거나 같은 threat fixture를 통과해야 한다. framework 이름을 적는 것만으로 구현 완료로 보지 않는다.

### D-027. Naming gate

이전 가칭은 한국어에서 `Pile`과 `File`이 모두 “파일”로 들려 제품 의미가 흐려진다는 사용자 피드백이 있다. 이름은 schema ID와 data format보다 나중에 바꿀 수 있어야 한다. 공개 명칭을 확정하기 전에는 다음을 통과해야 한다.

- 한국어·영어 발음과 오기 가능성
- app·CLI·package로 자연스러운가
- 기존 software·company·open-source project collision search
- GitHub, package registry, Homebrew tap, 주요 domain 가용성 snapshot
- 법률 자문이 필요한 상표 조사는 별도임을 명시

2026-08-09에 사용자가 `Gauntlet`을 선호했으나 공개 이름과 CLI 식별자로는 기각했다. 같은 이름의 [AI 통합·공유 메모리 서비스](https://www.trygauntlet.com/), [cross-platform application launcher와 `gauntlet` CLI](https://gauntlet.sh/docs/troubleshooting/), [AI agent testing platform](https://www.rungauntlet.tech/)이 이미 운영 중이다. 따라서 판타지 장비 세계관이나 내부 모듈명에는 사용할 수 있어도 제품명, executable, package ID에는 사용하지 않는다.

사용자는 이후 `Gauntlet`을 지식 앱의 working name으로 유지하고, 미래 앱도 갑옷 부품·무기·방패 이름으로 지어 `Fullplate`라는 하나의 제품군으로 묶기를 승인했다. 당시 우선 검토안은 앱 표시명 `Gauntlet by Fullplate`, root CLI `fullplate gauntlet`, 저장소 `fullplate-gauntlet`이었다. 이 중 제품군 구조는 유지됐지만 첫 앱 이름은 뒤의 D-036이 대체했다. `Fullplate` 역시 [동명의 AI consultancy](https://www.fullplate.ai/about)와 [상용 software product](https://kingbirdsolutions.com/)가 확인되어 법률·registry·domain clearance 전에는 organization·package ID에 고정하지 않는다.

같은 날 첫 앱의 대안으로 bare `Helm`을 검토했으나 Kubernetes package manager가 이미 [`helm` executable과 `brew install helm`](https://helm.sh/docs/intro/introduction/)을 사용하고, [AI coding assistant context용 Helm CLI](https://www.npmjs.com/package/%40helmai/cli)와 [AI knowledge platform](https://gethelm.ai/)도 운영 중이어서 단독 제품명·executable·package로는 기각했다. 같은 투구 계열의 `Barbute`도 검토했지만 사용자는 최종적으로 namespaced 표시명 `Fullplate: Helm`을 승인했다. 이에 D-036은 표시명과 app ID는 `Helm`을 쓰되 bare `helm`을 금지하고, root CLI `fullplate helm`, 목표 저장소 `fullplate-helm`, control namespace `.fullplate/helm/`으로 충돌 경계를 고정한다. 이 승인은 상표·domain·공개 registry 소유권 확보를 의미하지 않는다.

## 7. 외부 제품 비교 snapshot

확인일은 2026-08-09다. stars와 활동 상태는 시점에 따라 변하므로 선택 근거의 핵심 지표로 사용하지 않는다.

| 제품 | 확인한 특성 | license·상태 snapshot | 반영 판단 |
|---|---|---|---|
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | Markdown, Git/Obsidian, semantic·graph index, import/watch | AGPL-3.0, active, 확인 시 3,606 stars | 제품 패턴 참고, 코드 복사 금지 |
| [CodeAlmanac](https://github.com/AlmanacCode/codealmanac) | codebase Wiki, transcript sync, launchd, durable queue, local worker | Apache-2.0, active, 확인 시 789 stars | lifecycle·port 참고 |
| [Engram](https://github.com/semantic-craft/engram) | Markdown source, rebuildable index, typed graph, forget | MIT, active, 확인 시 0 stars | 성숙도 검증 전 component 채택 금지 |
| [Khoj](https://github.com/khoj-ai/khoj) | self-hosted second brain, semantic search | AGPL-3.0, active | 서버·범위가 무거워 dependency 기각 |
| [Reor](https://github.com/reorproject/reor) | local Markdown, LanceDB, Ollama | AGPL-3.0, 2026-03-07 archived | 신규 기반 기각 |
| [Open WebUI Knowledge](https://docs.openwebui.com/features/workspace/knowledge/) | full/focused context, hybrid, extractor·vector 선택 | 공식 docs, server product | 검색 UX만 참고 |
| [Fabric](https://github.com/danielmiessler/Fabric) | 작은 AI pattern registry | open source | Projection pattern 분리 참고 |
| [GraphRAG](https://github.com/microsoft/graphrag) | entity·relation·claim, local/global query | MIT | optional profile, 기본값 기각 |

CapsuleBase는 제품 사이트에서 source link가 보였으나 확인 당시 연결된 GitHub repository가 404였으므로 재현 가능한 근거로 채택하지 않았다.

## 8. 논문 근거 registry

전체 55편의 분류와 링크는 설계 문서의 [검색 정확도·토큰 설계 논문 목록](fullplate-helm-product-architecture.md#27-검색-정확도토큰-설계-논문-목록)에 보존한다. 이 원장은 결정에 직접 사용한 핵심 논문만 ID로 연결한다.

| 논문 ID | 논문 | 사용한 주장 | 결정 |
|---|---|---|---|
| P-001 | [DPR](https://aclanthology.org/2020.emnlp-main.550/) | dense retrieval 기준선 | D-008 |
| P-002 | [BEIR](https://openreview.net/forum?id=wCu6T5xFjeJ) | domain별 retriever 일반화 차이 | D-008, release gate |
| P-003 | [RRF](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/) | 여러 rank list 결합 | D-008 |
| P-004 | [ColBERTv2](https://aclanthology.org/2022.naacl-main.272/) | late interaction의 정확도·공간 trade-off | D-008 |
| P-005 | [Dense X Retrieval](https://arxiv.org/abs/2312.06648) | retrieval granularity가 성능·예산에 영향 | D-010 |
| P-006 | [Late Chunking](https://arxiv.org/abs/2409.04701) | chunk embedding의 주변 맥락 보존 | D-010 |
| P-007 | [RAPTOR](https://openreview.net/forum?id=GN921JHCRw) | 다중 추상화 계층 검색 | D-010 optional |
| P-008 | [Adaptive-k](https://aclanthology.org/2025.emnlp-main.1017/) | query별 context 크기 | D-009 |
| P-009 | [SetR](https://aclanthology.org/2025.acl-long.861/) | rank보다 근거 집합 coverage | D-009 |
| P-010 | [Lost in the Middle](https://aclanthology.org/2024.tacl-1.9/) | context 위치·길이 편향 | D-009, D-011 |
| P-011 | [Stronger Baselines for RAG](https://aclanthology.org/2025.emnlp-main.1656/) | 복잡도보다 구조·동일 예산 baseline | D-010 |
| P-012 | [RECOMP](https://proceedings.iclr.cc/paper_files/paper/2024/file/bda88ed2892f5e61c9a9bf215c566913-Paper-Conference.pdf) | selective augmentation과 context compression | D-011 |
| P-013 | [LongLLMLingua](https://aclanthology.org/2024.acl-long.91/) | query-aware compression·재배치 | D-011 optional |
| P-014 | [FrugalGPT](https://arxiv.org/abs/2305.05176) | model cascade 비용·품질 최적화 | D-012 |
| P-015 | [RouteLLM](https://arxiv.org/abs/2406.18665) | preference 기반 model routing | D-012 |
| P-016 | [RAGAS](https://aclanthology.org/2024.eacl-demo.16/) | context relevance·faithfulness 분해 | evaluation |
| P-017 | [ARES](https://aclanthology.org/2024.naacl-long.20/) | 소량 human label과 평가 confidence | evaluation |
| P-018 | [RAGChecker](https://arxiv.org/abs/2408.08067) | retrieval·generation 세부 진단 | evaluation |
| P-019 | [ColPali](https://arxiv.org/abs/2407.01449) | 시각 문서 page retrieval | multimodal optional |
| P-020 | [W3C PROV-O](https://www.w3.org/TR/prov-o/) | derivation·revision·invalidation vocabulary | D-004 |

### 8.1 비논문 규범·구현 근거 registry

| 근거 ID | 자료 | 사용한 주장 | 결정·검증 |
|---|---|---|---|
| S-001 | [SQLite Atomic Commit](https://www.sqlite.org/atomiccommit.html) | SQLite local transaction과 crash 복구 경계 | D-019, CP-002, CP-003 |
| S-002 | [SQLite WAL](https://www.sqlite.org/wal.html) | WAL mode의 concurrency·checkpoint 특성 | D-007, StateStore benchmark |
| S-003 | [Git environment와 alternate index](https://git-scm.com/docs/git) | `GIT_INDEX_FILE`로 사용자 index와 분리 | D-019, CP-010 |
| S-004 | [git-update-ref](https://git-scm.com/docs/git-update-ref) | old object 조건부 ref update | D-019, CP-002 |
| S-005 | [git-commit-tree](https://git-scm.com/docs/git-commit-tree) | 검증한 tree에서 commit object 생성 | D-019 |
| S-006 | [The Update Framework specification](https://theupdateframework.github.io/specification/latest/) | rollback·freeze·mix-and-match 방어 | D-026, update E2E |
| S-007 | [OWASP LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html) | untrusted data 분리, least privilege, output validation | 보안 경계, CP-013 |

공식 문서가 메커니즘을 제공한다는 사실과 이 제품 구현이 안전하다는 결론을 구분한다. 실제 채택은 pinned version, 구현 commit, fault fixture가 함께 있어야 `benchmarked` 또는 `verified`가 된다.

## 9. Benchmark 근거를 남기는 형식

향후 제품 저장소는 다음 구조를 정본으로 사용한다.

```text
evidence/
├─ source-registry.yaml
├─ decisions/
│  └─ adr-0001-markdown-canonical.md
├─ benchmarks/
│  └─ 01J.../
│     ├─ manifest.yaml
│     ├─ config.snapshot.yaml
│     ├─ dataset.manifest.json
│     ├─ metrics.json
│     ├─ failures.jsonl
│     └─ report.md
├─ product-snapshots/
└─ release-evidence/
```

`manifest.yaml` 최소 필드:

```yaml
run_id: 01J...
decision_ids: [D-008, D-009]
started_at: 2026-08-09T00:00:00Z
code_commit: <sha>
dirty_worktree: false
dataset:
  id: personal-retrieval-gold
  version: 1
  manifest_sha256: <sha256>
environment:
  os: <name-version>
  architecture: <arch>
  cpu: <model>
  memory_bytes: <bytes>
adapters:
  embedding: {id: <id>, version: <version>, model_revision: <revision>}
  lexical: {id: <id>, version: <version>}
  vector: {id: <id>, version: <version>}
  reranker: {id: <id>, version: <version>}
models:
  generator: {provider: <provider>, id: <id>, revision: <revision>}
budgets:
  context_tokens: 6000
  output_tokens: 1800
commands:
  - fullplate helm eval retrieval --profile baseline
artifacts:
  metrics_sha256: <sha256>
  report_sha256: <sha256>
```

개인 corpus·query text·model output은 private evidence 저장소에 둔다. 공개 repository에는 익명화한 aggregate와 synthetic fixture만 낸다.

## 10. ADR 규칙

중요 결정은 다음 template을 사용한다.

```markdown
# ADR-NNNN: 제목

- 상태: proposed | accepted | superseded | rejected
- 날짜:
- 요구 ID:
- 근거 ID:
- 구현 commit:
- supersedes:

## 문제
## 비교 후보
## 비교 데이터와 재현 명령
## 결정
## 선택 이유
## 기각 이유
## 위험과 완화
## 검증 상태
## 다시 검토할 조건
```

ADR은 과거 판단을 수정해 미화하지 않는다. 새 결정이 생기면 기존 ADR을 `superseded`로 바꾸고 새 ADR에 연결한다.

## 11. Source snapshot 정책

- 논문은 DOI/arXiv/ACL/OpenReview 식별자, 제목, 저자, version, 확인일을 저장한다.
- 저작권상 재배포할 수 없는 PDF 전체를 repository에 복제하지 않는다.
- 다운로드가 허용되고 재현에 필요하면 private cache에 원본과 SHA-256을 저장한다.
- 공식 웹 문서는 URL, 확인일, 필요한 주장, snapshot hash를 기록한다.
- GitHub 프로젝트는 owner/repo, commit 또는 release tag, license, archived 여부를 기록한다.
- stars는 성숙도 참고일 뿐 기능·품질 근거로 사용하지 않는다.
- benchmark dataset은 license, split, manifest hash, 변환 script commit을 기록한다.
- 링크가 사라져도 decision이 어떤 version을 봤는지 알 수 있도록 content hash와 bibliographic metadata를 남긴다.

## 12. 현재 증거의 한계

확인된 것:

- 현재 로컬 구현의 정본·검색·원본 staging·trigger 구조
- 공식 저장소·문서의 제품 특성·license·archive 상태
- 원 논문이 보고한 연구 결과와 평가 관점
- 사용자 요구에서 파생된 제품 불변식

아직 확인하지 않은 것:

- 정확성 계약 CP-001~CP-046의 실제 schema·property test·fault injection 구현
- 개인 10,000 Markdown corpus에서 embedding·BM25·reranker의 실제 정확도와 속도
- FastEmbed/LanceDB와 다른 adapter의 같은 조건 비교
- Docling과 Unstructured의 한국어 OCR·표 품질
- `gpt-5.6-terra`와 `gpt-5.6-sol` routing의 실제 token·latency·quality
- macOS·Linux·Windows packaging과 update rollback E2E
- 앱 UI의 실제 사용자 테스트
- 공개 제품명·CLI·package ID의 collision·상표 clearance

따라서 D-008, D-009, D-010, D-012, D-013의 구체 provider와 수치는 구현 전에 benchmark해야 한다. D-018~D-034는 accepted design이지만 contract test가 통과하기 전 completed implementation이 아니다. 이 결과가 없는 상태에서 특정 모델·DB·chunk 크기 또는 제품 전체가 “완벽하다”고 문서화하지 않는다.

## 13. 독립 독자 검토 기록

2026-08-09에 대화 문맥을 주지 않은 세 검토자가 세 문서를 각각 transaction, 삭제·사용성, 전체 architecture 관점에서 읽고 수정본을 다시 검토했다. 다음 결함은 최신 문서에 반영했지만 code와 fault test는 아직 없다.

| 결함 ID | 발견한 문제 | 반영한 계약 | 상태 |
|---|---|---|---|
| F-001 | canonical commit 뒤 DB 기록 전 crash에서 outbox 유실 | Git-tracked target desired state·outbox intent | spec-resolved, unimplemented |
| F-002 | ref CAS 뒤 editor 수정이 working tree 복구에 덮일 수 있음 | preserved atomic compare-exchange와 3-way recovery conflict | spec-resolved, unimplemented |
| F-003 | DB 손실 뒤 Recovery disposition·deadline 복구 불가 | pre-commit authenticated sidecar → capture·retention control manifest 승격 | spec-resolved, unimplemented |
| F-004 | deferred·blocked·quarantined 등 복귀 전이 없음 | closed Ingest/Projection/Publication transition table | spec-resolved, unimplemented |
| F-005 | superseded claim이 stale index deny set에서 누락 | 모든 inactive claim deny filter | spec-resolved, unimplemented |
| F-006 | secure erase의 상태·closure·undo boundary 없음 | capture selector·transitive set·shared asset·control ledger·checkpoint·remote 한계 | spec-resolved, unimplemented |
| F-007 | file missing·새 수동 파일 adoption 의미 불명 | missing confirmation과 ownership adoption 규칙 | spec-resolved, unimplemented |
| F-008 | branch 권위와 cross-platform hash 불명 | canonical branch와 SemanticWorkspaceDigest | spec-resolved, unimplemented |
| F-009 | paraphrase suppression을 HMAC만으로 설명 | versioned claim tuple 뒤 keyed HMAC | spec-resolved, unimplemented |
| F-010 | adapter 선언만 있고 OS enforcement·renderer 경계 없음 | sandbox, protocol allowlist, sanitizer, keyed receipt | spec-resolved, unimplemented |
| F-011 | 요구→test 역추적표 없음 | Clause registry와 U→D→CC→CP→AS→test matrix | spec-resolved, unimplemented |
| F-012 | legacy raw Git/LFS cutover·rollback 부족 | 별도 migration lifecycle과 acceptance | spec-resolved, unimplemented |
| F-013 | check-then-rename 사이 사용자 edit가 사라질 수 있음 | preserved atomic compare-exchange가 없으면 materialization 중지 | spec-resolved, unimplemented |
| F-014 | wall clock jump·reboot가 Recovery를 조기 purge할 수 있음 | monotonic elapsed + clock anomaly hold | spec-resolved, unimplemented |
| F-015 | raw purge 뒤 claim 근거를 검증할 최소 자료가 없음 | 정책을 통과한 EvidenceSnippet 또는 fingerprint-only 제한 | spec-resolved, unimplemented |
| F-016 | root unmount·대량 missing이 delete로 오인될 수 있음 | 비완화 safe threshold와 bulk delete approval | spec-resolved, unimplemented |
| F-017 | 공유 asset의 민감 bytes를 relation 삭제만으로 완료할 수 있음 | 전체 owner 확장·redacted replacement·block 3가지로 폐쇄 | spec-resolved, unimplemented |
| F-018 | secure erase가 자기 remote receipt·undo checkpoint를 먼저 지울 수 있음 | repo 밖 최소 EraseControlLedger와 checkpoint deletion ordering | spec-resolved, unimplemented |
| F-019 | entry point 동일성을 random plan ID와 live LLM bytes로 비교 | SemanticPlanDigest와 deterministic/persisted proposal fixture | spec-resolved, unimplemented |
| F-020 | processed-source compaction이 false positive duplicate를 만들 수 있음 | keyed HMAC exact set 유지 | spec-resolved, unimplemented |
| F-021 | 요구 추적표가 style·naming·파생 안전 요구를 과장·누락 | fine-grained clause, SR, AS-37~AS-47과 active-decision coverage 추가 | spec-resolved, unimplemented |
| F-022 | exchange 뒤 검증 전 crash가 preserved 사용자 bytes를 누락 | fsync materialization journal 우선 복구 | spec-resolved, unimplemented |
| F-023 | ref CAS 뒤 regular Git index가 old tree에 남음 | index lock/checksum/new-tree commit과 index recovery | spec-resolved, unimplemented |
| F-024 | idempotency가 remote concurrent edit를 보호하지 않음 | update/retract provider revision CAS 필수 | spec-resolved, unimplemented |
| F-025 | erase ledger의 root·key·startup·backup 권위 없음 | 필수 erase_control_root와 독립 wrapped operation key | spec-resolved, unimplemented |
| F-026 | reboot/forward clock anomaly 뒤 실제 잔여 retention 불명 | trusted remaining을 새 monotonic session에서 전부 재대기 | spec-resolved, unimplemented |
| F-027 | secure erase current partial과 취소 약속 모순 | 첫 mutation 전 full checkpoint 또는 no-undo 승인, 복구 검증 뒤 cancel | spec-resolved, unimplemented |
| F-028 | quarantine cancel/deadline과 raw-only 삭제 의미 불명 | cancel disposition과 raw-copy/source-lineage 명령 분리 | spec-resolved, unimplemented |
| F-029 | EvidenceSnippet 장기 보존을 raw purge처럼 숨길 수 있음 | onboarding evidence policy와 raw/snippet 분리 preview·receipt | spec-resolved, unimplemented |
| F-030 | bulk guard를 설정으로 사실상 비활성화 가능 | >= 비교와 file/ratio/impact invariant maximum | spec-resolved, unimplemented |
| F-031 | exact source HMAC set이 master-key rotation 불가능 | stable data key wrapping, packed exact set, reset trade-off | spec-resolved, unimplemented |
| F-032 | startup의 `current == new` 분기가 journal 검증을 우회 | 완료 조건을 journal+preserved 검증 한 곳으로 통일 | spec-resolved, unimplemented |
| F-033 | erase key/credential는 remote 전에 필요하지만 상태 기계는 current부터 삭제 | pre-local remote_authority와 final control erase 단계 추가 | spec-resolved, unimplemented |
| F-034 | checkpoint와 ledger/key backup 수명을 함께 축약 | checkpoint는 local-history 후, ledger/key는 remote terminal 후 제거 | spec-resolved, unimplemented |
| F-035 | accepted decision과 Review clause가 trace matrix에서 orphan | 모든 accepted decision·clause coverage invariant 추가 | spec-resolved, unimplemented |
| F-036 | license 결정이 provenance CP/AS에서 compatibility·금지 편입을 검사하지 않음 | CP-023·AS-35에 license/SBOM/source scan gate 추가 | spec-resolved, unimplemented |
| F-037 | pre-local remote mutation이 erase ledger commit point보다 먼저 가능 | preparing_control을 모든 local/remote mutation 전 commit point로 추가 | spec-resolved, unimplemented |
| F-038 | remote authority·control finalization 실패 전이가 없음 | 모든 execution phase의 partial·phase retry·authority retention 규칙 추가 | spec-resolved, unimplemented |

검토가 새 결함을 찾지 못했다는 사실도 correctness proof는 아니다. 다음 gate는 schema prototype과 CP-001~CP-046 fault/property/E2E test다.
