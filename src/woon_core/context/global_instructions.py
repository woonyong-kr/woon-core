"""Install the canonical Woon routing contract into global Codex instructions."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.registry import Registry
from woon_core.workspace import discover

START_MARKER = "<!-- woon:global-instructions:start -->"
END_MARKER = "<!-- woon:global-instructions:end -->"
SOURCE_REFERENCE = "repo://core/config/global-agents.md"
MAX_GLOBAL_BYTES = 32 * 1024
MAX_MANAGED_BYTES = 6 * 1024


@dataclass(frozen=True, slots=True)
class GlobalInstructionResult:
    target: Path
    changed: bool
    bytes: int


def render_managed_block(source: str) -> str:
    body = source.strip()
    if not body:
        raise WoonError("global instruction source is empty")
    if START_MARKER in body or END_MARKER in body:
        raise WoonError("global instruction source must not contain managed markers")
    rendered = (
        f"{START_MARKER}\n"
        f"<!-- Source: {SOURCE_REFERENCE}; install with woon-global-instructions. -->\n"
        f"{body}\n"
        f"{END_MARKER}"
    )
    if len(rendered.encode()) > MAX_MANAGED_BYTES:
        raise WoonError(f"managed global instructions exceed {MAX_MANAGED_BYTES} bytes")
    return rendered


def merge_managed_block(existing: str, block: str) -> str:
    starts = existing.count(START_MARKER)
    ends = existing.count(END_MARKER)
    if starts != ends or starts > 1:
        raise WoonError("global instruction file has malformed Woon managed markers")

    if starts == 1:
        prefix, remainder = existing.split(START_MARKER, 1)
        _, suffix = remainder.split(END_MARKER, 1)
        merged = prefix.rstrip() + "\n\n" + block + suffix
    else:
        merged = existing.rstrip()
        if merged:
            merged += "\n\n"
        merged += block
    merged = merged.rstrip() + "\n"
    if len(merged.encode()) > MAX_GLOBAL_BYTES:
        raise WoonError(f"global instructions exceed Codex limit of {MAX_GLOBAL_BYTES} bytes")
    return merged


def expected_document(root: Path, target: Path) -> bytes:
    registry = Registry.load(root)
    source_path = registry.resolve(root, SOURCE_REFERENCE)
    try:
        source = source_path.read_text(encoding="utf-8")
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
    except OSError as error:
        raise WoonError(f"read global instructions: {error}") from error
    return merge_managed_block(existing, render_managed_block(source)).encode()


def apply(root: Path, target: Path) -> GlobalInstructionResult:
    expected = expected_document(root, target)
    actual = target.read_bytes() if target.exists() else b""
    if actual == expected:
        return GlobalInstructionResult(target=target, changed=False, bytes=len(expected))
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    atomic_write(target, expected, mode=mode)
    return GlobalInstructionResult(target=target, changed=True, bytes=len(expected))


def check(root: Path, target: Path) -> GlobalInstructionResult:
    expected = expected_document(root, target)
    actual = target.read_bytes() if target.exists() else b""
    if actual != expected:
        raise WoonError(f"global instruction drift: {target}")
    return GlobalInstructionResult(target=target, changed=False, bytes=len(expected))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "check", "show"))
    parser.add_argument("--root", default="")
    parser.add_argument("--target", type=Path, default=Path.home() / ".codex/AGENTS.md")
    arguments = parser.parse_args(argv)
    try:
        root = discover(arguments.root).root
        target = arguments.target.expanduser().resolve(strict=False)
        if arguments.command == "show":
            registry = Registry.load(root)
            source = registry.resolve(root, SOURCE_REFERENCE).read_text(encoding="utf-8")
            print(render_managed_block(source))
            return 0
        result = apply(root, target) if arguments.command == "apply" else check(root, target)
        print(
            f"global-instructions target={result.target} "
            f"changed={str(result.changed).lower()} bytes={result.bytes}"
        )
        return 0
    except (OSError, WoonError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
