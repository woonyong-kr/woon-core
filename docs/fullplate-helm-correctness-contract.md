# Fullplate: Helm 정확성 계약

상태: 구현 전 규범 명세
기준일: 2026-08-09
제품명: `Fullplate: Helm` (`풀플레이트: 헬름`)
제품 방향 문서: [제품·아키텍처 설계](fullplate-helm-product-architecture.md)
비규범 근거: [설계 근거 원장](fullplate-helm-evidence-ledger.md)

이 문서는 제품의 **모든 규범 동작**에서 최우선이다. 제품·아키텍처 설계는 사용자 문제와 비규범 기본 방향을 설명하고, 근거 원장은 선택의 출처와 검증 상태를 기록한다. 충돌이 발견되면 구현은 이 계약을 따르되 release를 중지하고 이 계약과 하위 문서를 같은 변경에서 함께 수정한다.

## 1. 목적과 완전성의 의미

이 문서는 정상 흐름만 설명하지 않는다. 입력, 사용자 편집, 삭제, 충돌, crash, 저장소 분리, adapter 교체, 업데이트 중 어느 사건이 발생해도 다음 상태와 복구 행동이 하나로 결정되게 한다.

이 문서에서 말하는 설계 완료는 다음을 뜻한다.

- 모든 durable entity의 권위 저장소와 수명주기가 정해져 있다.
- 모든 destructive action은 대상, commit point, undo 범위가 정해져 있다.
- 모든 비동기 작업은 재실행해도 같은 최종 상태로 수렴한다.
- 부분 성공을 전체 성공으로 표시하지 않는다.
- 미측정 provider와 수치는 `provisional` 변수로 격리되고 선택 gate가 있다.
- 구현의 모든 `MUST`는 contract test 또는 acceptance scenario에 연결된다.

설계 문서만으로 실제 품질을 100% 보장할 수는 없다. 모델 품질, OCR 정확도, OS별 packaging처럼 실행으로만 확인할 수 있는 항목은 미결정 상태가 아니라 **검증 대기 상태와 통과 조건이 정의된 변수**다. benchmark와 E2E가 통과하기 전 release 상태를 `verified`로 올리지 않는다.

규범 용어는 다음처럼 사용한다.

- `MUST`: 위반하면 데이터 정합성·보안·사용자 의도가 깨진다.
- `SHOULD`: 기본 구현이 따라야 하며 예외는 ADR과 검증 근거가 필요하다.
- `MAY`: workspace 또는 adapter가 선택할 수 있다.

## 2. 권위 모델

“정본”을 하나의 뜻으로 사용하면 사람이 쓴 글, 출력별 편집, 계보, 민감한 식별 기준이 섞인다. 권위는 차원별로 분리한다.

| 차원 | 권위 저장소 | 포함 내용 | 잃었을 때 |
|---|---|---|---|
| 전역 의미 | `Library` Markdown | 여러 출력에 공통으로 전파할 사실·판단·설명 | 다른 데이터로 자동 복원했다고 주장할 수 없음 |
| 출력별 의미 | versioned `ProjectionOverlay` | 특정 블로그·Wiki·기술 문서에서만 유지할 문구·숨김·순서 | 해당 출력의 수동 편집을 완전 복원할 수 없음 |
| 제어 상태 | Git-tracked `.fullplate/helm/control/` current manifests | 안정 ID, 현재 lineage, suppression, type version, 최소 publication receipt | 삭제 전파·멱등 복구를 보장할 수 없음 |
| 민감한 사용자 기준 | 암호화된 local `Identity Vault` | 사용자 승인 인물 reference와 biometric template | 동일 인물 자동 식별을 재학습해야 함 |
| 운영 상태 | Runtime state DB | lease, queue, retry, index watermark, 최근 Review | manifest와 Library에서 재구축 |
| 파생 상태 | lexical/vector/graph index, generated block, cache | 빠른 검색과 생성 결과 | 재생성 |
| 외부 상태 | publication target | 원격 글·사이트 revision | receipt와 provider API 범위에서만 동기화 가능 |

### 2.1 우선순위

사실성 권위와 출력 표현 권위를 한 순서로 섞지 않는다.

```text
사실성: 보안·삭제 불변식
        > 사용자의 명시적 전역 편집
        > active Library claim
        > 근거가 연결된 model 제안

표현:   보안·사실성 불변식
        > 사용자의 type-local overlay·suppression
        > Projection Type 규칙
        > Core Voice
        > model 표현 제안
```

Overlay는 해당 출력에서 active canonical claim을 생략·재배열·다르게 표현할 수 있지만 factual value를 바꾸거나 없는 사실을 확정할 수 없다. overlay가 canonical claim과 모순되면 출력하지 않고 `blocked_by_local_edit`로 둔다. 모델 점수는 사용자 권한이나 삭제 의도를 대신하지 않는다.

### 2.2 재생성 가능성

- generated managed block과 모든 index는 재생성 가능해야 한다.
- Projection의 사용자 편집은 직접 파일에만 남기지 않고 다음 run에서 `ProjectionOverlay`로 수입해야 한다.
- current-state DB는 durable manifest의 유일한 사본이 아니어야 한다.
- raw source가 purge된 뒤 필요한 lineage는 source hash, 근거 span fingerprint, canonical relation으로 남기되 raw bytes나 전체 추출문을 숨겨 보존하지 않는다.

### 2.3 Durable layout

제품명과 내부 app ID는 [ADR-0001](adr/0001-fullplate-helm-product-identity.md)로 확정했다. Library의 durable control namespace는 `.fullplate/helm/`이며 public package·bundle ID와 분리한다. 이 namespace를 바꾸려면 versioned migration, rollback, dual-read 검증을 `MUST` 제공한다.

```text
<library-root>/
├─ <사용자 Markdown과 canonical assets>
└─ .fullplate/helm/
   ├─ schema-version
   ├─ workspace.yaml
   ├─ control/
   │  ├─ documents/<prefix>/<document-id>.json
   │  ├─ captures/<prefix>/<capture-id>.json
   │  ├─ evidence/<prefix>/<evidence-id>.json
   │  ├─ retention/<prefix>/<capture-id>.json
   │  ├─ claims/<prefix>/<claim-id>.json
   │  ├─ projections/<type-id>/<instance-id>.json
   │  ├─ targets/<target-id>.json
   │  ├─ outbox/<operation-id>.json
   │  ├─ suppressions/<prefix>/<rule-id>.json
   │  ├─ publications/<prefix>/<publication-id>.json
   │  └─ processed-sources/<generation>/{manifest.json,shard-*.pack,delta-*.pack}
   └─ overlays/<type-id>/<instance-id>.md
```

- manifest는 JSON Schema로 검증하고 key ordering·Unicode·number 형식을 canonical serialization한다.
- current state만 저장하며 run trace와 raw model output은 넣지 않는다.
- entity 한 건 변경이 전체 catalog rewrite가 되지 않도록 stable ID별 file로 나눈다.
- current manifest의 path는 ID prefix로 분산하고 사용자 제목·원본 파일명을 사용하지 않는다.
- Library content와 `.fullplate/helm/`은 같은 Git tree에서 함께 commit한다.
- `captures`와 `retention`에는 opaque source ID, Recovery object ID, disposition, `canonical_committed_at`, `purge_at`, hold reason을 보존한다.
- `evidence`에는 공개·보존 정책을 통과한 최소 근거 조각, 원본 page/offset, 추출기 version, keyed source fingerprint를 보존한다. raw source 전체를 숨겨 보존하는 경로가 아니다.
- `targets`는 target별 monotonic desired generation과 desired content/action을, `outbox`는 이를 적용할 deterministic operation ID와 idempotency key를 보존한다.
- `publications`에는 pointer가 아니라 최소 receipt 전체인 provider, remote ID, remote revision, last-known HMAC, permission scope를 보존한다.

canonical commit 전 custody의 권위는 Recovery/quarantine object 옆의 authenticated sidecar다. sidecar는 CaptureID, opaque source ID, keyed source fingerprint, object hash, custody time, provisional disposition, hold와 MAC을 가지며 raw text를 포함하지 않는다. startup은 sidecar로 orphan object를 복구하고 canonical commit에서 같은 정보를 Git control manifest로 승격한다. sidecar가 없거나 MAC이 틀리면 `orphan_recovery` hold로 두고 자동 처리·purge하지 않는다.

## 3. Workspace 경계와 경로 규칙

### 3.1 필수 경로

한 workspace는 다음 logical root를 가진다.

| root | 역할 | 겹침 허용 |
|---|---|---|
| `pile_root` | 미정리 입력 | 다른 root와 겹치지 않음 |
| `recovery_root` | purge 전 원본 | Git·cloud sync 기본 제외 |
| `library_root` | Markdown·canonical asset·control manifest | Projection root의 부모·자식 금지 |
| `projection_root[type]` | 유형별 materialized output | 다른 type과 target ownership 중복 금지 |
| `runtime_root` | DB·index·cache·temp·log | 모든 content root 밖 |
| `identity_vault_root` | 암호화된 identity reference | Git·backup 기본 제외 |
| `erase_control_root` | 진행 중 secure erase의 content-free 복구 원장·checkpoint | 모든 content root·Git·cloud sync 밖, 다른 workspace와 공유 금지 |

경로는 symlink를 해소한 실제 경로, case-folding, Unicode normalization을 기준으로 비교한다. 경로가 겹치거나 두 workspace가 같은 Library에 writer 권한을 주장하면 onboarding과 run을 fail closed한다.

`erase_control_root`는 owner-only permission, atomic write/fsync, authenticated encryption capability를 onboarding에서 검사한다. startup은 이 root의 MAC-valid active ledger를 content workspace scan보다 먼저 찾는다. active ledger가 있는데 root 또는 key가 없으면 content write·erase 재개를 중지하고 복구 진단만 제공한다.

### 3.2 파일시스템 전제

- primary Library는 atomic rename과 file locking을 지원하는 local filesystem이어야 한다.
- network volume이나 cloud-sync folder는 capability probe를 통과하지 않으면 primary Library로 사용하지 않는다.
- cloud sync는 publication 또는 backup adapter로 취급한다.
- wall clock은 표시와 retention deadline의 사용자 표현에 사용하고 lease·quiet window·동일 boot의 경과 시간은 monotonic clock을 사용한다.
- Runtime은 마지막 정상 wall time, boot/session ID, monotonic observation을 보존한다. 이 관측이 유실됐거나 wall clock 역행, 허용 범위를 넘는 전진, boot 변경 뒤 deadline 불확실성이 있으면 destructive retention을 `clock_anomaly_hold`로 멈춘다.
- same-boot의 작은 anomaly는 서로 다른 두 관측이 `clock_reconfirmation_window` 동안 비감소임을 확인한 뒤 해제할 수 있다. boot 변경·큰 forward jump에서는 마지막 신뢰 관측에 기록한 `remaining_retention`을 새 boot의 monotonic clock으로 **전부 다시 기다린 뒤** 해제한다. 신뢰 가능한 remaining 하한이 없으면 전체 retention duration을 다시 기다리며, 이 대기를 생략하는 유일한 경로는 영향 목록을 본 사용자의 destructive 승인이다. 시계가 안정됐다는 두 관측만으로 경과 시간을 인정하지 않는다.

### 3.3 Local Git

모든 Library workspace는 local Git repository를 `MUST` 사용한다. 기존 repository를 선택하거나 제품이 초기화한다. remote와 push는 선택 사항이다.

Git은 content revision과 사용자 diff의 권위다. `.fullplate/helm/control/` current manifest도 같은 commit에 들어간다. Runtime DB와 vector index는 Git에 넣지 않는다.

한 workspace는 write·Projection·publication 권위를 가진 `canonical_branch` 하나만 가진다. 기본 이름은 기존 repository의 default branch에서 onboarding 시 고정한다.

- canonical branch가 아닌 branch는 read-only preview이며 별도 ephemeral index를 쓸 수 있지만 외부 publish하지 않는다.
- detached HEAD에서는 plan과 진단만 허용한다.
- canonical branch switch, rebase, force update를 발견하면 merge-base와 manifests를 reconcile할 때까지 write·publish를 중지한다.
- 여러 branch를 동시에 publish하려면 branch마다 별도 workspace ID와 Projection target namespace를 사용한다.

### 3.4 Cross-platform canonicalization

OS별 Git tree hash가 우연히 같다고 가정하지 않고 `SemanticWorkspaceDigest`를 정의한다.

- logical path는 `/` separator와 Unicode NFC로 hash하되 실제 content의 Unicode code point는 조용히 바꾸지 않는다.
- case-fold, NFC/NFD, Windows reserved-name 충돌은 onboarding과 write 전에 거부한다.
- 관리 text는 UTF-8과 LF로 쓰며 Markdown·JSON·YAML에 `text eol=lf`, binary asset에 `-text`를 적용한다.
- executable bit, symlink, hardlink, junction/reparse point, mount alias는 managed content에서 허용하지 않는다.
- custom clean/smudge filter와 managed-path Git hook side effect를 허용하지 않는다. 제품 Git plumbing은 hook 실행을 비활성화하고 effective attributes를 검증한다.
- `SemanticWorkspaceDigest`는 Library Markdown·canonical asset·ProjectionOverlay·type definition·active semantic control을 대상으로 정렬된 `(normalized logical path, mode, canonical blob hash)` tuple을 hash한다.
- run ID, timestamp, retry count, outbox transport metadata, publication diagnostic receipt처럼 의미를 바꾸지 않는 operational field는 semantic digest에서 제외하고 별도 `ControlStateDigest`에 넣는다.

cross-platform release gate의 “같은 hash”는 Git commit hash가 아니라 이 semantic digest를 뜻한다. 원본 bytes 보존이 필요한 binary asset은 변환하지 않고 blob hash를 사용한다.

## 4. 안정 식별자

경로와 제목은 바뀔 수 있으므로 ID로 사용하지 않는다.

| 대상 | ID 규칙 |
|---|---|
| Workspace | UUIDv7, 생성 후 불변 |
| Capture event | UUIDv7 |
| Source content | Runtime의 SHA-256; durable manifest의 opaque source ID + keyed HMAC |
| Canonical document | `DocumentID` UUIDv7 |
| Canonical document revision | `DocumentRevisionID = DocumentID + Git blob SHA` |
| Canonical section | heading 앞 숨김 HTML marker의 `SectionID` UUIDv7 |
| Canonical section revision | `SectionRevisionID = SectionID + canonical commit + block hash` |
| Claim | `ClaimID` UUIDv7; dedupe fingerprint와 분리 |
| Claim revision | `ClaimRevisionID = ClaimID + canonical revision + claim hash` |
| Canonical asset | UUIDv7 + content hash |
| Projection Type | 불변 UUID + 변경 가능한 slug |
| Projection instance | 위치와 독립적인 `ProjectionInstanceID` UUIDv7 |
| Projection overlay | UUIDv7 + instance ID + anchor |
| Run / operation / review | UUIDv7 |

예시:

```markdown
---
canonical_id: 019...
---

<!-- fullplate-helm:section id="019..." -->
## 제목은 바뀌어도 section ID는 유지된다
```

content-derived 값은 duplicate candidate와 suppression 비교에만 사용한다. 문장 표현이 달라졌다고 기존 claim ID를 조용히 다른 의미에 재사용하지 않는다.

- path·heading·section 이동, 맞춤법·문체·근거 추가는 stable ID를 유지하고 revision만 만든다.
- 같은 subject·predicate·scope·valid-time의 값을 교정하면 ClaimID를 유지하고 이전 revision을 supersede한다.
- 다른 scope·valid-time에서 함께 참인 값은 새 ClaimID다.
- 서로 다른 proposition인지 불명확하면 새 ID를 자동 연결하지 않고 Review를 만든다.

삭제 재등장 방지 fingerprint는 평문 hash가 아니라 OS credential store의 workspace key로 만든 HMAC을 사용한다. 원문을 추측하는 dictionary attack과 workspace 간 연계를 줄이기 위해서다.

plain source SHA-256은 Recovery·Runtime receipt에서 무결성 확인에 사용하되 Git-tracked control manifest에는 opaque source ID와 workspace-key HMAC만 기록한다.

## 5. Durable entity

최소 durable entity는 다음과 같다.

- `WorkspaceManifest`: root, schema, policy, adapter binding, writer owner
- `Capture`: 한 번 발견된 입력 사건과 opaque source ID·keyed source fingerprint
- `SourceDisposition`: `discard_after_recovery | promote_asset | quarantine | metadata_only`
- `CanonicalDocument`, `CanonicalSection`, `Claim`, `CanonicalAsset`
- `CanonicalRevision`: Git commit + blob SHA + config hash
- `DerivationEdge`: source → canonical → projection → publication current relation
- `ProjectionType`과 version
- `ProjectionInstance`, `ProjectionOverlay`, `ProjectionSuppression`
- `SuppressionRule`: global 또는 type-local 재도입 방지
- `OutboxOperation`: projection/index/publication에 적용할 idempotent operation
- `PublicationReceipt`: remote ID, remote revision, last known hash, permission scope
- `IndexGeneration`: canonical watermark와 adapter spec
- `ReviewItem`: 사용자 결정이 필요한 current issue
- `ProcessedSourceFingerprint`: 같은 bytes의 재투입을 식별하는 content-proportional receipt

manifest는 current state만 보존한다. 과거 event 전체는 Git history에 있고 Runtime trace를 복제하지 않는다.

## 6. 서로 독립인 상태 기계

입력 처리, 원본 보존, 출력 동기화, 외부 발행을 하나의 `complete` 상태로 합치지 않는다. 서로 독립된 상태 기계로 관리한다.

### 6.1 IngestState

```text
discovered
→ stabilizing
→ preflighted
→ admitted
→ extracted
→ sanitized
→ composed
→ planned
→ canonical_committed
```

분기:

| 현재 | 사건 | 다음 | 규칙 |
|---|---|---|---|
| `stabilizing` | quiet window 미충족 | `deferred` | 다음 generation에서 재검사 |
| `deferred` | 다음 trigger·budget 확보 | `stabilizing` | source를 다시 snapshot |
| `preflighted` | top-level secret·금지 MIME | `quarantined` 또는 `blocked` | 일반 Recovery copy 전에 격리 |
| `admitted` | unsupported | `blocked` | metadata와 원본 유지, Review 집계 |
| `extracted` | secret 확정 | `quarantined` | raw는 LLM·Git·index에 전달 금지 |
| `quarantined` | sanitized copy 재검사 통과 | `sanitized` | raw는 quarantine에 유지 |
| `quarantined` | 자동 정제 불가 | `blocked` | 사용자 결정을 기다림 |
| `blocked` | adapter·policy·사용자 결정 변경 | `preflighted` 또는 `admitted` | block 원인에 따라 재개점 고정 |
| `sanitized` | secret 재검사 통과 | `composed` | sanitized IR만 전달 |
| `composed` | 함께 참일 수 없는 claim | `planned` + claim Review flag | 안전한 claim만 plan에 포함 |
| `planned` | base hash 변경 | `composed` | 이전 plan은 `superseded`, 새 snapshot으로 재계획 |
| any pre-commit non-quarantine | `capture cancel` | `cancelled` | Recovery는 사용자 retention policy에 따름 |
| quarantined/blocked with quarantine | `capture cancel` | `cancel_pending_disposition` | `retain until… | recover | erase raw copy` 선택 전 terminal 아님 |
| `cancel_pending_disposition` | disposition receipt 확정 | `cancelled` | raw lifecycle은 RetentionState가 계속 추적 |
| any pre-commit | 영구 오류 | `blocked` | 처리 완료로 표시하지 않음 |

`canonical_committed`가 ingest commit point다. Projection과 publication 실패는 이 commit을 되돌리지 않는다.

`partial`, `metadata_only`, `review_required`는 IngestState가 아니다. 각각 extractor checkpoint, SourceDisposition, Claim/Review의 orthogonal 상태다. Capture terminal state는 `canonical_committed` 또는 disposition이 확정된 `cancelled`뿐이며 `blocked`와 `cancel_pending_disposition`은 재개 가능한 non-terminal state다.

### 6.2 RetentionState

```text
pile_only → pile_and_recovery → recovery_only → purge_due → purged
                              ↘ retained_original
                              ↘ retained_as_canonical_asset
pile_only → quarantine_only → quarantine_and_sanitized
                 ├───────────→ quarantine_retained
                 └───────────→ quarantine_purge_due → quarantine_purged
```

| 현재 | 사건 | 다음 | 규칙 |
|---|---|---|---|
| `pile_only` | Recovery custody 성공 | `pile_and_recovery` | sidecar hash·MAC 검증 |
| `pile_and_recovery` | Pile copy 제거·사용자 유지 | `recovery_only` | Capture는 계속 처리 |
| `recovery_only` | canonical commit + disposition retain | `retained_original` 또는 `retained_as_canonical_asset` | asset 승격은 새 canonical asset receipt 필요 |
| `recovery_only` | deadline·hold·clock gate 통과 | `purge_due` | deletion set 재계산 |
| `purge_due` | deletion set verify 성공 | `purged` | raw-derived temp 포함 receipt |
| `pile_only` | quarantine custody 성공 | `quarantine_only` | 일반 Recovery·LLM·Git 접근 금지 |
| `quarantine_only` | sanitized copy 재검사 통과 | `quarantine_and_sanitized` | raw quarantine은 그대로 유지 |
| quarantine state | 사용자 영구 보관 선택 | `quarantine_retained` | 암호화·key recovery 상태 확인 |
| quarantine state | secret rotation·resolution 확인 + deadline·hold·clock gate 통과 | `quarantine_purge_due` | unresolved secret은 전이 금지 |
| `quarantine_purge_due` | encrypted raw·temp verify 제거 | `quarantine_purged` | sanitized canonical은 별도 lifecycle |

normal Recovery와 quarantine의 `purge_due`는 자동 재시도 가능한 상태다. 삭제 실패는 상태를 terminal로 바꾸지 않고 실패 대상 receipt와 hold를 남긴다.

- `preflighted`에서 stable source hash와 top-level secret scan을 끝낸 뒤 `admitted`에서 immutable Recovery copy와 hash receipt를 만든다.
- archive 내부처럼 preflight에서 볼 수 없는 내용은 owner-only sandbox에 풀고 secret scan을 통과하기 전 일반 Recovery·LLM·Git으로 이동하지 않는다.
- Recovery 또는 quarantine final object와 authenticated retention manifest가 생성된 시점이 custody transfer point다. 이후 사용자가 Pile copy를 지워도 Capture 취소가 아니다. 취소는 `capture cancel`로만 한다.
- custody transfer 뒤 같은 Pile path에 다른 bytes가 안정화되면 새 Capture를 만든다. 이전 Capture도 명시적으로 cancel하지 않는 한 계속 처리한다.
- canonical commit 전에는 Pile과 Recovery bytes를 자동 삭제하지 않는다.
- `purge_at = canonical_committed_at + retention_duration`이다. 처리가 오래 걸린 시간을 retention에서 차감하지 않는다.
- quarantine은 canonical commit이 없을 수 있으므로 별도 기준을 쓴다. sanitized result가 commit되고 secret rotation·resolution이 확인되면 `quarantine_purge_at = resolution_confirmed_at + quarantine_recovery_duration`이다. cancel이면 사용자가 `retain until… | recover | erase raw copy` disposition을 고른 시각과 기한을 receipt로 고정한다. blocked·unresolved quarantine에는 자동 purge deadline을 만들지 않는다.
- purge는 `canonical_committed`, disposition 확정, `purge_at` 경과, active hold 없음이 모두 참일 때만 실행한다.
- purge eligibility는 같은 boot에서는 monotonic elapsed time과 wall deadline을 모두 만족해야 한다. reboot·sleep 복귀·wall-clock jump 뒤에는 §3.2의 `clock_anomaly_hold` 해제 전 purge할 수 없다.
- 영구 보관 정책은 `retained_original`이며 자동으로 `purge_due`가 되지 않는다.
- unsupported, extraction failure, unresolved secret은 purge hold다.
- purge는 raw bytes, extracted IR cache, OCR temp, thumbnails, model input cache를 같은 deletion set으로 처리한다.
- SSD·backup·remote의 물리적 소거는 OS와 provider가 보장하는 범위만 약속한다.
- `recover raw`는 canonical을 rollback하지 않고 검증된 Recovery bytes를 사용자가 선택한 경로에 복사한다.
- `recover raw` 기본 목적지는 모든 managed root 밖이어야 한다. Pile에 다시 넣으려면 `recover raw --to-pile`을 사용하며 새 Capture가 된다.
- Recovery root는 제품 소유 read-only 영역이다. unknown file은 ingest하지 않고, expected hash가 다르면 `corrupt_recovery` hold로 두며 자동 purge하지 않는다.
- Recovery object가 없으면 같은 hash의 Pile copy가 있을 때만 새 object와 sidecar를 재생성한다. 없으면 `manual_loss` receipt를 남기고 복구 가능하다고 표시하지 않는다.
- quarantined raw에서 안전한 정제본이 만들어지면 `quarantine_and_sanitized`로 두고 sanitized capture만 canonical pipeline을 진행한다. raw quarantine은 별도 retention 또는 secure erase 대상으로 남는다.
- quarantine의 release는 `recover raw`로 managed root 밖에 사본을 내보낸 뒤 기존 정책을 유지하거나, `quarantine_retained`, 위 표의 safe purge, `erase raw copy only` plan 중 하나다. 기한이 끝나도 rotation·resolution 증거가 없거나 hold가 있으면 자동 삭제하지 않고 bulk Review로 집계한다.

### 6.3 CanonicalState와 ClaimState

Document lifecycle은 다음뿐이다.

```text
draft → active → retired → purged
```

충돌은 document 전체 상태가 아니라 claim 상태다.

```text
active | conflicted | suppressed | superseded | retracted
```

한 claim이 충돌해도 안전한 다른 section은 검색·출력할 수 있다. `conflicted`, `suppressed`, `superseded`, `retracted` claim은 index query 결과에서 즉시 deny한다.

### 6.4 ProjectionSyncState

각 target은 monotonic `desired_generation`을 가지고 `(projection instance, canonical revision, type version)`별 상태를 추적한다.

```text
queued → planned → applying → current
                    ↘ retryable
                    ↘ blocked_by_local_edit
                    ↘ disabled
```

같은 operation ID를 여러 번 적용해도 동일 bytes와 receipt가 나와야 한다. 외부 side effect가 exactly-once라고 주장하지 않고 provider idempotency key와 receipt로 at-least-once 실행을 수렴시킨다.

| 현재 | 사건 | 다음 | 규칙 |
|---|---|---|---|
| `queued` | 최신 desired generation 확인 | `planned` | 오래된 operation은 `superseded` |
| `planned` | target base hash 일치 | `applying` | 불일치면 local edit scan |
| `applying` | atomic write·verify 성공 | `current` | receipt에 generation 기록 |
| `applying` | retryable 오류 | `retryable` | bounded backoff 뒤 `queued` |
| `applying` | local edit | `blocked_by_local_edit` | overlay import 뒤 `queued` |
| any | type disabled | `disabled` | 파일 유지 |
| `disabled` | type re-enable·preflight 통과 | `queued` | 최신 generation만 적용 |

apply 직전에 operation generation이 target의 current desired generation인지 다시 확인한다. 아니면 write 없이 `superseded`로 종료한다.

### 6.5 PublicationState

```text
unpublished | pending | published | drifted | update_pending | retract_pending | retracted | failed
```

원격 사용자가 수정해 `last_known_hash`와 다르면 `drifted`다. 기본은 원격을 덮어쓰지 않고 Review를 만든다. 양방향 import는 별도 adapter와 명시적 ownership 계약이 있을 때만 허용한다.

| 현재 | 사건 | 다음 | 규칙 |
|---|---|---|---|
| `unpublished` | publish intent | `pending` | latest desired generation 고정 |
| `pending` | provider receipt | `published` | receipt를 control commit |
| `published` | canonical/type 변경 | `update_pending` | 새 generation |
| `update_pending` | provider receipt·reconcile 성공 | `published` | remote revision·HMAC와 generation 확인 |
| `published` | remote hash 변경 | `drifted` | 자동 overwrite 금지 |
| `drifted` | remote 유지·import·overwrite 선택 | `published` 또는 `update_pending` | 명시적 ownership 결정 |
| `published` | retract intent | `retract_pending` | intent는 canonical commit에 durable |
| `retract_pending` | provider 확인 | `retracted` | receipt commit |
| pending state | retryable 오류 | `failed` | bounded retry metadata 포함 |
| `failed` | retry·reconcile | 이전 pending state | 최신 generation만 |

writable publisher는 native idempotency key 또는 `lookup/reconcile(operation_id)`를 반드시 제공해야 한다. 자동 update·retract는 추가로 `If-Match`/ETag/revision CAS처럼 **검사와 mutation을 원격에서 조건부로 묶는 기능**이 필요하다. create retry의 idempotency와 concurrent edit 보호를 같은 기능으로 간주하지 않는다.

provider가 revision CAS를 제공하지 않으면 자동 update·retract를 허용하지 않고 remote bytes를 덮지 않는 manual/Review mode만 제공한다. 조건부 mutation이 precondition failure이면 `drifted`로 전이하고 receipt를 쓰지 않는다. remote side effect 뒤 receipt 전 crash는 같은 operation ID로 reconcile한다.

### 6.6 ProjectionTypeState

```text
draft → active → disabled → active
                 ↘ retired → purged
```

- 설정 파일이 사라지거나 invalid가 되면 `disabled`만 수행한다.
- `retire`와 `purge`는 명시적 plan/apply 명령으로만 수행한다.
- slug rename은 동일 불변 type ID를 유지한다.
- type version 변경은 모든 instance impact plan을 만든 뒤 새 version으로 전환한다.
- 새 type은 `draft`에서 schema validation, target ownership collision scan, sample render를 통과한 뒤에만 `active`가 되고 initial desired generation을 만든다.
- type definition은 version별 immutable이다. 수정은 새 version과 impact plan이며 in-place overwrite하지 않는다.
- target binding은 versioned path/provider record이며 instance ID에 포함하지 않는다. target move는 같은 instance ID와 overlay를 유지한 채 새 target에 최신 generation을 materialize·verify한 뒤 old target을 retire하는 saga다. old target을 먼저 삭제하지 않는다.
- activate, version change, target move, re-enable, retire, purge는 모두 materialized file hash scan과 pending overlay import를 먼저 수행한다.
- marker 손상·미수입 edit가 있으면 fail closed한다.
- retire는 overlay를 archive한다. purge도 overlay·수동 asset을 기본 보존하며 별도 secure erase 승인에서만 삭제한다.

## 7. 입력 admission과 대용량 처리

### 7.1 안정화

파일은 size와 mtime이 설정 횟수만큼 같고, 처리 전후 SHA-256이 같아야 한다. 폴더 투입은 child 안정화와 directory quiet window를 모두 만족한다. rename 중간 상태, `.part`, `.crdownload`, ignore pattern은 admission하지 않는다.

Pile에서 Recovery로 custody를 넘기는 순서는 다음과 같다.

1. source를 읽으며 preflight scan과 SHA-256을 계산한다.
2. secret이 없으면 Recovery temp로 streaming copy하고 fsync한다. secret이면 암호화된 quarantine temp로 쓴다.
3. source size·mtime·hash를 다시 확인한다.
4. temp를 content-addressed final path로 atomic rename한다.
5. canonical commit 전에는 Pile source를 지우지 않는다.
6. canonical commit 뒤 source hash가 그대로일 때만 Pile copy를 지운다.

process가 4번 뒤 crash하면 같은 content hash로 Recovery copy를 재사용한다. 6번의 삭제가 실패하면 다음 run에서 processed source fingerprint로 중복 정제를 막고 삭제만 재시도한다. 폴더 투입은 처리한 child만 지우며 ignored·new·failed child가 하나라도 남으면 parent directory를 지우지 않는다. Pile root 자체는 절대 삭제하지 않는다.

### 7.2 크기 정책

“파일 크기 제한 없음”은 무제한 memory·disk·시간 사용을 뜻하지 않는다.

- regular text는 streaming parser와 disk-backed intermediate를 사용한다.
- run은 memory, temp disk, wall time, extracted block count, model token hard budget를 가진다.
- budget을 넘으면 crash나 truncate-success가 아니라 checkpoint 후 `deferred` 또는 `metadata_only`가 된다.
- archive는 최대 file count, total expanded bytes, depth, compression ratio, path traversal 제한을 가진다.
- 원본을 임의로 자르지 않는다. 일부만 처리했으면 `partial`을 명시하고 canonical commit하지 않는다.
- Library로 승격된 binary asset은 90 MiB부터 Git LFS 후보이며 remote capability와 사용자 정책을 확인한다.

### 7.3 SourceDisposition

| 입력 | 기본 disposition | 이유 |
|---|---|---|
| 대화·임시 text | `discard_after_recovery` | 정제 Markdown이 사용 목적 |
| 출력에서 직접 표시할 image/audio | `promote_asset` | 결과 재현에 asset 필요 |
| page citation·표 검증이 필요한 PDF | `promote_asset` 또는 필요한 page evidence asset | raw purge 후에도 근거 검증 필요 |
| 처리되지 않은 binary/video/archive | `metadata_only` + hold | 성공하지 않은 입력을 삭제하지 않음 |
| secret 포함 원본 | `quarantine` | 외부 전달과 Git 차단 |

`promote_asset`은 Pile 경로를 그대로 보존하는 것이 아니다. stable ID, sanitized metadata, content hash를 가진 Library asset으로 이동·복제해 정리된 결과의 일부로 만든다.

## 8. 추출·정제·사실성

### 8.1 DocumentIR

Extractor는 text 하나가 아니라 ordered block, source offset, page, bounding box, language, confidence, adapter version을 반환한다. extractor가 보장하지 않은 구조를 모델이 사실처럼 보완하지 않는다.

### 8.2 Source content는 명령이 아니다

- source, OCR text, EXIF, web snapshot은 untrusted data channel로만 모델에 전달한다.
- content model은 shell, network, Git push, publisher 권한을 갖지 않는다.
- orchestration은 schema로 검증된 model output만 받아 allowlisted operation을 실행한다.
- source 안의 URL은 자동 fetch하지 않는다.
- injection 탐지 여부와 무관하게 권한 분리는 유지한다. 탐지기는 보조 방어이지 authorization이 아니다.

### 8.3 Claim 생성 규칙

각 factual claim은 raw source를 폐기한 뒤에도 최소한의 근거를 검사할 수 있도록 `EvidenceSnippet` 또는 명시적인 `fingerprint_only` 상태를 가진다.

- `EvidenceSnippet`은 claim을 뒷받침하는 최소 문장·표 cell·OCR 영역만 포함하며 source page/offset/bounding box, extractor version, keyed source fingerprint를 기록한다.
- snippet은 secret·private egress 정책을 다시 통과한 canonical evidence asset이고 Library와 같은 Git transaction으로 versioned된다. raw 전체, 주변의 불필요한 개인정보, 복원 가능한 secret은 넣지 않는다.
- 정책상 snippet 보존이 금지되면 `fingerprint_only`로 남긴다. 이 상태는 중복 판별에는 쓸 수 있지만 원문 검증이 필요한 `strict` 또는 public factual output의 단독 근거가 될 수 없다.
- raw purge 전에 active claim마다 위 두 상태 중 하나와 사용 가능 범위를 확정한다. 미확정 claim은 retention hold다.
- onboarding에서 workspace evidence policy를 `snippet | fingerprint_only | raw_hold` 중 하나로 명시한다. `snippet`은 위 최소 조각을 canonical evidence로 승격하고, `fingerprint_only`는 strict/public 제한을 수락하며, `raw_hold`는 검증 가능한 source를 유지한다. policy가 없으면 factual raw를 자동 purge하지 않는다.
- raw purge plan과 receipt는 제거되는 raw object 수·bytes와 새로 또는 기존에 영구 보존되는 verbatim snippet의 preview·Git/backup 보존 사실을 분리해 표시한다. 사용자가 “원본 삭제”를 snippet 삭제로 오해하게 만들지 않는다.

Canonical factual claim은 다음 중 하나를 가져야 한다.

- 원본의 정확한 source span 또는 page/bounding box
- 사용자가 직접 만든 canonical revision
- 다른 active claim의 명시적 inference relation

근거 없는 model 문장은 factual claim으로 승격하지 않는다. 추론은 `inference`로 표시하고 사용한 premise를 연결한다. 수치, 날짜, 부정, 인용, 개인 기여는 exact span validator를 통과해야 한다.

### 8.4 중복과 충돌

판단 순서는 고정한다.

1. exact bytes hash
2. normalized structural hash
3. semantic candidate retrieval
4. subject·predicate·object·scope·valid time 비교
5. source authority와 사용자 revision 확인

동시에 참일 수 있으면 scope와 시점을 분리해 둘 다 저장한다. 동시에 참일 수 없으면 모델이 하나를 선택하지 않고 claim 둘을 `conflicted`로 둔다. 다른 안전한 내용은 계속 승격한다.

## 9. 문체와 출력 편집

### 9.1 규칙 우선순위

```text
안전·사실성 규칙
> document override
> Projection Type overlay
> Core Voice
> 제품 기본값
```

문체 규칙은 사실·인용·불확실성을 바꾸지 못한다. 서로 모순되는 voice sample은 개수로 투표하지 않고 rule proposal을 만들며 profile version 승인 전까지 기존 rule을 유지한다.

### 9.2 Projection 파일 구성

Projection 파일은 다음 재료의 materialized view다.

```text
canonical managed block
+ type-local ProjectionOverlay
+ shared canonical asset reference
```

- marker 밖 직접 편집은 다음 run에서 overlay로 수입한다.
- marker 안 직접 편집은 claim-linked override 후보로 수입한다.
- 문장 삭제는 해당 type의 suppression으로 해석한다.
- `canonical-change` 명령만 전역 Library 수정으로 승격한다.
- import가 성공하기 전에는 generator가 파일을 덮어쓰지 않는다.
- marker 손상, 동일 anchor 중복, target path 충돌은 fail closed한다.

### 9.3 Asset ownership

asset은 content hash로 dedupe하되 owner relation을 가진다. canonical document, projection, publication, overlay 중 하나라도 참조하면 garbage collection하지 않는다. 사용자가 직접 추가한 asset은 ownership이 확인되기 전 자동 삭제하지 않는다.

## 10. 변경 감지와 Git transaction

### 10.1 변경 분류

Git diff 뒤 Markdown AST와 stable ID로 다음을 구분한다.

- 문구 수정
- claim 값 수정
- section 이동
- path rename
- heading rename
- claim 삭제
- document 삭제
- Projection type-local 편집
- generated output drift

같은 canonical ID가 다른 path에 있으면 rename 또는 move다. 파일이 잠시 사라졌다는 이유만으로 delete를 확정하지 않는다.

파일 부재는 다음 순서로 확정한다.

```text
missing_observed
→ 같은 stable ID 전체 scan
→ canonical branch와 parent root 접근 가능 확인
→ filesystem quiet window
→ Git base가 바뀌지 않았는지 확인
→ rename | delete_intent | transient_missing
```

- tracked Library file이 위 검사를 모두 통과해 계속 없으면 사용자의 전역 delete intent다.
- Projection file 전체가 계속 없으면 whole-instance type-local suppression이다.
- branch switch, root unmount, permission error, sync provider placeholder는 delete intent가 아니다.
- 전체 파일 삭제와 managed block 안 문장 삭제는 서로 다른 operation이다.
- 한 scan에서 삭제 후보 file 수, active document 비율, 영향받는 claim/publication 수 중 하나라도 threshold 이상(`>=`)이거나 root-level directory가 통째로 사라지면 `bulk_delete_suspected`다. 이 상태에서는 canonical delete commit, Projection retract, remote retract를 만들지 않는다.
- safe default와 사용자가 완화할 수 없는 최대치는 각각 `20 files / 10% / 100 impacts`, `50 files / 20% / 200 impacts`다. 사용자는 threshold를 낮출 수만 있다. root-level missing은 개수와 무관하게 항상 guard다.
- bulk delete는 mount·권한·branch·ignore 변경을 다시 검사하고 사용자가 집합과 영향을 승인한 뒤 하나의 versioned delete plan으로만 실행한다. threshold 미설정 시 safe default를 사용하며 비활성화할 수 없다.

사용자가 직접 새 파일을 만든 경우:

- Library의 새 Markdown에 ID가 없으면 schema·secret·duplicate preflight 뒤 ID와 section marker를 같은 canonical commit에서 삽입해 `adopted_user_canonical`로 수입한다. 충돌하면 draft와 Review로 두며 무시하거나 덮어쓰지 않는다.
- Projection root의 unowned file은 기본 `unmanaged`다. 사용자가 `overlay로 수입`, `새 canonical로 승격`, `계속 비관리` 중 하나를 고르기 전 제품 소유 path가 되지 않는다.
- unknown binary asset도 ownership receipt가 생기기 전 이동·삭제하지 않는다.

### 10.2 적용 전 조건

- Git index에 사용자의 staged change나 unmerged entry가 있으면 자동 commit을 하지 않는다.
- run 대상 path의 observed blob/working-tree hash를 고정한다.
- 대상 밖 unstaged·untracked 파일은 읽거나 stage하지 않는다.
- config와 adapter spec hash를 run 시작 시 고정한다.
- workspace lease는 fencing token을 포함한다. 만료된 worker는 commit할 수 없다.

### 10.3 Canonical commit protocol

Git commit은 다음 `commit_kind`를 control manifest와 trailer에 기록한다.

- `content`: Library, overlay, type 또는 deletion intent 변경. `content_generation`을 증가시키고 target desired generation을 계산한다.
- `control-receipt`: target/publication/index receipt와 outbox 완료만 반영. 새 content generation이나 target operation을 만들지 않는다.
- `migration-checkpoint`: schema·legacy migration의 복구 지점. migration plan에 정의된 operation만 만든다.

1. current HEAD, regular index checksum, selected working-tree hash, config hash로 immutable `ChangePlan`을 DB에 기록한다.
2. 새 Library tree, current manifests, target별 desired generation, pending outbox intent를 temp area에서 생성하고 전부 검증한다.
3. alternate Git index로 예상 tree와 commit object를 만든다.
4. apply 직전 HEAD와 selected path hash를 다시 비교하고 regular `.git/index.lock`을 획득해 index가 plan의 old checksum과 같음을 확인한다.
5. branch ref를 old commit 조건부 CAS로 새 commit에 이동한다.
6. locked regular index를 새 HEAD tree로 쓰고 fsync한 뒤 atomic commit한다. 실패하면 `index_recovery_pending`이며 working tree materialization을 시작하지 않는다.
7. 각 file 교환 전에 authenticated materialization journal을 Recovery root에 fsync하고, 제품 소유 file은 `MaterializationAdapter.CompareExchangePreserve(path, expectedOldHash, newObject)`로만 반영한다. 단순 check-then-rename을 금지한다.
8. preserved object 검증과 target fsync 뒤 journal phase를 `verified`로 바꾸고, 성공한 preserved object는 quiet recovery window 뒤 제거한다.
9. DB의 canonical receipt와 queue를 Git-tracked target/outbox intent에서 materialize한다.

5번이 `content` commit의 canonical commit point다. operation ID는 `(workspace ID, target ID, desired generation, action, desired content HMAC)`의 canonical tuple에서 결정적으로 만들고 실제 ID도 outbox manifest에 저장한다. 5번 뒤 crash하면 startup이 Git commit 안의 control manifest·target desired state·outbox intent로 DB와 queue를 복구한다. 5번 전 crash하면 temp와 uncommitted plan을 폐기하거나 같은 plan ID로 재실행한다.

`CompareExchangePreserve`는 교환 순간 target에 있던 bytes를 원자적으로 보존해야 한다. macOS의 atomic swap, Linux의 exchange rename, Windows의 backup을 동반한 replace처럼 검증된 primitive를 adapter가 제공한다. 교환 뒤 보존본 hash가 expected-old와 다르면 새 파일을 확정하지 않는다. target이 여전히 새 object일 때만 보존본으로 복구하고, 그 사이 다시 바뀌었으면 양쪽 bytes를 Recovery conflict로 보존한다.

materialization journal은 operation ID, path ID, expected-old/new hash, preserved path/hash, index old/new checksum, phase를 담고 raw content는 담지 않는다. startup은 `current == new`여도 완료로 추측하지 않고 journal과 preserved object를 먼저 검사한다. `preserved == expected-old`이고 phase가 `verified`일 때만 완료다. preserved가 다른 사용자 bytes면 conflict로 승격하고, journal·preserved가 없거나 MAC이 틀리면 자동 정리하지 않는다.

ref가 new인데 regular index가 old checksum이면 다른 process가 index를 바꾸지 않았을 때만 new tree로 복구한다. index가 new면 계속하고, 둘 다 아니면 `index_conflict`로 멈춘다. 이 규칙으로 HEAD=new, index=old 상태를 staged user change로 오인하거나 unknown index를 덮지 않는다.

해당 filesystem·OS에서 이 capability를 증명하지 못하면 working tree 자동 materialization을 하지 않는다. canonical commit과 새 object는 제품 temp/recovery 영역에 남기고 `materialization_pending`으로 표시하며 사용자의 file을 건드리지 않는다. ref CAS 뒤 사용자가 수정한 경우도 동일하게 `recovery_conflict`로 중지한다. startup recovery는 old tree, committed new tree, current working bytes와 preserved exchange object를 분류한다. current가 old면 journal 검증 뒤 새 compare-exchange를 시도하고, current가 new면 위의 **유일한 완료 조건**인 journal·preserved 검증으로 이동하며, 둘 다 아니면 사용자 bytes를 보존한다.

3~9번 동안 workspace write lock을 유지한다. 다른 process는 read-only status만 볼 수 있고, stale fencing token을 가진 process는 ref update와 DB commit을 모두 거부당한다.

Library와 control manifest가 다른 commit에 존재하는 상태는 유효하지 않다.

### 10.4 여러 저장소와 외부 시스템

Library repo, blog repo, Wiki repo, remote publisher를 하나의 transaction으로 묶을 수 없다. 다음 saga를 사용한다.

```text
canonical commit
→ 같은 commit의 durable desired state·outbox intent
→ target별 plan/apply
→ target receipt
→ receipt와 outbox 완료를 기록하는 control commit
```

- canonical commit은 downstream 실패로 rollback하지 않는다.
- target마다 독립 operation ID와 base revision을 사용한다.
- required target이 current가 아니면 run은 `partial_success`다.
- retry가 끝나도 실패하면 Review가 아니라 먼저 진단 가능한 blocked operation으로 남긴다.
- 사용자의 결정이 필요할 때만 Review로 승격한다.
- DB queue와 retry metadata는 materialized view다. 삭제·retract intent는 target receipt가 같은 generation을 확인한 control commit에 들어가기 전까지 Git outbox에서 제거하지 않는다.

## 11. 검색 일관성과 adapter migration

### 11.1 Index watermark

모든 index generation은 다음을 기록한다.

```text
workspace ID
content generation
SemanticWorkspaceDigest
inactive-claim deny manifest hash
adapter contract version
model/tokenizer/dimension spec
created-at
```

검색은 index watermark가 현재 content generation과 SemanticWorkspaceDigest보다 뒤처졌는지 확인한다. receipt-only Git commit은 index rebuild를 만들지 않는다.

- 삭제·충돌·suppression은 current control manifest deny filter를 먼저 적용해 stale index에서도 노출하지 않는다.
- 최신 변경 문서는 index가 따라오기 전 direct lexical scan 또는 targeted parse로 보완한다.
- 정확한 최신성이 필요한 동기화 작업은 index가 아니라 lineage와 canonical file을 사용한다.
- stale 여부를 숨기지 않고 query receipt에 기록한다.

### 11.2 Adapter 교체

새 embedding, vector, lexical, reranker adapter는 in-place 혼합하지 않는다.

```text
capability check
→ 새 generation build
→ contract test
→ 같은 gold set benchmark
→ completeness/hash verify
→ atomic active-generation switch
→ grace period
→ old generation GC
```

adapter 간 binary export/import는 선택 최적화다. canonical content에서 rebuild하는 경로가 항상 존재해야 한다. embedding 원문이 아니라 canonical ID와 chunk ID가 migration의 안정 key다.

## 12. 삭제와 실제 잊기

### 12.1 동작 구분

| 동작 | 현재 상태 | Git history | 재등장 방지 | 외부 |
|---|---|---|---|---|
| Pile 삭제 | 미처리 입력만 사라질 수 있음 | 없음 | 없음 | 없음 |
| Recovery purge | raw bytes·temp 삭제 | raw는 원래 없음 | processed source HMAC 유지 | 없음 |
| Canonical retire | 현재 Library·출력에서 제거 | 과거 commit에는 남음 | 동일 claim suppression 기본 생성 | update plan |
| Projection suppress | 특정 type에서만 제거 | overlay history에 남음 | type-local HMAC | 해당 publication update plan |
| Secure erase | 지정 content의 reachable copy 제거 시도 | history rewrite 필요 | 사용자 선택 | retract 요청, 보장 범위 보고 |

### 12.2 직접 편집의 기본 의미

- Library에서 claim을 지우면 **전역 canonical retraction**이다.
- 동일 claim·scope가 새 입력에서 자동 복원되지 않도록 suppression을 만든다.
- section 이동과 문구 교정은 retraction이 아니다.
- Projection에서 지우면 type-local suppression이다.
- Projection Type 설정 파일을 지우면 disable이지 content purge가 아니다.

Suppression은 문장 표면형을 HMAC하는 방식이 아니다. versioned normalizer가 다음 tuple을 만든 뒤 canonical serialization하고 HMAC한다.

```text
(subject entity ID, predicate ID, normalized value,
 scope IDs, valid-time interval, negation, policy version)
```

- 동일 entity·predicate·value·scope·time이면 표현이 달라도 같은 suppression이다.
- 새 scope·time이면 다른 claim 후보다.
- entity resolution 또는 value normalization이 불확실하면 자동 차단·재도입하지 않고 Review를 만든다.
- normalizer version 변경은 active suppression을 old/new dual-read로 migration하고 fixture를 통과한 뒤 switch한다.

사용자가 Library에 동일 claim을 직접 다시 쓰면 명시적 재추가 의도로 본다. stable ClaimID와 tuple이 정확히 일치하면 같은 transaction에서 suppression release를 만들고, semantic match만 있는 경우 Review를 만든다. Git revert가 Library와 control manifest를 함께 복원하면 해당 commit의 상태로 돌아가고 downstream outbox를 새로 계산한다. Library file만 복원해 active suppression과 불일치하면 claim을 즉시 활성화하지 않고 conflict로 둔다.

### 12.3 Secure erase

Git은 삭제한 내용을 과거 commit에 보존한다. 따라서 “현재 화면에서 제거”와 “복구 불가능하게 지우기”를 같은 버튼으로 제공하지 않는다.

Secure erase plan은 다음 대상을 열거한다.

- current Library와 canonical assets
- canonical `EvidenceSnippet`과 Git history의 snippet revision
- Projection과 overlays
- control manifests와 Runtime DB
- lexical/vector/graph index, cache, run·egress receipt, resolved Review, completed outbox trace
- Recovery, quarantine, temp, local backup
- Identity Vault의 label·template·positive/negative reference·match
- reachable Git objects, refs, reflog
- configured Git remote와 publication target

selector와 명령 의미를 다음처럼 분리한다.

- `erase raw copy only(capture/raw-object)`: Recovery·quarantine raw와 raw-derived temp만 지우고 Library·EvidenceSnippet·Projection·publication은 유지한다. `capture cancel --purge-raw`는 이 plan의 alias다.
- `forget source and derivations(source-lineage)`: source가 유일하게 뒷받침한 claim과 downstream output까지 closure를 계산한다. 다른 active evidence도 있는 claim은 relation만 제거하고 재검증한다.
- `claim | document | asset | identity-profile | identity-content | workspace`: 선택한 semantic content 기준 closure다.

UI와 CLI는 “원본 사본만 삭제”와 “이 원본에서 만들어진 지식도 잊기”를 같은 `source-object` 표현으로 합치지 않는다. 단순 capture cancel은 Recovery를 일반 retention 기간 동안 유지한다.

공유 asset 자체에 지워야 할 bytes가 있으면 relation 하나만 제거해서 성공 처리할 수 없다. plan은 다음 중 하나를 명시적으로 선택한다.

1. 모든 owner로 erase 범위를 확장한다.
2. 검증된 redacted replacement를 만들고 모든 owner를 교체한 뒤 원본 bytes를 지운다.
3. 영향받는 owner 승인 전 `blocked_shared_asset`로 멈춘다.

민감 bytes가 reachable copy에 남아 있으면 `completed`가 아니다. 삭제 대상이 relation뿐이고 asset bytes는 대상이 아닐 때에만 다른 owner가 있는 원본을 유지할 수 있다.

```text
planned → approved → preparing_control → [remote_authority_pending] → applying_current → current_removed
                                                        → applying_local_history → local_history_removed
                                                                                 → [remote_pending]
                                                                                 → finalize_control_erase
                                                                                 → completed | unverifiable
execution state → partial → retry recorded phase | cancelled | unverifiable
```

| 현재 | 사건 | 다음 | 규칙 |
|---|---|---|---|
| `planned` | 사용자 승인 | `approved` | selector·closure·remote·undo boundary hash 고정 |
| `approved` | 실행 시작 | `preparing_control` | 아직 local·remote mutation 없음 |
| `preparing_control` | ledger·key backup·reversible checkpoint 검증, pre-local remote 필요 | `remote_authority_pending` | key 삭제를 defer하고 원격부터 조건부 처리 |
| `preparing_control` | ledger·key backup·reversible checkpoint 검증, pre-local remote 불필요 | `applying_current` | execution commit point 통과 |
| `remote_authority_pending` | remote terminal receipt | `applying_current` | verified/unverifiable 결과 flag를 ledger에 고정 |
| `applying_current` | current 대상 확인 제거 | `current_removed` | 실패 대상이 있으면 `partial` |
| `current_removed` | history rewrite 승인 범위 있음 | `applying_local_history` | external checkpoint 검증 후 시작 |
| `current_removed` | local history 대상 없음·checkpoint 제거 성공 | `local_history_removed` | no-op history + checkpoint deletion receipt |
| `applying_local_history` | rewrite·GC·verify와 checkpoint 제거 성공 | `local_history_removed` | checkpoint 삭제 receipt 포함 |
| `local_history_removed` | configured remote 있음 | `remote_pending` | ledger의 opaque target으로 재개 가능 |
| `local_history_removed` | remote 없음 또는 pre-local remote 완료 | `finalize_control_erase` | remote result flag 유지 |
| `remote_pending` | 모두 확인 또는 확인 불가능 범위 확정 | `finalize_control_erase` | verified/unverifiable flag와 사유 고정 |
| `finalize_control_erase` | deferred key/credential·backup 제거, final receipt fsync | `completed` 또는 `unverifiable` | remote result에 따라 terminal 결정 |
| any execution state | 준비·mutation·finalization 일부 실패 | `partial` | 정확한 phase cursor와 실패 집합 유지 |
| `partial` | retry | 기록된 실패 phase | 성공 receipt는 재실행하지 않음 |
| `partial` at `applying_current` | 사용자가 복구 후 취소 | `cancelled` | 성공한 current deletion을 checkpoint로 전부 복구·검증한 경우만 |
| `partial` | retry 불가 범위 승인 | `unverifiable` | 실패·남은 사본·credential 상실을 명시한 bounded receipt |

- `planned`는 selector, transitive deletion set, 공유 owner, 예상 remote, undo boundary를 보여준다. raw purge plan은 제거될 raw와 장기 보존될 verbatim EvidenceSnippet을 별도 목록·preview로 보여준다.
- 첫 mutation 전에는 plan을 폐기해 취소할 수 있다. 첫 mutation 뒤 단순 plan 폐기는 취소가 아니다.
- 기본 reversible mode는 Recovery·quarantine·Identity Vault를 포함한 모든 current undo 대상을 external encrypted checkpoint로 만들고 hash·restore probe를 통과한 뒤에만 첫 mutation을 허용한다. checkpoint가 불가능한 대상은 plan에 `no_undo`로 표시하고 별도 승인을 받은 뒤 첫 mutation 순간부터 취소 불가다.
- `partial → cancelled`는 phase cursor가 `applying_current`이고 이미 성공한 deletion set을 checkpoint에서 전부 복원·hash 검증한 경우만 허용한다. local history rewrite나 remote mutation이 시작된 뒤에는 단순 cancel을 제공하지 않는다.
- `current_removed` 뒤 local history rewrite 전에는 보존된 checkpoint로 undo할 수 있다.
- undo checkpoint는 `erase_control_root`의 암호화된 임시 object이며 plan의 deletion set에도 포함된다. local history verify 직후 `local_history_removed`로 전이하기 전에 checkpoint를 제거하고 receipt로 확인한다. 제거 이후에는 undo 가능하다고 표시하지 않는다.
- reflog expiry·object GC·Identity Vault key deletion 또는 checkpoint 제거가 시작되면 해당 local copy의 undo를 보장하지 않는다.
- deletion set별 idempotent receipt를 기록하고 일부 실패는 `partial`로 남겨 실패 대상만 재시도한다.
- `completed`는 확인 가능한 local과 configured remote가 모두 확인된 경우다. provider가 증명할 수 없는 backup·제3자 사본이 있으면 `unverifiable`이지 completed가 아니다.

content repository의 manifest와 publication receipt 자체가 삭제 대상이어도 원격 retract를 끝내기 전에 control 정보를 없애지 않는다. `erase_control_root`의 별도 암호화·인증된 `EraseControlLedger`가 plan ID, phase cursor, opaque remote ID와 base revision, credential reference/permission scope, operation ID, checkpoint path/hash, deletion receipt만 보존하며 content bytes·제목·원문은 보존하지 않는다.

`preparing_control`은 ledger를 temp-write → file fsync → atomic rename → directory fsync → MAC readback하고 recovery-key backup과 restore probe를 통과한다. 이 성공이 첫 **local 또는 remote mutation 전** erase execution commit point다. startup은 active ledger를 우선 scan해 phase cursor부터 재개한다. active ledger와 checkpoint는 MUST backup 대상이고 restore probe를 통과해야 한다.

각 erase operation key는 workspace master key와 독립된 recovery key로 wrapping한다. master/key/credential 자체가 erase 대상이면 이를 `deferred control set`으로 표시하고 `remote_authority_pending`에서 configured remote terminal을 먼저 만든다. `finalize_control_erase`가 deferred workspace key·credential과 그 backup을 지우며, 이 단계가 끝나기 전 terminal이 될 수 없다. remote credential 원문은 ledger에 넣지 않고 OS credential store의 최소권한 lease reference만 두며 terminal 또는 사용자가 `unverifiable`을 승인할 때까지 lease를 유지·갱신한다. credential이 만료되면 `partial`이고 성공으로 표시하지 않는다.

`remote_authority_pending`, `finalize_control_erase`를 포함한 어느 phase가 `partial`이어도 ledger, operation key, 아직 필요한 credential lease와 남은 backup을 유지한다. ledger는 `completed | unverifiable | cancelled` terminal과 checkpoint 제거 확인 전에는 삭제할 수 없고, terminal 뒤 content-free bounded receipt로 축약하거나 삭제한다. operation key와 credential lease를 먼저 지워 재개 불가능하게 만드는 것도 금지한다.

local Git history rewrite, reflog expiry, object GC, remote force update는 별도 destructive 승인 대상이다. 이미 복제된 repository, 외부 provider backup, 검색 엔진 cache, 제3자 사본은 삭제를 보장할 수 없으며 receipt에 `unverifiable_remote_copy`로 남긴다.

Secure erase 시 사용자는 둘 중 하나를 선택한다.

- `allow_reintroduction`: suppression fingerprint도 제거한다.
- `block_reintroduction`: 원문 없이 keyed HMAC과 scope만 유지한다.

둘을 동시에 만족할 수 없다는 trade-off를 UI가 숨기지 않는다.

### 12.4 로그와 state 크기

기본 Runtime hard cap은 전체 256 MiB이며 category cap과 기간 중 먼저 도달한 조건을 적용한다.

| 범주 | 기본 기간 | 기본 byte cap | 넘을 때 |
|---|---:|---:|---|
| 성공 상세 trace | 7일 | 64 MiB | 오래된 상세→집계 receipt |
| retry·일반 실패 | 30일 | 64 MiB | fingerprint별 count·first/last만 유지 |
| 보안 metadata | 90일 | 96 MiB | 원문 없이 severity 집계, unresolved current issue는 manifest로 승격 |
| resolved Review·completed outbox trace | 30일 | 24 MiB | current receipt를 남기고 trace 삭제 |
| 여유·내부 metadata | 30일 | 8 MiB | oldest-first |

전체 cap에 먼저 도달하면 성공 상세, resolved trace, retry detail, resolved security detail 순으로 줄인다. pending outbox, active Review, current publication receipt, active suppression, retention hold는 log가 아니라 current manifest이므로 cap 때문에 삭제하지 않는다. 동일 fingerprint 오류는 rate-limit과 집계로 폭주를 막는다.

- current manifest, active suppression, publication receipt: 현재 content와 외부 상태에 비례해 유지
- processed source fingerprint: 투입한 고유 source 수에 비례하는 exact current state로 유지
- Git content history: 운영 log가 아니라 사용자 복구 이력이다. 자동 순환 삭제하지 않는다.

processed source fingerprint는 generation manifest, 정렬된 packed/Merkle exact shards, bounded delta pack으로 저장한다. compaction은 delta를 새 immutable shard generation으로 합치고 old generation을 검증 뒤 제거한다. per-source JSON과 event history를 영구 누적하지 않는다. Bloom filter처럼 false positive가 있는 구조를 유일한 duplicate authority로 쓰지 않는다. 과거 trace는 지워도 exact membership·key ID·policy version은 유지하므로 크기는 log가 아니라 고유 source 수에 선형 비례하며 UI는 현재 bytes와 증가율을 표시한다.

workspace quota 전에 자동 pack compaction을 먼저 수행한다. 그래도 hard quota에 도달하면 새 Capture를 IngestState `blocked` + `dedupe_capacity_hold` reason으로 두고 사용자가 quota 확대 또는 exact-set reset의 영향을 승인하기 전 fingerprint 없이 처리 완료하지 않는다. reset은 과거 raw 재투입을 exact duplicate로 알아보지 못할 수 있으나 canonical claim dedupe는 계속 수행한다.

Git history까지 용량 제한하려면 별도 `history compact` plan이 새 root commit과 backup/remote 영향을 보여준 뒤 실행한다. 이 명령은 secure erase의 local-history engine, CAS, external checkpoint, remote 영향 확인, undo boundary를 그대로 사용하며 자동 실행·무승인 force push를 금지한다. 조용히 history를 rewrite하지 않는다.

## 13. Secret, privacy, identity

Workspace 생성 시 master key를 만들고 OS credential store에 보관한다. HMAC, quarantine, Identity Vault에는 domain-separated derived key를 사용한다. processed-source exact set은 별도 안정 data key로 HMAC하고 이 key만 master key로 wrapping한다. 평문 key를 config·Git·log에 쓰지 않는다.

- onboarding은 암호화된 recovery key export와 restore test를 제공한다.
- key를 잃으면 Library Markdown은 읽을 수 있지만 suppression 비교, encrypted quarantine 복구, Identity Vault 사용은 불가능하다고 표시한다.
- master key rotation은 processed-source data key의 wrapping만 바꾸므로 raw가 없어도 exact set을 유지한다. suppression·vault처럼 재암호화 가능한 active state는 old key가 있는 동안 migrate·verify한 뒤 전환한다.
- processed-source data key 자체를 바꾸면 raw 없는 HMAC을 재계산할 수 없다. old exact set과 old wrapped key를 계속 보존하거나 exact-set reset과 duplicate 인식 저하를 명시 승인하는 두 경로만 허용한다.
- workspace key 삭제는 crypto-shredding의 한 단계지만 Git의 평문 Markdown과 외부 사본을 지우지는 않는다.

### 13.1 Secret

- 탐지한 raw는 owner-only permission의 Git 밖 quarantine으로 이동한다.
- quarantine은 OS credential store의 key로 암호화하고 backup exclusion을 적용한다.
- sanitized copy는 재검사 후에만 extractor 후속 단계, LLM, index로 전달한다.
- API key/token은 rotate 안내를 만들고 placeholder만 남긴다.
- 실제 secret, reversible mapping, raw prompt는 log와 manifest에 기록하지 않는다.
- fake secret allowlist는 raw 값이 아니라 test fixture path와 fingerprint scope로 제한한다.

SSD와 backup 환경에서 secure overwrite를 보장할 수 없으므로 secret 노출은 삭제가 아니라 rotation으로 해결한다.

### 13.2 Data classification과 egress

모든 block과 asset은 최소 `private | sensitive | publishable` 분류를 가진다. remote LLM, OCR cloud, web fetch, Git push, publisher는 독립 egress capability다.

- adapter는 자신이 요청하는 data class와 destination을 manifest에 선언한다.
- 선언은 권한이 아니다. launcher가 OS sandbox, filesystem allowlist, network destination allowlist, credential scope로 실제 capability를 강제한다.
- workspace consent와 맞지 않거나 OS에서 강제할 수 없으면 호출 전에 차단한다.
- egress receipt에는 destination, data class, keyed content HMAC, policy version을 남기되 평문 hash와 본문은 남기지 않는다.
- private repository 여부를 확인할 수 없는 remote에는 기본 push하지 않는다.

renderer는 `https`와 workspace canonical asset ID만 기본 허용한다. `file:`, `javascript:`, 임의 remote image, raw HTML active content는 sanitize 또는 차단한다. Mermaid는 strict security mode와 offline rendering을 사용하고 network fetch를 허용하지 않는다. preview와 publication 모두 같은 sanitizer contract를 통과한다.

### 13.3 인물 식별

사용자 label은 `Identity`의 표시 이름이고 얼굴 cluster ID와 분리한다.

```text
unlabeled_cluster → labeled_identity → corrected → labeled_identity
                                    ↘ retired
                                    ↘ erased
```

- 사용자가 `person-7`을 특정 이름으로 승인하면 이후 match는 같은 identity ID에 연결한다.
- 이름 변경은 label revision이며 얼굴을 다시 cluster하지 않는다.
- false positive 수정은 negative reference와 calibration set에 반영한다.
- label correction은 lineage로 기존 private canonical mention, Projection, search metadata를 찾아 impact plan을 만들고 새 label로 재생성한다. public publication은 자동 이름 노출이 금지되어야 하지만 이미 사용자가 발행했다면 update plan을 만든다.
- model·embedding spec이 바뀌면 기존 template을 섞지 않고 새 generation을 만든다.
- 원본을 purge하면서 미래 식별을 유지하려면 encrypted biometric template 보존에 동의해야 한다.
- 동의하지 않으면 purge 후 adapter migration 때 재등록이 필요하다.
- `identity profile retire`는 미래 자동 식별만 중지하고 기존 사진·글의 표현은 유지한다.
- `identity profile erase`는 template, label, match, index를 지우되 사진·언급은 자동 삭제하지 않는다.
- `identity content secure erase`만 연결된 사진·canonical mention·overlay·publication을 SecureErase closure에 포함한다.
- unlabeled cluster는 sensitive current state이며 보존 기간·byte cap을 identity policy로 설정한다. 참조가 없고 기간이 지나면 template을 GC한다.

## 14. 설정 결정성

설정 우선순위는 다음으로 고정한다.

```text
compiled safe defaults
< versioned workspace config
< Git 제외 machine-local config
< explicit one-shot CLI override
```

- unknown key, 잘못된 enum, 없는 profile, path collision은 오류다.
- `extends`는 한 단계만 허용하고 순환을 금지한다.
- local config는 안전 불변식을 완화할 수 없다.
- resolved config는 secret reference를 제외하고 canonical serialization 후 hash한다.
- run 도중 config가 바뀌어도 현재 run은 시작 hash를 사용한다.
- 설정 파일 삭제는 관련 기능 disable이지 destructive purge가 아니다.
- 기본값 변경은 schema migration과 release note, old/new eval을 요구한다.

모든 app·CLI·OS trigger는 같은 persisted `RunRequest` schema와 application service로 정규화한다. 하나의 detected generation에는 RunRequest가 한 번만 만들어지고 여러 entry point는 그 request 또는 같은 deterministic fixture를 실행한다. entry point 동등성은 임의 UUID, timestamp, retry count, lease, transport metadata를 제외한 `SemanticPlanDigest`로 비교한다.

`SemanticPlanDigest`는 input snapshot, resolved config hash, adapter/model spec, ordered semantic operations와 예상 semantic workspace를 canonical serialization해 계산한다. 새 UUIDv7은 digest 전에 operation order·parent placeholder에서 만든 typed placeholder(`new-document:1`, `new-claim:1` 등)로 치환하고, timestamp·commit metadata도 제외한다. 실제 stable ID는 plan 승인 시 한 번 할당해 persisted plan에 고정하며 apply/retry가 새로 만들지 않는다.

live LLM을 두 번 호출한 byte output이 우연히 같아야 한다는 뜻이 아니다. contract fixture에서는 deterministic adapter 또는 최초 persisted model proposal을 재사용하고, 실제 비결정 모델은 사실성·문체 gate와 승인된 plan의 멱등 적용으로 검증한다. fresh empty workspace fixture는 placeholder-normalized SemanticPlanDigest가 entry point마다 같고, 같은 persisted plan을 적용한 동일-ID workspace fixture는 실제 SemanticWorkspaceDigest가 같아야 한다.

## 15. Schema·binary 업데이트

- binary는 읽을 수 있는 schema의 `min_version`과 `max_version`을 선언한다.
- 너무 새로운 schema를 만나면 read-only 진단만 허용하고 write하지 않는다.
- migration은 `plan → backup → migrate copy → verify → switch` 순서다.
- 자동 업데이트는 rollback 가능한 expand/contract migration만 적용한다.
- irreversible migration은 자동 적용하지 않고 명시적 승인과 restore test를 요구한다.
- update metadata와 binary는 서명, hash, version, expiry, rollback protection을 검증한다.
- 실행 중 binary를 교체하지 않고 idle 시 switch한다.
- health check 실패 시 schema-compatible 이전 binary로 되돌린다.

### 15.1 기존 raw Git/LFS workspace migration

현재 구현처럼 raw source가 Git/LFS에 들어간 workspace는 일반 schema migration으로 처리하지 않는다.

1. dirty worktree, refs, remotes, visibility, LFS object manifest를 read-only snapshot한다.
2. 복구 branch와 repository bundle/checksum을 만들고 restore test한다.
3. 기존 catalog source/artifact ID를 새 DocumentID·ClaimID·lineage로 deterministic mapping한다.
4. raw마다 `discard_after_recovery | promote_asset | quarantine | metadata_only` disposition을 계산한다.
5. migration branch에서 Library, control manifests, overlays, index generation을 만들고 기존 결과와 shadow compare한다.
6. 사용자는 `preserve_history`와 `erase_raw_history` 중 하나를 선택한다.
7. canonical branch를 CAS cutover하고 E2E 후에만 old automation을 제거한다.

`preserve_history`는 새 commit부터 raw staging을 중지하지만 과거 Git/LFS와 remote에는 raw가 남는다. `erase_raw_history`는 history rewrite, reflog/object GC, LFS remote cleanup 가능 범위, force push, collaborator clone 영향을 별도 SecureErase plan으로 보여준다. remote LFS와 기존 clone 삭제를 보장하지 않는다. 자동 force push하지 않는다.

cutover acceptance는 ID mapping 100%, active canonical count·projection overlay 보존, secret scan, DB rebuild, old/new retrieval comparison, rollback checkpoint 복원을 포함한다.

## 16. Review와 자동화 경계

Review가 필요한 조건은 사용자의 의미 결정이 없으면 안전한 결과가 하나가 아닌 경우뿐이다.

| 사건 | 자동 처리 | Review |
|---|---|---|
| exact duplicate | skip·receipt | 없음 |
| retryable provider 오류 | bounded retry | 없음 |
| unsupported format | 다른 입력 계속 처리 | adapter 선택 필요 시 집계 |
| factual conflict | 안전한 claim만 승격 | 충돌 claim 1건으로 집계 |
| stale index | deny filter + fallback | 없음 |
| local Projection edit | overlay import | 동일 anchor merge 불가 시 |
| remote drift | overwrite 중지 | ownership 선택 |
| secure erase | plan만 생성 | destructive 승인 |

Review fingerprint는 cause + affected stable IDs + policy version으로 만든다. 원인이 사라지면 stale item을 자동 resolve한다. Review가 해결되지 않아도 독립적인 안전한 작업은 진행한다.

## 17. Resource와 비상주 실행

- OS trigger는 generation 요청을 durable queue에 기록하고 one-shot binary를 실행한다.
- workspace당 active worker는 하나이며 fencing token으로 stale writer를 차단한다.
- 새 generation이 들어오면 현재 run 뒤 이어서 처리하고 queue가 비면 종료한다.
- model, embedding, vector server는 기본적으로 command 수명과 함께 종료한다.
- 상주 daemon은 별도 deployment profile과 사용자 동의 없이는 설치하지 않는다.
- 모든 retry, iterative retrieval, tool call, model token, temp disk에는 hard limit가 있다.

## 18. Backup과 재해 복구

### 18.1 Backup 범위

- MUST: Library, canonical EvidenceSnippet, ProjectionOverlay, `.fullplate/helm/control/`의 최소 PublicationReceipt·pending outbox 포함, Git refs
- active secure erase 동안 MUST: `erase_control_root`의 ledger·checkpoint와 독립 recovery key restore material
- checkpoint와 그 backup은 local-history verify 뒤 제거한다. ledger·operation/recovery key backup은 remote terminal, `finalize_control_erase`, final receipt fsync가 모두 끝난 뒤에만 제거한다. 모든 임시 backup은 deletion set과 receipt에 포함한다.
- SHOULD: Projection repository와 provider raw response·diagnostic attachment
- MAY: Runtime DB와 index. 없어도 rebuild 가능
- 별도 동의: Identity Vault, quarantine, Recovery

### 18.2 Restore 검증

restore는 파일 존재만 확인하지 않는다.

1. Git fsck와 expected ref 확인
2. Library schema와 stable ID uniqueness 확인
3. control manifest hash와 canonical blob 일치 확인
4. overlay target ownership 확인
5. DB rebuild
6. 새 index generation build
7. publication은 remote drift scan 후 write 금지 상태로 시작

backup이 한 번도 restore test를 통과하지 않았으면 UI는 `복구 검증됨`이라고 표시하지 않는다.

## 19. Adapter contract

모든 adapter는 다음 metadata와 capability를 제공한다.

```text
adapter ID and version
contract version
input/output schema
determinism level
resource limits
network destinations
data classes accepted
idempotency support
export/import/rebuild capability
license and model provenance
```

필수 contract test:

- create/upsert/search/delete 또는 해당 domain equivalent
- empty, duplicate, Unicode, case collision, huge streaming input
- retryable/permanent error 분류
- cancellation과 deadline
- crash 뒤 resume
- stale delete와 dimension/schema mismatch
- idempotent operation replay
- secret·private data egress 차단
- old/new version migration 또는 rebuild

`MaterializationAdapter`는 `CompareExchangePreserve` capability, 지원 filesystem, 교환·복구 primitive, preserved-object verification을 선언한다. 이 capability가 없는 adapter는 자동 working-tree write를 지원한다고 광고할 수 없다.

third-party adapter는 기본적으로 별도 process와 최소 filesystem/network capability로 실행한다.

adapter self-declaration만 신뢰하지 않는다. macOS sandbox profile, Linux namespace/seccomp 계열, Windows restricted token/job object처럼 각 OS에서 검증한 enforcement adapter가 없으면 third-party adapter는 read-only offline input/output directory 외 권한을 받지 않는다.

## 20. Correctness property

구현은 예시 테스트만 맞추지 않고 다음 property를 만족해야 한다.

| ID | Property |
|---|---|
| CP-001 | 동일 source bytes와 policy로 N회 실행해 active canonical 의미는 한 벌이다. |
| CP-002 | canonical commit 전 crash는 active Library를 바꾸지 않는다. |
| CP-003 | canonical commit 후 어느 지점에서 crash해도 replay가 같은 downstream 상태로 수렴한다. |
| CP-004 | retracted/conflicted/suppressed/superseded claim은 stale index에도 query 결과로 나오지 않는다. |
| CP-005 | 한 Projection target의 실패가 canonical commit과 다른 target을 rollback하지 않는다. |
| CP-006 | 사용자가 수정한 overlay는 generator upgrade 뒤에도 보존되거나 명시적 conflict가 된다. |
| CP-007 | source purge 뒤 raw bytes와 raw-derived temp가 Git, Runtime, cache에 남지 않는다. |
| CP-008 | secure erase는 transitive deletion set과 공유 owner를 계산하고 deletion-set별 receipt를 남기며 미확인 remote를 성공으로 표시하지 않는다. |
| CP-009 | adapter 교체 중 old/new index record가 한 generation에서 섞이지 않는다. |
| CP-010 | staged Git change, stale lease, ref-CAS 직전·직후 concurrent edit가 있으면 사용자 bytes를 덮지 않고 fail closed 또는 recovery conflict가 된다. |
| CP-011 | path rename과 section move는 delete+recreate가 아니라 동일 stable ID revision이다. |
| CP-012 | 설정 파일 실종·오류는 disable 또는 no-op이지 purge가 아니다. |
| CP-013 | 모델 output만으로 권한 상승, network 호출, destructive action을 만들 수 없다. |
| CP-014 | 로그 제한이 current manifest나 active suppression을 제거하지 않는다. |
| CP-015 | DB와 index를 모두 지운 뒤 Library와 control manifest로 outbox, publication, retention eligibility를 포함한 current state를 재구축할 수 있다. |
| CP-016 | remote publication drift를 감지하면 기본 동작은 overwrite가 아니라 정지다. |
| CP-017 | budget 초과 입력을 성공 또는 완전 처리로 표시하지 않는다. |
| CP-018 | claim을 삭제하면 연결된 모든 managed output에서 사라지고 type-local overlay는 보존된다. |
| CP-019 | 같은 오류 1,000건은 fingerprint로 집계되고 안전한 999개 입력 처리를 막지 않는다. |
| CP-020 | 앱, CLI, OS trigger가 같은 fixture에서 같은 SemanticPlanDigest를 만들고 동일 승인 plan 적용 뒤 같은 SemanticWorkspaceDigest로 수렴한다. |
| CP-021 | Core Voice·type overlay 변경은 gold style set의 rule assertion을 통과하며 사용자의 승인된 표현을 사실 변경 없이 보존한다. |
| CP-022 | 오류 폭주에서도 Runtime 전체 byte cap을 넘지 않고 pending·active current state는 보존된다. |
| CP-023 | release의 기본 adapter·parameter·dependency·외부 자료는 requirement, ADR, versioned source, license/compatibility, benchmark run, implementation commit으로 역추적되고 금지 license 코드가 source/SBOM scan에서 편입되지 않는다. |
| CP-024 | signed update의 rollback·freeze·mix-and-match fixture와 schema rollback을 통과한다. |
| CP-025 | 표현이 다른 동일 claim tuple은 suppression되고 다른 scope·valid-time은 차단되지 않는다. |
| CP-026 | path alias·case·Unicode collision과 복수 writer workspace는 write 전에 차단된다. |
| CP-027 | canonical branch 밖의 branch·detached HEAD·force update는 publish하지 않고 reconcile 전 write를 막는다. |
| CP-028 | macOS·Windows·Linux의 동일 fixture가 같은 SemanticWorkspaceDigest를 만든다. |
| CP-029 | legacy raw Git/LFS migration이 ID·overlay를 보존하고 rollback checkpoint에서 복원된다. |
| CP-030 | identity label correction·profile erase·content secure erase가 각기 정의된 lineage 범위만 갱신한다. |
| CP-031 | transient missing, rename, Library delete, Projection whole-file suppression을 오분류하지 않는다. |
| CP-032 | custody transfer 뒤 Pile delete·path overwrite·capture cancel이 정의된 별도 결과를 만든다. |
| CP-033 | wall clock 역행·전진·reboot·sleep fixture가 retention을 조기 purge하지 않고 clock anomaly hold 뒤에만 재개한다. |
| CP-034 | materialization 경쟁을 교환 직전·직후에 주입해도 사용자 bytes는 target 또는 preserved conflict 중 하나에 온전히 남는다. |
| CP-035 | bulk missing·root unmount·permission loss가 대량 canonical delete나 remote retract를 자동 생성하지 않는다. |
| CP-036 | raw purge 뒤 strict/public factual claim은 허용된 EvidenceSnippet으로 검증되거나 출력에서 제외된다. |
| CP-037 | 공유 asset secure erase는 모든 민감 bytes가 제거·대체·차단되기 전 completed가 되지 않는다. |
| CP-038 | EraseControlLedger와 undo checkpoint는 원격·local-history 단계가 끝날 때까지 복구에 필요한 최소 상태를 보존하고 terminal 뒤 deletion set에서 제거된다. |
| CP-039 | processed-source checkpoint compaction 전후 exact duplicate 판정이 동일하다. |
| CP-040 | naming clearance는 발음·collision·registry/domain snapshot·사용자 승인 receipt 없이는 public name·CLI·package ID를 release namespace로 고정하지 않는다. |
| CP-041 | fresh-ingest entry point fixture는 generated ID placeholder 정규화 뒤 같은 SemanticPlanDigest를 만들고 하나의 persisted plan은 retry마다 ID를 재할당하지 않는다. |
| CP-042 | ref CAS·regular index update·file exchange 각 crash point에서 startup은 journal과 old/new checksum으로 복구하며 사용자 bytes와 index intent를 잃지 않는다. |
| CP-043 | remote revision 확인과 update/retract 사이 concurrent edit는 provider CAS failure로 drifted가 되고 덮어쓰지 않는다. |
| CP-044 | boot/forward clock anomaly는 trusted remaining retention을 새 monotonic session에서 다시 기다리거나 destructive 승인을 받기 전 purge하지 않는다. |
| CP-045 | secure erase partial cancel은 checkpoint 복구 검증 없이 cancelled가 되지 않고, operation key·credential loss는 completed가 아니라 partial/unverifiable이다. |
| CP-046 | master key rewrap와 processed-source data-key reset fixture가 exact-set 보존 또는 승인된 duplicate-detection 저하를 정확히 보고한다. |

각 property는 unit test만으로 끝내지 않는다. 해당하는 property-based test, crash/fault injection, golden fixture, cross-platform E2E 중 필요한 검증층을 가진다.

### 20.1 Normative clause registry

| Clause ID | 권위 절 | 주 계약 |
|---|---|---|
| CC-AUTH-001 | §2 | 차원별 권위와 factual/presentation precedence |
| CC-PATH-001 | §3 | workspace root, canonical branch, cross-platform path |
| CC-ID-001 | §4 | stable ID, revision ID, HMAC |
| CC-STATE-INGEST | §6.1 | ingest의 closed transition |
| CC-STATE-RETENTION | §6.2 | retention·quarantine·clock hold transition |
| CC-STATE-CANONICAL | §6.3 | document와 claim transition |
| CC-STATE-PROJECTION | §6.4·6.6 | Projection과 type transition |
| CC-STATE-PUBLICATION | §6.5 | publication transition과 receipt |
| CC-INGEST-CUSTODY | §7 | 안정화, custody, source disposition |
| CC-INGEST-EVIDENCE | §8 | DocumentIR, claim, EvidenceSnippet, conflict |
| CC-STYLE-001 | §9 | voice와 ProjectionOverlay |
| CC-GIT-DETECT | §10.1 | rename·missing·bulk delete 판별 |
| CC-GIT-COMMIT | §10.2–10.3 | canonical ref CAS와 commit point |
| CC-GIT-MATERIALIZE | §10.3 | preserved compare-exchange와 recovery |
| CC-GIT-OUTBOX | §10.4 | durable desired state와 saga |
| CC-INDEX-001 | §11 | watermark, deny filter, generation migration |
| CC-DELETE-RETRACT | §12.1–12.2 | retire·suppression·reintroduction |
| CC-DELETE-ERASE | §12.3 | secure erase closure·control ledger·undo |
| CC-DELETE-STATE | §12.4 | log cap·exact source set·history compact |
| CC-SEC-001 | §13 | secret, egress, identity, renderer sandbox |
| CC-CONFIG-001 | §14 | deterministic config precedence |
| CC-MIGRATE-001 | §15 | schema, updater, legacy raw migration |
| CC-REVIEW-001 | §16 | Review와 자동 처리 경계 |
| CC-RUNTIME-001 | §17 | one-shot queue와 resource hard limit |
| CC-BACKUP-001 | §18 | authoritative backup과 restore |
| CC-ADAPTER-001 | §19 | capability·contract·sandbox |
| CC-EVIDENCE-001 | 근거 원장 §9–11 | ADR·benchmark·source snapshot 계보 |
| CC-NAME-001 | §22.2 | public name·CLI·namespace release gate |

### 20.2 요구 추적표

`AS-nn`은 제품·아키텍처 설계 §25의 합격 시나리오, test ID는 구현 시 동일 ID로 생성할 최소 검증 묶음이다. `SR-nnn`은 사용자 요구를 구현 가능하게 만들기 위해 파생된 system requirement다. 근거 원장의 `accepted` decision과 registry의 모든 clause/property/scenario는 최소 한 행에 연결되어야 한다. `superseded | rejected` decision은 대체·기각 관계만 보존하고 active trace 대상에서는 제외한다.

| 요구 | 결정 | Clause | Property | AS | 최소 test ID |
|---|---|---|---|---|---|
| U-001 drop 자동 정리 | D-006, D-013, D-016, D-023, D-024 | CC-STATE-INGEST, CC-INGEST-CUSTODY, CC-REVIEW-001 | CP-001, CP-017, CP-019, CP-032 | AS-01, AS-05, AS-13, AS-24, AS-32 | CT-INGEST, FI-CUSTODY |
| U-002 Markdown 전역 정본 | D-018 | CC-AUTH-001, CC-ID-001 | CP-011, CP-015 | AS-06, AS-25, AS-30 | CT-CANONICAL |
| U-003 임의 출력 유형 | D-003, D-014, D-021 | CC-STATE-PROJECTION, CC-STYLE-001 | CP-006, CP-012 | AS-10, AS-11, AS-28, AS-29 | CT-PROJECTION-TYPE |
| U-004 문체 일관성 | D-028 | CC-STYLE-001 | CP-021 | AS-37 | EVAL-VOICE |
| U-005 raw purge | D-002, D-022, D-024 | CC-STATE-RETENTION, CC-DELETE-ERASE | CP-007, CP-008, CP-032, CP-033, CP-036, CP-044 | AS-06, AS-31, AS-39, AS-41, AS-44 | FI-PURGE, E2E-ERASE |
| U-006 정본 수정 전파 | D-004, D-019, D-020, D-023, D-025 | CC-GIT-DETECT, CC-GIT-COMMIT, CC-GIT-OUTBOX, CC-INDEX-001, CC-DELETE-RETRACT | CP-003, CP-004, CP-018, CP-025, CP-031 | AS-07, AS-12, AS-22, AS-23, AS-26, AS-27, AS-30 | FI-OUTBOX, E2E-SYNC |
| U-007 출력별 편집 | D-021 | CC-STYLE-001 | CP-006, CP-018 | AS-08, AS-09, AS-22, AS-29 | E2E-OVERLAY |
| U-008 bounded log | D-029 | CC-DELETE-STATE | CP-014, CP-022, CP-039, CP-046 | AS-17, AS-24, AS-46 | LOAD-LOG-CAP |
| U-009 비상주 자동화 | D-006, D-017 | CC-RUNTIME-001 | CP-020 | AS-15, AS-16 | E2E-TRIGGER |
| U-010 adapter 교체 | D-007 | CC-INDEX-001, CC-ADAPTER-001 | CP-009 | AS-12 | CT-ADAPTER, EVAL-MIGRATION |
| U-011 앱 없이 동일 동작 | D-005, D-017 | CC-RUNTIME-001, CC-CONFIG-001 | CP-020, CP-041 | AS-15, AS-16, AS-34, AS-47 | E2E-ENTRYPOINT |
| U-012 근거 아카이빙 | D-030 | CC-EVIDENCE-001 | CP-023 | AS-35 | CT-EVIDENCE-LINK |
| U-013 논리적으로 닫힌 명세 | D-018, D-019, D-020, D-021, D-022, D-023, D-024, D-025, D-026 | CC-AUTH-001, CC-STATE-INGEST, CC-GIT-COMMIT, CC-DELETE-ERASE, CC-ADAPTER-001 | CP-002, CP-003, CP-010, CP-015, CP-034 | AS-13, AS-14, AS-20, AS-21, AS-33 | SPEC-TRACE, FI-MATRIX |
| U-014 제품명 재검토 | D-027 | CC-NAME-001 | CP-040 | AS-36 | RELEASE-NAME-CLEARANCE |
| SR-001 검색·context 구성은 budget 안에서 근거 없거나 inactive인 사실을 생성·노출하지 않음 | D-008, D-009, D-010, D-011, D-012, D-020 | CC-INGEST-EVIDENCE, CC-INDEX-001 | CP-004, CP-017, CP-036 | AS-18, AS-19, AS-27, AS-41 | EVAL-RETRIEVAL, EVAL-FAITHFULNESS |
| SR-002 source content와 secret은 권한·명령으로 승격되지 않음 | D-024, D-031 | CC-SEC-001, CC-ADAPTER-001 | CP-013 | AS-05, AS-21 | SEC-PROMPT-INJECTION, SEC-EGRESS |
| SR-003 외부 side effect는 drift·부분 실패·재시도에 수렴 | D-019, D-023 | CC-STATE-PUBLICATION, CC-GIT-OUTBOX | CP-005, CP-016, CP-043 | AS-26, AS-43 | FI-PUBLISH-SAGA |
| SR-004 사용자 파일과 대량 삭제는 경쟁·환경 오류에서 보존 | D-019, D-032 | CC-GIT-DETECT, CC-GIT-MATERIALIZE | CP-010, CP-031, CP-034, CP-035, CP-042 | AS-14, AS-22, AS-38, AS-42 | FI-MATERIALIZE, FI-BULK-DELETE |
| SR-005 보안 삭제는 current·history·remote·공유 자산을 과장 없이 처리 | D-022, D-032 | CC-DELETE-ERASE | CP-008, CP-037, CP-038, CP-045 | AS-31, AS-40, AS-45 | E2E-SECURE-ERASE |
| SR-006 update·migration·cross-platform 상태는 rollback·재구축 가능 | D-007, D-026 | CC-PATH-001, CC-MIGRATE-001, CC-BACKUP-001 | CP-024, CP-026, CP-027, CP-028, CP-029 | AS-20, AS-25, AS-33 | E2E-UPDATE, E2E-MIGRATE, E2E-3OS |
| SR-007 identity correction·retire·erase는 서로 다른 범위만 변경 | D-033 | CC-SEC-001, CC-DELETE-ERASE | CP-030 | AS-40 | E2E-IDENTITY |
| SR-008 duplicate·scope·conflict는 서로 다른 의미 상태로 분류 | D-034 | CC-INGEST-EVIDENCE, CC-STATE-CANONICAL | CP-001, CP-004 | AS-02, AS-03, AS-04 | CT-CLAIM-MERGE |
| SR-009 dependency·외부 자료는 license와 version provenance가 추적됨 | D-015, D-030 | CC-EVIDENCE-001 | CP-023 | AS-35 | LICENSE-AUDIT, CT-EVIDENCE-LINK |

## 21. Release gate

### 21.1 Architecture-complete

- 모든 entity와 state transition이 schema에 존재
- CP-001~CP-046가 실제 test ID와 CI job에 연결
- threat model과 data-flow review 완료
- migration dry-run과 restore test 통과

### 21.2 Beta-ready

- 10,000 Markdown fixture ingest·incremental update·rebuild 통과
- 개인 50-query gold set retrieval·generation floor 통과
- 30일 source retention을 시간 가속 테스트로 검증
- crash injection, disk full, network loss, provider timeout 통과
- macOS 실제 trigger install/uninstall과 non-residency 확인
- onboarding reader/user test에서 destructive 의미 오해 없음

### 21.3 Cross-platform release

- macOS, Windows, Linux에서 동일-ID workspace fixture의 SemanticWorkspaceDigest 일치
- macOS APFS, Windows NTFS, Linux의 release 지원 native filesystem 각각에서 `CompareExchangePreserve → regular index/working tree materialization complete → crash/restart recovery` E2E 통과
- capability probe를 통과하지 못한 filesystem은 auto materialization 미지원으로 명시하고 cross-platform 지원 표에서 숨기지 않음
- OS별 path, Unicode, permission, sleep/wake, installer rollback 검증
- signed update와 rollback attack fixture 통과
- uninstall이 binary·trigger·Runtime·사용자 content를 구분해 보여줌

## 22. 의도적으로 확정하지 않은 변수와 식별자

### 22.1 Benchmark-selected technical variable

다음은 논리적 구멍이 아니라 benchmark가 선택할 adapter와 수치다.

- embedding model과 dimension
- vector index implementation
- lexical index implementation
- reranker model
- chunk/section policy의 수치
- Docling 또는 Unstructured
- 경량/강한 LLM routing
- GUI framework와 배포 channel

이 변수는 safe baseline, 후보, 측정값, release gate가 정의되기 전 기본값으로 승격하지 않는다.

### 22.2 공개 배포 식별자 gate

제품 표시명 `Fullplate: Helm`, app ID `helm`, root CLI command surface `fullplate helm`, Library control namespace `.fullplate/helm/`은 [ADR-0001](adr/0001-fullplate-helm-product-identity.md)로 승인됐다. bare `helm` executable·package는 `MUST NOT` 생성한다.

조직 계정, domain, bundle ID, package ID, Homebrew formula·cask·tap과 registry namespace는 별도의 공개 배포 식별자다. 각각 후보 ID, 표기·한국어/영어 발음, 오기 위험, 기존 software/company/open-source collision search, GitHub·package registry·Homebrew·주요 domain의 확인 시각 snapshot, 사용자 승인 receipt를 가진다.

- 법률상 상표 clearance는 자동 검색과 다르며 필요 범위를 별도로 표시한다.
- 공개 식별자 하나라도 미통과면 해당 organization·domain·bundle·package·tap·registry 이름을 배포 설정에 고정하지 않는다.
- 승인된 내부 app ID나 control namespace 변경은 superseding naming ADR, migration/redirect plan, rollback 영향을 요구한다.
- 기존 `woon knowledge` 경로와 `.product/` 실험 placeholder는 migration 입력일 뿐 `Fullplate: Helm`의 release identifier가 아니다.

### 22.3 Parameter 분류

문서의 숫자는 다음 네 종류 중 하나로 registry에 등록한다.

| 분류 | 의미 | 현재 예 |
|---|---|---|
| `invariant` | 설정으로 완화할 수 없는 정확성 조건 | stable ID uniqueness, secret 외부 전달 0건, stale writer commit 0건, bulk guard 상한 50 files/20%/200 impacts |
| `safe-default` | 사용자가 안전한 방향으로 바꾸거나 schema 범위 안에서 조정 | Recovery 30일, log 7/30/90일·256 MiB, bulk guard 20 files/10%/100 impacts, LFS 전환 90 MiB |
| `provisional` | benchmark 전 초기 실험값 | chunk token, candidate limit, rerank limit, context budget, face threshold |
| `release-fixture` | 기능 기본값이 아니라 검증 workload | 50-query gold set, 10,000 Markdown, 5 GiB streaming input, 3개 OS |

각 parameter는 ID, 단위, min/max, default, 분류, 근거 ID, 변경 시 필요한 test를 schema registry에 가진다. 숫자를 코드 literal과 문서 prose에만 중복 기록하지 않는다.

## 23. 비목표와 보장 한계

- 모델이 모든 문장을 사용자의 마음에 들게 쓴다고 수학적으로 보장하지 않는다. style eval과 사용자 수정 보존을 보장한다.
- OCR·인물 식별이 항상 맞다고 보장하지 않는다. confidence, calibration, correction lifecycle을 보장한다.
- 제3자에게 이미 복제·발행된 데이터를 강제로 삭제한다고 보장하지 않는다. 요청·receipt·미확인 범위를 보고한다.
- 상주 서버 없이 실시간 millisecond 반응을 보장하지 않는다. one-shot generation의 유실 방지를 보장한다.
- 1억 chunk를 개인 local 기본 profile에서 처리한다고 약속하지 않는다. adapter 경계와 별도 deployment profile을 제공한다.

## 24. 변경 통제

이 계약을 바꾸는 변경은 다음을 모두 포함해야 한다.

1. requirement 또는 defect ID
2. superseding ADR
3. 영향받는 state transition과 schema migration
4. 추가·수정한 CP test
5. backward/rollback 영향
6. 사용자 데이터와 삭제 의미 변화

문서 문구만 바꾸고 schema·test·migration이 따라오지 않으면 계약 변경으로 인정하지 않는다.
