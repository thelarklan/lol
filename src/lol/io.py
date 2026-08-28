from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def atomic_write(path: Path, content: str, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def write_yaml(path: Path, value: Any, *, mode: int = 0o644) -> None:
    atomic_write(path, yaml.safe_dump(value, sort_keys=False), mode=mode)
