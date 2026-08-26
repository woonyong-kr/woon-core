from pathlib import Path

from woon_core import cli

ROOT = Path(__file__).parents[1]
VAULT_TOOLS = ROOT / "src/woon_core/knowledge/vault_tools"


def test_every_public_python_vault_tool_is_registered() -> None:
    registered = set(cli._VAULT_TOOL_SCRIPTS.values())
    executable = {path.name for path in VAULT_TOOLS.glob("*.py") if not path.name.startswith("_")}

    assert executable == registered


def test_vault_tools_do_not_contain_unregistered_subapplications() -> None:
    directories = {
        path.name for path in VAULT_TOOLS.iterdir() if path.is_dir() and path.name != "__pycache__"
    }

    assert directories == set()
