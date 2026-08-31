from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any

import requests

from lol.cache import sha256_file
from lol.errors import HarnessError
from lol.io import write_json
from lol.jenkins import JenkinsClient
from lol.runs import RunRecord


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise HarnessError(f"unsafe Jenkins artifact path: {value!r}")
    return path


def matches(path: str, patterns: list[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _contained(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise HarnessError(f"artifact directory is a symbolic link: {path}")
    if path.exists() and not path.is_dir():
        raise HarnessError(f"artifact path is not a directory: {path}")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.chmod(0o700)
    except OSError as exc:
        raise HarnessError(f"cannot prepare artifact directory: {path}") from exc


def _private_artifact_parent(base: Path, relative: PurePosixPath) -> Path:
    current = base
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise HarnessError(f"artifact directory is a symbolic link: {current}")
        if current.exists() and not current.is_dir():
            raise HarnessError(f"artifact path is not a directory: {current}")
        try:
            current.mkdir(mode=0o700, exist_ok=True)
            current.chmod(0o700)
        except OSError as exc:
            raise HarnessError(f"cannot prepare artifact directory: {current}") from exc
    return current


def _download(
    client: JenkinsClient,
    url: str,
    destination: Path,
    relative: PurePosixPath,
) -> None:
    if destination.is_symlink():
        raise HarnessError(f"artifact destination is a symbolic link: {relative}")
    response = client.get_response(url, action=f"download artifact {relative}", stream=True)
    temp_path: Path | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=destination.parent)
        temp_path = Path(temporary)
        with os.fdopen(descriptor, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temp_path.chmod(0o600)
        os.replace(temp_path, destination)
    except (OSError, requests.RequestException) as exc:
        raise HarnessError(f"could not store Jenkins artifact {relative}: {exc}") from exc
    finally:
        response.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def download_artifacts(
    client: JenkinsClient,
    build_url: str,
    record: RunRecord,
    patterns: list[str],
) -> list[dict[str, Any]]:
    response = client.get_response(
        f"{build_url.rstrip('/')}/api/json?tree=artifacts[fileName,relativePath]",
        action="list Jenkins artifacts",
    )
    try:
        try:
            value = response.json()
        except ValueError as exc:
            raise HarnessError("could not list Jenkins artifacts: invalid JSON") from exc
    finally:
        response.close()
    if not isinstance(value, dict) or not isinstance(value.get("artifacts", []), list):
        raise HarnessError("could not list Jenkins artifacts: invalid response")
    _private_directory(record.artifacts_path)
    index: list[dict[str, Any]] = []
    seen: set[PurePosixPath] = set()
    for item in value.get("artifacts", []):
        if not isinstance(item, dict):
            raise HarnessError("could not list Jenkins artifacts: invalid entry")
        relative_value = str(item.get("relativePath") or item.get("fileName") or "")
        relative = safe_relative(relative_value)
        if not matches(relative.as_posix(), patterns):
            continue
        if relative in seen:
            raise HarnessError(f"Jenkins returned duplicate artifact path: {relative}")
        seen.add(relative)
        parent = _private_artifact_parent(record.artifacts_path, relative)
        destination = parent / relative.name
        if not _contained(record.artifacts_path, destination):
            raise HarnessError(f"unsafe Jenkins artifact destination: {relative}")
        url_path = "/".join(urllib.parse.quote(part, safe="") for part in relative.parts)
        _download(
            client,
            f"{build_url.rstrip('/')}/artifact/{url_path}",
            destination,
            relative,
        )
        try:
            index.append(
                {
                    "path": relative.as_posix(),
                    "size": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
            )
        except OSError as exc:
            raise HarnessError(f"could not index Jenkins artifact {relative}") from exc
    index.sort(key=lambda item: str(item["path"]))
    index_path = record.directory / "artifacts.json"
    try:
        write_json(index_path, {"artifacts": index}, mode=0o600)
    except OSError as exc:
        raise HarnessError(f"could not write artifact index: {index_path}") from exc
    return index


def _reject_symlink_ancestors(base: Path, relative: Path) -> None:
    current = base
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise HarnessError(f"unsafe artifact copy destination: {current}")


def copy_artifacts(record: RunRecord, output: Path) -> list[Path]:
    if record.artifacts_path.is_symlink():
        raise HarnessError(f"unsafe stored artifact directory: {record.artifacts_path}")
    if not record.artifacts_path.exists():
        return []
    if output.is_symlink():
        raise HarnessError(f"unsafe artifact output directory: {output}")
    if output.exists() and not output.is_dir():
        raise HarnessError(f"artifact output is not a directory: {output}")
    try:
        output.mkdir(parents=True, exist_ok=True)
        sources = sorted(record.artifacts_path.rglob("*"))
    except OSError as exc:
        raise HarnessError("could not inspect stored artifacts") from exc
    copied: list[Path] = []
    for source in sources:
        if source.is_symlink():
            raise HarnessError(f"unsafe stored artifact: {source}")
        if not source.is_file():
            continue
        if not _contained(record.artifacts_path, source):
            raise HarnessError(f"unsafe stored artifact: {source}")
        relative = source.relative_to(record.artifacts_path)
        destination = output / relative
        _reject_symlink_ancestors(output, relative)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError(f"could not prepare artifact output: {destination.parent}") from exc
        if not _contained(output, destination):
            raise HarnessError(f"unsafe artifact copy destination: {destination}")
        if _contained(record.artifacts_path, destination):
            raise HarnessError(f"artifact output overlaps stored artifacts: {destination}")
        descriptor, temporary = tempfile.mkstemp(prefix=".lol-copy-", dir=destination.parent)
        os.close(descriptor)
        temp_path = Path(temporary)
        try:
            shutil.copyfile(source, temp_path)
            os.replace(temp_path, destination)
        except OSError as exc:
            raise HarnessError(f"could not copy artifact {relative}: {exc}") from exc
        finally:
            temp_path.unlink(missing_ok=True)
        copied.append(destination)
    return copied
