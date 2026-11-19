from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from lol.config import EffectiveConfig, load_effective
from lol.controller import (
    ControllerStatus,
    _prepare_home,
    _wait_ready,
    credentials,
    down,
    open_ui,
    reset,
    status,
    up,
)
from lol.errors import HarnessError
from lol.io import write_json
from lol.paths import AppPaths, ProjectPaths
from lol.process import (
    exclusive_lock,
    open_verified_process,
    process_matches,
    process_start_time,
)


def project_paths(tmp_path: Path) -> ProjectPaths:
    app = AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache")
    return ProjectPaths.from_id(tmp_path / "repo", "fixture", app)


def test_process_identity_uses_pid_and_start_time() -> None:
    start = process_start_time(os.getpid())
    assert start is not None
    assert process_matches(os.getpid(), start)
    assert not process_matches(os.getpid(), "0")


def test_exclusive_lock_rejects_concurrent_owner_and_symbolic_link(tmp_path: Path) -> None:
    lock = tmp_path / "lifecycle.lock"
    with exclusive_lock(lock):
        assert lock.stat().st_mode & 0o777 == 0o600
        with (
            pytest.raises(HarnessError, match="another LOL operation"),
            exclusive_lock(lock),
        ):
            pass
    lock.unlink()
    lock.symlink_to(tmp_path / "elsewhere")
    with (
        pytest.raises(HarnessError, match="cannot open LOL operation lock"),
        exclusive_lock(lock),
    ):
        pass


def test_controller_status_detects_live_and_stale_metadata(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    start = process_start_time(os.getpid())
    write_json(
        paths.runtime / "controller.json",
        {
            "pid": os.getpid(),
            "process_start_time": start,
            "endpoint": "http://127.0.0.1:1234",
        },
        mode=0o600,
    )
    assert status(paths).state == "running"
    value = {
        "pid": os.getpid(),
        "process_start_time": "0",
        "endpoint": "http://127.0.0.1:1234",
    }
    write_json(paths.runtime / "controller.json", value, mode=0o600)
    result = status(paths)
    assert result.state == "stopped"
    assert result.stale


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:1234",
        "http://127.0.0.1:0",
        "http://127.0.0.1:1234/path",
        "https://127.0.0.1:1234",
        "http://user@127.0.0.1:1234",
    ],
)
def test_controller_status_rejects_unsafe_endpoint(tmp_path: Path, endpoint: str) -> None:
    paths = project_paths(tmp_path)
    write_json(
        paths.runtime / "controller.json",
        {
            "pid": os.getpid(),
            "process_start_time": process_start_time(os.getpid()),
            "endpoint": endpoint,
        },
        mode=0o600,
    )

    result = status(paths)

    assert result.state == "stopped"
    assert result.stale


def test_controller_credentials_require_private_regular_file(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    value = paths.runtime / "credentials.json"
    write_json(value, {"username": "lol", "password": "secret"}, mode=0o600)
    assert credentials(paths) == ("lol", "secret")
    value.unlink()
    value.symlink_to(tmp_path / "missing")
    with pytest.raises(HarnessError, match="credentials are unavailable"):
        credentials(paths)


def test_controller_credentials_reject_world_readable_file(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    value = paths.runtime / "credentials.json"
    write_json(value, {"username": "lol", "password": "secret"}, mode=0o644)

    with pytest.raises(HarnessError, match="credentials are unavailable"):
        credentials(paths)


def test_prepare_home_installs_only_digest_verified_locked_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = project_paths(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    content = b"locked plugin"
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / "xdg-cache" / "lol" / "plugins" / "git" / "1.0" / "git.jpi"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    lock = {
        "jenkins": {"version": "1.0"},
        "plugins": [{"id": "git", "version": "1.0", "sha256": digest}],
    }

    war = _prepare_home(paths, lock)

    installed = paths.jenkins_home / "plugins" / "git.jpi"
    assert installed.read_bytes() == content
    assert installed.stat().st_mode & 0o777 == 0o600
    assert war == tmp_path / "xdg-cache" / "lol" / "jenkins" / "1.0" / "jenkins.war"
    (paths.jenkins_home / "plugins" / "stray.hpi").write_bytes(b"stray")
    with pytest.raises(HarnessError, match="not present in lock: stray.hpi"):
        _prepare_home(paths, lock)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None or timeout > 0
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class FakeResponse:
    status_code = 200
    headers = {"X-Jenkins": "2.0"}


class FakeSession:
    def __init__(self) -> None:
        self.trust_env = True
        self.requests: list[str] = []

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *values: object) -> None:
        pass

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        assert timeout == 2
        assert not self.trust_env
        self.requests.append(url)
        return FakeResponse()


def test_wait_ready_ignores_failure_from_previous_log_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "controller.log"
    log_path.write_text("Failed to initialize Jenkins\n", encoding="utf-8")
    log_start = log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as log:
        log.write("Starting a healthy controller\n")
    session = FakeSession()
    monkeypatch.setattr("lol.controller.requests.Session", lambda: session)

    _wait_ready(
        "http://127.0.0.1:1234",
        cast(subprocess.Popen[bytes], FakeProcess(42)),
        1,
        log_path,
        log_start,
    )

    assert session.requests == ["http://127.0.0.1:1234/login"]


def _mock_startup(
    config: EffectiveConfig,
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], list[FakeProcess]]:
    lock = {
        "jenkins": {"version": "1.0", "sha256": "a" * 64},
        "plugins": [],
    }
    war = tmp_path / "jenkins.war"
    war.write_bytes(b"war")
    monkeypatch.setattr("lol.controller.load_lock", lambda value: lock)
    monkeypatch.setattr("lol.controller.ensure_lock_cache", lambda value: None)
    monkeypatch.setattr("lol.controller._prepare_home", lambda *values: war)
    monkeypatch.setattr("lol.controller.verify_file", lambda path, digest: True)
    monkeypatch.setattr("lol.controller.shutil.which", lambda name: "/usr/bin/java")
    monkeypatch.setattr("lol.controller._available_port", lambda: 12345)
    monkeypatch.setattr("lol.controller.process_start_time", lambda pid: f"start-{pid}")
    monkeypatch.setattr("lol.controller._wait_ready", lambda *values: None)
    invocations: list[dict[str, Any]] = []
    processes: list[FakeProcess] = []

    def popen(command: list[str], **kwargs: Any) -> FakeProcess:
        invocations.append({"command": command, **kwargs})
        process = FakeProcess(4000 + len(processes))
        processes.append(process)
        return process

    monkeypatch.setattr("lol.controller.subprocess.Popen", popen)
    return invocations, processes


def test_up_starts_loopback_controller_with_sanitized_environment(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    config.values["environment"] = {"pass": ["SAFE_VALUE"], "set": {"FIXED": "yes"}}
    paths = project_paths(tmp_path)
    monkeypatch.setenv("HOME", "/home/developer")
    monkeypatch.setenv("SAFE_VALUE", "allowed")
    monkeypatch.setenv("SECRET_VALUE", "excluded")
    invocations, _ = _mock_startup(config, paths, monkeypatch, tmp_path)

    result = up(config, paths)

    assert result == ControllerStatus("running", "http://127.0.0.1:12345", 4000)
    command = invocations[0]["command"]
    assert "--httpListenAddress=127.0.0.1" in command
    assert "--httpPort=12345" in command
    environment = invocations[0]["env"]
    assert environment["HOME"] == "/home/developer"
    assert environment["SAFE_VALUE"] == "allowed"
    assert environment["FIXED"] == "yes"
    assert "SECRET_VALUE" not in environment
    assert (paths.runtime / "credentials.json").stat().st_mode & 0o777 == 0o600
    assert (paths.runtime / "controller.json").stat().st_mode & 0o777 == 0o600
    assert (paths.controller / "jenkins.yaml").stat().st_mode & 0o777 == 0o600


def test_up_retries_only_after_current_attempt_port_conflict(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    paths = project_paths(tmp_path)
    _, processes = _mock_startup(config, paths, monkeypatch, tmp_path)
    ports = iter([12345, 12346])
    monkeypatch.setattr("lol.controller._available_port", lambda: next(ports))
    attempts = 0

    def wait_ready(
        endpoint: str,
        process: subprocess.Popen[bytes],
        timeout: float,
        log_path: Path,
        log_start: int,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            with log_path.open("ab") as log:
                log.write(b"Failed to bind: address already in use\n")
            raise HarnessError("startup failed")

    monkeypatch.setattr("lol.controller._wait_ready", wait_ready)

    result = up(config, paths)

    assert result.endpoint == "http://127.0.0.1:12346"
    assert len(processes) == 2
    assert processes[0].terminated


def test_up_cleans_process_and_runtime_when_interrupted(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    paths = project_paths(tmp_path)
    _, processes = _mock_startup(config, paths, monkeypatch, tmp_path)

    def interrupt(*values: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("lol.controller._wait_ready", interrupt)

    with pytest.raises(KeyboardInterrupt):
        up(config, paths)

    assert processes[0].terminated
    assert not (paths.runtime / "controller.json").exists()
    assert not (paths.runtime / "credentials.json").exists()


def test_up_rejects_reserved_controller_environment_override(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    config.values["environment"] = {"pass": [], "set": {"JENKINS_HOME": "/elsewhere"}}
    paths = project_paths(tmp_path)
    _mock_startup(config, paths, monkeypatch, tmp_path)

    with pytest.raises(HarnessError, match="reserved variables: JENKINS_HOME"):
        up(config, paths)

    assert not (paths.runtime / "credentials.json").exists()


def test_up_refuses_configuration_drift_for_running_process(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    paths = project_paths(tmp_path)
    write_json(
        paths.runtime / "controller.json",
        {
            "pid": os.getpid(),
            "process_start_time": process_start_time(os.getpid()),
            "endpoint": "http://127.0.0.1:1234",
            "configuration_digest": "different",
        },
        mode=0o600,
    )
    monkeypatch.setattr(
        "lol.controller.load_lock",
        lambda value: {"jenkins": {"version": "1"}, "plugins": []},
    )

    with pytest.raises(HarnessError, match="configuration changed"):
        up(config, paths)


class FakeVerifiedProcess:
    def __init__(self, pid: int, running_states: list[bool]) -> None:
        self.pid = pid
        self.signals: list[int] = []
        self._running_states = iter(running_states)

    def send_signal(self, sig: int) -> bool:
        self.signals.append(sig)
        return True

    def is_running(self) -> bool:
        return next(self._running_states, False)


def _write_controller_runtime(paths: ProjectPaths, pid: int, start_time: str) -> None:
    write_json(
        paths.runtime / "controller.json",
        {
            "pid": pid,
            "process_start_time": start_time,
            "endpoint": "http://127.0.0.1:1234",
        },
        mode=0o600,
    )


def test_down_signals_only_matching_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = project_paths(tmp_path)
    _write_controller_runtime(paths, 42, "start-42")
    fake = FakeVerifiedProcess(42, [False])
    opened: list[tuple[int, str]] = []

    @contextmanager
    def fake_open(pid: int, start_time: str) -> Any:
        opened.append((pid, start_time))
        yield fake

    monkeypatch.setattr("lol.controller.open_verified_process", fake_open)

    result = down(paths)

    assert result.state == "stopped"
    assert opened == [(42, "start-42")]
    assert fake.signals == [int(signal.SIGTERM)]
    assert not (paths.runtime / "controller.json").exists()


def test_open_requires_running_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = project_paths(tmp_path)
    with pytest.raises(HarnessError, match="not running"):
        open_ui(paths)
    opened: list[str] = []
    monkeypatch.setattr(
        "lol.controller.status",
        lambda value: ControllerStatus("running", "http://127.0.0.1:1234", 1),
    )
    monkeypatch.setattr("lol.controller.webbrowser.open", lambda endpoint: opened.append(endpoint))
    assert open_ui(paths) == "http://127.0.0.1:1234"
    assert opened == ["http://127.0.0.1:1234"]


def test_reset_preserves_runs(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    (paths.controller / "jenkins-home").mkdir(parents=True)
    (paths.controller / "jenkins-home" / "state").write_text("generated")
    (paths.runtime / "endpoint.json").parent.mkdir(parents=True)
    (paths.runtime / "endpoint.json").write_text("generated")
    run = paths.runs / "run-1"
    run.mkdir(parents=True)
    (run / "console.log").write_text("preserved")
    reset(paths)
    assert not paths.controller.exists()
    assert (run / "console.log").read_text() == "preserved"


def test_reset_unlinks_controller_symlink_without_touching_target(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    paths.state.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "preserved"
    marker.write_text("yes", encoding="utf-8")
    paths.controller.symlink_to(external, target_is_directory=True)

    reset(paths)

    assert not paths.controller.exists()
    assert marker.read_text(encoding="utf-8") == "yes"


def test_down_raises_without_force_when_timeout_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = project_paths(tmp_path)
    _write_controller_runtime(paths, 42, "start-42")
    fake = FakeVerifiedProcess(42, [True, True, True])

    @contextmanager
    def fake_open(pid: int, start_time: str) -> Any:
        yield fake

    monkeypatch.setattr("lol.controller.open_verified_process", fake_open)
    ticks = iter([0.0, 0.05, 0.15])
    monkeypatch.setattr("lol.controller.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("lol.controller.time.sleep", lambda _: None)

    with pytest.raises(HarnessError, match="did not stop before the timeout"):
        down(paths, timeout=0.1)

    assert fake.signals == [int(signal.SIGTERM)]
    assert (paths.runtime / "controller.json").exists()


def test_down_force_sends_sigkill_after_timeout_and_checks_after_final_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = project_paths(tmp_path)
    _write_controller_runtime(paths, 42, "start-42")
    # Running during SIGTERM wait, still running immediately after SIGKILL,
    # then exited by the check following the sleep that reaches kill_deadline.
    fake = FakeVerifiedProcess(42, [True, True, True, False])

    @contextmanager
    def fake_open(pid: int, start_time: str) -> Any:
        yield fake

    monkeypatch.setattr("lol.controller.open_verified_process", fake_open)
    ticks = iter([0.0, 0.05, 0.2, 0.2, 0.25, 0.35])
    monkeypatch.setattr("lol.controller.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("lol.controller.time.sleep", lambda _: None)

    result = down(paths, timeout=0.1, force=True)

    assert result.state == "stopped"
    assert fake.signals == [int(signal.SIGTERM), int(signal.SIGKILL)]
    assert not (paths.runtime / "controller.json").exists()


def test_open_verified_process_binds_pidfd_and_rejects_mismatched_start_time() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        start = process_start_time(proc.pid)
        assert start is not None
        with open_verified_process(proc.pid, "0") as mismatched:
            assert mismatched is None
        assert proc.poll() is None

        with open_verified_process(proc.pid, start) as verified:
            assert verified is not None
            assert verified.send_signal(signal.SIGTERM) is True
            proc.wait(timeout=5)
            assert verified.is_running() is False
            assert verified.send_signal(signal.SIGKILL) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_down_force_terminates_real_sigterm_ignoring_process(tmp_path: Path) -> None:
    paths = project_paths(tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        start = process_start_time(proc.pid)
        assert start is not None
        _write_controller_runtime(paths, proc.pid, start)

        result = down(paths, timeout=0.1, force=True)

        assert result.state == "stopped"
        assert proc.wait(timeout=5) == -int(signal.SIGKILL)
        assert not (paths.runtime / "controller.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.parametrize("bad_timeout", [float("nan"), float("inf"), float("-inf")])
def test_up_and_down_reject_non_finite_timeouts(
    repository: Path, tmp_path: Path, bad_timeout: float
) -> None:
    config = load_effective(repository)
    paths = project_paths(tmp_path)
    with pytest.raises(HarnessError, match="finite number greater than zero"):
        up(config, paths, timeout=bad_timeout)
    with pytest.raises(HarnessError, match="finite number greater than zero"):
        down(paths, timeout=bad_timeout, force=True)
