from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lol.errors import ConfigError


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ConfigError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def find_repository(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ConfigError(f"not inside a Git repository: {start}")
    return Path(result.stdout.strip()).resolve()


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.")
    return value[:48] or "project"


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    fingerprint: str
    root: Path
    origin: str | None


def project_identity(root: Path, manifest: dict[str, Any] | None = None) -> ProjectIdentity:
    explicit = ((manifest or {}).get("project") or {}).get("id")
    origin = git(root, "remote", "get-url", "origin", check=False) or None
    root_commit = git(root, "rev-list", "--max-parents=0", "HEAD", check=False) or "unborn"
    canonical = str(root.resolve())
    fingerprint = hashlib.sha256(
        f"root={canonical}\norigin={origin or ''}\ninitial={root_commit}\n".encode()
    ).hexdigest()
    project_id = explicit or f"{_slug(root.name)}-{fingerprint[:12]}"
    return ProjectIdentity(project_id, fingerprint, root, origin)
