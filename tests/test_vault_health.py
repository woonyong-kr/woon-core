#!/usr/bin/env python3
"""Regression tests for the retired external-video archive boundary."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).parents[1] / "src/woon_core/knowledge/vault_tools/audit-vault-health.py"
)
SPEC = importlib.util.spec_from_file_location("audit_vault_health", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


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
            today = datetime.now(AUDIT.ZoneInfo("Asia/Seoul")).date().isoformat()
            (daily / f"{today}.md").write_text(
                f"![[../daily-digests/{today}]]\n", encoding="utf-8"
            )
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
            (root / "brain/.git").mkdir(parents=True)

            issues = AUDIT.retired_ai_instruction_boundary_issues(root)

            self.assertEqual(len(issues), 3)
            self.assertTrue(any("ai-reference" in issue for issue in issues))
            self.assertTrue(any(".legacy-backup" in issue for issue in issues))
            self.assertTrue(any("nested Git" in issue for issue in issues))


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
        (root / "users/choi-woonyoung").mkdir(parents=True)
        parent = 'parent_moc: "[[people-index|인물 관계]]"\n' if scope == "general" else ""
        (root / "users/choi-woonyoung/README.md").write_text(
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
            "# 인물 관계\n\n![[../inbox/person-directory.base]]\n",
            encoding="utf-8",
        )

    def _write_registry(self, root: Path) -> None:
        (root / "users/README.md").write_text(
            "# 등록 인물\n\n- [[users/choi-woonyoung/README|최우녕]]\n",
            encoding="utf-8",
        )

    def test_accepts_general_person_cards_and_empty_template_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)
            self._write_registry(root)

            self.assertEqual(AUDIT.person_schema_issues(root), [])

    def test_rejects_registered_card_missing_from_visible_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)
            self._write_registry(root)
            registry = root / "users/README.md"
            registry.write_text("# 등록 인물\n", encoding="utf-8")

            issues = AUDIT.person_schema_issues(root)

            self.assertTrue(any("must link registered person card" in issue for issue in issues))

    def test_rejects_missing_default_owner_and_novel_card_on_general_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_schema(root)
            self._write_template(root)
            self._write_card(root)
            self._write_map(root)
            self._write_registry(root)
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
                people_map.read_text(encoding="utf-8") + "[[users/lee-minjeong/README|이민정]]\n",
                encoding="utf-8",
            )

            issues = AUDIT.person_schema_issues(root)

            self.assertTrue(any("record_owner" in issue for issue in issues))
            self.assertTrue(any("novel-local-only" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
