"""Local-only Docling conversion into non-canonical curation candidates."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import platform
import re
import shutil
import tempfile
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

SUPPORTED_SUFFIXES = frozenset(
    {
        ".adoc",
        ".asciidoc",
        ".bmp",
        ".csv",
        ".docx",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".md",
        ".pdf",
        ".png",
        ".pptx",
        ".tif",
        ".tiff",
        ".webp",
        ".xlsx",
    }
)
MODEL_REQUIRED_SUFFIXES = frozenset(
    {".bmp", ".jpeg", ".jpg", ".pdf", ".png", ".tif", ".tiff", ".webp"}
)
IMAGE_SUFFIXES = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
_PACKAGE_NAMES = (
    "docling",
    "docling-core",
    "docling-ibm-models",
    "docling-parse",
    "numpy",
    "pillow",
    "torch",
    "transformers",
)
_OCR_PACKAGE_NAMES = {
    "ocrmac": ("ocrmac",),
    "rapidocr": ("onnxruntime", "rapidocr"),
}
_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
}
DOCUMENT_INTAKE_SCHEMA_VERSION = 5
DOCUMENT_PROJECTION_VERSION = 3
_ADAPTER_NAME = "woon-docling-document-intake"
_OFFLINE_CONVERSION_LOCK = threading.RLock()
_IMAGE_STRUCTURAL_LABELS = frozenset({"section_header", "list_item", "paragraph", "title"})
_IMAGE_IGNORED_LABELS = frozenset({"page_header", "page_footer"})
_SAFE_SHORT_TECHNICAL_TOKENS = frozenset(
    {"ai", "api", "cli", "cpu", "css", "faq", "gpu", "html", "id", "pc", "sql", "ui", "url"}
)


@dataclass(frozen=True, slots=True)
class DocumentCandidateResult:
    """Verified local candidate outcome; never a canonical Wiki acceptance."""

    candidate_id: str
    status: str
    replayed: bool
    candidate: str | None
    receipt: str
    observation: str
    source_sha256: str


def ingest_document_candidate(
    source: Path,
    vault: Path,
    *,
    source_locator: str | None = None,
    ocr: str = "off",
    model_cache: Path | None = None,
) -> DocumentCandidateResult:
    """Convert one file into lossless JSON, Markdown, and structured JSONL chunks.

    All artifacts live below the vault's ignored ``.local`` runtime boundary. The
    function never edits ``wiki/``, compiler inputs, source catalogs, or compiler
    receipts. Identical bytes and options resolve to the same candidate ID.
    """

    root = vault.expanduser().resolve()
    requested_path = source.expanduser()
    input_path = requested_path.resolve()
    if not root.is_dir():
        raise WoonError(f"knowledge vault does not exist: {root}")
    if not input_path.is_file():
        raise WoonError(f"document intake source does not exist: {input_path}")

    suffix = input_path.suffix.lower()
    source_hash = _sha256(input_path)
    runtime = root / ".local/woon-knowledge/document-intake"
    requested_options = {
        "model_cache_requested": model_cache is not None,
        "ocr": ocr,
        "suffix": suffix,
    }
    attempt_identity = {
        "adapter": _adapter_contract(),
        "locator_sha256": hashlib.sha256(
            (source_locator or input_path.name).encode("utf-8")
        ).hexdigest(),
        "request": requested_options,
        "source_sha256": source_hash,
    }
    attempt_id = f"docling-attempt-{_json_sha256(attempt_identity)}"
    locator: str | None = None
    package_versions: dict[str, str] = {}
    resolved_cache: Path | None = None
    options: dict[str, Any] = requested_options
    candidate_id: str | None = None
    try:
        if requested_path.is_symlink():
            raise WoonError("document intake rejects symlink sources")
        if suffix not in SUPPORTED_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
            raise WoonError(
                f"unsupported document intake format {suffix!r}; supported: {supported}"
            )
        if ocr not in {"off", "rapidocr", "ocrmac"}:
            raise WoonError("document intake OCR must be off, rapidocr, or ocrmac")
        if ocr == "ocrmac" and platform.system() != "Darwin":
            raise WoonError("document intake ocrmac requires macOS")

        locator = _safe_locator(source_locator or input_path.name)
        package_versions = _docling_versions(ocr)
        resolved_cache = _resolve_model_cache(root, suffix, model_cache)
        cache_manifest = _model_cache_manifest(resolved_cache) if resolved_cache else None
        options = {
            "allow_external_plugins": False,
            "chunker": "hierarchical-v1",
            "enable_remote_services": False,
            "input_format": suffix,
            "image_ocr_full_page": suffix in IMAGE_SUFFIXES and ocr != "off",
            "model_cache": cache_manifest,
            "network": "offline",
            "ocr": ocr,
            "ocr_languages": _ocr_languages(ocr),
            "table_structure": "accurate",
        }
        identity = {
            "adapter": _adapter_contract(),
            "converter": {"name": "docling", "packages": package_versions},
            "options": options,
            "runtime": _runtime_contract(),
            "source_sha256": source_hash,
        }
        candidate_id = f"docling-{_json_sha256(identity)}"
        candidate_directory = runtime / "candidates" / candidate_id
        lock = runtime / "locks" / f"{candidate_id}.lock"

        with exclusive_file_lock(lock):
            existing_receipt = candidate_directory / "receipt.json"
            replayed = existing_receipt.is_file()
            if replayed:
                source_size = _verify_candidate(
                    candidate_directory, candidate_id, source_hash, options
                )
            else:
                if candidate_directory.exists():
                    raise WoonError(f"incomplete document candidate exists: {candidate_id}")
                with _source_snapshot(runtime, input_path, source_hash) as snapshot:
                    converted = _convert(
                        snapshot,
                        suffix=suffix,
                        ocr=ocr,
                        model_cache=resolved_cache,
                    )
                    _write_candidate(
                        candidate_directory,
                        candidate_id=candidate_id,
                        source_hash=source_hash,
                        source_size=snapshot.stat().st_size,
                        source_suffix=suffix,
                        package_versions=package_versions,
                        options=options,
                        converted=converted,
                    )
                source_size = _verify_candidate(
                    candidate_directory, candidate_id, source_hash, options
                )

            observation = _write_observation(
                runtime,
                candidate_directory,
                candidate_id=candidate_id,
                locator=locator,
                source_hash=source_hash,
                source_size=source_size,
                source_suffix=suffix,
            )
            return DocumentCandidateResult(
                candidate_id=candidate_id,
                status="candidate",
                replayed=replayed,
                candidate=_relative(root, candidate_directory),
                receipt=_relative(root, candidate_directory / "receipt.json"),
                observation=_relative(root, observation),
                source_sha256=source_hash,
            )
    except Exception as error:
        failure_receipt = _write_failure_receipt(
            runtime,
            attempt_id=attempt_id,
            candidate_id=candidate_id,
            locator=locator,
            source_hash=source_hash,
            source_suffix=suffix,
            package_versions=package_versions,
            options=options,
            error=error,
            source=input_path,
            cache=resolved_cache,
            vault=root,
        )
        raise WoonError(
            "Docling intake failed: "
            f"{_safe_error_message(error, input_path, resolved_cache, root)}; "
            f"error receipt: {_relative(root, failure_receipt)}"
        ) from error


def _convert(source: Path, *, suffix: str, ocr: str, model_cache: Path | None) -> dict[str, Any]:
    with _offline_environment():
        modules = _docling_modules()
        input_format = _input_format(modules["InputFormat"], suffix)
        format_options: dict[Any, Any] = {}
        if suffix in MODEL_REQUIRED_SUFFIXES:
            pipeline_options = modules["PdfPipelineOptions"](
                artifacts_path=model_cache,
                allow_external_plugins=False,
                enable_remote_services=False,
                do_ocr=ocr != "off",
                do_table_structure=True,
            )
            pipeline_options.table_structure_options.mode = modules["TableFormerMode"].ACCURATE
            if ocr == "rapidocr":
                pipeline_options.ocr_options = modules["RapidOcrOptions"](
                    force_full_page_ocr=suffix in IMAGE_SUFFIXES,
                    lang=["chinese"],
                )
            elif ocr == "ocrmac":
                pipeline_options.ocr_options = modules["OcrMacOptions"](
                    force_full_page_ocr=suffix in IMAGE_SUFFIXES,
                    lang=["ko-KR", "en-US"],
                    recognition="accurate",
                )
            format_option = (
                modules["ImageFormatOption"]
                if suffix in IMAGE_SUFFIXES
                else modules["PdfFormatOption"]
            )
            format_options[input_format] = format_option(pipeline_options=pipeline_options)

        converter = modules["DocumentConverter"](
            allowed_formats=[input_format],
            format_options=format_options or None,
        )
        result = converter.convert(source)
        conversion_status = str(getattr(result.status, "value", result.status))
        if conversion_status not in {"success", "partial_success"}:
            raise WoonError(f"Docling returned conversion status {conversion_status}")
        document = result.document
        chunker = modules["HierarchicalChunker"]()
        chunks = []
        for index, chunk in enumerate(chunker.chunk(document)):
            chunks.append(
                {
                    "index": index,
                    "contextualized_text": chunker.contextualize(chunk=chunk),
                    "chunk": chunk.model_dump(mode="json"),
                }
            )
        raw_markdown = document.export_to_markdown().rstrip()
        markdown = raw_markdown
        if suffix in IMAGE_SUFFIXES and ocr != "off":
            markdown, chunks = _add_image_ocr_projection(document, markdown, chunks)
        cleaned_markdown, quality = _clean_document_markdown(
            document,
            markdown,
            suffix=suffix,
        )
        errors = [
            _redact_error_value(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else str(item),
                source,
                model_cache,
            )
            for item in result.errors
        ]
        return {
            "status": conversion_status,
            "document": document.export_to_dict(),
            "raw_markdown": markdown.rstrip() + "\n",
            "markdown": cleaned_markdown,
            "quality": quality,
            "chunks": chunks,
            "errors": errors,
        }


def _clean_document_markdown(
    document: Any,
    markdown: str,
    *,
    suffix: str,
) -> tuple[str, dict[str, Any]]:
    """Build a deterministic review projection without rewriting source facts.

    The lossless Docling JSON and raw Markdown remain untouched.  For standalone
    screenshots we use Docling labels and geometry to suppress page chrome,
    collapse exact duplicates, and remove obvious OCR glyph noise.  Ambiguous
    prose is never guessed or silently repaired; the semantic curation stage
    must either integrate or discard the candidate.
    """

    transformations: Counter[str] = Counter()
    if suffix in IMAGE_SUFFIXES:
        cleaned = _clean_image_markdown(document, transformations)
    else:
        cleaned = _normalize_markdown(markdown, transformations)
    normalized = cleaned.strip()
    suspicious = _suspicious_fragments(normalized)
    visible_characters = sum(not character.isspace() for character in normalized)
    korean_characters = sum("가" <= character <= "힣" for character in normalized)
    state = "ready-for-semantic-curation" if visible_characters >= 40 else "discard-low-quality"
    quality = {
        "version": 1,
        "state": state,
        "metrics": {
            "cleaned_characters": len(normalized),
            "korean_characters": korean_characters,
            "raw_characters": len(markdown.strip()),
            "suspicious_fragments": suspicious,
        },
        "transformations": dict(sorted(transformations.items())),
        "canonical_writes": False,
    }
    return normalized + "\n", quality


def _clean_image_markdown(document: Any, transformations: Counter[str]) -> str:
    items = list(getattr(document, "texts", ()))
    content_bounds = _image_content_bounds(items)
    lines: list[str] = []
    seen: set[str] = set()
    heading_level = 1
    for item in items:
        label = _label_value(getattr(item, "label", "text"))
        if label in _IMAGE_IGNORED_LABELS:
            transformations["page-chrome-removed"] += 1
            continue
        if content_bounds is not None and not _inside_content_bounds(item, content_bounds):
            transformations["off-content-region-removed"] += 1
            continue
        text = " ".join(str(getattr(item, "text", "")).split())
        text = _strip_obvious_ocr_glyphs(text, transformations)
        if not text or _is_breadcrumb(text) or _is_symbol_only(text):
            transformations["symbol-or-breadcrumb-removed"] += 1
            continue
        identity = text.casefold()
        if identity in seen:
            transformations["exact-duplicate-removed"] += 1
            continue
        seen.add(identity)
        if label in {"title", "section_header"}:
            prefix = "#" if heading_level == 1 else "##"
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([f"{prefix} {text}", ""])
            heading_level = 2
        elif label == "list_item" and len(text) <= 36 and text.endswith("유의사항"):
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([f"## {text}", ""])
            transformations["list-heading-promoted"] += 1
        elif label == "list_item":
            lines.append(f"- {text}")
        else:
            lines.extend([text, ""])
    while lines and lines[-1] == "":
        lines.pop()
    if lines and lines[-1].startswith("#"):
        lines.pop()
        while lines and lines[-1] == "":
            lines.pop()
        transformations["orphan-heading-removed"] += 1
    return "\n".join(lines)


def _normalize_markdown(markdown: str, transformations: Counter[str]) -> str:
    lines: list[str] = []
    previous_blank = False
    for raw_line in markdown.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.strip() == "<!-- image -->":
            lines.append(line)
            previous_blank = False
            continue
        if not line:
            if previous_blank:
                transformations["extra-blank-line-removed"] += 1
                continue
            previous_blank = True
        else:
            previous_blank = False
        lines.append(line)
    return "\n".join(lines)


def _image_content_bounds(items: list[Any]) -> tuple[float, float, float, float] | None:
    bounds = [
        _item_bounds(item)
        for item in items
        if _label_value(getattr(item, "label", "text")) in _IMAGE_STRUCTURAL_LABELS
    ]
    valid = [value for value in bounds if value is not None]
    if len(valid) < 3:
        return None
    left = min(value[0] for value in valid)
    right = max(value[1] for value in valid)
    bottom = min(value[2] for value in valid)
    top = max(value[3] for value in valid)
    horizontal_margin = max((right - left) * 0.08, 12.0)
    vertical_margin = max((top - bottom) * 0.08, 12.0)
    return (
        left - horizontal_margin,
        right + horizontal_margin,
        bottom - vertical_margin,
        top + vertical_margin,
    )


def _item_bounds(item: Any) -> tuple[float, float, float, float] | None:
    provenance = getattr(item, "prov", ())
    if not provenance:
        return None
    box = getattr(provenance[0], "bbox", None)
    left = getattr(box, "l", None)
    right = getattr(box, "r", None)
    bottom = getattr(box, "b", None)
    top = getattr(box, "t", None)
    if not (
        isinstance(left, (int, float))
        and isinstance(right, (int, float))
        and isinstance(bottom, (int, float))
        and isinstance(top, (int, float))
    ):
        return None
    return float(left), float(right), float(bottom), float(top)


def _inside_content_bounds(
    item: Any,
    bounds: tuple[float, float, float, float],
) -> bool:
    item_bounds = _item_bounds(item)
    if item_bounds is None:
        return True
    center_x = (item_bounds[0] + item_bounds[1]) / 2
    center_y = (item_bounds[2] + item_bounds[3]) / 2
    return bounds[0] <= center_x <= bounds[1] and bounds[2] <= center_y <= bounds[3]


def _label_value(value: object) -> str:
    return str(getattr(value, "value", value)).lower()


def _strip_obvious_ocr_glyphs(text: str, transformations: Counter[str]) -> str:
    value = text.strip()
    stripped = value.lstrip("~@®©•·*#①②③④⑤⑥⑦⑧⑨⑩ ")
    if stripped != value:
        transformations["leading-glyph-removed"] += 1
    value = stripped
    normalized_symbols = re.sub(r"\s+[@®©]\s+", " · ", value)
    if normalized_symbols != value:
        transformations["embedded-symbol-normalized"] += 1
        value = normalized_symbols
    match = re.search(r"\s+([a-z]{1,4})$", value)
    if match and any("가" <= character <= "힣" for character in value):
        token = match.group(1)
        if token not in _SAFE_SHORT_TECHNICAL_TOKENS:
            value = value[: match.start()].rstrip()
            transformations["suspicious-trailing-token-removed"] += 1
    return value


def _is_breadcrumb(text: str) -> bool:
    return len(text) <= 80 and text.startswith(">") and text.endswith(">")


def _is_symbol_only(text: str) -> bool:
    return not any(character.isalnum() or "가" <= character <= "힣" for character in text)


def _suspicious_fragments(markdown: str) -> list[str]:
    fragments: set[str] = set()
    for line in markdown.splitlines():
        visible = line.lstrip("#- ").strip()
        if "�" in visible:
            fragments.add("replacement-character")
        if re.search(r"[가-힣]\s*[@®©]", visible):
            fragments.add("embedded-symbol")
        if re.search(r"\b[a-z]{1,2}\d\b", visible):
            fragments.add("short-alpha-numeric-token")
    return sorted(fragments)


def _add_image_ocr_projection(
    document: Any, markdown: str, chunks: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """Expose standalone-image OCR omitted by Markdown and hierarchical chunks."""

    texts = _stable_document_texts(document)
    missing_markdown = _missing_texts(texts, markdown)
    if missing_markdown:
        projected = "\n".join(f"- {text}" for text in missing_markdown)
        markdown = f"{markdown}\n\n## OCR text\n\n{projected}".strip()

    chunk_text = "\n".join(str(chunk.get("contextualized_text", "")) for chunk in chunks)
    missing_chunks = _missing_texts(texts, chunk_text)
    if missing_chunks:
        fallback_text = "\n".join(missing_chunks)
        chunks.append(
            {
                "index": len(chunks),
                "contextualized_text": f"OCR text\n{fallback_text}",
                "chunk": {
                    "text": fallback_text,
                    "meta": {
                        "origin": "standalone-image-ocr-projection",
                        "schema_version": 1,
                    },
                },
            }
        )
    return markdown, chunks


def _stable_document_texts(document: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in getattr(document, "texts", []):
        text = " ".join(str(getattr(item, "text", "")).split())
        if text and text not in seen:
            seen.add(text)
            values.append(text)
    return values


def _missing_texts(texts: list[str], projection: str) -> list[str]:
    normalized_projection = " ".join(projection.split())
    return [text for text in texts if text not in normalized_projection]


def _write_candidate(
    destination: Path,
    *,
    candidate_id: str,
    source_hash: str,
    source_size: int,
    source_suffix: str,
    package_versions: dict[str, str],
    options: dict[str, Any],
    converted: dict[str, Any],
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=f".{candidate_id}-", dir=destination.parent) as name:
        staging = Path(name)
        staging.chmod(0o700)
        document_bytes = encode_json(converted["document"])
        raw_markdown_bytes = cast(str, converted["raw_markdown"]).encode("utf-8")
        markdown_bytes = cast(str, converted["markdown"]).encode("utf-8")
        quality_bytes = encode_json(converted["quality"])
        chunks_bytes = b"".join(_encode_jsonl(chunk) for chunk in converted["chunks"])
        promotion_bytes = encode_json(
            {
                "version": 1,
                "candidate_id": candidate_id,
                "state": "curation-required",
                "body": {
                    "path": "document.md",
                    "sha256": hashlib.sha256(markdown_bytes).hexdigest(),
                    "normalized_sha256": _normalized_text_sha256(markdown_bytes.decode("utf-8")),
                },
                "required_gates": [
                    "semantic-value",
                    "knowledge-search",
                    "duplicate-resolution",
                    "terminal-resolution-receipt",
                ],
                "curation_contract": {
                    "input_sha256": _normalized_text_sha256(markdown_bytes.decode("utf-8")),
                    "raw_input_sha256": _normalized_text_sha256(raw_markdown_bytes.decode("utf-8")),
                    "promotion_interface": "woon knowledge document-resolve",
                },
                "canonical_writes": False,
            }
        )
        outputs = {
            "chunks.jsonl": _output_record(chunks_bytes),
            "document.json": _output_record(document_bytes),
            "document.raw.md": _output_record(raw_markdown_bytes),
            "document.md": _output_record(markdown_bytes),
            "promotion.json": _output_record(promotion_bytes),
            "quality.json": _output_record(quality_bytes),
        }
        receipt = {
            "version": DOCUMENT_INTAKE_SCHEMA_VERSION,
            "adapter": _adapter_contract(),
            "candidate_id": candidate_id,
            "status": "candidate",
            "promotion_state": "curation-required",
            "source": {
                "sha256": source_hash,
                "size": source_size,
                "suffix": source_suffix,
            },
            "converter": {"name": "docling", "packages": package_versions},
            "runtime": _runtime_contract(),
            "conversion_status": converted["status"],
            "conversion_errors": converted["errors"],
            "options": options,
            "outputs": outputs,
            "canonical_writes": False,
            "next_gate": "semantic curation must end in one terminal resolution receipt",
        }
        atomic_write(staging / "document.json", document_bytes, mode=0o600)
        atomic_write(staging / "document.raw.md", raw_markdown_bytes, mode=0o600)
        atomic_write(staging / "document.md", markdown_bytes, mode=0o600)
        atomic_write(staging / "chunks.jsonl", chunks_bytes, mode=0o600)
        atomic_write(staging / "promotion.json", promotion_bytes, mode=0o600)
        atomic_write(staging / "quality.json", quality_bytes, mode=0o600)
        atomic_write(staging / "receipt.json", encode_json(receipt), mode=0o600)
        staging.replace(destination)


def _verify_candidate(
    directory: Path,
    candidate_id: str,
    source_hash: str,
    options: dict[str, Any],
) -> int:
    try:
        receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"invalid document candidate receipt: {candidate_id}") from error
    if (
        receipt.get("candidate_id") != candidate_id
        or receipt.get("version") != DOCUMENT_INTAKE_SCHEMA_VERSION
        or receipt.get("adapter") != _adapter_contract()
        or receipt.get("status") != "candidate"
        or receipt.get("promotion_state") != "curation-required"
        or receipt.get("runtime") != _runtime_contract()
        or receipt.get("source", {}).get("sha256") != source_hash
        or receipt.get("options") != options
        or receipt.get("canonical_writes") is not False
    ):
        raise WoonError(f"document candidate receipt mismatch: {candidate_id}")
    outputs = receipt.get("outputs")
    source_size = receipt.get("source", {}).get("size")
    if not isinstance(source_size, int) or source_size < 0:
        raise WoonError(f"document candidate source size is invalid: {candidate_id}")
    if not isinstance(outputs, dict):
        raise WoonError(f"document candidate outputs are missing: {candidate_id}")
    for name in (
        "chunks.jsonl",
        "document.json",
        "document.raw.md",
        "document.md",
        "promotion.json",
        "quality.json",
    ):
        record = outputs.get(name)
        path = directory / name
        if (
            not isinstance(record, dict)
            or not path.is_file()
            or record.get("sha256") != _sha256(path)
            or record.get("bytes") != path.stat().st_size
        ):
            raise WoonError(f"document candidate output mismatch: {candidate_id}/{name}")
    return source_size


def _docling_modules() -> dict[str, Any]:
    try:
        base_models = importlib.import_module("docling.datamodel.base_models")
        pipeline_options = importlib.import_module("docling.datamodel.pipeline_options")
        converter = importlib.import_module("docling.document_converter")
        chunking = importlib.import_module("docling.chunking")
    except ImportError as error:
        raise WoonError(
            "Docling is not installed; run `uv sync --extra documents` in woon-core"
        ) from error
    return {
        "DocumentConverter": converter.DocumentConverter,
        "HierarchicalChunker": chunking.HierarchicalChunker,
        "ImageFormatOption": converter.ImageFormatOption,
        "InputFormat": base_models.InputFormat,
        "PdfFormatOption": converter.PdfFormatOption,
        "PdfPipelineOptions": pipeline_options.PdfPipelineOptions,
        "OcrMacOptions": pipeline_options.OcrMacOptions,
        "RapidOcrOptions": pipeline_options.RapidOcrOptions,
        "TableFormerMode": pipeline_options.TableFormerMode,
    }


def _docling_versions(ocr: str) -> dict[str, str]:
    installed: dict[str, str] = {}
    try:
        package_names = _PACKAGE_NAMES + _OCR_PACKAGE_NAMES.get(ocr, ())
        for package in package_names:
            installed[package] = version(package)
    except PackageNotFoundError as error:
        raise WoonError(
            "Docling is not installed; run `uv sync --extra documents` in woon-core"
        ) from error
    return installed


def _ocr_languages(ocr: str) -> list[str]:
    if ocr == "rapidocr":
        return ["chinese"]
    if ocr == "ocrmac":
        return ["ko-KR", "en-US"]
    return []


def _input_format(enum: Any, suffix: str) -> Any:
    names = {
        ".adoc": "ASCIIDOC",
        ".asciidoc": "ASCIIDOC",
        ".bmp": "IMAGE",
        ".csv": "CSV",
        ".docx": "DOCX",
        ".htm": "HTML",
        ".html": "HTML",
        ".jpeg": "IMAGE",
        ".jpg": "IMAGE",
        ".md": "MD",
        ".pdf": "PDF",
        ".png": "IMAGE",
        ".pptx": "PPTX",
        ".tif": "IMAGE",
        ".tiff": "IMAGE",
        ".webp": "IMAGE",
        ".xlsx": "XLSX",
    }
    return getattr(enum, names[suffix])


def _resolve_model_cache(vault: Path, suffix: str, value: Path | None) -> Path | None:
    if suffix not in MODEL_REQUIRED_SUFFIXES:
        return None
    requested = (
        value.expanduser()
        if value is not None
        else vault / ".local/woon-knowledge/document-intake/models"
    )
    if requested.is_symlink():
        raise WoonError("Docling model cache rejects a symlink root")
    cache = requested.resolve()
    if not cache.is_dir() or not any(path.is_file() for path in cache.rglob("*")):
        raise WoonError(
            "PDF/image conversion requires a pre-fetched local model cache; run "
            "`docling-tools models download layout tableformer rapidocr --output-dir <path>` "
            "and pass --model-cache <path>"
        )
    return cache


def _model_cache_manifest(cache: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(cache.rglob("*")):
        if path.is_symlink():
            raise WoonError("Docling model cache rejects symlinks")
        if not path.is_file():
            continue
        relative = path.relative_to(cache).as_posix()
        file_hash = _sha256(path)
        digest.update(f"{relative}\0{path.stat().st_size}\0{file_hash}\n".encode())
        count += 1
    return {
        "files": count,
        "manifest_sha256": digest.hexdigest(),
        "source": "pre-fetched-local-artifacts",
    }


@contextmanager
def _offline_environment() -> Iterator[None]:
    with _OFFLINE_CONVERSION_LOCK:
        previous = {name: os.environ.get(name) for name in _OFFLINE_ENVIRONMENT}
        os.environ.update(_OFFLINE_ENVIRONMENT)
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@contextmanager
def _source_snapshot(runtime: Path, source: Path, expected_hash: str) -> Iterator[Path]:
    snapshots = runtime / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshots.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="source-", dir=snapshots) as name:
        directory = Path(name)
        directory.chmod(0o700)
        snapshot = directory / source.name
        shutil.copyfile(source, snapshot)
        snapshot.chmod(0o600)
        if _sha256(snapshot) != expected_hash:
            raise WoonError("document intake source changed while creating its snapshot")
        yield snapshot


def _write_observation(
    runtime: Path,
    candidate_directory: Path,
    *,
    candidate_id: str,
    locator: str,
    source_hash: str,
    source_size: int,
    source_suffix: str,
) -> Path:
    observation_id = (
        f"observation-{_json_sha256({'candidate_id': candidate_id, 'locator': locator})}"
    )
    path = runtime / "observations" / candidate_id / f"{observation_id}.json"
    payload = {
        "version": DOCUMENT_INTAKE_SCHEMA_VERSION,
        "adapter": _adapter_contract(),
        "observation_id": observation_id,
        "candidate_id": candidate_id,
        "status": "observed",
        "source": {
            "locator": locator,
            "sha256": source_hash,
            "size": source_size,
            "suffix": source_suffix,
        },
        "candidate_receipt_sha256": _sha256(candidate_directory / "receipt.json"),
        "canonical_writes": False,
    }
    lock = runtime / "locks" / f"{observation_id}.lock"
    with exclusive_file_lock(lock):
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WoonError(
                    f"invalid document observation receipt: {observation_id}"
                ) from error
            if existing != payload:
                raise WoonError(f"document observation receipt mismatch: {observation_id}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.parent.chmod(0o700)
            atomic_write(path, encode_json(payload), mode=0o600)
    return path


def _write_failure_receipt(
    runtime: Path,
    *,
    attempt_id: str,
    candidate_id: str | None,
    locator: str | None,
    source_hash: str,
    source_suffix: str,
    package_versions: dict[str, str],
    options: dict[str, Any],
    error: Exception,
    source: Path,
    cache: Path | None,
    vault: Path,
) -> Path:
    payload = {
        "version": DOCUMENT_INTAKE_SCHEMA_VERSION,
        "adapter": _adapter_contract(),
        "attempt_id": attempt_id,
        "candidate_id": candidate_id,
        "status": "failed",
        "source": {
            "locator": locator,
            "sha256": source_hash,
            "suffix": source_suffix,
        },
        "converter": {"name": "docling", "packages": package_versions},
        "runtime": _runtime_contract(),
        "options": options,
        "error": {
            "type": type(error).__name__,
            "message": _safe_error_message(error, source, cache, vault),
        },
        "canonical_writes": False,
    }
    failure_id = f"failure-{_json_sha256(payload)}"
    payload["failure_id"] = failure_id
    path = runtime / "failures" / f"{failure_id}.json"
    lock = runtime / "locks" / f"{failure_id}.lock"
    with exclusive_file_lock(lock):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as read_error:
                raise WoonError(f"invalid document failure receipt: {failure_id}") from read_error
            if existing != payload:
                raise WoonError(f"document failure receipt mismatch: {failure_id}")
        else:
            atomic_write(path, encode_json(payload), mode=0o600)
    return path


def _adapter_contract() -> dict[str, object]:
    return {
        "name": _ADAPTER_NAME,
        "schema_version": DOCUMENT_INTAKE_SCHEMA_VERSION,
        "projection_version": DOCUMENT_PROJECTION_VERSION,
    }


def _runtime_contract() -> dict[str, str]:
    return {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "system": platform.system(),
    }


def _normalized_text_sha256(value: str) -> str:
    normalized = "\n".join(
        line.rstrip() for line in value.replace("\r\n", "\n").split("\n")
    ).strip()
    return hashlib.sha256((normalized + "\n").encode()).hexdigest()


def _safe_locator(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = Path(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise WoonError("document source locator must be a safe relative path")
    return normalized


def _safe_error_message(error: Exception, source: Path, cache: Path | None, vault: Path) -> str:
    message = str(error).replace(str(source), source.name)
    if cache is not None:
        message = message.replace(str(cache), "<model-cache>")
    message = message.replace(str(vault), "<vault>")
    message = message.replace(str(Path.home()), "<home>")
    return message[:2000]


def _redact_error_value(value: Any, source: Path, cache: Path | None) -> Any:
    if isinstance(value, str):
        redacted = value.replace(str(source), source.name)
        if cache is not None:
            redacted = redacted.replace(str(cache), "<model-cache>")
        return redacted.replace(str(Path.home()), "<home>")
    if isinstance(value, list):
        return [_redact_error_value(item, source, cache) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_error_value(item, source, cache) for key, item in value.items()}
    return value


def _output_record(data: bytes) -> dict[str, object]:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _json_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _encode_jsonl(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
