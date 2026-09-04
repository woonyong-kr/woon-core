"""Validate a complete, replay-safe Wiki restructure manifest.

The manifest is an instruction for a one-time physical migration; it is not a
second knowledge graph.  Keeping validation separate from mutation makes it
possible to reject an incomplete or stale migration before any canonical page,
catalog, or receipt is touched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import iter_wiki_pages, split_markdown

_DISPOSITIONS = {"keep", "merge", "move", "retire", "review"}


@dataclass(frozen=True, slots=True)
class WikiRestructurePreflight:
    """Read-only result for one complete Wiki restructure instruction."""

    document_count: int
    disposition_counts: dict[str, int]
    target_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WikiRestructureClassification:
    """A complete, non-mutating assignment to the approved target tree."""

    document_count: int
    disposition_counts: dict[str, int]
    scope_counts: dict[str, int]
    records: tuple[dict[str, str], ...]


def render_wiki_restructure_template(vault: Path) -> bytes:
    """Render a complete local baseline without assigning final destinations.

    Every record starts as ``review``.  This is intentionally not an apply
    manifest: a reviewer must assign a destination or a merge successor before
    the preflight can describe the transaction as ready.
    """

    root = vault.expanduser().resolve()
    compiler_owned = _compiler_owned_paths(root)
    records: list[dict[str, str]] = []
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        canonical_id = metadata.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            raise WoonError(f"Wiki template requires canonical_id: {relative}")
        records.append(
            {
                "current_path": relative,
                "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "canonical_id": canonical_id,
                "source_owner": "compiler" if relative in compiler_owned else "manual",
                "disposition": "review",
            }
        )
    payload = {"version": 1, "records": records}
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_template(vault: Path, output_path: Path) -> Path:
    """Create one local baseline manifest without overwriting prior review work."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure template must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure template already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_template(root), mode=0o600)
    return output


def render_wiki_restructure_classification(vault: Path) -> bytes:
    """Classify every active page before any path or catalog mutation.

    A scope names the sole branch approved by the user.  It is intentionally
    not a target path: compiler-owned pages must still move through their
    source/claim/page-spec transaction, never through a Markdown rename.
    """

    root = vault.expanduser().resolve()
    records: list[dict[str, str]] = []
    dispositions: dict[str, int] = {}
    scopes: dict[str, int] = {}
    for path in iter_wiki_pages(root / "wiki"):
        relative = path.relative_to(root).as_posix()
        scope, disposition, rationale = _approved_scope_for_legacy_path(relative)
        records.append(
            {
                "current_path": relative,
                "target_scope": scope,
                "disposition": disposition,
                "rationale": rationale,
            }
        )
        dispositions[disposition] = dispositions.get(disposition, 0) + 1
        scopes[scope] = scopes.get(scope, 0) + 1
    payload = {
        "version": 1,
        "document_count": len(records),
        "disposition_counts": dispositions,
        "scope_counts": dict(sorted(scopes.items())),
        "records": records,
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8")


def write_wiki_restructure_classification(vault: Path, output_path: Path) -> Path:
    """Write the complete local-only classification without touching Wiki pages."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki restructure classification must stay below .local/woon-knowledge/wiki-restructure"
        )
    if output.exists():
        raise WoonError(f"Wiki restructure classification already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_wiki_restructure_classification(root), mode=0o600)
    return output


def prepare_wiki_restructure_preflight(
    vault: Path, manifest_path: Path
) -> WikiRestructurePreflight:
    """Validate that a manifest accounts for every active human Wiki page.

    A later writer may consume this result, but this function deliberately does
    not rename files or rewrite metadata.  In particular, raw evidence below
    ``wiki/private/_sources`` is excluded because its movement is owned by the
    source resolver rather than the human Wiki tree.
    """

    root = vault.expanduser().resolve()
    manifest_file = manifest_path.expanduser().resolve()
    if not manifest_file.is_file():
        raise WoonError(f"Wiki restructure manifest is missing: {manifest_file}")
    try:
        payload = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError("Wiki restructure manifest must use version: 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("Wiki restructure manifest requires a records list")

    active = {path.relative_to(root).as_posix(): path for path in iter_wiki_pages(root / "wiki")}
    compiler_owned = _compiler_owned_paths(root)
    issues: list[str] = []
    seen: set[str] = set()
    targets: dict[str, str] = {}
    final_paths: set[str] = set()
    move_parents: list[tuple[str, str]] = []
    counts = {disposition: 0 for disposition in sorted(_DISPOSITIONS)}

    for index, record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        current = _relative_path(record.get("current_path"), label, "current_path", issues)
        disposition = record.get("disposition")
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            issues.append(f"{label}: unsupported disposition {disposition!r}")
            continue
        counts[disposition] += 1
        if current is None:
            continue
        if current in seen:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active Wiki page: {current}")
            continue
        expected_hash = record.get("current_sha256")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if expected_hash != actual_hash:
            issues.append(f"{label}: current_sha256 does not match: {current}")
        try:
            metadata, _ = split_markdown(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, WoonError) as error:
            issues.append(f"{label}: cannot read frontmatter for {current}: {error}")
            continue
        if record.get("canonical_id") != metadata.get("canonical_id"):
            issues.append(f"{label}: canonical_id does not match: {current}")
        expected_owner = "compiler" if current in compiler_owned else "manual"
        if record.get("source_owner") != expected_owner:
            issues.append(f"{label}: source_owner must be {expected_owner!r} for {current}")

        target = record.get("target_path")
        if disposition == "move":
            target_path = _relative_path(target, label, "target_path", issues)
            if target_path is None:
                continue
            if not target_path.startswith("wiki/"):
                issues.append(f"{label}: target_path must stay below wiki/: {target_path}")
                continue
            previous = targets.setdefault(target_path, current)
            if previous != current:
                issues.append(f"{label}: target_path collision {target_path} with {previous}")
            final_paths.add(target_path)
            target_parent = _relative_path(
                record.get("target_parent"), label, "target_parent", issues
            )
            if target_parent is not None:
                move_parents.append((label, target_parent))
        elif disposition == "keep":
            final_paths.add(current)
        elif target not in {None, ""}:
            issues.append(f"{label}: only move records may define target_path")
        if disposition in {"merge", "retire"}:
            successor = record.get("link_successor")
            if not isinstance(successor, str) or not successor.strip():
                issues.append(f"{label}: {disposition} requires link_successor")

    missing = sorted(set(active) - seen)
    extra = sorted(seen - set(active))
    if missing:
        issues.append(f"manifest omits {len(missing)} active Wiki pages")
    if extra:
        issues.append(f"manifest names {len(extra)} non-active Wiki pages")
    for label, target_parent in move_parents:
        if target_parent not in final_paths:
            issues.append(f"{label}: target_parent is not a final Wiki page: {target_parent}")
    return WikiRestructurePreflight(
        document_count=len(active),
        disposition_counts={key: value for key, value in counts.items() if value},
        target_count=len(targets),
        issues=tuple(issues),
    )


def _relative_path(value: Any, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()


def _compiler_owned_paths(root: Path) -> frozenset[str]:
    """Read compiler page ownership without treating a missing catalog as an error."""

    catalog = root / "catalog/llm-wiki/pages.yaml"
    if not catalog.is_file():
        return frozenset()
    try:
        payload = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki page catalog is unreadable: {error}") from error
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        raise WoonError("Wiki page catalog requires a pages list")
    result: set[str] = set()
    for index, page in enumerate(pages, start=1):
        output = page.get("output_path") if isinstance(page, dict) else None
        if not isinstance(output, str) or not output.strip():
            raise WoonError(f"Wiki page catalog pages[{index}] has invalid output_path")
        result.add((Path("wiki") / output).as_posix())
    return frozenset(result)


def _approved_scope_for_legacy_path(relative: str) -> tuple[str, str, str]:
    """Assign legacy areas to one branch of the fixed, approved tree."""

    exact = {
        "wiki/common/README.md": ("개인 운영 > 지식 운영", "merge", "legacy-topic-map"),
        "wiki/common/aws-cost-cleanup-guardrails.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/aws-immersion-day-retry-roadmap.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/aws-well-architected-review.md": (
            "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
            "move",
            "topic",
        ),
        "wiki/common/c-pointer-and-array.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > C·C++",
            "move",
            "topic",
        ),
        "wiki/common/cpu-vs-gpu.md": (
            "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
            "move",
            "topic",
        ),
        "wiki/common/floating-point-fixed-point.md": (
            "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
            "move",
            "topic",
        ),
        "wiki/common/garbage-collection.md": (
            "Wiki > 프로그래밍 언어·런타임 > 런타임·빌드",
            "move",
            "topic",
        ),
        "wiki/common/jpg-png-gif-image-formats.md": (
            "Wiki > 프런트엔드·클라이언트 > 웹 플랫폼",
            "move",
            "topic",
        ),
        "wiki/common/python-call-by-object-reference.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > Python",
            "move",
            "topic",
        ),
        "wiki/common/software-design-complexity.md": (
            "Wiki > 소프트웨어 설계·아키텍처 > 코드 설계·리팩터링",
            "move",
            "topic",
        ),
        "wiki/common/technical-writing.md": ("개인 운영 > 지식 운영", "move", "operating"),
        "wiki/concepts/README.md": ("개인 운영 > 지식 운영", "merge", "legacy-topic-map"),
        "wiki/concepts/kotlin.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 > Kotlin",
            "move",
            "topic",
        ),
        "wiki/concepts/programming-languages.md": (
            "Wiki > 프로그래밍 언어·런타임 > 언어 공통",
            "move",
            "topic",
        ),
        "wiki/concepts/refactoring.md": (
            "Wiki > 소프트웨어 설계·아키텍처 > 코드 설계·리팩터링",
            "move",
            "topic",
        ),
        "wiki/nodes/aice-associate-30일-학습-계획.md": (
            "Wiki > AI·머신러닝 > 머신러닝 기초",
            "move",
            "learning-plan",
        ),
        "wiki/nodes/이메일-별칭.md": ("개인 운영 > 생활", "move", "personal-operating"),
        "wiki/personal/ai-서비스-만들기.md": (
            "Wiki > AI·머신러닝 > AI 애플리케이션",
            "move",
            "topic",
        ),
        "wiki/personal/projects/(미정)소설-집필.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스.md": (
            "Wiki > 프로젝트 > K8s Clue",
            "move",
            "project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스-런타임.md": (
            "Wiki > 프로젝트 > K8s Clue > 아키텍처",
            "move",
            "project",
        ),
        "wiki/personal/projects/kubernetes-장애-복구-서비스-이벤트-계약.md": (
            "Wiki > 프로젝트 > K8s Clue > 아키텍처",
            "move",
            "project",
        ),
        "wiki/personal/projects/minidb-데이터베이스-엔진.md": (
            "Wiki > 데이터·저장소",
            "move",
            "learning-implementation",
        ),
        "wiki/personal/projects/private-ai-animation-production.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/private-ai-animation-production/creator-research.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/private-ai-animation-production/production-learning-path.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/return-evidence-camera.md": (
            "창작 > 창작 프로젝트",
            "move",
            "creative-project",
        ),
        "wiki/personal/projects/temporal-k8s-ops-port.md": (
            "Wiki > 플랫폼·전달·운영 > 컨테이너·Kubernetes",
            "move",
            "learning-implementation",
        ),
        "wiki/personal/projects/woon-지식-운영-시스템.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/projects/README.md": (
            "개인 운영 > 지식 운영",
            "merge",
            "legacy-project-map",
        ),
        "wiki/personal/aice-associate-준비.md": (
            "Wiki > AI·머신러닝 > 머신러닝 기초",
            "move",
            "learning-plan",
        ),
        "wiki/personal/codex-대화는-완료된-경계부터-누적-정리한다.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/context-calendar.md": ("일정", "move", "schedule"),
        "wiki/personal/linked-graph.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/personal/woon-obsidian-테마.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/personal/이력서-복원은-검증-문장-우선.md": ("커리어 > 지원 자료", "move", "career"),
        "wiki/personal/이민정-ai-서비스-구현-분석.md": ("인물 > 이민정", "move", "person"),
        "wiki/personal/자동화는-실제-산출물로-검증한다.md": (
            "개인 운영 > 지식 운영",
            "move",
            "operating",
        ),
        "wiki/personal/테스트-실패-원인을-조건별로-분리한다.md": (
            "Wiki > 품질·보안·신뢰성 > 테스트·검증",
            "move",
            "topic",
        ),
        "wiki/personal/플러그인-가치는-연결된-원문-탐색-경험에-둔다.md": (
            "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
            "move",
            "topic",
        ),
        "wiki/private/이민정.md": ("인물 > 이민정", "move", "person"),
        "wiki/private/이민정-데이터-ai-커리어-전환-자료.md": (
            "인물 > 이민정",
            "move",
            "person-evidence",
        ),
        "wiki/private/이민정-크래프톤-입사-주거-보증금대출.md": (
            "인물 > 이민정",
            "move",
            "person-evidence",
        ),
    }
    if relative in exact:
        return exact[relative]
    if relative.startswith("wiki/personal/리모트ai-"):
        return "Wiki > AI·머신러닝 > AI 애플리케이션", "move", "private-design-topic"
    if relative.startswith("wiki/personal/") and relative.rsplit("/", 1)[-1] in {
        "강승렬.md",
        "김정선.md",
        "김희준.md",
        "신다영.md",
        "최우녕.md",
        "홍윤기.md",
    }:
        return "인물", "move", "person"
    if relative.startswith("wiki/hubs/"):
        return _legacy_hub_scope(relative)
    if relative.startswith("wiki/resources/"):
        return _legacy_resource_scope(relative)
    prefixes = (
        ("wiki/personal/kotlin-in-action", "Wiki > 책 > 프로그래밍 언어·설계", "book"),
        ("wiki/personal/컴퓨터-시스템-3판", "Wiki > 책 > 시스템·플랫폼", "book"),
        ("wiki/personal/밑바닥부터-시작하는-딥러닝-1", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/personal/밑바닥부터-만들면서-배우는-llm", "Wiki > 책 > AI·머신러닝", "book"),
        ("wiki/private/novel", "창작 > 창작 프로젝트", "creative-project"),
        ("wiki/personal/career", "커리어", "career"),
        ("wiki/personal/interview", "커리어 > 지원 자료 > 면접 준비", "career"),
        ("wiki/ai", "Wiki > AI·머신러닝", "domain"),
        ("wiki/algorithm", "Wiki > 컴퓨터 과학 기초", "domain"),
        ("wiki/backend", "Wiki > 백엔드·서비스", "domain"),
        ("wiki/database", "Wiki > 데이터·저장소", "domain"),
        ("wiki/network", "Wiki > 컴퓨터 시스템·네트워크 > 네트워크", "domain"),
        ("wiki/os", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "domain"),
        ("wiki/pintos", "Wiki > 컴퓨터 시스템·네트워크 > 운영체제", "learning-implementation"),
        ("wiki/security", "Wiki > 품질·보안·신뢰성", "domain"),
        ("wiki/books", "Wiki > 책", "book-map"),
        ("wiki/tools", "개인 운영 > 지식 운영", "operating"),
        ("wiki/knowledge", "개인 운영 > 지식 운영", "operating"),
        ("wiki/people", "인물", "people-map"),
    )
    for prefix, scope, rationale in prefixes:
        if relative == f"{prefix}.md" or relative.startswith(prefix + "/"):
            return scope, "move", rationale
    if relative == "wiki/README.md":
        return "Vault root", "keep", "vault-root"
    if relative.startswith(("wiki/common/", "wiki/concepts/", "wiki/nodes/")):
        return "review", "review", "mixed-legacy-topic"
    if relative.startswith("wiki/private/"):
        return "review", "review", "private-legacy-boundary"
    if relative.startswith("wiki/personal/"):
        return "review", "review", "personal-legacy-boundary"
    return "review", "review", "unclassified-legacy-path"


def _legacy_hub_scope(relative: str) -> tuple[str, str, str]:
    """Retire only the wrapper, merging its links into one final Map."""

    stem = Path(relative).stem
    scopes = {
        "ai-concept-to-code": "Wiki > AI·머신러닝",
        "ai-llm": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "ai-neural-network": "Wiki > AI·머신러닝 > 딥러닝",
        "algorithm-data-structure": "Wiki > 컴퓨터 과학 기초",
        "aws-immersion-day": "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
        "backend-runtime": "Wiki > 백엔드·서비스",
        "cnn-vision": "Wiki > AI·머신러닝 > 딥러닝",
        "concept-to-code": "개인 운영 > 지식 운영",
        "cpu-execution-program-loading": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "cs-basics": "Wiki > 컴퓨터 과학 기초",
        "database-storage": "Wiki > 데이터·저장소",
        "file-system-storage": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "knowledge-operations": "개인 운영 > 지식 운영",
        "llm-alignment-finetuning": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "llm-inference-serving": "Wiki > AI·머신러닝 > AI 애플리케이션",
        "llm-pretraining-scaling": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "local-private-index": "개인 운영 > 지식 운영",
        "math-statistics-foundations": "Wiki > AI·머신러닝 > 머신러닝 기초",
        "network-protocol": "Wiki > 컴퓨터 시스템·네트워크 > 네트워크",
        "neural-network-fundamentals": "Wiki > AI·머신러닝 > 딥러닝",
        "os-responsibility-boundary": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "os": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-alarm-clock-question-navigation": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-process-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-user-program-execution-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-vm-implementation-readiness": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos-vm-visual": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "pintos": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "process-lifecycle": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "qemu-hardware-byte-debugging": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "rag-agent-application": "Wiki > AI·머신러닝 > AI 애플리케이션",
        "security-web": "Wiki > 품질·보안·신뢰성 > 애플리케이션 보안",
        "sequence-model-rnn": "Wiki > AI·머신러닝 > 딥러닝",
        "threads-execution-model": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "tools-obsidian-pkm": "개인 운영 > 지식 운영",
        "transformer-attention": "Wiki > AI·머신러닝 > 대규모 언어 모델",
        "user-program-execution-boundary": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "vault-taxonomy": "개인 운영 > 지식 운영",
        "virtual-memory-translation": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "web-server-proxy": "Wiki > 백엔드·서비스 > 웹 애플리케이션",
    }
    return (
        scopes.get(stem, "review"),
        "merge" if stem in scopes else "review",
        "legacy-navigation-wrapper",
    )


def _legacy_resource_scope(relative: str) -> tuple[str, str, str]:
    """Retire resource link wrappers after their catalog relations move."""

    scopes = {
        "README": "개인 운영 > 지식 운영",
        "ai": "Wiki > AI·머신러닝",
        "algorithm": "Wiki > 컴퓨터 과학 기초",
        "aws": "Wiki > 플랫폼·전달·운영 > Cloud·Infrastructure as Code",
        "books": "Wiki > 책",
        "c-memory": "Wiki > 컴퓨터 시스템·네트워크 > 컴퓨터 구조",
        "career": "커리어",
        "developer-references": "개인 운영 > 지식 운영",
        "learning-repositories": "개인 운영 > 지식 운영",
        "legacy-vault-2026": "개인 운영 > 지식 운영",
        "obsidian": "Wiki > 플랫폼·전달·운영 > 개발 환경·협업",
        "operating-system": "Wiki > 컴퓨터 시스템·네트워크 > 운영체제",
        "programming-language": "Wiki > 프로그래밍 언어·런타임",
        "writing": "개인 운영 > 지식 운영",
    }
    stem = Path(relative).stem
    return (
        scopes.get(stem, "review"),
        "merge" if stem in scopes else "review",
        "legacy-resource-wrapper",
    )
