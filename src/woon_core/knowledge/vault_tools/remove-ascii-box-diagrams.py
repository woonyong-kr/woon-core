#!/usr/bin/env python3
"""Replace legacy ASCII box diagrams in user-facing Markdown with Markdown lists.

This is intentionally conservative: it only rewrites unlabeled or `text`
fenced blocks that contain box-drawing characters. Source code blocks keep
their language fences intact.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path.cwd().resolve()
TARGETS = [ROOT / "wiki", ROOT / "maps", ROOT / "projects", ROOT / "README.md"]
BOX_CHARS = "┌┐└┘├┤┬┴┼─│►▶◄▼▲"
BOX_RE = re.compile(f"[{re.escape(BOX_CHARS)}]")
FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)


def iter_markdown_files() -> list[Path]:
    files: list[Path] = []
    for target in TARGETS:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(target.rglob("*.md"))
    return sorted(files)


def clean_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""

    border_only = re.sub(f"[{re.escape(BOX_CHARS)}\\s]+", "", line)
    if not border_only:
        return ""

    line = line.replace("│", " | ")
    line = line.replace("├", " ")
    line = line.replace("┤", " ")
    line = line.replace("┌", " ")
    line = line.replace("┐", " ")
    line = line.replace("└", " ")
    line = line.replace("┘", " ")
    line = line.replace("┬", " ")
    line = line.replace("┴", " ")
    line = line.replace("┼", " ")
    line = line.replace("─", " ")
    line = line.replace("►", "->")
    line = line.replace("▶", "->")
    line = line.replace("◄", "<-")
    line = line.replace("▼", "↓")
    line = line.replace("▲", "↑")
    line = re.sub(r"\s*\|\s*", " | ", line)
    line = re.sub(r"\s{2,}", " ", line)
    line = line.strip(" |")
    return line.strip()


def block_to_markdown(body: str) -> str:
    rows: list[str] = []
    for raw in body.splitlines():
        cleaned = clean_line(raw)
        if not cleaned:
            continue
        if rows and rows[-1] == cleaned:
            continue
        rows.append(cleaned)

    if not rows:
        return ""

    if len(rows) == 1:
        return rows[0] + "\n"

    return "\n".join(f"- {row}" for row in rows) + "\n"


def rewrite_text(text: str) -> tuple[str, int]:
    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        lang = match.group(1).strip()
        body = match.group(2)
        if lang not in {"", "text"}:
            return match.group(0)
        if not BOX_RE.search(body):
            return match.group(0)
        changed += 1
        return block_to_markdown(body)

    return FENCE_RE.sub(replace, text), changed


def main() -> int:
    apply = "--apply" in sys.argv
    changed_files = 0
    changed_blocks = 0
    for path in iter_markdown_files():
        text = path.read_text(encoding="utf-8")
        new_text, blocks = rewrite_text(text)
        if blocks == 0:
            continue
        changed_files += 1
        changed_blocks += blocks
        if apply:
            path.write_text(new_text, encoding="utf-8")
    print(f"apply={apply} files={changed_files} blocks={changed_blocks}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
