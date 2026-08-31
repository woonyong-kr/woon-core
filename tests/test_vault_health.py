#!/usr/bin/env python3
"""Regression tests for the retired external-video archive boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

MODULE_PATH = (
    Path(__file__).parents[1] / "src/woon_core/knowledge/vault_tools/audit-vault-health.py"
)
SPEC = importlib.util.spec_from_file_location("audit_vault_health", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class CanonicalWikiLinkTests(unittest.TestCase):
    def test_checks_local_only_wiki_pages_but_not_raw_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "wiki/personal/person.md"
            source = root / "wiki/private/_sources/knowledge/raw.md"
            asset = root / "wiki/private/_sources/knowledge/evidence.pdf"
            page.parent.mkdir(parents=True)
            source.parent.mkdir(parents=True)
            page.write_text(
                "# Person\n\n[[wiki/missing|Missing]]\n"
                "[[wiki/private/_sources/knowledge/evidence.pdf|Evidence]]\n",
                encoding="utf-8",
            )
            source.write_text("# Raw\n\n[[wiki/also-missing]]\n", encoding="utf-8")
            asset.write_bytes(b"pdf")
            texts = {
                page: page.read_text(encoding="utf-8"),
                source: source.read_text(encoding="utf-8"),
            }
            with mock.patch.object(AUDIT, "VAULT", root):
                index = AUDIT.target_index_any([page, source, asset])
                broken, ambiguous = AUDIT.canonical_wiki_link_issues(root, texts, index)

            self.assertEqual(broken, ["wiki/personal/person.md -> wiki/missing"])
            self.assertEqual(ambiguous, [])


class SourceCatalogBoundaryTests(unittest.TestCase):
    def test_rejects_external_private_and_verifies_canonical_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog/sources/example.yaml"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                "version: 1\nwiki_subject: wiki/example.md\nrecords:\n"
                "- state: external-private\n  target: null\n",
                encoding="utf-8",
            )

            issues = AUDIT.source_catalog_boundary_issues(root)

            self.assertTrue(any("must move" in issue for issue in issues))

            target = root / "wiki/private/_sources/knowledge/local-only/example/data.txt"
            target.parent.mkdir(parents=True)
            target.write_text("evidence\n", encoding="utf-8")
            subject = root / "wiki/example.md"
            subject.parent.mkdir(parents=True, exist_ok=True)
            subject.write_text(
                "# Example\n\n"
                "[[wiki/private/_sources/knowledge/local-only/example/data.txt|Data]]\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            catalog.write_text(
                "version: 1\nwiki_subject: wiki/example.md\nrecords:\n"
                "- state: canonical\n"
                "  target: wiki/private/_sources/knowledge/local-only/example/data.txt\n"
                f"  target_sha256: {digest}\n",
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.source_catalog_boundary_issues(root), [])

    def test_rejects_missing_wiki_subject(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog/sources/example.yaml"
            catalog.parent.mkdir(parents=True)
            catalog.write_text("version: 1\nrecords: []\n", encoding="utf-8")

            issues = AUDIT.source_catalog_boundary_issues(root)

            self.assertTrue(any("requires a Wiki subject" in issue for issue in issues))

    def test_one_archive_index_link_covers_a_verified_local_only_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "wiki/private/_sources/knowledge/local-only/example"
            archive.mkdir(parents=True)
            readme = archive / "README.md"
            data = archive / "data.txt"
            readme.write_text("# 원자료\n", encoding="utf-8")
            data.write_text("evidence\n", encoding="utf-8")
            subject = root / "wiki/example.md"
            subject.parent.mkdir(parents=True, exist_ok=True)
            subject.write_text(
                "# Example\n\n"
                "[[wiki/private/_sources/knowledge/local-only/example/README|원자료]]\n",
                encoding="utf-8",
            )
            catalog = root / "catalog/sources/example.yaml"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(
                "version: 1\nsource: example\nwiki_subject: wiki/example.md\nrecords:\n"
                "- state: canonical\n"
                "  target: wiki/private/_sources/knowledge/local-only/example/README.md\n"
                f"  target_sha256: {hashlib.sha256(readme.read_bytes()).hexdigest()}\n"
                "- state: canonical\n"
                "  target: wiki/private/_sources/knowledge/local-only/example/data.txt\n"
                f"  target_sha256: {hashlib.sha256(data.read_bytes()).hexdigest()}\n",
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.source_catalog_boundary_issues(root), [])


class WikiBaseContractTests(unittest.TestCase):
    def test_accepts_semantically_equal_quoted_or_plain_yaml_scalars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "inbox/wiki/wiki.base"
            base.parent.mkdir(parents=True)
            base.write_text(
                """formulas:
  title_link: file.asLink(title)
views:
  - {type: table, name: 전체}
  - {type: table, name: 프로젝트}
  - {type: table, name: 책}
  - {type: table, name: 리소스}
  - {type: table, name: 개념}
  - {type: table, name: 학습}
  - {type: table, name: 커리어}
  - {type: table, name: 생활}
  - {type: table, name: 인물}
""",
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.wiki_base_contract_issues(root), [])

    def test_reports_missing_human_title_formula_and_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "inbox/wiki/wiki.base"
            base.parent.mkdir(parents=True)
            base.write_text("views:\n  - {type: table, name: 전체}\n", encoding="utf-8")

            issues = AUDIT.wiki_base_contract_issues(root)

            self.assertTrue(any("file.asLink(title)" in issue for issue in issues))
            self.assertTrue(any("프로젝트" in issue for issue in issues))


class MermaidQualityTests(unittest.TestCase):
    def test_rejects_legacy_converter_markup_and_duplicate_stand_in_node(self) -> None:
        shapes, placeholders = AUDIT.mermaid_quality_issues(
            "wiki/example.md",
            """```mermaid
flowchart TD
  input["사용자 입력"]
  input_2["input"]
  output["B&gt;결과&lt;/FONT"]
  input_2 --> output
```
""",
        )

        self.assertEqual(shapes, ["wiki/example.md: line 5"])
        self.assertEqual(placeholders, ["wiki/example.md: line 4: input_2"])

    def test_accepts_clean_mermaid(self) -> None:
        shapes, placeholders = AUDIT.mermaid_quality_issues(
            "wiki/example.md",
            """```mermaid
flowchart TD
  input["사용자 입력"]
  output["결과"]
  input --> output
```
""",
        )

        self.assertEqual(shapes, [])
        self.assertEqual(placeholders, [])

    def test_rejects_unquoted_syntax_in_edge_labels_and_reserved_node_ids(self) -> None:
        shapes, _ = AUDIT.mermaid_quality_issues(
            "wiki/example.md",
            """```mermaid
flowchart TD
  start["시작"]
  end["종료"]
  start -->|do_work()| end
```
""",
        )

        self.assertEqual(
            shapes,
            ["wiki/example.md: line 4", "wiki/example.md: line 5"],
        )

    def test_rejects_local_mermaid_colors_and_accepts_labeled_line_styles(self) -> None:
        colored = """```mermaid
flowchart TD
  failed["실패"]
  style failed fill:#f55,color:#fff
```
"""
        semantic = """```mermaid
flowchart TD
  start["시작"] -.->|"확인 필요"| review["검토"]
```
"""

        self.assertEqual(
            AUDIT.mermaid_color_issues("wiki/example.md", colored),
            ["wiki/example.md: line 4"],
        )
        self.assertEqual(AUDIT.mermaid_color_issues("wiki/example.md", semantic), [])


class CanonicalSectionQualityTests(unittest.TestCase):
    def test_rejects_duplicate_h2_and_timeline_row(self) -> None:
        headings, timeline = AUDIT.canonical_section_quality_issues(
            "wiki/example.md",
            """# 예제

## 판단

첫 판단.

## 판단

두 번째 판단.

<!-- woon-wiki-timeline:start -->
- 2026-08-27 · 변경 — 같은 내용
- 2026-08-27 · 변경 — 같은 내용
<!-- woon-wiki-timeline:end -->
""",
        )

        self.assertEqual(headings, ["wiki/example.md: duplicated H2 '판단' x2"])
        self.assertEqual(len(timeline), 1)

    def test_ignores_heading_like_text_inside_code_fence(self) -> None:
        headings, timeline = AUDIT.canonical_section_quality_issues(
            "wiki/example.md",
            """# 예제

## 판단

```markdown
## 판단
```
""",
        )

        self.assertEqual(headings, [])
        self.assertEqual(timeline, [])

    def test_rejects_duplicate_paragraph_empty_h2_and_false_link_heading(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """---
title: 예제
---

# 예제

같은 설명을 두 번 쓰면 탐색할 때 잡음이 된다.

같은 설명을 두 번 쓰면 탐색할 때 잡음이 된다.

## 핵심 링크

- 링크가 아닌 설명

## 비어 있음
""",
        )

        self.assertTrue(any("duplicated prose paragraph" in issue for issue in issues))
        self.assertTrue(any("contains no hyperlink" in issue for issue in issues))
        self.assertTrue(any("empty H2" in issue for issue in issues))

    def test_rejects_duplicate_blockquote_paragraph(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """# 예제

> 확인 범위: 이 설명은 검증된 범위 안에서만 사용한다.

> 확인 범위: 이 설명은 검증된 범위 안에서만 사용한다.
""",
        )

        self.assertTrue(any("duplicated blockquote paragraph" in issue for issue in issues))
        self.assertTrue(any("duplicated blockquote label" in issue for issue in issues))

    def test_rejects_near_duplicate_labeled_blockquotes(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """# 예제

> 확인 범위: 일반 원리를 설명한다.

> 확인 범위: 일반 원리만 설명한다.
""",
        )

        self.assertTrue(any("duplicated blockquote label" in issue for issue in issues))

    def test_rejects_scaffold_narration_and_current_history_repetition(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """---
title: 예제
summary: 현재 결론은 대화 로그보다 다시 사용할 판단을 남기는 것이다.
state_reason: legacy-normalization
---

# 예제

## 현재 이해

<!-- woon-wiki-current:start -->
현재 결론은 대화 로그보다 다시 사용할 판단을 남기는 것이다.
<!-- woon-wiki-current:end -->

## 한 줄 이력

<!-- woon-wiki-timeline:start -->
- 2026-08-28 · 실행 — 현재 결론은 대화 로그보다 다시 사용할 판단을 남기는 것이다.
<!-- woon-wiki-timeline:end -->

## 남긴 의도

추정 의도: 나중에 다시 읽는다.

아직 답변하지 않았다.

## 현재 최선 답변

<!-- woon-interview-current:start -->
답변이다.
<!-- woon-interview-current:end -->
""",
        )

        self.assertTrue(any("summary is repeated" in issue for issue in issues))
        self.assertTrue(any("추정 의도" in issue for issue in issues))
        self.assertTrue(any("empty interview answer placeholder" in issue for issue in issues))
        self.assertTrue(any("replace legacy state reason" in issue for issue in issues))
        self.assertTrue(any("duplicates generic current" in issue for issue in issues))
        self.assertTrue(any("duplicated in timeline" in issue for issue in issues))

    def test_rejects_retired_map_migration_as_current_evidence_basis(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """---
title: 예제
state_reason: map-to-wiki-tree-migration
---

# 예제

현재 기준으로 다시 읽을 수 있는 설명이다.
""",
        )

        self.assertTrue(any("replace legacy state reason" in issue for issue in issues))

    def test_rejects_conversation_scaffold_headings(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """# 예제

## 현재 이해

현재 결론이다.

## 다음 질문

실행 결과를 검증한다.
""",
        )

        self.assertTrue(
            any("conversation scaffold heading '현재 이해'" in issue for issue in issues)
        )
        self.assertTrue(
            any("conversation scaffold heading '다음 질문'" in issue for issue in issues)
        )

    def test_rejects_embedded_base_as_wiki_navigation(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """# 예제

## 관련 질문

![[inbox/wiki/wiki.base#예제]]
""",
        )

        self.assertTrue(any("direct Wiki hyperlinks" in issue for issue in issues))

    def test_accepts_linked_and_code_backed_sections(self) -> None:
        issues = AUDIT.canonical_body_quality_issues(
            "wiki/example.md",
            """# 예제

## 핵심 링크

- [[wiki/topic|주제]]

## 순서도

```mermaid
flowchart LR
  A --> B
```
""",
        )

        self.assertEqual(issues, [])

    def test_planned_book_chapter_is_not_mistaken_for_completed_learning(self) -> None:
        planned = AUDIT.canonical_body_quality_issues(
            "wiki/personal/book/chapter-01.md",
            """---
title: Chapter 1
state_reason: planned-reading
learning_status: Planned
---

# Chapter 1

## 학습 자료

[공식 장 열기](https://example.com/chapter-1)
""",
        )
        started = AUDIT.canonical_body_quality_issues(
            "wiki/personal/book/chapter-01.md",
            """---
title: Chapter 1
state_reason: reading-started
learning_status: Reading
---

# Chapter 1

## 학습 자료

[공식 장 열기](https://example.com/chapter-1)
""",
        )

        self.assertEqual(planned, [])
        self.assertTrue(any("requires retained learning notes" in issue for issue in started))


class VaultRootDirectoryTests(unittest.TestCase):
    def test_allows_only_canonical_visible_root_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in AUDIT.ALLOWED_VISIBLE_ROOT_DIRECTORIES:
                (root / name).mkdir()
            (root / ".local").mkdir()

            self.assertEqual(AUDIT.unexpected_root_directory_issues(root), [])

    def test_rejects_accidental_visible_root_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "무제").mkdir()

            self.assertEqual(
                AUDIT.unexpected_root_directory_issues(root),
                ["unexpected visible Vault root directory: 무제"],
            )


class RetiredExternalVideoBoundaryTests(unittest.TestCase):
    def test_allows_no_external_video_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(AUDIT.retired_external_video_boundary_issues(Path(directory)), [])

    def test_rejects_external_video_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "wiki/private/_sources/knowledge/external-video").mkdir(parents=True)

            issues = AUDIT.retired_external_video_boundary_issues(root)

            self.assertEqual(len(issues), 1)
            self.assertIn("retired", issues[0])


class GlobalGraphRootTests(unittest.TestCase):
    def test_requires_project_cards_to_connect_through_the_single_wiki_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki_root = root / "wiki/README.md"
            project_card = root / "wiki/personal/자격-준비.md"
            for path in (wiki_root, project_card):
                path.parent.mkdir(parents=True, exist_ok=True)
            wiki_root.write_text("[[wiki/personal/자격-준비|자격 준비]]\n", encoding="utf-8")
            project_card.write_text("# 자격 준비\n", encoding="utf-8")
            files = [wiki_root, project_card]
            texts = {path: path.read_text(encoding="utf-8") for path in files}
            metadata = {
                wiki_root: {"publish": True, "access": "public"},
                project_card: {"publish": False, "access": "local-only"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                issues = AUDIT.global_graph_root_issues(
                    files, texts, metadata, AUDIT.target_index(files)
                )

            self.assertEqual(issues, [])


class WikiAndEntityPolicyTests(unittest.TestCase):
    def _contract(self, **overrides: object) -> dict[str, object]:
        metadata: dict[str, object] = {
            "type": "Wiki",
            "canonical_id": "personal/example",
            "node_kind": "topic",
            "parent": "[[wiki/README|Wiki]]",
            "keywords": ["예시"],
            "aliases": [],
            "view_mode": "tree",
            "updated": "2026-08-25",
            "facets": ["개념"],
            "knowledge_state": "확인 필요",
        }
        metadata.update(overrides)
        return metadata

    def test_reports_single_wiki_contract_violations_directly(self) -> None:
        wiki, entities = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/깨진-지식.md",
            "---\ntype: Project\naccess: local-only\n---\n# 깨진 성장\n",
            {"type": "Project", "access": "local-only"},
        )

        self.assertTrue(wiki)
        self.assertEqual(entities, [])

    def test_reports_retired_parallel_roots_directly(self) -> None:
        _, project_entities = AUDIT.wiki_and_entity_policy_issues(
            "projects/깨진-프로젝트.md",
            "# 깨진 프로젝트\n",
            {"type": "Wiki", "access": "local-only"},
        )
        _, content_entities = AUDIT.wiki_and_entity_policy_issues(
            "content/깨진-콘텐츠.md",
            "# 깨진 콘텐츠\n",
            {"type": "Content", "access": "local-only", "content_kind": "unknown"},
        )

        self.assertTrue(project_entities)
        self.assertTrue(content_entities)

    def test_rejects_keyword_documents_outside_the_single_wiki(self) -> None:
        self.assertEqual(
            AUDIT.nonwiki_keyword_policy_issues("inbox/capture/README.md", {"type": "키워드"}),
            [
                "inbox/capture/README.md: keyword knowledge belongs only under wiki/; "
                "use an operational type"
            ],
        )
        self.assertEqual(
            AUDIT.nonwiki_keyword_policy_issues("inbox/capture/README.md", {"type": "Operations"}),
            [],
        )

    def test_accepts_project_and_resource_facets_in_the_same_wiki_contract(self) -> None:
        project = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/자격-준비.md",
            "# 자격 준비\n",
            self._contract(
                **{
                    "access": "local-only",
                    "canonical_id": "personal/자격-준비",
                    "node_kind": "entity",
                    "view_mode": "project",
                    "keywords": ["자격 준비", "AICE Associate"],
                    "facets": ["프로젝트", "학습"],
                    "knowledge_state": "생각 중",
                    "project_id": "aice-associate",
                    "objective": "자격 취득",
                }
            ),
        )
        content = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/학습-자료.md",
            "# 학습 자료\n",
            self._contract(
                **{
                    "access": "local-only",
                    "canonical_id": "personal/학습-자료",
                    "node_kind": "entity",
                    "view_mode": "linear",
                    "keywords": ["학습 자료"],
                    "facets": ["리소스", "학습"],
                    "knowledge_state": "확인 필요",
                    "content_kind": "course",
                }
            ),
        )

        self.assertEqual(project, ([], []))
        self.assertEqual(content, ([], []))

    def test_rejects_an_invalid_canonical_identity_and_facet(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/깨진-정체성.md",
            "# 깨진 정체성\n",
            self._contract(
                **{
                    "canonical_id": "../깨진 정체성",
                    "facets": ["학습", "학습", "임의 종류"],
                }
            ),
        )

        self.assertTrue(any("canonical_id" in issue for issue in wiki))
        self.assertTrue(any("facets" in issue for issue in wiki))

    def test_rejects_retired_multiple_parent_field(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/여러-부모.md",
            "# 여러 부모\n",
            self._contract(
                **{
                    "canonical_id": "personal/여러-부모",
                    "parent_topics": ["[[wiki/README|Wiki]]", "[[wiki/ai/README|AI]]"],
                }
            ),
        )

        self.assertTrue(any("legacy Wiki tree field" in issue for issue in wiki))

    def test_rejects_numbered_interview_identity_and_wiki_root_parent(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/Q06-Kyro-문제와-역할.md",
            "# Q06. Kyro 문제와 역할\n",
            self._contract(
                **{
                    "title": "Q06. Kyro 문제와 역할",
                    "canonical_id": "personal/interview/Q06-Kyro-문제와-역할",
                    "facets": ["커리어", "학습"],
                    "question_kind": "interview",
                    "interview_tracks": ["KRAFTON AI Engineer"],
                    "question_topic": "Kubernetes 장애 복구 서비스",
                }
            ),
        )

        self.assertTrue(any("sequence or archive" in issue for issue in wiki))
        self.assertTrue(any("without numbering" in issue for issue in wiki))
        self.assertTrue(any("semantic parent" in issue for issue in wiki))

    def test_accepts_semantic_interview_identity(self) -> None:
        wiki, entities = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/kubernetes-장애-원인을-어떻게-판정했습니까.md",
            "# Kubernetes 장애 원인을 어떻게 판정했습니까?\n",
            self._contract(
                **{
                    "title": "Kubernetes 장애 원인을 어떻게 판정했습니까?",
                    "canonical_id": "personal/interview/kubernetes-장애-원인을-어떻게-판정했습니까",
                    "facets": ["커리어", "학습"],
                    "parent": "[[wiki/personal/projects/kubernetes-장애-복구-서비스|"
                    "Kubernetes 장애 복구 서비스]]",
                    "question_kind": "interview",
                    "interview_tracks": ["KRAFTON AI Engineer"],
                    "question_topic": "Kubernetes 장애 복구 서비스",
                }
            ),
        )

        self.assertEqual((wiki, entities), ([], []))

    def test_rejects_job_track_as_question_parent(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/ai-engineer/지원-이유.md",
            "# 지원 이유는 무엇입니까?\n",
            self._contract(
                **{
                    "title": "지원 이유는 무엇입니까?",
                    "canonical_id": "personal/interview/ai-engineer/지원-이유",
                    "facets": ["커리어", "학습"],
                    "parent": "[[wiki/personal/interview/ai-engineer/README|"
                    "KRAFTON AI Engineer 면접 준비]]",
                    "question_kind": "interview",
                    "interview_tracks": ["KRAFTON AI Engineer"],
                    "question_topic": "경력 서사와 지원 동기",
                }
            ),
        )

        self.assertTrue(any("job track" in issue for issue in wiki))
        self.assertTrue(any("semantic parent title" in issue for issue in wiki))


class RuntimePermissionTests(unittest.TestCase):
    def test_requires_user_only_runtime_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".local/woon-knowledge"
            receipts = runtime / "automation-receipts"
            receipt = receipts / "run.json"
            receipts.mkdir(parents=True)
            receipt.write_text("{}\n", encoding="utf-8")
            runtime.chmod(0o700)
            receipts.chmod(0o755)
            receipt.chmod(0o644)

            issues = AUDIT.runtime_permission_issues(root)

            self.assertEqual(len(issues), 2)
            self.assertTrue(any("directory mode must be 0700" in issue for issue in issues))
            self.assertTrue(any("file mode must be 0600" in issue for issue in issues))

    def test_accepts_user_only_runtime_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".local/woon-knowledge"
            runtime.mkdir(parents=True)
            receipt = runtime / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            runtime.chmod(0o700)
            receipt.chmod(0o600)

            self.assertEqual(AUDIT.runtime_permission_issues(root), [])


class CareerSourceAssetTests(unittest.TestCase):
    def test_accepts_private_career_pdf_and_structured_jd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root / "wiki/private/_sources/knowledge/private/career/applications/"
                "krafton-ai-engineer"
            )
            source.mkdir(parents=True)
            pdf = source / "draft-abc.pdf"
            jd = source / "jd.yaml"
            pdf.write_bytes(b"%PDF-1.4\n")
            jd.write_text("title: JD\n", encoding="utf-8")
            previous = AUDIT.VAULT
            try:
                AUDIT.VAULT = root
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(pdf))
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(jd))
            finally:
                AUDIT.VAULT = previous


class ObsidianWorkspaceTests(unittest.TestCase):
    def test_rejects_a_saved_tab_for_a_removed_vault_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obsidian = root / ".obsidian"
            obsidian.mkdir()
            (obsidian / "workspaces.json").write_text(
                '{"workspaces":{"Knowledge":{"main":{"children":['
                '{"state":{"state":{"file":"maps/삭제된-데모.md"}}}'
                "]}}}}\n",
                encoding="utf-8",
            )

            issues = AUDIT.obsidian_workspace_issues(root)

            self.assertEqual(
                issues,
                [".obsidian/workspaces.json: saved file is missing: maps/삭제된-데모.md"],
            )

    def test_accepts_saved_tabs_that_resolve_to_existing_vault_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "brain/home.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Home\n", encoding="utf-8")
            obsidian = root / ".obsidian"
            obsidian.mkdir()
            (obsidian / "workspace.json").write_text(
                '{"main":{"state":{"state":{"file":"brain/home.md"}}}}\n',
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.obsidian_workspace_issues(root), [])

    def test_ignores_missing_recent_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obsidian = root / ".obsidian"
            obsidian.mkdir()
            (obsidian / "workspace.json").write_text(
                '{"lastOpenFiles":["maps/legacy/삭제된.canvas"]}\n',
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.obsidian_workspace_issues(root), [])

    def test_ignores_missing_file_properties_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            obsidian = root / ".obsidian"
            obsidian.mkdir()
            (obsidian / "workspace.json").write_text(
                '{"main":{"type":"file-properties","state":{"file":"inbox/daily/removed.md"}}}\n',
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.obsidian_workspace_issues(root), [])

    def test_requires_content_cards_to_connect_through_the_wiki_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki_root = root / "wiki/README.md"
            content_card = root / "wiki/personal/예시-책.md"
            for path in (wiki_root, content_card):
                path.parent.mkdir(parents=True, exist_ok=True)
            wiki_root.write_text("[[wiki/personal/예시-책|예시 책]]\n", encoding="utf-8")
            content_card.write_text("# 예시 책\n", encoding="utf-8")
            files = [wiki_root, content_card]
            texts = {path: path.read_text(encoding="utf-8") for path in files}
            metadata = {
                wiki_root: {"publish": True, "access": "public"},
                content_card: {"publish": False, "access": "local-only"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                issues = AUDIT.global_graph_root_issues(
                    files, texts, metadata, AUDIT.target_index(files)
                )

            self.assertEqual(issues, [])

    def test_requires_visible_personal_notes_to_link_from_wiki_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki_root = root / "wiki/README.md"
            note = root / "wiki/personal/연결된-노트.md"
            for path in (wiki_root, note):
                path.parent.mkdir(parents=True, exist_ok=True)
            wiki_root.write_text("[[wiki/personal/연결된-노트|연결된 노트]]\n", encoding="utf-8")
            note.write_text("# 연결된 노트\n", encoding="utf-8")
            files = [wiki_root, note]
            texts = {path: path.read_text(encoding="utf-8") for path in files}
            metadata = {
                wiki_root: {"publish": True, "access": "public"},
                note: {"publish": False, "access": "local-only"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                issues = AUDIT.global_graph_root_issues(
                    files, texts, metadata, AUDIT.target_index(files)
                )

            self.assertEqual(issues, [])

    def test_rejects_an_unconnected_visible_graph_note(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki_root = root / "wiki/README.md"
            orphan = root / "maps/context-graph/example/고아.md"
            for path in (wiki_root, orphan):
                path.parent.mkdir(parents=True, exist_ok=True)
            wiki_root.write_text("# Wiki\n", encoding="utf-8")
            orphan.write_text("# 고아\n", encoding="utf-8")
            files = [wiki_root, orphan]
            texts = {path: path.read_text(encoding="utf-8") for path in files}
            metadata = {
                wiki_root: {"publish": True, "access": "public"},
                orphan: {"publish": False, "access": "local-only"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                issues = AUDIT.global_graph_root_issues(
                    files, texts, metadata, AUDIT.target_index(files)
                )

            self.assertEqual(
                issues,
                ["Wiki root cannot reach maps/context-graph/example/고아.md"],
            )

    def test_rejects_a_separate_local_graph_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki_root = root / "wiki/README.md"
            map_index = root / "maps/local-private-index.md"
            note = root / "wiki/personal/연결된-노트.md"
            for path in (wiki_root, map_index, note):
                path.parent.mkdir(parents=True, exist_ok=True)
            wiki_root.write_text("# Wiki\n", encoding="utf-8")
            map_index.write_text("[[wiki/personal/연결된-노트|연결된 노트]]\n", encoding="utf-8")
            note.write_text("[[maps/local-private-index|로컬 지도]]\n", encoding="utf-8")
            files = [wiki_root, map_index, note]
            texts = {path: path.read_text(encoding="utf-8") for path in files}
            metadata = {
                wiki_root: {"publish": True, "access": "public"},
                map_index: {"publish": False, "access": "local-only"},
                note: {"publish": False, "access": "local-only"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                issues = AUDIT.global_graph_root_issues(
                    files, texts, metadata, AUDIT.target_index(files)
                )

            self.assertEqual(
                issues,
                [
                    "Wiki root cannot reach maps/local-private-index.md",
                    "Wiki root cannot reach wiki/personal/연결된-노트.md",
                ],
            )


class VaultExecutionOwnershipTests(unittest.TestCase):
    def test_rejects_a_vault_owned_scripts_directory_and_top_level_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "repair.py").write_text("print('unsafe')\n", encoding="utf-8")

            issues = AUDIT.vault_execution_ownership_issues(root)

            self.assertEqual(len(issues), 2)
            self.assertTrue(any("scripts/" in issue for issue in issues))
            self.assertTrue(any("repair.py" in issue for issue in issues))

    def test_allows_code_as_raw_source_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "wiki/private/_sources/knowledge/imports/example.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('source material')\n", encoding="utf-8")

            self.assertEqual(AUDIT.vault_execution_ownership_issues(root), [])


class ManagedNonMarkdownFileTests(unittest.TestCase):
    def test_allows_only_the_core_owned_local_apple_calendar_ics_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = root / "inbox/calendar/apple-calendar.ics"
            projection.parent.mkdir(parents=True)
            projection.write_text(
                "BEGIN:VCALENDAR\r\n"
                "PRODID:-//Woon//Apple Calendar Read-only Projection//KO\r\n"
                "END:VCALENDAR\r\n",
                encoding="utf-8",
            )
            projection.chmod(0o400)
            unrelated = root / "inbox/calendar/other.ics"
            unrelated.touch()

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(projection))
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(unrelated))

    def test_allows_only_markdown_backed_canvas_in_maps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "wiki/example.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Example\n", encoding="utf-8")
            canvas = root / "maps/example.canvas"
            canvas.parent.mkdir(parents=True)
            canvas.write_text(
                '{"nodes":[{"id":"auto-example","type":"file","file":"wiki/example.md"}],'
                '"edges":[]}',
                encoding="utf-8",
            )
            invalid = root / "maps/invalid.canvas"
            invalid.write_text(
                '{"nodes":[{"id":"text","type":"text","text":"hidden"}],"edges":[]}',
                encoding="utf-8",
            )

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(canvas))
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(invalid))

    def test_rejects_retired_linked_canvas_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "wiki/example.md"
            note.parent.mkdir(parents=True)
            note.write_text("# Example\n", encoding="utf-8")
            canvas = root / "maps/linked-canvas/example.canvas"
            canvas.parent.mkdir(parents=True)
            canvas.write_text(
                '{"nodes":[{"id":"example","type":"file","file":"wiki/example.md"}],"edges":[]}',
                encoding="utf-8",
            )
            state = root / "maps/linked-canvas/example.linked-canvas.json"
            state.write_text(
                '{"schemaVersion":1,"canvasPath":"maps/linked-canvas/example.canvas",'
                '"rootPaths":["wiki/example.md"],"seedPaths":["wiki/example.md"],'
                '"managed":{"filesByNodeId":{"example":"wiki/example.md"}}}',
                encoding="utf-8",
            )

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(state))

    def test_rejects_retired_context_graph_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = root / "maps/context-graph/example.context-graph"
            layout.parent.mkdir(parents=True)
            layout.write_text("{}\n", encoding="utf-8")
            unrelated = root / "maps/example.context-graph"
            unrelated.write_text("{}\n", encoding="utf-8")

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(layout))
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(unrelated))


class DailyDigestProjectionTests(unittest.TestCase):
    def test_rejects_retired_embeds_and_duplicate_digest_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            daily = root / "inbox/daily"
            daily.mkdir(parents=True)
            today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
            (daily / f"{today}.md").write_text(f"![[../daily-digests/{today}]]\n", encoding="utf-8")
            legacy = root / "inbox/daily-digests" / f"{today}.md"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("# retired\n", encoding="utf-8")

            issues = AUDIT.daily_digest_embed_issues(root)

            self.assertEqual(len(issues), 2)
            self.assertTrue(any("retired Codex digest embed" in issue for issue in issues))
            self.assertTrue(any("retired duplicate daily digest file" in issue for issue in issues))


class CalendarProjectionHealthTests(unittest.TestCase):
    def test_marks_calendar_markdown_as_non_knowledge_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = root / "inbox/calendar/events/2026-08-18-example.md"
            projection.parent.mkdir(parents=True)
            projection.write_text("# 일정\n", encoding="utf-8")

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertTrue(AUDIT.is_calendar_projection_markdown(projection))

    def test_accepts_core_owned_read_only_markdown_and_ics_projections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "inbox/calendar/events"
            events.mkdir(parents=True)
            event = events / "2026-08-18-example.md"
            event.write_text(
                """---
type: calendar-event
title: 일정
publish: false
access: local-only
status: Generated
source: apple-calendar-readonly
calendar: Woon 일정
Date: 2026-08-18
Category: 기타
Category ID: other
Start Date: 2026-08-18T13:00:00+09:00
End Date: 2026-08-18T14:00:00+09:00
All Day: false
woon_projection: apple-calendar
---
""",
                encoding="utf-8",
            )
            event.chmod(0o400)
            events.chmod(0o500)
            ics = root / AUDIT.CALENDAR_ICS_PROJECTION_PATH
            ics.write_text(
                "BEGIN:VCALENDAR\r\n"
                "PRODID:-//Woon//Apple Calendar Read-only Projection//KO\r\n"
                "END:VCALENDAR\r\n",
                encoding="utf-8",
            )
            ics.chmod(0o400)
            dashboard = root / AUDIT.CALENDAR_DASHBOARD_PROJECTION_PATH
            dashboard.write_text(
                """---
type: calendar-dashboard
title: Apple Calendar
publish: false
access: local-only
status: Generated
source: apple-calendar-readonly
woon_projection: apple-calendar-dashboard
cssclasses: link-calendar-dashboard
---

```link-calendar
profile: woon-apple-calendar
```
""",
                encoding="utf-8",
            )
            dashboard.chmod(0o400)

            self.assertEqual(AUDIT.calendar_projection_issues(root), [])

    def test_rejects_calendar_projection_symlinks_outside_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            outside = Path(directory) / "outside"
            outside.mkdir()
            events = root / "inbox/calendar/events"
            events.parent.mkdir(parents=True)
            events.symlink_to(outside, target_is_directory=True)
            dashboard_target = Path(directory) / "outside-dashboard.md"
            dashboard_target.write_text(
                "---\nwoon_projection: apple-calendar-dashboard\n---\n", encoding="utf-8"
            )
            dashboard = root / AUDIT.CALENDAR_DASHBOARD_PROJECTION_PATH
            dashboard.symlink_to(dashboard_target)

            issues = AUDIT.calendar_projection_issues(root)

            self.assertTrue(any("directory must be Vault-local" in issue for issue in issues))
            self.assertTrue(
                any("dashboard must be a Vault-local file" in issue for issue in issues)
            )

    def test_reports_dashboard_directory_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dashboard = root / AUDIT.CALENDAR_DASHBOARD_PROJECTION_PATH
            dashboard.mkdir(parents=True)

            issues = AUDIT.calendar_projection_issues(root)

            self.assertTrue(
                any("dashboard must be a Vault-local file" in issue for issue in issues)
            )

    def test_reports_unreadable_dashboard_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dashboard = root / AUDIT.CALENDAR_DASHBOARD_PROJECTION_PATH
            dashboard.parent.mkdir(parents=True)
            dashboard.write_text("---\nwoon_projection: apple-calendar-dashboard\n---\n")

            with mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")):
                issues = AUDIT.calendar_projection_issues(root)

            self.assertTrue(any("dashboard must be readable" in issue for issue in issues))

    def test_rejects_calendar_event_symlink_outside_the_vault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "vault"
            events = root / "inbox/calendar/events"
            events.mkdir(parents=True)
            outside_event = Path(directory) / "outside-event.md"
            outside_event.write_text(
                "---\nwoon_projection: apple-calendar\n---\n", encoding="utf-8"
            )
            (events / "linked.md").symlink_to(outside_event)
            events.chmod(0o500)

            issues = AUDIT.calendar_projection_issues(root)

            self.assertTrue(
                any("calendar projection must be a Vault-local file" in issue for issue in issues)
            )

    def test_rejects_writable_or_retired_prisma_calendar_projection_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "inbox/calendar/events"
            events.mkdir(parents=True)
            (events / ".prisma-virtual-events.md").write_text(
                "```prisma-virtual-events\n[{}]\n```\n", encoding="utf-8"
            )

            issues = AUDIT.calendar_projection_issues(root)

            self.assertTrue(any("directory must be read-only" in issue for issue in issues))
            self.assertTrue(any("retired Prisma support file" in issue for issue in issues))


class RetiredAiInstructionBoundaryTests(unittest.TestCase):
    def test_ignores_legacy_locator_inside_source_inventory_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_catalog = root / "catalog/sources/legacy-vault.yaml"
            reconciliation = root / "catalog/reconciliation/legacy-vault.yaml"
            source_catalog.parent.mkdir(parents=True)
            reconciliation.parent.mkdir(parents=True)
            source_catalog.write_text("path: ai-reference/legacy.md\n", encoding="utf-8")
            reconciliation.write_text("source: ai-reference/legacy.md\n", encoding="utf-8")

            self.assertEqual(
                AUDIT.retired_ai_instruction_boundary_issues(root),
                [],
            )

    def test_ignores_preserved_source_history_but_rejects_active_stale_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_source = root / "wiki/private/_sources/knowledge/private/writing/voice.md"
            private_source.parent.mkdir(parents=True)
            private_source.write_text(
                "---\ntype: AI Reference\n---\n# Legacy\n",
                encoding="utf-8",
            )
            active_review = root / "brain/review/governance/report.md"
            active_review.parent.mkdir(parents=True)
            active_review.write_text(
                "old locator: ai-reference/legacy.md\n",
                encoding="utf-8",
            )

            issues = AUDIT.retired_ai_instruction_boundary_issues(root)

            self.assertEqual(len(issues), 1)
            self.assertTrue(any("ai-reference/" in issue for issue in issues))

    def test_rejects_removed_instruction_roots_and_nested_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ai-reference").mkdir()
            (root / ".legacy-backup").mkdir()
            (root / "wiki/canonical").mkdir(parents=True)
            (root / "brain/.git").mkdir(parents=True)

            issues = AUDIT.retired_ai_instruction_boundary_issues(root)

            self.assertEqual(len(issues), 4)
            self.assertTrue(any("ai-reference" in issue for issue in issues))
            self.assertTrue(any(".legacy-backup" in issue for issue in issues))
            self.assertTrue(any("wiki/canonical" in issue for issue in issues))
            self.assertTrue(any("nested Git" in issue for issue in issues))

    def test_rejects_retired_visualization_copy_and_sample_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "maps/legacy").mkdir(parents=True)
            (root / "maps/samples").mkdir(parents=True)
            duplicate_base = root / "inbox/recently-touched.base"
            duplicate_base.parent.mkdir(parents=True)
            duplicate_base.write_text("views: []\n", encoding="utf-8")

            issues = AUDIT.retired_legacy_view_issues(root)

            self.assertEqual(len(issues), 3)
            self.assertTrue(any("maps/legacy" in issue for issue in issues))
            self.assertTrue(any("maps/samples" in issue for issue in issues))
            self.assertTrue(any("inbox/recently-touched.base" in issue for issue in issues))


class BrainActivityLogTests(unittest.TestCase):
    def test_accepts_a_complete_user_confirmed_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "brain/log.md"
            log.parent.mkdir(parents=True)
            log.write_text(
                """# Log

- event_id: activity-001
  occurred_at: 2026-08-15T09:00:00+09:00
  kind: meeting
  source_id: gmail-thread:opaque-001
  facts: 면접 시간 확인
  interpretation:
  external_ids:
    task: task-001
    calendar: calendar-001
  privacy: local-only
  completion_source: user_confirmed
""",
                encoding="utf-8",
            )

            self.assertEqual(AUDIT.brain_activity_log_issues(log), [])

    def test_rejects_missing_fields_duplicate_ids_and_unconfirmed_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "brain/log.md"
            log.parent.mkdir(parents=True)
            log.write_text(
                """# Log

- event_id: activity-001
  occurred_at: 2026-08-15T09:00:00+09:00
  kind: learning
  source_id:
  facts: first
  interpretation: none
  external_ids:
    task: null
    calendar: null
  privacy: local-only
  completion_source: inferred
- event_id: activity-001
  occurred_at: 2026-08-15T10:00:00
  kind: unknown
  source_id: source://approved/example
  facts: second
  interpretation: none
  external_ids:
    task: null
    calendar: null
  privacy: public
  completion_source: not-completed
""",
                encoding="utf-8",
            )

            issues = AUDIT.brain_activity_log_issues(log)

            self.assertTrue(any("missing source_id" in issue for issue in issues))
            self.assertTrue(any("unsupported completion_source" in issue for issue in issues))
            self.assertTrue(any("duplicate event_id" in issue for issue in issues))
            self.assertTrue(any("timezone-aware" in issue for issue in issues))
            self.assertTrue(any("unsupported kind" in issue for issue in issues))
            self.assertTrue(any("privacy" in issue for issue in issues))


class PersonSchemaTests(unittest.TestCase):
    def _write_schema(self, root: Path) -> None:
        (root / "config").mkdir(parents=True)
        (root / "config/person-schema.json").write_text(
            """{
  "version": 1,
  "default_record_owner": {
    "person_id": "choi-woonyoung",
    "representation": "person-id",
    "mode": "implicit-if-omitted"
  },
  "privacy": {"automation_identity_inference": "forbidden"}
}
""",
            encoding="utf-8",
        )

    def _write_template(self, root: Path) -> None:
        (root / "templates").mkdir(exist_ok=True)
        (root / "templates/note.md").write_text(
            """---
type: Wiki
title: ""
publish: false
access: local-only
status: Draft
record_owner: choi-woonyoung
people: []
person_roles: []
attributions: []
---
""",
            encoding="utf-8",
        )

    def _write_card(self, root: Path, *, scope: str = "general") -> None:
        folder = "personal" if scope == "general" else "private"
        (root / f"wiki/{folder}").mkdir(parents=True)
        parent = (
            'parent: "[[wiki/people/README|인물 관계]]"\n'
            if scope == "general"
            else 'parent: "[[wiki/private/README|비공개 지식]]"\n'
        )
        (root / f"wiki/{folder}/최우녕.md").write_text(
            "---\n"
            "type: Wiki\ntitle: 최우녕\npublish: false\naccess: local-only\nstatus: Active\n"
            "entity_type: person\nperson_id: choi-woonyoung\nperson_kind: vault-owner\n"
            f"person_scope: {scope}\nrelationship_to_owner: 볼트 사용자\n{parent}"
            "---\n\n# 최우녕\n",
            encoding="utf-8",
        )

    def _write_map(self, root: Path) -> None:
        (root / "wiki/people").mkdir(parents=True, exist_ok=True)
        (root / "wiki/people/README.md").write_text(
            "# 인물 관계\n\n- [[wiki/personal/최우녕|최우녕]]\n",
            encoding="utf-8",
        )

    def test_accepts_general_person_cards_and_empty_template_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)

            self.assertEqual(AUDIT.person_schema_issues(root), [])

    def test_rejects_person_card_missing_required_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)
            card = root / "wiki/personal/최우녕.md"
            card.write_text(
                card.read_text(encoding="utf-8").replace("person_id: choi-woonyoung\n", ""),
                encoding="utf-8",
            )

            issues = AUDIT.person_schema_issues(root)

            self.assertTrue(any("must define person_id" in issue for issue in issues))

    def test_rejects_missing_default_owner_and_novel_card_on_general_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)
            template = root / "templates/note.md"
            template.write_text(
                template.read_text(encoding="utf-8").replace(
                    "record_owner: choi-woonyoung",
                    "record_owner: unknown",
                ),
                encoding="utf-8",
            )
            self._write_card(root, scope="novel-local-only")
            private_card = root / "wiki/private/최우녕.md"
            private_card.write_text(
                private_card.read_text(encoding="utf-8").replace(
                    "[[wiki/private/README|비공개 지식]]",
                    "[[wiki/people/README|인물 관계]]",
                ),
                encoding="utf-8",
            )

            issues = AUDIT.person_schema_issues(root)

            self.assertTrue(any("record_owner" in issue for issue in issues))
            self.assertTrue(any("novel-local-only" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
