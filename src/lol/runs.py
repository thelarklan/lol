from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lol.errors import HarnessError
from lol.io import write_json
from lol.paths import ProjectPaths

RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


def new_run_id() -> str:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def _validated_run_id(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise HarnessError(f"invalid LOL run ID: {value!r}")
    try:
        dt.datetime.strptime(value[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise HarnessError(f"invalid LOL run ID: {value!r}") from exc
    return value


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise HarnessError(f"LOL run directory is a symbolic link: {path}")
    if path.exists() and not path.is_dir():
        raise HarnessError(f"LOL run path is not a directory: {path}")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise HarnessError(f"cannot prepare LOL run directory {path}: {exc}") from exc


def _read_private_metadata(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"cannot read LOL run metadata: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise HarnessError(f"unsafe LOL run metadata: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            try:
                value = json.load(handle)
            except (OSError, ValueError) as exc:
                raise HarnessError(f"invalid LOL run metadata: {path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise HarnessError(f"invalid LOL run metadata: {path}")
    return value


@dataclass(slots=True)
class RunRecord:
    run_id: str
    directory: Path
    metadata: dict[str, Any]

    @property
    def metadata_path(self) -> Path:
        return self.directory / "metadata.json"

    @property
    def console_path(self) -> Path:
        return self.directory / "console.log"

    @property
    def artifacts_path(self) -> Path:
        return self.directory / "artifacts"

    def save(self) -> None:
        if self.metadata.get("run_id") != self.run_id:
            raise HarnessError("LOL run metadata ID does not match its record")
        _ensure_private_directory(self.directory.parent)
        _ensure_private_directory(self.directory)
        if self.metadata_path.is_symlink():
            raise HarnessError(f"LOL run metadata is a symbolic link: {self.metadata_path}")
        try:
            write_json(self.metadata_path, self.metadata, mode=0o600)
        except OSError as exc:
            raise HarnessError(f"cannot write LOL run metadata: {self.metadata_path}") from exc

    def update(self, **values: Any) -> None:
        if "run_id" in values and values["run_id"] != self.run_id:
            raise HarnessError("LOL run ID cannot be changed")
        self.metadata.update(values)
        self.save()


def create_run(paths: ProjectPaths, metadata: dict[str, Any]) -> RunRecord:
    _ensure_private_directory(paths.state)
    _ensure_private_directory(paths.runs)
    for _ in range(10):
        run_id = _validated_run_id(new_run_id())
        directory = paths.runs / run_id
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        except OSError as exc:
            raise HarnessError(f"cannot create LOL run directory: {directory}") from exc
        record = RunRecord(run_id, directory, {**metadata, "run_id": run_id})
        record.save()
        return record
    raise HarnessError("could not allocate a unique LOL run ID")


def load_run(paths: ProjectPaths, run_id: str) -> RunRecord:
    identifier = _validated_run_id(run_id)
    directory = paths.runs / identifier
    if directory.is_symlink():
        raise HarnessError(f"LOL run directory is a symbolic link: {directory}")
    if not directory.is_dir():
        raise FileNotFoundError(f"LOL run not found: {identifier}")
    value = _read_private_metadata(directory / "metadata.json")
    if value.get("run_id") != identifier:
        raise HarnessError(f"LOL run metadata ID does not match directory: {identifier}")
    return RunRecord(identifier, directory, value)


def list_runs(paths: ProjectPaths) -> list[RunRecord]:
    if paths.runs.is_symlink():
        raise HarnessError(f"LOL runs directory is a symbolic link: {paths.runs}")
    if not paths.runs.exists():
        return []
    if not paths.runs.is_dir():
        raise HarnessError(f"LOL runs path is not a directory: {paths.runs}")
    records: list[RunRecord] = []
    try:
        directories = sorted(paths.runs.iterdir(), key=lambda item: item.name, reverse=True)
    except OSError as exc:
        raise HarnessError(f"cannot list LOL runs: {paths.runs}") from exc
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir() or not RUN_ID.fullmatch(directory.name):
            continue
        try:
            records.append(load_run(paths, directory.name))
        except (HarnessError, OSError):
            continue
    return records


def select_run(paths: ProjectPaths, run_id: str | None = None) -> RunRecord:
    if run_id:
        return load_run(paths, run_id)
    records = list_runs(paths)
    if not records:
        raise FileNotFoundError("no LOL runs are recorded")
    return records[0]
