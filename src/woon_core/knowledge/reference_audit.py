"""Deterministic, content-minimizing audit for local PDF reference materials."""

from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from collections import Counter
from pathlib import Path
from typing import TypedDict

import pdfplumber
from pypdf import PdfReader


class AuditDocument(TypedDict):
    relative_path: str
    size_bytes: int
    sha256: str
    page_count: int
    embedded_image_occurrences: int
    unique_embedded_images: int
    pages: list[dict[str, object]]
    extraction_errors: list[str]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--material-directory", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    manifest = audit(arguments.source_root, arguments.material_directory)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "files": manifest["file_count"],
                "pages": manifest["page_count"],
                "embedded_images": manifest["embedded_image_occurrences"],
            },
            ensure_ascii=False,
        )
    )


def audit(source_root: Path, material_directories: list[Path]) -> dict[str, object]:
    """Account for every PDF page and embedded image without copying source text."""

    root = source_root.resolve()
    directories = [path.resolve() for path in material_directories]
    pdfs = sorted(path for directory in directories for path in directory.glob("*.pdf"))
    documents = [_audit_pdf(root, path) for path in pdfs]
    errors = [error for document in documents for error in document["extraction_errors"]]
    if errors:
        raise RuntimeError("reference audit failed: " + "; ".join(str(error) for error in errors))
    return {
        "schema_version": 1,
        "source": "external-local-course-materials",
        "file_count": len(documents),
        "page_count": sum(document["page_count"] for document in documents),
        "embedded_image_occurrences": sum(
            document["embedded_image_occurrences"] for document in documents
        ),
        "documents": documents,
    }


def _audit_pdf(root: Path, path: Path) -> AuditDocument:
    reader = PdfReader(path)
    pages: list[dict[str, object]] = []
    image_hashes: Counter[str] = Counter()
    extraction_errors: list[str] = []
    with pdfplumber.open(path) as document:
        if len(reader.pages) != len(document.pages):
            raise RuntimeError(f"page count disagreement: {_relative(root, path)}")
        for index, (reader_page, page) in enumerate(
            zip(reader.pages, document.pages, strict=True), 1
        ):
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
            page_images: list[str] = []
            try:
                for image in reader_page.images:
                    image_hash = hashlib.sha256(image.data).hexdigest()
                    image_hashes[image_hash] += 1
                    page_images.append(image_hash)
            except Exception as error:  # optional image streams can be malformed
                extraction_errors.append(f"page {index}: {type(error).__name__}: {error}")
            pages.append(
                {
                    "page": index,
                    "text_chars": len(text),
                    "text_lines": len(text.splitlines()),
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "embedded_images": len(page_images),
                    "embedded_image_sha256": page_images,
                    "drawn_lines": len(page.lines),
                    "rectangles": len(page.rects),
                    "curves": len(page.curves),
                }
            )
    return {
        "relative_path": _relative(root, path),
        "size_bytes": path.stat().st_size,
        "sha256": _digest(path),
        "page_count": len(pages),
        "embedded_image_occurrences": sum(image_hashes.values()),
        "unique_embedded_images": len(image_hashes),
        "pages": pages,
        "extraction_errors": extraction_errors,
    }


def _relative(root: Path, path: Path) -> str:
    return unicodedata.normalize("NFC", str(path.relative_to(root)))


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()
