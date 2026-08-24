#!/usr/bin/env python3
from __future__ import annotations

import re
import os
from pathlib import Path


ROOT = Path.cwd().resolve()
START = "<!-- breadcrumb:start -->"
END = "<!-- breadcrumb:end -->"

USER_FACING_ROOTS = [
    Path("README.md"),
    Path("maps"),
    Path("wiki"),
    Path("users"),
]

DOMAIN_HUBS = {
    "os": Path("maps/os-moc.md"),
    "pintos": Path("maps/pintos-moc.md"),
    "tools": Path("maps/tools-obsidian-pkm-map.md"),
}

AI_HUBS = {
    "foundations": Path("maps/ai-neural-network-moc.md"),
    "neural-network": Path("maps/ai-neural-network-moc.md"),
    "vision": Path("maps/cnn-vision-map.md"),
    "sequence": Path("maps/sequence-model-rnn-map.md"),
    "transformer": Path("maps/ai-llm-moc.md"),
    "llm-pretraining": Path("maps/ai-llm-moc.md"),
    "llm-alignment": Path("maps/ai-llm-moc.md"),
    "llm-inference": Path("maps/ai-llm-moc.md"),
    "rag-agent": Path("maps/rag-agent-application-map.md"),
}


def user_facing_markdown() -> list[Path]:
    paths: list[Path] = []
    for root in USER_FACING_ROOTS:
        abs_root = ROOT / root
        if abs_root.is_file():
            paths.append(abs_root)
        elif abs_root.is_dir():
            paths.extend(abs_root.rglob("*.md"))
    return sorted(paths)


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


def title_for(path: Path, text_cache: dict[Path, str]) -> str:
    text = text_cache.get(path)
    if text is None:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        text_cache[path] = text
    title = frontmatter_value(text, "title") or first_h1(text)
    if title:
        return title.strip()
    if path.name in {"README.md", "index.md"}:
        return path.parent.name
    return path.stem.replace("-", " ")


def link_target(path: Path) -> str:
    rel = path.relative_to(ROOT)
    if rel == Path("README.md"):
        return "README"
    return rel.with_suffix("").as_posix()


def markdown_link(owner: Path, target: Path, text_cache: dict[Path, str]) -> str:
    if target.relative_to(ROOT) == Path("README.md"):
        href = Path(os.path.relpath(ROOT, owner.parent)).as_posix()
        if href == ".":
            href = "./"
        elif not href.endswith("/"):
            href = f"{href}/"
        return f"[{title_for(target, text_cache)}]({href})"
    href = Path(os.path.relpath(target, owner.parent)).as_posix()
    return f"[{title_for(target, text_cache)}]({href})"


def wikilink(path: Path, text_cache: dict[Path, str]) -> str:
    return f"[[{link_target(path)}|{title_for(path, text_cache)}]]"


def build_lookup(paths: list[Path]) -> dict[str, Path]:
    lookup: dict[str, Path] = {}
    for path in paths:
        rel = path.relative_to(ROOT).with_suffix("").as_posix()
        lookup[rel] = path
        if path.name == "README.md" and path.parent == ROOT:
            lookup.setdefault("index", path)
            lookup.setdefault("README", path)
        elif path.name == "README.md":
            lookup.setdefault(rel.removesuffix("/README"), path)
            lookup.setdefault(path.parent.name, path)
        else:
            lookup.setdefault(path.stem, path)
    return lookup


def target_from_wikilink(value: str | None, lookup: dict[str, Path]) -> Path | None:
    if not value:
        return None
    match = re.search(r"\[\[([^|\]#]+)", value)
    if not match:
        return None
    target = match.group(1).strip()
    if target in lookup:
        return lookup[target]
    target_path = (ROOT / target).with_suffix(".md")
    if target_path.exists():
        return target_path
    return None


def append_unique(items: list[Path], path: Path | None) -> None:
    if path and path.exists() and path not in items:
        items.append(path)


def domain_hub(path: Path) -> Path | None:
    rel = path.relative_to(ROOT)
    if len(rel.parts) < 2 or rel.parts[0] != "wiki":
        return None
    domain = rel.parts[1]
    if domain == "ai" and len(rel.parts) >= 3:
        return AI_HUBS.get(rel.parts[2], Path("maps/ai-neural-network-moc.md"))
    if domain in DOMAIN_HUBS:
        return DOMAIN_HUBS[domain]
    domain_index = ROOT / "wiki" / domain / "README.md"
    if domain_index.exists():
        return domain_index.relative_to(ROOT)
    return Path("wiki/README.md")


def folder_indexes(path: Path) -> list[Path]:
    rel = path.relative_to(ROOT)
    out: list[Path] = []
    if rel.parts[0] == "wiki":
        parent_parts = rel.parts[:-1]
        # The domain hub already carries the first-level wiki domain.
        for depth in range(3, len(parent_parts) + 1):
            candidate = ROOT.joinpath(*parent_parts[:depth], "README.md")
            if candidate.exists() and candidate != path:
                out.append(candidate)
    return out


def crumbs_for(path: Path, text: str, lookup: dict[str, Path], text_cache: dict[Path, str]) -> list[Path]:
    rel = path.relative_to(ROOT)
    crumbs: list[Path] = []
    append_unique(crumbs, ROOT / "README.md")

    if rel == Path("README.md"):
        return crumbs

    if rel.parts[0] == "maps":
        append_unique(crumbs, path)
        return crumbs

    parent = target_from_wikilink(frontmatter_value(text, "parent_moc"), lookup)

    if rel.parts[0] == "wiki":
        append_unique(crumbs, parent or (ROOT / domain_hub(path)))
        for idx in folder_indexes(path):
            append_unique(crumbs, idx)
        append_unique(crumbs, path)
        return crumbs

    if rel.parts[0] == "users":
        append_unique(crumbs, parent or (ROOT / "maps/people-index.md"))
        append_unique(crumbs, path)
        return crumbs

    append_unique(crumbs, path)
    return crumbs


def breadcrumb_block(path: Path, text: str, lookup: dict[str, Path], text_cache: dict[Path, str]) -> str:
    links = []
    for crumb in crumbs_for(path, text, lookup, text_cache):
        if crumb.relative_to(ROOT) == Path("README.md"):
            links.append(markdown_link(path, crumb, text_cache))
        else:
            links.append(wikilink(crumb, text_cache))
    return f"{START}\n상위 링크: {' / '.join(links)}\n{END}"


def replace_or_insert(path: Path, text: str, lookup: dict[str, Path], text_cache: dict[Path, str]) -> tuple[str, bool]:
    block = breadcrumb_block(path, text, lookup, text_cache)
    pattern = re.compile(
        rf"\n?{re.escape(START)}\n.*?\n{re.escape(END)}\n?",
        re.S,
    )
    if START in text and END in text:
        updated = pattern.sub(f"\n\n{block}\n\n", text, count=1)
        updated = normalize_breadcrumb_spacing(updated)
        return updated, updated != text

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("# "):
            insert_at = i + 1
            while insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            lines[i + 1 : insert_at] = ["\n", block + "\n", "\n"]
            return normalize_breadcrumb_spacing("".join(lines)), True
    return text, False


def normalize_breadcrumb_spacing(text: str) -> str:
    text = re.sub(
        rf"(?m)(^# .+\n)\n+({re.escape(START)})",
        r"\1\n\2",
        text,
        count=1,
    )
    text = re.sub(
        rf"({re.escape(END)})\n{{3,}}",
        r"\1\n\n",
        text,
        count=1,
    )
    return text


def main() -> None:
    paths = user_facing_markdown()
    lookup = build_lookup(paths)
    text_cache: dict[Path, str] = {}
    changed = 0
    skipped: list[Path] = []

    for path in paths:
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        text_cache[path] = text
        if first_h1(text) is None:
            skipped.append(path.relative_to(ROOT))
            continue
        updated, did_change = replace_or_insert(path, text, lookup, text_cache)
        if did_change:
            path.write_text(updated)
            changed += 1

    print(f"breadcrumb_changed={changed}")
    print(f"breadcrumb_skipped={len(skipped)}")
    for path in skipped[:40]:
        print(f"skipped_no_h1={path}")
    if len(skipped) > 40:
        print(f"skipped_no_h1_more={len(skipped) - 40}")


if __name__ == "__main__":
    main()
