#!/usr/bin/env python3
"""Regression checks for document-cohesion paragraph classification."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).parents[1] / "src/woon_core/knowledge/vault_tools/assess-document-cohesion.py"
)
SPEC = importlib.util.spec_from_file_location("assess_document_cohesion", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CohesionParagraphTests(unittest.TestCase):
    def test_source_archive_path_is_excluded_from_reader_facing_corpus(self) -> None:
        self.assertTrue(MODULE._is_source_archive(MODULE.VAULT / "wiki/_sources/raw.md"))
        self.assertFalse(MODULE._is_source_archive(MODULE.VAULT / "wiki/concepts/raw.md"))

    def test_keeps_a_fenced_block_with_internal_blank_lines_as_code(self) -> None:
        body = """설명 문단입니다.

```python
# 주석

value = 1
```

다음 설명입니다.
"""

        self.assertEqual(
            MODULE._paragraphs_with_kind(body),
            [
                ("prose", "설명 문단입니다."),
                ("code-or-output", "```python\n# 주석\n\nvalue = 1\n```"),
                ("prose", "다음 설명입니다."),
            ],
        )


if __name__ == "__main__":
    unittest.main()
