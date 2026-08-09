from __future__ import annotations

from pathlib import Path

from woon_core.knowledge.document_quality import validate_markdown_candidate


def document(body: str, *, publish: bool = False) -> str:
    return f"""---
title: 예제
type: Wiki
publish: {str(publish).lower()}
access: local-only
---

# 예제

{body}
"""


def test_candidate_rejects_broken_added_link_and_protected_metadata_change(
    tmp_path: Path,
) -> None:
    target = document("기존 본문.")
    candidate = document("기존 본문.\n\n[[missing|없는 문서]]", publish=True)

    errors = validate_markdown_candidate(tmp_path, "wiki/example.md", target, candidate)

    assert "protected frontmatter field changed: publish" in errors
    assert "new wikilink does not resolve: missing" in errors


def test_candidate_accepts_existing_link_and_unchanged_target_contract(tmp_path: Path) -> None:
    linked = tmp_path / "wiki/linked.md"
    linked.parent.mkdir(parents=True)
    linked.write_text(document("연결 대상."), encoding="utf-8")
    target = document("기존 본문.")
    candidate = document("기존 본문.\n\n[[linked|연결]]")

    assert validate_markdown_candidate(tmp_path, "wiki/example.md", target, candidate) == []


def test_candidate_rejects_unclosed_fence_and_absolute_local_path(tmp_path: Path) -> None:
    local_path = "/" + "Users/example/private.md"
    candidate = document(f"```python\nprint('x')\n\n{local_path}")

    errors = validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate)

    assert "candidate contains an unclosed fenced code block" in errors
    assert "candidate exposes an absolute local path" in errors
