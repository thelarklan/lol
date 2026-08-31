from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lol.cli import cli
from lol.controller import ControllerStatus
from lol.errors import InteractionError


def _prepare(repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_up_emits_machine_readable_controller_status(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    calls: list[float] = []

    def start(config: object, paths: object, *, timeout: float) -> ControllerStatus:
        calls.append(timeout)
        return ControllerStatus("running", "http://127.0.0.1:1234", 42)

    monkeypatch.setattr("lol.cli.controller_up", start)

    result = CliRunner().invoke(cli, ["--format", "json", "up", "--timeout", "5"])

    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["project_id"].startswith("repo-")
    assert value["state"] == "running"
    assert value["endpoint"] == "http://127.0.0.1:1234"
    assert value["pid"] == 42
    assert calls == [5.0]


def test_status_and_down_use_repository_scoped_state(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lol.cli.controller_status", lambda paths: ControllerStatus("stopped", stale=True)
    )
    timeouts: list[float] = []

    def stop(paths: object, *, timeout: float) -> ControllerStatus:
        timeouts.append(timeout)
        return ControllerStatus("stopped")

    monkeypatch.setattr("lol.cli.controller_down", stop)

    status_result = CliRunner().invoke(cli, ["--format", "json", "status"])
    down_result = CliRunner().invoke(cli, ["--format", "json", "down", "--timeout", "4"])

    assert status_result.exit_code == 0, status_result.output
    assert json.loads(status_result.output)["stale"] is True
    assert down_result.exit_code == 0, down_result.output
    assert json.loads(down_result.output)["state"] == "stopped"
    assert timeouts == [4.0]


def test_open_reports_the_loopback_endpoint(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr("lol.cli.open_ui", lambda paths: "http://127.0.0.1:1234")

    result = CliRunner().invoke(cli, ["--format", "json", "open"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["endpoint"] == "http://127.0.0.1:1234"


def test_reset_requires_confirmation_when_noninteractive(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: False)
    monkeypatch.setattr("lol.cli.controller_reset", lambda paths: pytest.fail("reset must not run"))

    result = CliRunner().invoke(cli, ["reset"])

    assert result.exit_code != 0
    assert isinstance(result.exception, InteractionError)
    assert "Generated controller state will be removed" in result.output
    assert "review the targets and use --yes" in str(result.exception)


def test_reset_yes_previews_preserves_runs_and_restarts(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    calls: list[str] = []

    def reset(paths: object) -> None:
        calls.append("reset")

    def start(config: object, paths: object, *, timeout: float = 120.0) -> ControllerStatus:
        calls.append("up")
        return ControllerStatus("running", "http://127.0.0.1:1234", 42)

    monkeypatch.setattr("lol.cli.controller_reset", reset)
    monkeypatch.setattr("lol.cli.controller_up", start)

    result = CliRunner().invoke(cli, ["reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Generated controller state will be removed and recreated" in result.output
    assert "Preserved run history:" in result.output
    assert "state: running" in result.output
    assert calls == ["reset", "up"]
