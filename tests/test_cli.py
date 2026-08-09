from io import StringIO

import pytest

from woon_core.cli import run
from woon_core.errors import WoonError


def test_version() -> None:
    output = StringIO()
    run(["version"], output)
    assert output.getvalue().strip() == "0.5.4"


def test_unknown_command_fails() -> None:
    with pytest.raises(WoonError, match="unknown command"):
        run(["unknown"], StringIO())
