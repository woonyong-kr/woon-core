"""Deterministic filesystem and serialization helpers."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError

_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise WoonError(f"read or parse {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise WoonError(f"{path} must contain a YAML mapping")
    return loaded


def encode_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize a critical section in this process and across OS processes."""

    resolved = path.expanduser().resolve()
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(resolved, threading.RLock())
    with process_lock:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                stream.write(b"\0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    stream.fileno(),
                    msvcrt.LK_LOCK,  # type: ignore[attr-defined]
                    1,
                )
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(  # type: ignore[attr-defined]
                        stream.fileno(),
                        msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                        1,
                    )
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
