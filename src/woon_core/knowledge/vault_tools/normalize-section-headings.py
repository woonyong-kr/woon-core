#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path.cwd().resolve()

TARGET_ROOTS = [
    Path("README.md"),
    Path("maps"),
    Path("wiki"),
    Path("users"),
]

SKIP_DIRS = {
    ".git",
    ".obsidian",
    "assets",
    "node_modules",
    "quartz",
}

HEADING_MAP = [
    (re.compile(r"^(왜 필요(?:한가|할까)\??)$"), "배경"),
    (re.compile(r"^(작은 질문)$"), "출발점"),
    (re.compile(r"^(핵심 모델)(?:\s*\(.+?\))?$"), "동작 원리"),
    (re.compile(r"^(핵심 개념)$"), "동작 원리"),
    (re.compile(r"^(예시 상황)(?:\s*\(.+?\))?$"), "예시"),
    (re.compile(r"^(작은 예시)$"), "예시"),
    (re.compile(r"^(Linux\s*/\s*Windows에서는)(?:\s*\(.+?\))?$"), "실제 운영체제"),
    (re.compile(r"^(실제 OS에서는)$"), "실제 운영체제"),
    (re.compile(r"^(PintOS에서는)(?:\s*\(.+?\))?$"), "PintOS"),
    (re.compile(r"^(QEMU에서는)(?:\s*\(.+?\))?$"), "QEMU"),
    (re.compile(r"^(차이점)$"), "비교"),
    (re.compile(r"^(코드 증거)$"), "코드에서 보기"),
    (re.compile(r"^(코드로 보기)$"), "코드에서 보기"),
    (re.compile(r"^(숫자와 메모리)(?::.*)?$"), "값으로 보기"),
    (re.compile(r"^(숫자와 메모리로 보는 예시)$"), "값으로 보기"),
    (re.compile(r"^(직접 확인)(?:\s*\(.+?\))?$"), "확인 방법"),
    (re.compile(r"^(직접 확인할 질문)$"), "확인 방법"),
    (re.compile(r"^(다음 링크)$"), "이어서 볼 문서"),
    (re.compile(r"^(관련 링크)$"), "이어서 볼 문서"),
    (re.compile(r"^(정리하며)$"), "정리"),
    (re.compile(r"^(판단 기준)$"), "선택 기준"),
    (re.compile(r"^(체크포인트)$"), "확인할 것"),
    (re.compile(r"^(이 문서의 키워드)$"), "키워드"),
    (re.compile(r"^(질문 흐름)$"), "질문"),
]

ANCHOR_REPLACEMENTS = {
    "#왜 필요한가": "#배경",
    "#왜 필요할까": "#배경",
    "#작은 질문": "#출발점",
    "#핵심 모델": "#동작 원리",
    "#핵심 개념": "#동작 원리",
    "#예시 상황": "#예시",
    "#작은 예시": "#예시",
    "#Linux / Windows에서는": "#실제 운영체제",
    "#Linux/Windows에서는": "#실제 운영체제",
    "#실제 OS에서는": "#실제 운영체제",
    "#PintOS에서는": "#PintOS",
    "#QEMU에서는": "#QEMU",
    "#차이점": "#비교",
    "#코드 증거": "#코드에서 보기",
    "#코드로 보기": "#코드에서 보기",
    "#숫자와 메모리": "#값으로 보기",
    "#숫자와 메모리로 보는 예시": "#값으로 보기",
    "#직접 확인": "#확인 방법",
    "#직접 확인할 질문": "#확인 방법",
    "#다음 링크": "#이어서 볼 문서",
    "#관련 링크": "#이어서 볼 문서",
    "#정리하며": "#정리",
    "#판단 기준": "#선택 기준",
    "#체크포인트": "#확인할 것",
    "#이 문서의 키워드": "#키워드",
    "#질문 흐름": "#질문",
}

VISIBLE_LABEL_REPLACEMENTS = {
    "|왜 필요한가]]": "|배경]]",
    "|왜 필요할까]]": "|배경]]",
    "|작은 질문]]": "|출발점]]",
    "|핵심 모델]]": "|동작 원리]]",
    "|핵심 개념]]": "|동작 원리]]",
    "|예시 상황]]": "|예시]]",
    "|작은 예시]]": "|예시]]",
    "|차이점]]": "|비교]]",
    "|코드 증거]]": "|코드에서 보기]]",
    "|코드로 보기]]": "|코드에서 보기]]",
    "|숫자와 메모리]]": "|값으로 보기]]",
    "|숫자와 메모리로 보는 예시]]": "|값으로 보기]]",
    "|직접 확인]]": "|확인 방법]]",
    "|직접 확인할 질문]]": "|확인 방법]]",
    "|다음 링크]]": "|이어서 볼 문서]]",
    "|관련 링크]]": "|이어서 볼 문서]]",
    "|정리하며]]": "|정리]]",
    "|판단 기준]]": "|선택 기준]]",
    "|체크포인트]]": "|확인할 것]]",
    "|이 문서의 키워드]]": "|키워드]]",
    "|질문 흐름]]": "|질문]]",
}


def markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in TARGET_ROOTS:
        path = ROOT / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                rel = item.relative_to(ROOT)
                if any(part in SKIP_DIRS for part in rel.parts):
                    continue
                if rel.parts[:2] == ("projects", "writing"):
                    continue
                files.append(item)
    return sorted(set(files))


def map_heading(title: str) -> str | None:
    title = title.strip()
    for pattern, replacement in HEADING_MAP:
        if pattern.match(title):
            return replacement
    return None


def normalize_text(text: str) -> tuple[str, int]:
    changed = 0
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            lines.append(line)
            continue

        if not in_fence:
            match = re.match(r"^(#{2,4})\s+(.+?)(\s*)$", line.rstrip("\n"))
            if match:
                replacement = map_heading(match.group(2))
                if replacement is not None:
                    newline = "\n" if line.endswith("\n") else ""
                    line = f"{match.group(1)} {replacement}{newline}"
                    changed += 1
        lines.append(line)

    updated = "".join(lines)
    for old, new in ANCHOR_REPLACEMENTS.items():
        if old in updated:
            updated = updated.replace(old, new)
            changed += 1
    for old, new in VISIBLE_LABEL_REPLACEMENTS.items():
        if old in updated:
            updated = updated.replace(old, new)
            changed += 1

    return updated, changed


def main() -> None:
    files_changed = 0
    heading_changes = 0
    for path in markdown_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        updated, changes = normalize_text(text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            files_changed += 1
            heading_changes += changes

    print(f"section_heading_files_changed={files_changed}")
    print(f"section_heading_changes={heading_changes}")
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("personalize-section-headings.py"))],
        check=True,
    )


if __name__ == "__main__":
    main()
