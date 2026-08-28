from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

import requests

from lol.errors import HarnessError

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _normalized_sha256(value: str) -> str:
    normalized = value.lower()
    if not SHA256.fullmatch(normalized):
        raise HarnessError(f"invalid SHA-256 digest: {value}")
    return normalized


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, expected: str) -> bool:
    normalized = _normalized_sha256(expected)
    return path.is_file() and not path.is_symlink() and sha256_file(path) == normalized


def store_verified(source: Path, destination: Path, expected_sha256: str) -> Path:
    expected = _normalized_sha256(expected_sha256)
    if sha256_file(source) != expected:
        raise HarnessError(f"checksum mismatch for local artifact: {source}")
    if verify_file(destination, expected):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".cache-", dir=destination.parent)
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        shutil.copyfile(source, temp_path)
        if sha256_file(temp_path) != expected:
            raise HarnessError(f"checksum mismatch while caching local artifact: {source}")
        os.replace(temp_path, destination)
        return destination
    finally:
        temp_path.unlink(missing_ok=True)


def download_verified(
    url: str,
    destination: Path,
    expected_sha256: str,
    *,
    timeout: float = 60.0,
) -> Path:
    expected = _normalized_sha256(expected_sha256)
    if verify_file(destination, expected):
        return destination
    if destination.exists() or destination.is_symlink():
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        try:
            with requests.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                with temp_path.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            output.write(chunk)
        except (OSError, requests.RequestException) as exc:
            raise HarnessError(f"download failed for {url}: {exc}") from exc
        actual = sha256_file(temp_path)
        if actual != expected:
            raise HarnessError(f"checksum mismatch for {url}: expected {expected}, got {actual}")
        os.replace(temp_path, destination)
        return destination
    finally:
        temp_path.unlink(missing_ok=True)
