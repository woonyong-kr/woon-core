"""Bounded, content-aware YAML parsing for repeated knowledge audits."""

from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

import yaml

_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_MAX_FILE_ENTRIES = 16


@dataclass(frozen=True, slots=True)
class _FileCacheEntry:
    digest: bytes
    value: Any


_file_cache: OrderedDict[Path, _FileCacheEntry] = OrderedDict()
_file_cache_lock = RLock()


@lru_cache(maxsize=4096)
def _parse_yaml_text(text: str) -> Any:
    return yaml.load(text, Loader=_SAFE_LOADER)


def load_yaml_text(text: str) -> Any:
    """Parse YAML text once while isolating every caller from cached mutations."""

    return copy.deepcopy(_parse_yaml_text(text))


def load_yaml_file(path: Path) -> Any:
    """Load current file bytes with a bounded cache keyed by their SHA-256.

    Reading and hashing on every call prevents stale reuse when a writer replaces
    a catalog without changing its size or observable timestamp. Returned values
    are deep copies because compiler transactions mutate catalog records in memory.
    """

    content = path.read_bytes()
    digest = hashlib.sha256(content).digest()
    resolved = path.resolve()
    with _file_cache_lock:
        cached = _file_cache.get(resolved)
        if cached is not None and cached.digest == digest:
            _file_cache.move_to_end(resolved)
            return copy.deepcopy(cached.value)

    parsed = yaml.load(content, Loader=_SAFE_LOADER)
    with _file_cache_lock:
        _file_cache[resolved] = _FileCacheEntry(digest=digest, value=parsed)
        _file_cache.move_to_end(resolved)
        while len(_file_cache) > _MAX_FILE_ENTRIES:
            _file_cache.popitem(last=False)
    return copy.deepcopy(parsed)
