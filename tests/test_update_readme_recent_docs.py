"""Regression tests for concise, human-readable recent-document lists."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = (
    Path(__file__).parents[1]
    / "src/woon_core/knowledge/vault_tools/update-readme-recent-docs.py"
)
SPEC = importlib.util.spec_from_file_location("update_readme_recent_docs", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
RECENT_DOCS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECENT_DOCS)


class RecentDocumentBlockTests(unittest.TestCase):
    def test_renders_human_titles_without_redundant_date_or_type_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "inbox/daily/README.md"
            daily = root / "inbox/daily/2026-08-23.md"
            readme.parent.mkdir(parents=True)
            readme.write_text("# Daily\n", encoding="utf-8")
            daily.write_text(
                "---\n"
                'title: "2026-08-23"\n'
                "type: Daily\n"
                "date: 2026-08-23\n"
                "---\n\n"
                "# 2026-08-23\n",
                encoding="utf-8",
            )

            with mock.patch.object(RECENT_DOCS, "ROOT", root), mock.patch.object(
                RECENT_DOCS, "_GIT_ADDED_DATES", {}
            ):
                block = RECENT_DOCS.block_for(readme)

        self.assertIn("## 최근 문서", block)
        self.assertIn("[[inbox/daily/2026-08-23|2026-08-23]]", block)
        self.assertNotIn("· Daily", block)
        self.assertNotIn("최근 추가된 문서", block)

    def test_daily_notes_are_sorted_by_their_canonical_date_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "inbox/daily/README.md"
            older = root / "inbox/daily/2026-08-14.md"
            newer = root / "inbox/daily/2026-08-23.md"
            readme.parent.mkdir(parents=True)
            readme.write_text("# Daily\n", encoding="utf-8")
            for path in (older, newer):
                path.write_text(
                    "---\n"
                    f'title: "{path.stem}"\n'
                    "type: Daily\n"
                    "---\n\n"
                    f"# {path.stem}\n",
                    encoding="utf-8",
                )

            with mock.patch.object(RECENT_DOCS, "ROOT", root), mock.patch.object(
                RECENT_DOCS,
                "_GIT_ADDED_DATES",
                {
                    "inbox/daily/2026-08-14.md": RECENT_DOCS.dt.datetime(2026, 8, 23),
                    "inbox/daily/2026-08-23.md": RECENT_DOCS.dt.datetime(2026, 8, 14),
                },
            ):
                docs = RECENT_DOCS.docs_under(readme, limit=50)

        self.assertEqual([path.name for path in docs], ["2026-08-23.md", "2026-08-14.md"])
