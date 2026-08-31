from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from lol.cache import store_verified, verify_file
from lol.config import EffectiveConfig
from lol.errors import HarnessError
from lol.io import write_json, write_yaml
from lol.lockfile import ensure_lock_cache, load_lock
from lol.paths import AppPaths, ProjectPaths
from lol.process import exclusive_lock, process_matches, process_start_time

TEMPLATE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ControllerStatus:
    state: str
    endpoint: str | None = None
    pid: int | None = None
    stale: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "endpoint": self.endpoint,
            "pid": self.pid,
            "stale": self.stale,
        }


def _runtime_file(paths: ProjectPaths) -> Path:
    return paths.runtime / "controller.json"


def _credentials_file(paths: ProjectPaths) -> Path:
    return paths.runtime / "credentials.json"


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise HarnessError(f"LOL state directory is a symbolic link: {path}")
    if path.exists() and not path.is_dir():
        raise HarnessError(f"LOL state path is not a directory: {path}")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise HarnessError(f"cannot prepare LOL state directory {path}: {exc}") from exc


def _read_private_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError(f"unsafe state file: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return value


def _loopback_endpoint(value: object) -> str | None:
    endpoint = str(value or "")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"http://127.0.0.1:{port}"


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _configuration_digest(config: EffectiveConfig, lock: dict[str, Any]) -> str:
    payload = json.dumps(
        {"config": config.values, "lock": lock, "template": TEMPLATE_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def status(paths: ProjectPaths) -> ControllerStatus:
    runtime = _runtime_file(paths)
    if not runtime.exists() and not runtime.is_symlink():
        return ControllerStatus("stopped")
    try:
        value = _read_private_json(runtime)
        pid = int(value["pid"])
        start_time = str(value["process_start_time"])
        endpoint = _loopback_endpoint(value.get("endpoint"))
        if pid <= 0 or not start_time or endpoint is None:
            raise ValueError("invalid controller metadata")
    except (OSError, ValueError, KeyError, TypeError):
        return ControllerStatus("stopped", stale=True)
    if not process_matches(pid, start_time):
        return ControllerStatus("stopped", endpoint, pid, True)
    return ControllerStatus("running", endpoint, pid)


def credentials(paths: ProjectPaths) -> tuple[str, str]:
    try:
        value = _read_private_json(_credentials_file(paths))
        username = str(value["username"])
        password = str(value["password"])
        if not username or not password:
            raise ValueError("empty controller credentials")
        return username, password
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HarnessError("controller credentials are unavailable") from exc


def _render_casc(config: EffectiveConfig, paths: ProjectPaths, endpoint: str) -> Path:
    labels = " ".join(sorted(set(config.values["node"]["labels"])))
    casc = {
        "jenkins": {
            "systemMessage": "Managed by LOL — Launch on Local",
            "numExecutors": int(config.values["node"]["executors"]),
            "mode": "NORMAL",
            "labelString": labels,
            "securityRealm": {
                "local": {
                    "allowsSignup": False,
                    "users": [{"id": "lol", "password": "${LOL_JENKINS_ADMIN_PASSWORD}"}],
                }
            },
            "authorizationStrategy": {"loggedInUsersCanDoAnything": {"allowAnonymousRead": False}},
            "crumbIssuer": {"standard": {}},
            "disabledAdministrativeMonitors": ["jenkins.diagnostics.RootUrlNotSetMonitor"],
        },
        "unclassified": {"location": {"url": f"{endpoint}/"}},
    }
    target = paths.controller / "jenkins.yaml"
    write_yaml(target, casc, mode=0o600)
    return target


def _prepare_home(paths: ProjectPaths, lock: dict[str, Any]) -> Path:
    app = AppPaths.discover()
    _ensure_private_directory(paths.controller)
    _ensure_private_directory(paths.jenkins_home)
    plugins_dir = paths.jenkins_home / "plugins"
    _ensure_private_directory(plugins_dir)
    expected_names: set[str] = set()
    for plugin in lock["plugins"]:
        plugin_id = str(plugin["id"])
        version = str(plugin["version"])
        digest = str(plugin["sha256"])
        source = app.cache / "plugins" / plugin_id / version / f"{plugin_id}.jpi"
        if not verify_file(source, digest):
            raise HarnessError(f"locked plugin is missing or corrupt: {plugin_id}; run `lol lock`")
        destination = plugins_dir / f"{plugin_id}.jpi"
        expected_names.add(destination.name)
        if destination.is_symlink():
            raise HarnessError(f"controller plugin path is a symbolic link: {destination}")
        if not verify_file(destination, digest):
            store_verified(source, destination, digest)
        destination.chmod(0o600)
    for existing in plugins_dir.glob("*.?pi"):
        if existing.name not in expected_names:
            raise HarnessError(
                f"controller contains plugin not present in lock: {existing.name}; run `lol reset`"
            )
    return app.cache / "jenkins" / str(lock["jenkins"]["version"]) / "jenkins.war"


def _wait_ready(
    endpoint: str,
    process: subprocess.Popen[bytes],
    timeout: float,
    log_path: Path,
    log_start: int,
) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    with requests.Session() as session:
        session.trust_env = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise HarnessError(
                    f"Jenkins exited during startup with status {process.returncode}"
                )
            if log_path.exists():
                with log_path.open("rb") as log:
                    size = os.fstat(log.fileno()).st_size
                    log.seek(max(log_start, size - 200_000))
                    recent = log.read().decode("utf-8", errors="replace")
                if "Failed to initialize Jenkins" in recent:
                    raise HarnessError(
                        f"Jenkins failed to initialize; inspect the controller log: {log_path}"
                    )
            try:
                response = session.get(f"{endpoint}/login", timeout=2)
                headers = {name.lower() for name in response.headers}
                if response.status_code < 500 and {"x-jenkins", "x-hudson"} & headers:
                    return
                last_error = f"endpoint did not identify itself as Jenkins ({response.status_code})"
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(0.5)
    raise HarnessError(f"Jenkins did not become ready within {timeout:g}s: {last_error}")


def _port_conflict(log_path: Path, start: int) -> bool:
    try:
        with log_path.open(encoding="utf-8", errors="replace") as log:
            log.seek(start)
            recent = log.read()[-200_000:].lower()
    except OSError:
        return False
    return "address already in use" in recent or "failed to bind" in recent


def _open_private_log(path: Path) -> int:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HarnessError(f"cannot open controller log {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"controller log is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _terminate(process: subprocess.Popen[bytes], timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def up(config: EffectiveConfig, paths: ProjectPaths, *, timeout: float = 120.0) -> ControllerStatus:
    if timeout <= 0:
        raise HarnessError("controller startup timeout must be greater than zero")
    _ensure_private_directory(paths.state)
    _ensure_private_directory(paths.runtime)
    with exclusive_lock(paths.runtime / "lifecycle.lock"):
        current = status(paths)
        lock = load_lock(config)
        digest = _configuration_digest(config, lock)
        if current.state == "running":
            runtime = _read_private_json(_runtime_file(paths))
            if runtime.get("configuration_digest") != digest:
                raise HarnessError(
                    "controller configuration changed while Jenkins is running; "
                    "run `lol down` then `lol up`"
                )
            return current
        if current.stale:
            _runtime_file(paths).unlink(missing_ok=True)
        ensure_lock_cache(lock)
        war = _prepare_home(paths, lock)
        if not verify_file(war, str(lock["jenkins"]["sha256"])):
            raise HarnessError("locked Jenkins WAR is missing or corrupt; run `lol lock`")
        java = shutil.which("java")
        if not java:
            raise HarnessError("Java is unavailable; run `lol doctor`")
        password = secrets.token_urlsafe(32)
        credentials_path = _credentials_file(paths)
        write_json(credentials_path, {"username": "lol", "password": password}, mode=0o600)
        log_path = paths.runtime / "controller.log"
        protected_environment = {
            "CASC_JENKINS_CONFIG",
            "HOME",
            "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS",
            "JENKINS_HOME",
            "LOL_JENKINS_ADMIN_PASSWORD",
            "_JAVA_OPTIONS",
        }
        configured_environment = {
            *(str(name) for name in config.values["environment"]["pass"]),
            *(str(name) for name in config.values["environment"]["set"]),
        }
        reserved = sorted(protected_environment & configured_environment)
        if reserved:
            credentials_path.unlink(missing_ok=True)
            raise HarnessError(
                "controller environment cannot override reserved variables: " + ", ".join(reserved)
            )
        allowed_environment = {
            "HOME": str(paths.state),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "JENKINS_HOME": str(paths.jenkins_home),
            "LOL_JENKINS_ADMIN_PASSWORD": password,
        }
        for name in config.values["environment"]["pass"]:
            if str(name) in os.environ:
                allowed_environment[str(name)] = os.environ[str(name)]
        for name, value in config.values["environment"]["set"].items():
            allowed_environment[str(name)] = str(value)
        for attempt in range(3):
            port = _available_port()
            endpoint = f"http://127.0.0.1:{port}"
            casc = _render_casc(config, paths, endpoint)
            allowed_environment["CASC_JENKINS_CONFIG"] = str(casc)
            command = [
                java,
                "-Djenkins.install.runSetupWizard=false",
                "-jar",
                str(war),
                "--httpListenAddress=127.0.0.1",
                f"--httpPort={port}",
            ]
            log_descriptor = _open_private_log(log_path)
            log_start = os.fstat(log_descriptor).st_size
            log = os.fdopen(log_descriptor, "ab", buffering=0)
            try:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=allowed_environment,
                        start_new_session=True,
                    )
                except OSError as exc:
                    credentials_path.unlink(missing_ok=True)
                    raise HarnessError(f"could not start Jenkins: {exc}") from exc
            finally:
                log.close()
            try:
                start_time = process_start_time(process.pid)
                if not start_time:
                    raise HarnessError("could not identify the Jenkins process")
                write_json(
                    _runtime_file(paths),
                    {
                        "pid": process.pid,
                        "process_start_time": start_time,
                        "endpoint": endpoint,
                        "configuration_digest": digest,
                        "log": str(log_path),
                    },
                    mode=0o600,
                )
                _wait_ready(endpoint, process, timeout, log_path, log_start)
            except KeyboardInterrupt:
                _terminate(process)
                _runtime_file(paths).unlink(missing_ok=True)
                credentials_path.unlink(missing_ok=True)
                raise
            except Exception:
                _terminate(process)
                _runtime_file(paths).unlink(missing_ok=True)
                if attempt < 2 and _port_conflict(log_path, log_start):
                    continue
                credentials_path.unlink(missing_ok=True)
                raise
            return ControllerStatus("running", endpoint, process.pid)
        raise HarnessError("could not allocate a Jenkins loopback port")


def _down_locked(paths: ProjectPaths, *, timeout: float) -> ControllerStatus:
    current = status(paths)
    runtime = _runtime_file(paths)
    if current.state != "running" or current.pid is None:
        runtime.unlink(missing_ok=True)
        return ControllerStatus("stopped")
    try:
        os.kill(current.pid, signal.SIGTERM)
    except ProcessLookupError:
        runtime.unlink(missing_ok=True)
        return ControllerStatus("stopped")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status(paths).state == "stopped":
            runtime.unlink(missing_ok=True)
            return ControllerStatus("stopped")
        time.sleep(0.2)
    raise HarnessError("Jenkins did not stop before the timeout")


def down(paths: ProjectPaths, *, timeout: float = 30.0) -> ControllerStatus:
    if timeout <= 0:
        raise HarnessError("controller shutdown timeout must be greater than zero")
    _ensure_private_directory(paths.state)
    _ensure_private_directory(paths.runtime)
    with exclusive_lock(paths.runtime / "lifecycle.lock"):
        return _down_locked(paths, timeout=timeout)


def open_ui(paths: ProjectPaths) -> str:
    current = status(paths)
    if current.state != "running" or not current.endpoint:
        raise HarnessError("controller is not running")
    webbrowser.open(current.endpoint)
    return current.endpoint


def _remove_generated(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not stat.S_ISDIR(path.lstat().st_mode)):
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def reset(paths: ProjectPaths) -> None:
    _ensure_private_directory(paths.state)
    _ensure_private_directory(paths.runtime)
    with exclusive_lock(paths.runtime / "lifecycle.lock"):
        _down_locked(paths, timeout=30.0)
        _remove_generated(paths.controller)
        for item in paths.runtime.iterdir():
            if item.name != "lifecycle.lock":
                _remove_generated(item)
