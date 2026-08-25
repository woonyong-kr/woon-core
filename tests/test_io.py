from pathlib import Path

from woon_core.io import exclusive_file_lock


def test_exclusive_file_lock_is_user_only(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"

    with exclusive_file_lock(lock):
        assert lock.stat().st_mode & 0o777 == 0o600
