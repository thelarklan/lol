from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lol.cli import cli


def test_config_show_json(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(cli, ["--format", "json", "config", "show"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["configuration"]["version"] == 1
    assert value["configuration"]["pipeline"]["file"] == "Jenkinsfile"
