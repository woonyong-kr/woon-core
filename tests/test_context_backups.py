from pathlib import Path

from woon_core.context.compiler import audit_directory_names


def test_directory_name_audit_skips_tool_managed_backups(tmp_path: Path) -> None:
    (tmp_path / "backups/20260814T065540.435101Z").mkdir(parents=True)

    audit_directory_names(tmp_path)
