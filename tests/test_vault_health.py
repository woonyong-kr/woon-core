#!/usr/bin/env python3
"""Regression tests for the retired external-video archive boundary."""

from __future__ import annotations

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
            (root / "sources/external-video").mkdir(parents=True)

            issues = AUDIT.retired_external_video_boundary_issues(root)

            self.assertEqual(len(issues), 1)
            self.assertIn("retired", issues[0])


class ScopedContextTreeTitleTests(unittest.TestCase):
    def test_allows_distinct_context_graph_cards_with_the_same_display_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            basic = root / "maps/context-graph/basic/PintOS.md"
            interview = root / "maps/context-graph/interview/PintOS.md"
            for path in (basic, interview):
                path.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                basic: {"context_tree": True, "context_tree_parent": "[[기본]]"},
                interview: {"context_tree": True, "context_tree_parent": "[[면접]]"},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertTrue(
                    AUDIT.is_scoped_context_tree_title_collision([basic, interview], metadata)
                )

    def test_rejects_same_title_when_a_non_index_document_is_mixed_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scoped = root / "maps/context-graph/basic/PintOS.md"
            unrelated = root / "maps/pintos-note.md"
            for path in (scoped, unrelated):
                path.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                scoped: {"context_tree": True, "context_tree_parent": "[[기본]]"},
                unrelated: {},
            }

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertFalse(
                    AUDIT.is_scoped_context_tree_title_collision([scoped, unrelated], metadata)
                )


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

    def test_accepts_project_and_content_facets_in_the_same_wiki_contract(self) -> None:
        project = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/자격-준비.md",
            "# 자격 준비\n",
            {
                "type": "Wiki",
                "access": "local-only",
                "canonical_id": "personal/자격-준비",
                "facets": ["프로젝트", "학습"],
                "knowledge_state": "생각 중",
                "parent_topics": ["[[wiki/README|Wiki]]"],
                "project_id": "aice-associate",
                "objective": "자격 취득",
            },
        )
        content = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/학습-자료.md",
            "# 학습 자료\n",
            {
                "type": "Wiki",
                "access": "local-only",
                "canonical_id": "personal/학습-자료",
                "facets": ["콘텐츠", "학습"],
                "knowledge_state": "확인 필요",
                "parent_topics": ["[[wiki/README|Wiki]]"],
                "content_kind": "course",
            },
        )

        self.assertEqual(project, ([], []))
        self.assertEqual(content, ([], []))

    def test_rejects_an_invalid_canonical_identity_and_facet(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/깨진-정체성.md",
            "# 깨진 정체성\n",
            {
                "type": "Wiki",
                "canonical_id": "../깨진 정체성",
                "facets": ["학습", "학습", "임의 종류"],
                "knowledge_state": "생각 중",
                "parent_topics": ["[[wiki/README|Wiki]]"],
            },
        )

        self.assertTrue(any("canonical_id" in issue for issue in wiki))
        self.assertTrue(any("facets" in issue for issue in wiki))

    def test_rejects_multiple_primary_parent_topics(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/여러-부모.md",
            "# 여러 부모\n",
            {
                "type": "Wiki",
                "canonical_id": "personal/여러-부모",
                "facets": ["개념"],
                "knowledge_state": "생각 중",
                "parent_topics": ["[[wiki/README|Wiki]]", "[[wiki/ai/README|AI]]"],
            },
        )

        self.assertTrue(any("exactly one primary" in issue for issue in wiki))

    def test_rejects_numbered_interview_identity_and_wiki_root_parent(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/Q06-Kyro-문제와-역할.md",
            "# Q06. Kyro 문제와 역할\n",
            {
                "type": "Wiki",
                "title": "Q06. Kyro 문제와 역할",
                "canonical_id": "personal/interview/Q06-Kyro-문제와-역할",
                "facets": ["커리어", "학습"],
                "knowledge_state": "확인 필요",
                "parent_topics": ["[[wiki/README|Wiki]]"],
                "question_kind": "interview",
                "interview_tracks": ["KRAFTON AI Engineer"],
                "question_topic": "Kubernetes 장애 복구 서비스",
            },
        )

        self.assertTrue(any("sequence or archive" in issue for issue in wiki))
        self.assertTrue(any("without numbering" in issue for issue in wiki))
        self.assertTrue(any("semantic parent" in issue for issue in wiki))

    def test_accepts_semantic_interview_identity(self) -> None:
        wiki, entities = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/kubernetes-장애-원인을-어떻게-판정했습니까.md",
            "# Kubernetes 장애 원인을 어떻게 판정했습니까?\n",
            {
                "type": "Wiki",
                "title": "Kubernetes 장애 원인을 어떻게 판정했습니까?",
                "canonical_id": "personal/interview/kubernetes-장애-원인을-어떻게-판정했습니까",
                "facets": ["커리어", "학습"],
                "knowledge_state": "확인 필요",
                "parent_topics": [
                    "[[wiki/personal/projects/kubernetes-장애-복구-서비스|"
                    "Kubernetes 장애 복구 서비스]]"
                ],
                "question_kind": "interview",
                "interview_tracks": ["KRAFTON AI Engineer"],
                "question_topic": "Kubernetes 장애 복구 서비스",
            },
        )

        self.assertEqual((wiki, entities), ([], []))

    def test_rejects_job_track_as_question_parent(self) -> None:
        wiki, _ = AUDIT.wiki_and_entity_policy_issues(
            "wiki/personal/interview/ai-engineer/지원-이유.md",
            "# 지원 이유는 무엇입니까?\n",
            {
                "type": "Wiki",
                "title": "지원 이유는 무엇입니까?",
                "canonical_id": "personal/interview/ai-engineer/지원-이유",
                "facets": ["커리어", "학습"],
                "knowledge_state": "확인 필요",
                "parent_topics": [
                    "[[wiki/personal/interview/ai-engineer/README|KRAFTON AI Engineer 면접 준비]]"
                ],
                "question_kind": "interview",
                "interview_tracks": ["KRAFTON AI Engineer"],
                "question_topic": "경력 서사와 지원 동기",
            },
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
            source = root / "sources/imports/example.py"
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

    def test_allows_linked_canvas_state_only_when_all_targets_exist(self) -> None:
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
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(state))
                state.write_text(
                    state.read_text(encoding="utf-8").replace("wiki/example.md", "wiki/missing.md"),
                    encoding="utf-8",
                )
                self.assertFalse(AUDIT.is_allowed_non_markdown_file(state))

    def test_allows_context_graph_layout_only_beside_context_map_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout = root / "maps/context-graph/example.context-graph"
            layout.parent.mkdir(parents=True)
            layout.write_text("{}\n", encoding="utf-8")
            unrelated = root / "maps/example.context-graph"
            unrelated.write_text("{}\n", encoding="utf-8")

            with mock.patch.object(AUDIT, "VAULT", root):
                self.assertTrue(AUDIT.is_allowed_non_markdown_file(layout))
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
cssclasses: context-calendar-dashboard
---

```context-calendar
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
    def test_rejects_legacy_ai_reference_and_active_stale_locator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_source = root / "sources/private/writing/voice.md"
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

            self.assertEqual(len(issues), 2)
            self.assertTrue(any("AI Reference" in issue for issue in issues))
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
        parent = 'parent_moc: "[[people-index|인물 관계]]"\n' if scope == "general" else ""
        (root / f"wiki/{folder}/최우녕.md").write_text(
            "---\n"
            "type: Wiki\ntitle: 최우녕\npublish: false\naccess: local-only\nstatus: Active\n"
            "entity_type: person\nperson_id: choi-woonyoung\nperson_kind: vault-owner\n"
            f"person_scope: {scope}\nrelationship_to_owner: 볼트 사용자\n{parent}"
            "---\n\n# 최우녕\n",
            encoding="utf-8",
        )

    def _write_map(self, root: Path) -> None:
        (root / "maps").mkdir(exist_ok=True)
        (root / "maps/people-index.md").write_text(
            "# 인물 관계\n\n![[../inbox/wiki/wiki.base#인물]]\n",
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
            people_map = root / "maps/people-index.md"
            people_map.write_text(
                people_map.read_text(encoding="utf-8") + "[[wiki/private/이민정|이민정]]\n",
                encoding="utf-8",
            )

            issues = AUDIT.person_schema_issues(root)

            self.assertTrue(any("record_owner" in issue for issue in issues))
            self.assertTrue(any("novel-local-only" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
