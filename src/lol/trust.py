from __future__ import annotations

from pathlib import Path
from typing import Any

from lol.io import read_json, write_json
from lol.project import ProjectIdentity


def _path(state: Path) -> Path:
    return state / "trust.json"


def is_trusted(state: Path, identity: ProjectIdentity) -> bool:
    try:
        value = read_json(_path(state))
    except (OSError, ValueError):
        return False
    return (
        value.get("fingerprint") == identity.fingerprint and value.get("origin") == identity.origin
    )


def trust(state: Path, identity: ProjectIdentity) -> None:
    value: dict[str, Any] = {
        "fingerprint": identity.fingerprint,
        "origin": identity.origin,
        "root": str(identity.root),
    }
    write_json(_path(state), value, mode=0o600)
