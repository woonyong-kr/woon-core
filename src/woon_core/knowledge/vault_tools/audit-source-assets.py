#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


VAULT = Path.cwd().resolve()
CATALOG = VAULT / "catalog" / "assets" / "vault.json"
CANONICAL_ROOT = VAULT / "assets" / "source" / "vault"
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_error(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_record_shape(
    record: dict[str, Any], category: str, errors: list[str]
) -> None:
    for key in ("sha256", "locator"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            record_error(errors, f"{category}: missing {key}: {record!r}")
    value = record.get("sha256")
    if isinstance(value, str) and not re.fullmatch(r"[0-9a-f]{64}", value):
        record_error(errors, f"{category}: invalid sha256: {value!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        help="read-only source root; omit only when source bytes are unavailable",
    )
    args = parser.parse_args()

    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    errors: list[str] = []
    categories = ("embedded", "mermaid_preferred")
    records = {key: data.get(key, []) for key in categories}

    for category, items in records.items():
        if not isinstance(items, list):
            record_error(errors, f"{category}: expected list")
            continue
        expected = data.get("summary", {}).get(category)
        if expected != len(items):
            record_error(
                errors,
                f"summary {category}: expected {expected}, actual {len(items)}",
            )
        for record in items:
            if not isinstance(record, dict):
                record_error(errors, f"{category}: expected object: {record!r}")
                continue
            validate_record_shape(record, category, errors)

    all_records = [record for items in records.values() for record in items]
    hashes = [record.get("sha256") for record in all_records]
    for digest, count in Counter(hashes).items():
        if isinstance(digest, str) and count > 1:
            record_error(errors, f"duplicate catalog identity: {digest} x{count}")

    embedded_hashes: set[str] = set()
    for record in records["embedded"]:
        digest = record["sha256"]
        embedded_hashes.add(digest)
        for field in ("source_document", "target_document", "caption", "dimensions"):
            if not record.get(field):
                record_error(errors, f"embedded {digest}: missing {field}")

        asset = CANONICAL_ROOT / f"{digest}.png"
        if not asset.is_file():
            record_error(errors, f"embedded {digest}: missing canonical asset")
        elif sha256(asset) != digest:
            record_error(errors, f"embedded {digest}: canonical bytes mismatch")

        target = VAULT / record["target_document"]
        if not target.is_file():
            record_error(errors, f"embedded {digest}: missing target document")
        else:
            text = target.read_text(encoding="utf-8")
            relative_ref = f"../assets/source/vault/{digest}.png"
            if text.count(relative_ref) != 2:
                record_error(
                    errors,
                    f"embedded {digest}: expected one metadata and one body reference",
                )
            if record["caption"] not in text:
                record_error(errors, f"embedded {digest}: caption not mapped")

    actual_canonical = {
        path.stem for path in CANONICAL_ROOT.glob("*.png") if path.is_file()
    }
    for digest in sorted(embedded_hashes - actual_canonical):
        record_error(errors, f"catalog asset missing: {digest}")
    for digest in sorted(actual_canonical - embedded_hashes):
        record_error(errors, f"orphan canonical asset: {digest}")

    canonical_files = sorted(CANONICAL_ROOT.glob("*.png"))
    byte_hashes = [sha256(path) for path in canonical_files]
    for digest, count in Counter(byte_hashes).items():
        if count > 1:
            record_error(errors, f"duplicate canonical bytes: {digest} x{count}")

    if args.source:
        source = args.source.resolve()
        for category, items in records.items():
            for record in items:
                source_asset = source / record["locator"]
                if not source_asset.is_file():
                    record_error(
                        errors, f"{category} {record['sha256']}: source asset missing"
                    )
                elif sha256(source_asset) != record["sha256"]:
                    record_error(
                        errors, f"{category} {record['sha256']}: source bytes drift"
                    )

    wiki_documents = [
        path
        for path in (VAULT / "wiki").rglob("*.md")
        if "_sources" not in path.relative_to(VAULT / "wiki").parts
    ]
    for document in wiki_documents + list((VAULT / "maps").rglob("*.md")):
        text = document.read_text(encoding="utf-8")
        for alt, href in IMAGE_RE.findall(text):
            clean_href = href.split()[0].split("#")[0].split("?")[0]
            if clean_href.startswith(("http://", "https://", "data:")):
                continue
            asset = (document.parent / clean_href).resolve()
            if not asset.is_file():
                record_error(
                    errors,
                    f"broken image: {document.relative_to(VAULT)} -> {clean_href}",
                )
            if not alt.strip():
                record_error(
                    errors,
                    f"empty image alt: {document.relative_to(VAULT)} -> {clean_href}",
                )

    result = {
        "catalog": str(CATALOG.relative_to(VAULT)),
        "source_checked": args.source is not None,
        "counts": {key: len(value) for key, value in records.items()},
        "canonical_files": len(canonical_files),
        "issues": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
