from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from lol.cli import cli, main


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


def test_config_show_text_redacts_secret_shaped_values(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = repository / "lol.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["environment"]["set"]["API_TOKEN"] = "do-not-print"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    result = CliRunner().invoke(cli, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert "API_TOKEN: <redacted>" in result.output
    assert "do-not-print" not in result.output


def test_main_reports_non_repository_as_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lol", "config", "show"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "not inside a Git repository" in capsys.readouterr().err
