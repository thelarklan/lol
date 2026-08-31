from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, TextIO

from lol.artifacts import download_artifacts
from lol.config import SECRET_KEY, EffectiveConfig
from lol.constants import EXIT_PIPELINE, EXIT_SUCCESS
from lol.controller import credentials as controller_credentials
from lol.controller import up
from lol.errors import HarnessError
from lol.io import write_json
from lol.jenkins import JenkinsClient, QueueCancelled
from lol.paths import ProjectPaths
from lol.process import exclusive_lock
from lol.project import ProjectIdentity
from lol.redaction import StreamingRedactor
from lol.runs import RunRecord, create_run
from lol.snapshot import create_snapshot

CredentialKind = Literal["secret-text", "username-password"]
RESERVED_PARAMETERS = {
    "LOL_CONTROLLER_URL",
    "LOL_PROJECT_ID",
    "LOL_RUN_ID",
    "LOL_WORKSPACE",
}


@dataclass(frozen=True, slots=True)
class TemporaryCredential:
    credential_id: str
    kind: CredentialKind
    secret: str
    username: str | None = None

    def secret_values(self) -> tuple[str, ...]:
        return (self.secret, self.username) if self.username else (self.secret,)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _job_name(project_id: str) -> str:
    return "lol-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", project_id)[:64]


def _create_credentials(
    client: JenkinsClient,
    values: Sequence[TemporaryCredential],
    run_id: str,
    created: list[str],
) -> None:
    identifiers = [value.credential_id for value in values]
    if len(identifiers) != len(set(identifiers)):
        raise HarnessError("temporary credential IDs must be unique")
    existing = client.credential_descriptions() if values else {}
    for value in values:
        if value.credential_id in existing:
            raise HarnessError(
                f"credential ID already exists and will not be overwritten: {value.credential_id}"
            )
        if value.kind == "secret-text":
            client.create_secret_text(value.credential_id, value.secret, run_id)
        elif value.kind == "username-password":
            if value.username is None:
                raise HarnessError(f"username is required for credential {value.credential_id}")
            client.create_username_password(
                value.credential_id,
                value.username,
                value.secret,
                run_id,
            )
        else:
            raise HarnessError(f"unsupported temporary credential kind: {value.kind}")
        created.append(value.credential_id)


def _interrupt_build(
    client: JenkinsClient,
    record: RunRecord,
    build_url: str | None,
    queue_url: str | None = None,
) -> NoReturn:
    # Preserve exit 130 even if Jenkins becomes unreachable during interruption.
    with suppress(Exception):
        if build_url:
            client.stop(build_url)
        elif queue_url:
            client.cancel_queue(queue_url)
    with suppress(Exception):
        record.update(status="interrupted", result="ABORTED", finished_at=_now())
    raise KeyboardInterrupt


def _private_console(record: RunRecord) -> TextIO:
    path = record.console_path
    if path.is_symlink():
        raise HarnessError(f"LOL run console is a symbolic link: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HarnessError(f"cannot open LOL run console: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HarnessError(f"unsafe LOL run console: {path}")
        return os.fdopen(descriptor, "a", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def read_console(record: RunRecord, offset: int = 0) -> tuple[str, int]:
    if offset < 0:
        raise HarnessError("LOL run console offset must not be negative")
    path = record.console_path
    if not path.exists() and not path.is_symlink():
        return "", offset
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"cannot read LOL run console: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HarnessError(f"unsafe LOL run console: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            handle.seek(offset)
            content = handle.read()
            next_offset = handle.tell()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return content.decode("utf-8", errors="replace"), next_offset


def _parameter_metadata_path(paths: ProjectPaths) -> Path:
    return paths.runtime / "parameters.json"


def _safe_parameter_definitions(values: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for value in values:
        name = value.get("name")
        if not isinstance(name, str) or not name or name.startswith("LOL_"):
            continue
        kind = str(value.get("_class") or "")
        item: dict[str, Any] = {"name": name, "_class": kind}
        choices = value.get("choices")
        if isinstance(choices, list):
            item["choices"] = [str(choice) for choice in choices]
        default = value.get("defaultParameterValue")
        if isinstance(default, dict) and not kind.endswith("PasswordParameterDefinition"):
            item["defaultParameterValue"] = {"value": default.get("value")}
        safe.append(item)
    return safe


def save_parameter_definitions(paths: ProjectPaths, values: Sequence[dict[str, Any]]) -> None:
    path = _parameter_metadata_path(paths)
    if path.is_symlink():
        raise HarnessError(f"pipeline parameter metadata is a symbolic link: {path}")
    try:
        write_json(path, {"parameters": _safe_parameter_definitions(values)}, mode=0o600)
    except OSError as exc:
        raise HarnessError(f"cannot write pipeline parameter metadata: {path}") from exc


def load_parameter_definitions(paths: ProjectPaths) -> list[dict[str, Any]]:
    path = _parameter_metadata_path(paths)
    if not path.exists() and not path.is_symlink():
        return []
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"cannot read pipeline parameter metadata: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HarnessError(f"unsafe pipeline parameter metadata: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise HarnessError(f"invalid pipeline parameter metadata: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or not isinstance(value.get("parameters"), list):
        raise HarnessError(f"invalid pipeline parameter metadata: {path}")
    return [item for item in value["parameters"] if isinstance(item, dict)]


def _secret_values(
    config: EffectiveConfig,
    secret_parameters: dict[str, str],
    temporary_credentials: Sequence[TemporaryCredential],
) -> list[str]:
    values = list(secret_parameters.values())
    for item in temporary_credentials:
        values.extend(item.secret_values())
    environment = config.values["environment"]
    for name, value in environment["set"].items():
        if SECRET_KEY.search(str(name)):
            values.append(str(value))
    for name in environment["pass"]:
        key = str(name)
        if SECRET_KEY.search(key) and key in os.environ:
            values.append(os.environ[key])
    return values


def _script_path(config: EffectiveConfig, override: str | None) -> str:
    value = override or str(config.values["pipeline"]["file"])
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HarnessError("Jenkinsfile must be a repository-relative path without '..'")
    try:
        resolved = (config.root / candidate).resolve(strict=True)
        resolved.relative_to(config.root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise HarnessError(f"Jenkinsfile is unavailable inside the repository: {value}") from exc
    if not resolved.is_file():
        raise HarnessError(f"Jenkinsfile is not a file: {value}")
    return candidate.as_posix()


def _configured_parameters(config: EffectiveConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in config.values["pipeline"].get("parameters", {}).items():
        name = str(key)
        if name in RESERVED_PARAMETERS:
            raise HarnessError(f"reserved LOL parameter in configuration: {name}")
        values[name] = "" if value is None else str(value)
    return values


def execute(
    config: EffectiveConfig,
    identity: ProjectIdentity,
    paths: ProjectPaths,
    *,
    revision: str | None,
    jenkinsfile: str | None,
    parameters: dict[str, str],
    secret_parameters: dict[str, str],
    temporary_credentials: Sequence[TemporaryCredential],
    emit: Callable[[str], None],
    controller_timeout: float = 120.0,
    queue_timeout: float = 60.0,
) -> tuple[int, RunRecord]:
    supplied = set(parameters) | set(secret_parameters)
    reserved = supplied & RESERVED_PARAMETERS
    if reserved:
        raise HarnessError("reserved LOL parameter: " + ", ".join(sorted(reserved)))
    script = _script_path(config, jenkinsfile)
    record = create_run(
        paths,
        {
            "project_id": identity.project_id,
            "repository": str(config.root),
            "revision": revision,
            "jenkinsfile": script,
            "started_at": _now(),
            "status": "provisioning",
            "parameters": sorted(parameters),
            "secret_parameters": sorted(secret_parameters),
            "credential_ids": sorted(item.credential_id for item in temporary_credentials),
        },
    )
    snapshot = None
    client: JenkinsClient | None = None
    created: list[str] = []
    build_url: str | None = None
    queue_url: str | None = None
    pending_error = False
    cleanup_error: Exception | None = None
    credential_cleanup_failed = False
    operation_lock = exclusive_lock(paths.runtime / "run.lock")
    lock_acquired = False
    try:
        operation_lock.__enter__()
        lock_acquired = True
        submitted = _configured_parameters(config)
        controller = up(config, paths, timeout=controller_timeout)
        if not controller.endpoint:
            raise HarnessError("controller has no endpoint")
        username, password = controller_credentials(paths)
        client = JenkinsClient(controller.endpoint, username, password)
        snapshot = create_snapshot(config.root, record.directory / "scm", revision)
        repository_url = snapshot.serve()
        job = _job_name(identity.project_id)
        submitted.update(parameters)
        submitted.update(secret_parameters)
        client.ensure_pipeline_job(
            job,
            repository_url,
            snapshot.branch,
            script,
            parameters=sorted(set(submitted) - set(secret_parameters)),
            secret_parameters=sorted(secret_parameters),
        )
        _create_credentials(client, temporary_credentials, record.run_id, created)
        submitted.update(
            {
                "LOL_PROJECT_ID": identity.project_id,
                "LOL_RUN_ID": record.run_id,
                "LOL_CONTROLLER_URL": controller.endpoint,
                "LOL_WORKSPACE": str(paths.jenkins_home / "workspace" / job),
            }
        )
        queue_url = client.trigger(job, submitted)
        record.update(
            status="queued",
            snapshot_commit=snapshot.commit,
            snapshot_tree=snapshot.tree,
            jenkins_job=job,
            jenkins_queue=queue_url,
        )
        build = client.wait_for_build(queue_url, timeout=queue_timeout)
        build_url = build.url
        record.update(
            status="running",
            jenkins_build=build.number,
            jenkins_url=build.url,
        )
        redactor = StreamingRedactor(
            _secret_values(config, secret_parameters, temporary_credentials)
        )
        with _private_console(record) as console:
            for chunk in client.console_chunks(build.url, follow=True):
                safe = redactor.feed(chunk)
                if safe:
                    console.write(safe)
                    console.flush()
                    emit(safe)
            final = redactor.finish()
            if final:
                console.write(final)
                console.flush()
                emit(final)
        build = client.wait_until_complete(build.url)
        result = build.result or "UNKNOWN"
        record.update(status="collecting", result=result)
        artifacts = download_artifacts(
            client,
            build.url,
            record,
            [str(item) for item in config.values["artifacts"]["patterns"]],
        )
        save_parameter_definitions(paths, client.parameter_definitions(job))
        record.update(
            status="completed",
            result=result,
            artifacts=len(artifacts),
            finished_at=_now(),
        )
        return (EXIT_SUCCESS if result == "SUCCESS" else EXIT_PIPELINE), record
    except KeyboardInterrupt:
        pending_error = True
        if client is None:
            with suppress(Exception):
                record.update(status="interrupted", result="ABORTED", finished_at=_now())
            raise
        _interrupt_build(client, record, build_url, queue_url)
    except QueueCancelled:
        record.update(status="completed", result="ABORTED", finished_at=_now())
        return EXIT_PIPELINE, record
    except BaseException:
        pending_error = True
        with suppress(Exception):
            record.update(status="harness-failed", finished_at=_now())
        raise
    finally:
        if client is not None:
            for credential_id in reversed(created):
                try:
                    client.delete_credential(credential_id)
                except Exception as exc:  # cleanup must continue for remaining credentials
                    cleanup_error = cleanup_error or exc
                    credential_cleanup_failed = True
        if snapshot is not None:
            try:
                snapshot.stop()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if lock_acquired:
            try:
                operation_lock.__exit__(None, None, None)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            with suppress(Exception):
                values = {"cleanup": "failed"}
                if credential_cleanup_failed:
                    values["credential_cleanup"] = "failed"
                record.update(**values)
            if not pending_error:
                raise HarnessError(
                    "run cleanup failed; inspect the controller and run-scoped state"
                ) from cleanup_error
