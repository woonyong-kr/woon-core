#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

ROOT = Path.cwd().resolve()
START = "<!-- recent-docs:start -->"
END = "<!-- recent-docs:end -->"
LIMIT = 50

SKIP_PARTS = {
    ".git",
    ".obsidian",
    "assets",
    "exports",
    "node_modules",
    "quartz",
    "scripts",
    "templates",
}

PROTECTED_PREFIXES = {
    ("projects", "writing"),
}

README_ROOTS = {
    "README.md",
    "maps",
    "wiki",
    "users",
    "projects",
    "sources",
    "inbox",
    "types",
}

_GIT_ADDED_DATES: dict[str, dt.datetime] | None = None


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    return text[: end + 4], text[end + 5 :]


def frontmatter_value(text: str, key: str) -> str | None:
    frontmatter, _ = split_frontmatter(text)
    if not frontmatter:
        return None
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", frontmatter, re.M)
    if not match:
        return None
    value = match.group(1).strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


def first_h1(text: str) -> str | None:
    _, body = split_frontmatter(text)
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def title_for(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return frontmatter_value(text, "title") or first_h1(text) or path.stem.replace("-", " ")


def doc_type(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return frontmatter_value(text, "type") or "문서"


def access_for(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return frontmatter_value(text, "access") or "private"


def is_public(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    return (frontmatter_value(text, "publish") or "").lower() == "true" and (
        frontmatter_value(text, "access") or ""
    ).lower() == "public"


def is_protected(rel: Path) -> bool:
    return any(rel.parts[: len(prefix)] == prefix for prefix in PROTECTED_PREFIXES)


def is_skipped(rel: Path) -> bool:
    return any(part in SKIP_PARTS for part in rel.parts) or is_protected(rel)


def readme_in_scope(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if path.name != "README.md":
        return False
    if is_skipped(rel):
        return False
    if rel.as_posix() == "README.md":
        return False
    top = rel.parts[0]
    return top in README_ROOTS


def note_in_scope(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if is_skipped(rel):
        return False
    return path.suffix == ".md"


def note_date(path: Path) -> dt.datetime:
    text = path.read_text(encoding="utf-8", errors="ignore")
    for key in ("created", "created_at", "date"):
        raw = frontmatter_value(text, key)
        if not raw:
            continue
        raw = raw.strip().strip('"').strip("'")
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return dt.datetime.strptime(raw[: len(fmt)], fmt)
            except ValueError:
                pass
    relative_path = path.relative_to(ROOT).as_posix()
    added_at = git_added_dates().get(relative_path)
    if added_at is not None:
        return added_at

    # Git에 아직 들어가지 않은 새 문서만 파일 수정 시각을 임시 기준으로 쓴다.
    return dt.datetime.fromtimestamp(path.stat().st_mtime)


def git_added_dates() -> dict[str, dt.datetime]:
    global _GIT_ADDED_DATES
    if _GIT_ADDED_DATES is not None:
        return _GIT_ADDED_DATES

    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "log",
            "--all",
            "--reverse",
            "--diff-filter=A",
            "--format=@@%cs",
            "--name-only",
            "--",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    dates: dict[str, dt.datetime] = {}
    current_date: dt.datetime | None = None
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("@@"):
                try:
                    current_date = dt.datetime.strptime(line[2:], "%Y-%m-%d")
                except ValueError:
                    current_date = None
            elif line and current_date is not None:
                dates.setdefault(line, current_date)
    _GIT_ADDED_DATES = dates
    return dates


def wikilink(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("").as_posix()
    if rel == "README":
        rel = "README"
    return f"[[{rel}|{title_for(path)}]]"


def docs_under(readme: Path, limit: int) -> list[Path]:
    root = readme.parent

    docs: list[Path] = []
    public_readme = is_public(readme)
    for path in root.rglob("*.md"):
        if path == readme:
            continue
        if not note_in_scope(path):
            continue
        if public_readme and not is_public(path):
            continue
        docs.append(path)
    docs.sort(key=lambda p: (note_date(p), p.relative_to(ROOT).as_posix()), reverse=True)
    return docs[:limit]


def block_for(readme: Path) -> str:
    docs = docs_under(readme, LIMIT)
    lines = [
        START,
        "## 최근 추가 문서",
        "",
        f"최근 추가된 문서 {LIMIT}개.",
        "",
    ]
    if not docs:
        lines.append("- 아직 하위 문서가 없습니다.")
    for path in docs:
        when = note_date(path).strftime("%Y-%m-%d")
        lines.append(f"- {wikilink(path)} — {when} · {doc_type(path)}")
    lines.extend(["", END])
    return "\n".join(lines)


def replace_or_append(text: str, block: str) -> str:
    pattern = re.compile(rf"\n*{re.escape(START)}\n.*?\n{re.escape(END)}\n*", re.S)
    if START in text and END in text:
        updated = pattern.sub(f"\n\n{block}\n", text, count=1)
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"
    return updated.rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="갱신할 README의 저장소 상대 경로. 생략하면 전체를 갱신한다.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    changed = 0
    if args.path:
        readmes = [ROOT / relative for relative in args.path]
        invalid = [path for path in readmes if not path.is_file() or not readme_in_scope(path)]
        if invalid:
            raise SystemExit(
                "invalid README path: " + ", ".join(path.as_posix() for path in invalid)
            )
    else:
        readmes = [path for path in sorted(ROOT.rglob("README.md")) if readme_in_scope(path)]
    for path in readmes:
        text = path.read_text(encoding="utf-8", errors="ignore")
        updated = replace_or_append(text, block_for(path))
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    print(f"recent_docs_readmes={len(readmes)}")
    print(f"recent_docs_changed={changed}")


if __name__ == "__main__":
    main()
