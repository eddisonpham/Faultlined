import sys

import pytest

from data_engine.cli import main


@pytest.mark.unit
def test_cli_placeholder_is_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["de", "dev"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "commands are not implemented yet" in capsys.readouterr().err
