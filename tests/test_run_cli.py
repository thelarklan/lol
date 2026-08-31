from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lol.cli import cli
from lol.config import load_effective
from lol.constants import EXIT_PIPELINE, EXIT_SUCCESS
from lol.controller import ControllerStatus
from lol.errors import InteractionError, LolError
from lol.io import write_json
from lol.paths import AppPaths, ProjectPaths
from lol.project import ProjectIdentity, project_identity
from lol.runner import save_parameter_definitions
from lol.runs import RunRecord, create_run, load_run


def _prepare(repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProjectPaths:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = load_effective()
    identity = project_identity(config.root, config.values)
    return ProjectPaths.from_id(config.root, identity.project_id, AppPaths.discover())


class FakeExecute:
    exit_code = EXIT_SUCCESS
    calls: list[dict[str, Any]] = []

    @classmethod
    def run(
        cls,
        config: object,
        identity: ProjectIdentity,
        paths: ProjectPaths,
        **values: Any,
    ) -> tuple[int, RunRecord]:
        cls.calls.append(values)
        record = create_run(
            paths,
            {
                "status": "completed",
                "result": "SUCCESS" if cls.exit_code == EXIT_SUCCESS else "FAILURE",
                "project_id": identity.project_id,
            },
        )
        return cls.exit_code, record


def test_run_requires_explicit_trust_when_prompts_are_disabled(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr("lol.cli.execute", lambda *args, **kwargs: pytest.fail("must not run"))

    result = CliRunner().invoke(cli, ["run", "--non-interactive"])

    assert result.exit_code != 0
    assert isinstance(result.exception, InteractionError)
    assert "pass --trust-repository explicitly" in str(result.exception)


def test_run_parses_safe_inputs_without_printing_secret_values(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setenv("RUN_TOKEN", "pipeline-secret")
    monkeypatch.setenv("CREDENTIAL_VALUE", "credential-secret")
    FakeExecute.calls = []
    FakeExecute.exit_code = EXIT_SUCCESS
    monkeypatch.setattr("lol.cli.execute", FakeExecute.run)

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--trust-repository",
            "--non-interactive",
            "--parameter",
            "TARGET=local",
            "--secret-parameter",
            "TOKEN=env:RUN_TOKEN",
            "--credential",
            "api=secret-text:env:CREDENTIAL_VALUE",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "pipeline-secret" not in result.output
    assert "credential-secret" not in result.output
    assert "Secret parameters: TOKEN" in result.output
    assert "Temporary credentials: api" in result.output
    call = FakeExecute.calls[0]
    assert call["parameters"] == {"TARGET": "local"}
    assert call["secret_parameters"] == {"TOKEN": "pipeline-secret"}
    assert call["temporary_credentials"][0].secret == "credential-secret"
    assert (paths.state / "trust.json").is_file()


def test_run_json_is_machine_readable_and_suppresses_streamed_console(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    FakeExecute.calls = []
    FakeExecute.exit_code = EXIT_SUCCESS

    def execute_with_output(*args: Any, **kwargs: Any) -> tuple[int, RunRecord]:
        kwargs["emit"]("console must not be emitted")
        return FakeExecute.run(*args, **kwargs)

    monkeypatch.setattr("lol.cli.execute", execute_with_output)
    result = CliRunner().invoke(
        cli, ["--format", "json", "run", "--trust-repository", "--non-interactive"]
    )

    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["run"]["status"] == "completed"
    assert "console must not be emitted" not in result.output


def test_run_preserves_pipeline_exit_status(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    FakeExecute.calls = []
    FakeExecute.exit_code = EXIT_PIPELINE
    monkeypatch.setattr("lol.cli.execute", FakeExecute.run)

    result = CliRunner().invoke(cli, ["run", "--trust-repository", "--non-interactive"])

    assert result.exit_code == EXIT_PIPELINE
    assert isinstance(result.exception, LolError)
    assert result.exception.exit_code == EXIT_PIPELINE
    assert "Run " in result.output and ": FAILURE" in result.output


def test_run_noninteractive_reports_all_missing_learned_parameters(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    paths.runtime.mkdir(parents=True)
    save_parameter_definitions(
        paths,
        [
            {"name": "TARGET", "_class": "hudson.model.StringParameterDefinition"},
            {"name": "TOKEN", "_class": "hudson.model.PasswordParameterDefinition"},
            {
                "name": "WITH_DEFAULT",
                "_class": "hudson.model.StringParameterDefinition",
                "defaultParameterValue": {"value": "local"},
            },
        ],
    )
    monkeypatch.setattr("lol.cli.execute", lambda *args, **kwargs: pytest.fail("must not run"))

    result = CliRunner().invoke(cli, ["run", "--trust-repository", "--non-interactive"])

    assert result.exit_code != 0
    assert "required pipeline parameters are missing: TARGET, TOKEN" in str(result.exception)
    assert "WITH_DEFAULT" not in str(result.exception)


def test_run_noninteractive_never_uses_secret_prompt_even_on_tty(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: True)
    monkeypatch.setattr("lol.cli.execute", lambda *args, **kwargs: pytest.fail("must not run"))

    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--trust-repository",
            "--non-interactive",
            "--secret-parameter",
            "TOKEN=prompt",
        ],
    )

    assert result.exit_code != 0
    assert "secure prompt is unavailable" in str(result.exception)
    assert "Secret pipeline parameter" not in result.output


def test_status_reports_latest_active_run_as_stale_when_controller_stopped(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    record = create_run(paths, {"status": "running", "result": None})
    monkeypatch.setattr(
        "lol.cli.controller_status", lambda paths: ControllerStatus("stopped", stale=True)
    )

    result = CliRunner().invoke(cli, ["--format", "json", "status"])

    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["latest_run"]["run_id"] == record.run_id
    assert value["latest_run"]["status"] == "stale"
    assert load_run(paths, record.run_id).metadata["status"] == "running"


def test_runs_and_logs_read_private_recorded_output(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    record = create_run(paths, {"status": "completed", "result": "SUCCESS"})
    record.console_path.write_text("redacted output\n", encoding="utf-8")
    record.console_path.chmod(0o600)

    runs = CliRunner().invoke(cli, ["--format", "json", "runs"])
    logs = CliRunner().invoke(cli, ["--format", "json", "logs", "--run", record.run_id])

    assert json.loads(runs.output)["runs"][0]["run_id"] == record.run_id
    assert json.loads(logs.output) == {
        "run_id": record.run_id,
        "console": "redacted output\n",
    }
    follow = CliRunner().invoke(cli, ["--format", "json", "logs", "--follow"])
    assert follow.exit_code != 0
    assert "cannot be combined" in str(follow.exception)


class StopClient:
    stopped: list[str] = []
    cancelled: list[str] = []

    def __init__(self, *_: object) -> None:
        pass

    def __enter__(self) -> StopClient:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def stop(self, value: str) -> None:
        type(self).stopped.append(value)

    def cancel_queue(self, value: str) -> None:
        type(self).cancelled.append(value)


def test_stop_cancels_active_build_and_requires_yes_noninteractively(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    record = create_run(
        paths,
        {
            "status": "running",
            "jenkins_url": "http://127.0.0.1:8080/job/fixture/7/",
        },
    )
    monkeypatch.setattr(
        "lol.cli.controller_status",
        lambda paths: ControllerStatus("running", "http://127.0.0.1:8080", 42),
    )
    monkeypatch.setattr("lol.cli.controller_credentials", lambda paths: ("lol", "password"))
    monkeypatch.setattr("lol.cli.JenkinsClient", StopClient)
    StopClient.stopped = []

    refused = CliRunner().invoke(cli, ["stop", "--run", record.run_id])
    stopped = CliRunner().invoke(cli, ["--format", "json", "stop", "--run", record.run_id, "--yes"])

    assert refused.exit_code != 0
    assert "requires confirmation" in str(refused.exception)
    assert stopped.exit_code == 0, stopped.output
    assert StopClient.stopped == ["http://127.0.0.1:8080/job/fixture/7/"]
    assert load_run(paths, record.run_id).metadata["status"] == "stopping"


def test_artifacts_lists_and_copies_run_scoped_files(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    record = create_run(paths, {"status": "completed", "result": "SUCCESS"})
    artifact = record.artifacts_path / "reports" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("result", encoding="utf-8")
    artifact.chmod(0o600)
    write_json(
        record.directory / "artifacts.json",
        {"artifacts": [{"path": "reports/result.txt", "size": 6, "sha256": "a" * 64}]},
        mode=0o600,
    )

    listed = CliRunner().invoke(cli, ["--format", "json", "artifacts", "--run", record.run_id])
    assert json.loads(listed.output)["artifacts"][0]["path"] == "reports/result.txt"
    (record.directory / "artifacts.json").write_text("not valid json\n", encoding="utf-8")
    output = tmp_path / "output"
    copied = CliRunner().invoke(
        cli,
        [
            "--format",
            "json",
            "artifacts",
            "--run",
            record.run_id,
            "--output",
            str(output),
        ],
    )

    assert copied.exit_code == 0, copied.output
    assert (output / "reports" / "result.txt").read_text(encoding="utf-8") == "result"


def test_down_refuses_to_stop_active_run_without_confirmation(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _prepare(repository, monkeypatch, tmp_path)
    create_run(paths, {"status": "running"})
    monkeypatch.setattr(
        "lol.cli.controller_status",
        lambda paths: ControllerStatus("running", "http://127.0.0.1:8080", 42),
    )
    monkeypatch.setattr(
        "lol.cli.controller_down", lambda *args, **kwargs: pytest.fail("must not stop")
    )

    result = CliRunner().invoke(cli, ["down"])

    assert result.exit_code != 0
    assert "a pipeline is active" in str(result.exception)
