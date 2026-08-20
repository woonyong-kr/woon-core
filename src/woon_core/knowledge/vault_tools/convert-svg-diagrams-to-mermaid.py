#!/usr/bin/env python3
"""Replace SVG image embeds with Mermaid blocks recovered from Graphviz DOT/SVG.

The vault previously kept many diagrams as rendered SVG assets. This script
turns those embeds back into Markdown-renderable Mermaid code blocks so the
diagram source lives with the learning note instead of as external images.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path.cwd().resolve()
MARKDOWN_ROOTS = [ROOT / "README.md", ROOT / "maps", ROOT / "wiki", ROOT / "projects" / "writing"]
SVG_EMBED_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+\.svg)\)")


@dataclass
class Edge:
    source: str
    target: str
    label: str = ""
    dashed: bool = False
    both: bool = False


def iter_markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in MARKDOWN_ROOTS:
        if root.is_file():
            files.append(root)
        elif root.exists():
            files.extend(sorted(root.rglob("*.md")))
    return files


def git_show(path: Path) -> str | None:
    rel = path.relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout if result.returncode == 0 else None


def resolve_ref(markdown_file: Path, ref: str) -> Path:
    raw = Path(ref)
    if raw.is_absolute():
        return raw
    return (markdown_file.parent / raw).resolve()


def split_dot_statements(dot: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    bracket_depth = 0
    quote = False
    escape = False
    html_depth = 0
    i = 0
    while i < len(dot):
        ch = dot[i]
        nxt = dot[i + 1] if i + 1 < len(dot) else ""

        if quote:
            buf.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            i += 1
            continue

        if ch == '"':
            quote = True
            buf.append(ch)
            i += 1
            continue

        if ch == "<" and nxt == "<":
            html_depth += 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue

        if ch == ">" and nxt == ">" and html_depth > 0:
            html_depth -= 1
            buf.append(ch)
            buf.append(nxt)
            i += 2
            continue

        if html_depth == 0:
            if ch == "[":
                bracket_depth += 1
            elif ch == "]" and bracket_depth:
                bracket_depth -= 1
            elif ch == ";" and bracket_depth == 0:
                statement = "".join(buf).strip()
                if statement:
                    statements.append(statement)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    trailing = "".join(buf).strip()
    if trailing:
        statements.append(trailing)
    return statements


def strip_dot_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//.*", "", text)
    return text


def extract_attrs(statement: str) -> dict[str, str]:
    start = statement.find("[")
    end = statement.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return {}
    body = statement[start + 1 : end]
    attrs: dict[str, str] = {}
    token: list[str] = []
    quote = False
    escape = False
    html_depth = 0
    parts: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if quote:
            token.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                quote = False
            i += 1
            continue
        if ch == '"':
            quote = True
            token.append(ch)
            i += 1
            continue
        if ch == "<" and nxt == "<":
            html_depth += 1
            token.extend([ch, nxt])
            i += 2
            continue
        if ch == ">" and nxt == ">" and html_depth:
            html_depth -= 1
            token.extend([ch, nxt])
            i += 2
            continue
        if ch == "," and html_depth == 0:
            parts.append("".join(token).strip())
            token = []
        else:
            token.append(ch)
        i += 1
    if token:
        parts.append("".join(token).strip())

    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        attrs[key.strip()] = value.strip()
    return attrs


def dot_unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
        value = value.replace(r"\"", '"').replace(r"\n", "\n")
    return value


def strip_html_label(value: str) -> str:
    value = value.strip()
    if value.startswith("<<") and value.endswith(">>"):
        value = value[2:-2]
    elif value.startswith("<") and value.endswith(">"):
        value = value[1:-1]

    value = re.sub(r"<\s*BR\s*/?\s*>", "\n", value, flags=re.I)
    value = re.sub(r"</\s*TR\s*>", "\n", value, flags=re.I)
    value = re.sub(r"</\s*TD\s*>\s*<\s*TD[^>]*>", " | ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    lines = [" ".join(line.split()) for line in value.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return "\n".join(deduped[:10])


def clean_label(value: str, fallback: str = "") -> str:
    if not value:
        return fallback
    value = dot_unquote(value.strip())
    if value.startswith("<"):
        value = strip_html_label(value)
    else:
        value = html.unescape(value)
    value = value.replace("\\l", "\n").replace("\\n", "\n")
    lines = [" ".join(line.split()) for line in value.splitlines()]
    lines = [line for line in lines if line]
    value = "\n".join(lines)
    return value or fallback


def statement_head(statement: str) -> str:
    head = statement.split("[", 1)[0].strip()
    head = re.sub(r"^(strict\s+)?(di)?graph\s+\w*\s*\{?", "", head).strip()
    return head


def parse_dot(dot: str) -> tuple[str, dict[str, str], list[Edge]]:
    dot = strip_dot_comments(dot)
    direction = "LR" if re.search(r"rankdir\s*=\s*LR", dot) else "TD"
    nodes: dict[str, str] = {}
    edges: list[Edge] = []

    for statement in split_dot_statements(dot):
        if "->" in statement:
            attrs = extract_attrs(statement)
            head = statement_head(statement)
            head = re.sub(r"\[.*$", "", head, flags=re.S).strip()
            parts = [part.strip() for part in head.split("->")]
            ids = [normalize_dot_id(part) for part in parts if normalize_dot_id(part)]
            label = clean_label(attrs.get("label", ""))
            dashed = attrs.get("style", "").strip('"') == "dashed"
            both = attrs.get("dir", "").strip('"') == "both"
            for source, target in zip(ids, ids[1:]):
                edges.append(Edge(source, target, label, dashed, both))
                nodes.setdefault(source, source)
                nodes.setdefault(target, target)
            continue

        if "->" not in statement and "[" in statement and "]" in statement:
            head = statement_head(statement)
            if not head or head in {"node", "edge", "graph"} or head.startswith("{"):
                continue
            node_id = normalize_dot_id(head)
            if not node_id:
                continue
            attrs = extract_attrs(statement)
            label = clean_label(attrs.get("label", ""), node_id)
            nodes[node_id] = label

    return direction, nodes, edges


def normalize_dot_id(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\{.*?\}", "", value).strip()
    value = re.sub(r"\[.*$", "", value, flags=re.S).strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    value = value.strip(";{} ")
    return value


def parse_graphviz_svg(svg: str) -> tuple[str, dict[str, str], list[Edge]]:
    nodes: dict[str, str] = {}
    edges: list[Edge] = []

    for block in re.findall(r'<g id="node\d+".*?</g>', svg, flags=re.S):
        title = first_match(r"<title>(.*?)</title>", block)
        if not title:
            continue
        texts = re.findall(r"<text[^>]*>(.*?)</text>", block, flags=re.S)
        label = "\n".join(clean_svg_text(t) for t in texts)
        label = "\n".join(line for line in label.splitlines() if line.strip())
        nodes[html.unescape(title)] = label or html.unescape(title)

    for block in re.findall(r'<g id="edge\d+".*?</g>', svg, flags=re.S):
        title = first_match(r"<title>(.*?)</title>", block)
        if not title:
            continue
        title = html.unescape(title)
        if "->" not in title:
            continue
        source, target = title.split("->", 1)
        texts = re.findall(r"<text[^>]*>(.*?)</text>", block, flags=re.S)
        label = "\n".join(clean_svg_text(t) for t in texts)
        label = " / ".join(line for line in label.splitlines() if line.strip())
        edges.append(Edge(source, target, label))
        nodes.setdefault(source, source)
        nodes.setdefault(target, target)

    return "LR", nodes, edges


def first_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, flags=re.S)
    return match.group(1) if match else None


def clean_svg_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(" ".join(value.split()))


def mermaid_id(dot_id: str, used: set[str]) -> str:
    base = re.sub(r"\W+", "_", dot_id).strip("_")
    if not base:
        base = "node"
    if base[0].isdigit():
        base = f"n_{base}"
    if base.lower() in {"graph", "flowchart", "subgraph", "end", "class", "style", "linkstyle"}:
        base = f"n_{base}"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def mermaid_label(label: str) -> str:
    lines = [line.strip() for line in label.splitlines() if line.strip()]
    compact: list[str] = []
    for line in lines[:10]:
        if len(line) > 90:
            line = line[:87] + "..."
        compact.append(line)
    value = "<br/>".join(compact) if compact else "diagram"
    value = html.escape(value, quote=False)
    value = value.replace("&lt;br/&gt;", "<br/>")
    value = value.replace('"', "&quot;")
    value = value.replace("|", "&#124;")
    return value


def mermaid_edge_label(label: str) -> str:
    value = " ".join(label.split())
    if len(value) > 70:
        value = value[:67] + "..."
    value = html.escape(value, quote=False).replace('"', "&quot;").replace("|", "&#124;")
    return value


def build_mermaid(title: str, source_path: Path, direction: str, nodes: dict[str, str], edges: list[Edge]) -> str:
    used: set[str] = set()
    id_map = {node_id: mermaid_id(node_id, used) for node_id in nodes}
    lines = ["```mermaid", f"%% {title}", f"%% recovered from {source_path.relative_to(ROOT).as_posix()}", f"flowchart {direction}"]

    for node_id, label in nodes.items():
        lines.append(f'  {id_map[node_id]}["{mermaid_label(label)}"]')

    for edge in edges:
        if edge.source not in id_map or edge.target not in id_map:
            continue
        source = id_map[edge.source]
        target = id_map[edge.target]
        label = mermaid_edge_label(edge.label)
        if edge.both:
            op = "<-->"
            lines.append(f"  {source} {op}{f'|{label}|' if label else ''} {target}")
        elif edge.dashed:
            lines.append(f"  {source} -. {label} .-> {target}" if label else f"  {source} -.-> {target}")
        else:
            lines.append(f"  {source} -->|{label}| {target}" if label else f"  {source} --> {target}")

    lines.append("```")
    return "\n".join(lines)


def source_for_svg(markdown_file: Path, ref: str) -> tuple[Path, str | None, bool]:
    svg_path = resolve_ref(markdown_file, ref)
    dot_path = svg_path.with_suffix(".dot")
    if dot_path.exists():
        return dot_path, dot_path.read_text(encoding="utf-8"), True

    dot_text = git_show(dot_path)
    if dot_text is not None:
        return dot_path, dot_text, True

    if svg_path.exists():
        return svg_path, svg_path.read_text(encoding="utf-8"), False

    return svg_path, None, False


def replacement_for(markdown_file: Path, alt: str, ref: str) -> str:
    source_path, source_text, is_dot = source_for_svg(markdown_file, ref)
    title = alt.strip() or source_path.stem
    if source_text is None:
        return f"<!-- diagram-missing: {ref} -->"

    if is_dot:
        direction, nodes, edges = parse_dot(source_text)
    else:
        direction, nodes, edges = parse_graphviz_svg(source_text)

    if not nodes:
        nodes = {source_path.stem: title}
        edges = []
        direction = "TD"

    return build_mermaid(title, source_path, direction, nodes, edges)


def convert_file(path: Path, apply: bool) -> int:
    text = path.read_text(encoding="utf-8")
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        alt, ref = match.groups()
        return replacement_for(path, alt, ref)

    new_text = SVG_EMBED_RE.sub(repl, text)
    if apply and new_text != text:
        path.write_text(new_text, encoding="utf-8")
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes")
    args = parser.parse_args()

    total = 0
    changed_files = 0
    for path in iter_markdown_files():
        count = convert_file(path, args.apply)
        if count:
            total += count
            changed_files += 1

    mode = "applied" if args.apply else "dry_run"
    print(f"{mode}=true files={changed_files} svg_embeds={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
