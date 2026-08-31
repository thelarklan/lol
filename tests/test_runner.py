from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from lol.config import DEFAULTS, EffectiveConfig
from lol.constants import EXIT_PIPELINE, EXIT_SUCCESS
from lol.controller import ControllerStatus
from lol.errors import HarnessError
from lol.jenkins import Build, JenkinsClient, QueueCancelled
from lol.paths import AppPaths, ProjectPaths
from lol.process import exclusive_lock
from lol.project import ProjectIdentity
from lol.runner import (
    TemporaryCredential,
    _create_credentials,
    _interrupt_build,
    execute,
    load_parameter_definitions,
    read_console,
    save_parameter_definitions,
)
from lol.runs import create_run, list_runs


def _project(tmp_path: Path) -> tuple[EffectiveConfig, ProjectIdentity, ProjectPaths]:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "Jenkinsfile").write_text("pipeline { agent any }\n", encoding="utf-8")
    values = deepcopy(DEFAULTS)
    config = EffectiveConfig(root, root / "lol.yaml", values, {})
    identity = ProjectIdentity("fixture-123", "fingerprint", root, None)
    app = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return config, identity, ProjectPaths.from_id(root, identity.project_id, app)


class FakeSnapshot:
    branch = "lol-run"
    commit = "a" * 40
    tree = "b" * 40

    def __init__(self) -> None:
        self.stopped = False

    def serve(self) -> str:
        return "git://127.0.0.1:1234/snapshot.git"

    def stop(self) -> None:
        self.stopped = True


class FakeClient:
    last: FakeClient
    result = "SUCCESS"
    fail_delete = False

    def __init__(self, endpoint: str, username: str, password: str) -> None:
        assert endpoint == "http://127.0.0.1:8080"
        assert (username, password) == ("lol", "controller-password")
        type(self).last = self
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.submitted: dict[str, str] = {}
        self.job_parameters: list[str] = []
        self.secret_parameters: list[str] = []
        self.closed = False
        self.stopped: list[str] = []
        self.cancelled: list[str] = []

    def ensure_pipeline_job(
        self,
        name: str,
        repository_url: str,
        branch: str,
        script: str,
        parameters: list[str] | None = None,
        secret_parameters: list[str] | None = None,
    ) -> None:
        assert name == "lol-fixture-123"
        assert repository_url.startswith("git://127.0.0.1:")
        assert branch == "lol-run"
        assert script == "Jenkinsfile"
        self.job_parameters = parameters or []
        self.secret_parameters = secret_parameters or []

    def credential_descriptions(self) -> dict[str, str]:
        return {}

    def create_secret_text(self, credential_id: str, secret: str, run_id: str) -> None:
        assert secret == "credential-secret"
        assert run_id
        self.created.append(credential_id)

    def create_username_password(self, *_: object) -> None:
        raise AssertionError("not expected")

    def trigger(self, job: str, parameters: dict[str, str]) -> str:
        assert job == "lol-fixture-123"
        self.submitted = dict(parameters)
        return "http://127.0.0.1:8080/queue/item/3/"

    def wait_for_build(self, queue_url: str, timeout: float = 60.0) -> Build:
        assert queue_url.endswith("/queue/item/3/")
        assert timeout == 17.0
        return Build(7, "http://127.0.0.1:8080/job/fixture/7/", True, None)

    def console_chunks(self, build_url: str, *, follow: bool = True) -> Any:
        assert build_url.endswith("/job/fixture/7/")
        assert follow
        yield "token secret-"
        yield "value credential credential-secret env env-secret\n"

    def wait_until_complete(self, build_url: str) -> Build:
        return Build(7, build_url, False, self.result)

    def parameter_definitions(self, job: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "TARGET",
                "_class": "hudson.model.StringParameterDefinition",
                "defaultParameterValue": {"value": "local"},
            },
            {
                "name": "TOKEN",
                "_class": "hudson.model.PasswordParameterDefinition",
                "defaultParameterValue": {"value": "must-not-persist"},
            },
            {"name": "LOL_RUN_ID", "_class": "ignored"},
        ]

    def delete_credential(self, credential_id: str) -> None:
        self.deleted.append(credential_id)
        if self.fail_delete:
            raise HarnessError("delete failed with credential-secret")

    def stop(self, build_url: str) -> None:
        self.stopped.append(build_url)

    def cancel_queue(self, queue_url: str) -> None:
        self.cancelled.append(queue_url)

    def close(self) -> None:
        self.closed = True


def _wire_runner(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: FakeSnapshot,
    *,
    artifacts: list[dict[str, Any]] | None = None,
) -> None:
    monkeypatch.setattr(
        "lol.runner.up",
        lambda config, paths, *, timeout: ControllerStatus("running", "http://127.0.0.1:8080", 42),
    )
    monkeypatch.setattr(
        "lol.runner.controller_credentials", lambda paths: ("lol", "controller-password")
    )
    monkeypatch.setattr("lol.runner.create_snapshot", lambda *args: snapshot)
    monkeypatch.setattr("lol.runner.JenkinsClient", FakeClient)
    monkeypatch.setattr(
        "lol.runner.download_artifacts", lambda *args: artifacts or [{"path": "result.txt"}]
    )


def test_execute_runs_pipeline_redacts_secrets_and_persists_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    config.values["pipeline"]["parameters"] = {"CONFIGURED": 3}
    config.values["environment"]["set"] = {"API_TOKEN": "env-secret"}
    snapshot = FakeSnapshot()
    _wire_runner(monkeypatch, snapshot)
    emitted: list[str] = []

    exit_code, record = execute(
        config,
        identity,
        paths,
        revision=None,
        jenkinsfile=None,
        parameters={"TARGET": "test"},
        secret_parameters={"TOKEN": "secret-value"},
        temporary_credentials=[
            TemporaryCredential("temporary", "secret-text", "credential-secret")
        ],
        emit=emitted.append,
        controller_timeout=11.0,
        queue_timeout=17.0,
    )

    assert exit_code == EXIT_SUCCESS
    assert record.metadata["status"] == "completed"
    assert record.metadata["result"] == "SUCCESS"
    assert record.metadata["artifacts"] == 1
    assert record.metadata["parameters"] == ["TARGET"]
    assert record.metadata["secret_parameters"] == ["TOKEN"]
    assert record.metadata["credential_ids"] == ["temporary"]
    serialized = json.dumps(record.metadata)
    assert "secret-value" not in serialized
    assert "credential-secret" not in serialized
    console = record.console_path.read_text(encoding="utf-8")
    assert console == "token **** credential **** env ****\n"
    assert "".join(emitted) == console
    assert record.console_path.stat().st_mode & 0o777 == 0o600
    assert FakeClient.last.submitted["CONFIGURED"] == "3"
    assert FakeClient.last.submitted["TOKEN"] == "secret-value"
    assert FakeClient.last.submitted["LOL_RUN_ID"] == record.run_id
    assert FakeClient.last.secret_parameters == ["TOKEN"]
    assert FakeClient.last.deleted == ["temporary"]
    assert FakeClient.last.closed
    assert snapshot.stopped
    learned = load_parameter_definitions(paths)
    assert learned[0]["defaultParameterValue"] == {"value": "local"}
    assert "defaultParameterValue" not in learned[1]
    assert all(item["name"] != "LOL_RUN_ID" for item in learned)


def test_execute_maps_non_successful_jenkins_result_to_pipeline_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    snapshot = FakeSnapshot()
    _wire_runner(monkeypatch, snapshot)
    monkeypatch.setattr(FakeClient, "result", "FAILURE")

    exit_code, record = execute(
        config,
        identity,
        paths,
        revision="HEAD",
        jenkinsfile="Jenkinsfile",
        parameters={},
        secret_parameters={},
        temporary_credentials=[],
        emit=lambda _: None,
        queue_timeout=17.0,
    )

    assert exit_code == EXIT_PIPELINE
    assert record.metadata["result"] == "FAILURE"


def test_execute_maps_cancelled_queue_to_aborted_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    _wire_runner(monkeypatch, FakeSnapshot())

    def cancelled(*_: object, **__: object) -> Build:
        raise QueueCancelled("cancelled")

    monkeypatch.setattr(FakeClient, "wait_for_build", cancelled)
    exit_code, record = execute(
        config,
        identity,
        paths,
        revision=None,
        jenkinsfile=None,
        parameters={},
        secret_parameters={},
        temporary_credentials=[],
        emit=lambda _: None,
    )

    assert exit_code == EXIT_PIPELINE
    assert record.metadata["status"] == "completed"
    assert record.metadata["result"] == "ABORTED"


def test_execute_records_harness_failure_and_stops_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    snapshot = FakeSnapshot()
    _wire_runner(monkeypatch, snapshot)

    def fail(*_: object, **__: object) -> str:
        raise HarnessError("trigger failed")

    monkeypatch.setattr(FakeClient, "trigger", fail)
    with pytest.raises(HarnessError, match="trigger failed"):
        execute(
            config,
            identity,
            paths,
            revision=None,
            jenkinsfile=None,
            parameters={},
            secret_parameters={},
            temporary_credentials=[],
            emit=lambda _: None,
        )

    record = list_runs(paths)[0]
    assert record.metadata["status"] == "harness-failed"
    assert "trigger failed" not in json.dumps(record.metadata)
    assert snapshot.stopped
    assert FakeClient.last.closed


def test_successful_run_reports_credential_cleanup_failure_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    _wire_runner(monkeypatch, FakeSnapshot())
    monkeypatch.setattr(FakeClient, "fail_delete", True)

    with pytest.raises(HarnessError) as exc_info:
        execute(
            config,
            identity,
            paths,
            revision=None,
            jenkinsfile=None,
            parameters={},
            secret_parameters={},
            temporary_credentials=[
                TemporaryCredential("temporary", "secret-text", "credential-secret")
            ],
            emit=lambda _: None,
            queue_timeout=17.0,
        )

    assert "credential-secret" not in str(exc_info.value)
    assert list_runs(paths)[0].metadata["credential_cleanup"] == "failed"


def test_partial_credential_creation_is_available_for_cleanup() -> None:
    class PartialClient:
        def credential_descriptions(self) -> dict[str, str]:
            return {}

        def create_secret_text(self, credential_id: str, *_: str) -> None:
            assert credential_id == "first"

        def create_username_password(self, *_: str) -> None:
            raise HarnessError("creation failed")

    created: list[str] = []
    with pytest.raises(HarnessError, match="creation failed"):
        _create_credentials(
            cast(JenkinsClient, PartialClient()),
            [
                TemporaryCredential("first", "secret-text", "value"),
                TemporaryCredential("second", "username-password", "value", "user"),
            ],
            "run-id",
            created,
        )
    assert created == ["first"]


def test_interrupt_cancels_queue_and_marks_run(tmp_path: Path) -> None:
    _, _, paths = _project(tmp_path)
    record = create_run(paths, {"status": "queued"})
    client = FakeClient("http://127.0.0.1:8080", "lol", "controller-password")

    with pytest.raises(KeyboardInterrupt):
        _interrupt_build(
            cast(JenkinsClient, client),
            record,
            None,
            "http://127.0.0.1:8080/queue/item/3/",
        )

    assert client.cancelled == ["http://127.0.0.1:8080/queue/item/3/"]
    assert record.metadata["status"] == "interrupted"
    assert record.metadata["result"] == "ABORTED"


def test_parameter_metadata_and_console_reject_unsafe_files(tmp_path: Path) -> None:
    _, _, paths = _project(tmp_path)
    paths.runtime.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_text('{"parameters": []}\n', encoding="utf-8")
    (paths.runtime / "parameters.json").symlink_to(outside)
    with pytest.raises(HarnessError, match="cannot read pipeline parameter metadata"):
        load_parameter_definitions(paths)

    record = create_run(paths, {"status": "running"})
    record.console_path.write_text("unsafe", encoding="utf-8")
    record.console_path.chmod(0o644)
    with pytest.raises(HarnessError, match="unsafe LOL run console"):
        read_console(record)


def test_parameter_metadata_round_trip_is_private(tmp_path: Path) -> None:
    _, _, paths = _project(tmp_path)
    paths.runtime.mkdir(parents=True)
    save_parameter_definitions(
        paths,
        [{"name": "FLAG", "_class": "Boolean", "defaultParameterValue": {"value": True}}],
    )
    path = paths.runtime / "parameters.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_parameter_definitions(paths)[0]["name"] == "FLAG"


def test_read_console_uses_byte_offsets(tmp_path: Path) -> None:
    _, _, paths = _project(tmp_path)
    record = create_run(paths, {"status": "completed"})
    record.console_path.write_text("first\nsecond\n", encoding="utf-8")
    record.console_path.chmod(0o600)
    first, offset = read_console(record)
    assert first == "first\nsecond\n"
    assert read_console(record, offset) == ("", offset)


def test_configured_reserved_parameter_is_recorded_as_harness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    config.values["pipeline"]["parameters"] = {"LOL_RUN_ID": "bad"}
    monkeypatch.setattr("lol.runner.up", lambda *args, **kwargs: pytest.fail("must not start"))

    with pytest.raises(HarnessError, match="reserved LOL parameter in configuration"):
        execute(
            config,
            identity,
            paths,
            revision=None,
            jenkinsfile=None,
            parameters={},
            secret_parameters={},
            temporary_credentials=[],
            emit=lambda _: None,
        )

    assert list_runs(paths)[0].metadata["status"] == "harness-failed"


def test_execute_refuses_overlapping_run_for_same_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, identity, paths = _project(tmp_path)
    monkeypatch.setattr("lol.runner.up", lambda *args, **kwargs: pytest.fail("must not start"))

    with (
        exclusive_lock(paths.runtime / "run.lock"),
        pytest.raises(HarnessError, match="another LOL operation owns"),
    ):
        execute(
            config,
            identity,
            paths,
            revision=None,
            jenkinsfile=None,
            parameters={},
            secret_parameters={},
            temporary_credentials=[],
            emit=lambda _: None,
        )

    assert list_runs(paths)[0].metadata["status"] == "harness-failed"
