from __future__ import annotations

from pathlib import Path

from woon_core.knowledge.document_quality import (
    contains_absolute_local,
    validate_markdown_candidate,
)


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
    assert "wikilink does not resolve: missing" in errors


def test_candidate_accepts_existing_link_and_unchanged_target_contract(tmp_path: Path) -> None:
    linked = tmp_path / "wiki/linked.md"
    linked.parent.mkdir(parents=True)
    linked.write_text(document("연결 대상."), encoding="utf-8")
    target = document("기존 본문.")
    candidate = document("기존 본문.\n\n[[linked|연결]]")

    assert validate_markdown_candidate(tmp_path, "wiki/example.md", target, candidate) == []


def test_candidate_does_not_resolve_links_from_quarantine(tmp_path: Path) -> None:
    quarantined = tmp_path / "_quarantine/linked.md"
    quarantined.parent.mkdir(parents=True)
    quarantined.write_text(document("격리 문서."), encoding="utf-8")
    candidate = document("[[linked|격리 링크]]")

    assert validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate) == [
        "wikilink does not resolve: linked"
    ]


def test_candidate_does_not_resolve_archived_markdown(tmp_path: Path) -> None:
    archived = tmp_path / "maps/old.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        document("이전 색인.")
        .replace("type: Wiki", "type: 키워드")
        .replace("access: local-only", "access: local-only\nstatus: Archived"),
        encoding="utf-8",
    )
    candidate = document("[[old|이전 색인]]")

    assert validate_markdown_candidate(tmp_path, "maps/current.md", None, candidate) == [
        "wikilink does not resolve: old"
    ]

    archived_text = archived.read_text(encoding="utf-8").replace(
        "이전 색인.", "[[maps/old|이전 색인]]"
    )
    assert validate_markdown_candidate(tmp_path, "maps/old.md", None, archived_text) == []


def test_candidate_rejects_missing_operational_path_and_public_private_locator(
    tmp_path: Path,
) -> None:
    candidate = document(
        "`scripts/missing.sh`를 실행하고 projects/writing/을 공개한다.",
        publish=True,
    ).replace("access: local-only", "access: public")

    errors = validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate)

    assert "local file reference does not resolve: scripts/missing.sh" in errors
    assert "public candidate exposes the private writing locator" in errors


def test_candidate_rejects_unclosed_fence_and_absolute_local_path(tmp_path: Path) -> None:
    local_path = "/" + "Users/example/private.md"
    candidate = document(f"```python\nprint('x')\n\n{local_path}")

    errors = validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate)

    assert "candidate contains an unclosed fenced code block" in errors
    assert "candidate exposes an absolute local path" in errors
    assert contains_absolute_local(candidate) is True


def test_candidate_ignores_markdown_syntax_examples_inside_code(tmp_path: Path) -> None:
    candidate = document(
        """문법 예시다.

```markdown
# 예시 H1
[[wikilink]]
maps/not-a-real-tree/
```

인라인 `[[placeholder]]`도 링크가 아니다.
"""
    )

    assert validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate) == []


def test_candidate_preserves_inline_code_when_comparing_h1_with_title(
    tmp_path: Path,
) -> None:
    candidate = (
        document("")
        .replace(
            "title: 예제\n",
            'title: "Alarm Clock 실험: `sleep_list`가 깨어나는 순간"\n',
        )
        .replace(
            "# 예제\n",
            "# Alarm Clock 실험: `sleep_list`가 깨어나는 순간\n",
        )
    )

    assert validate_markdown_candidate(tmp_path, "wiki/example.md", None, candidate) == []
