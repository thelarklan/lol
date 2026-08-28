from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

from lol.cli import cli, main
from lol.config import EffectiveConfig
from lol.constants import (
    PINNED_JENKINS_SHA256,
    PINNED_JENKINS_URL,
    PLUGIN_MANAGER_SHA256,
    PLUGIN_MANAGER_URL,
    PLUGIN_MANAGER_VERSION,
)
from lol.io import write_yaml
from lol.lockfile import normalized_manifest_digest


def _fake_create_lock(config: EffectiveConfig, destination: Path | None = None) -> dict[str, Any]:
    assert destination is not None
    plugins = []
    for value in sorted(config.values["jenkins"]["plugins"]):
        plugin_id = str(value).split(":", 1)[0]
        plugins.append(
            {
                "id": plugin_id,
                "version": "1.0",
                "requested": True,
                "url": (
                    f"https://updates.jenkins.io/download/plugins/{plugin_id}/1.0/{plugin_id}.hpi"
                ),
                "sha256": "0" * 64,
            }
        )
    lock = {
        "version": 1,
        "manifest_digest": normalized_manifest_digest(config),
        "jenkins": {
            "version": config.values["jenkins"]["version"],
            "url": PINNED_JENKINS_URL,
            "sha256": PINNED_JENKINS_SHA256,
        },
        "resolver": {
            "version": PLUGIN_MANAGER_VERSION,
            "url": PLUGIN_MANAGER_URL,
            "sha256": PLUGIN_MANAGER_SHA256,
        },
        "plugins": plugins,
    }
    write_yaml(destination, lock)
    return lock


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


def test_lock_writes_previewed_lock_and_check_detects_no_drift(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    created = CliRunner().invoke(cli, ["lock", "--yes"])

    assert created.exit_code == 0, created.output
    assert "--- " in created.output
    assert "+++ " in created.output
    assert "lol.plugins.lock.yaml" in created.output
    assert (repository / "lol.plugins.lock.yaml").is_file()

    checked = CliRunner().invoke(cli, ["lock", "--check"])
    assert checked.exit_code == 0, checked.output
    assert checked.output == "Plugin lock matches lol.yaml.\n"


def test_lock_skips_write_when_generated_lock_is_unchanged(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    assert CliRunner().invoke(cli, ["lock", "--yes"]).exit_code == 0

    result = CliRunner().invoke(cli, ["lock"])

    assert result.exit_code == 0, result.output
    assert result.output == "Plugin lock is already up to date.\n"


def test_lock_cancellation_leaves_repository_unchanged(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive_terminal", lambda: True)

    result = CliRunner().invoke(cli, ["lock"], input="n\n")

    assert result.exit_code != 0
    assert "lock update cancelled" in str(result.exception)
    assert not (repository / "lol.plugins.lock.yaml").exists()


def test_lock_requires_yes_when_terminal_is_not_interactive(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive_terminal", lambda: False)
    monkeypatch.setattr(sys, "argv", ["lol", "lock"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "lock update requires an interactive terminal or --yes" in captured.err
    assert "Traceback" not in captured.err
    assert not (repository / "lol.plugins.lock.yaml").exists()


def test_main_maps_click_abort_to_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def abort(*args: object, **kwargs: object) -> None:
        raise click.Abort()

    monkeypatch.setattr("lol.cli.cli", abort)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 130
    assert capsys.readouterr().err == "Interrupted.\n"
