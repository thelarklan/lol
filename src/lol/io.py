from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
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


def write_yaml_bundle(entries: Sequence[tuple[Path, Any]], *, mode: int = 0o644) -> None:
    contents = [(path, yaml.safe_dump(value, sort_keys=False)) for path, value in entries]
    originals: dict[Path, tuple[str, int] | None] = {}
    for path, _ in contents:
        if path.is_symlink():
            raise OSError(f"refusing to replace symbolic link: {path}")
        if path.exists():
            originals[path] = (path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777)
        else:
            originals[path] = None
    written: list[Path] = []
    try:
        for path, content in contents:
            original = originals[path]
            target_mode = original[1] if original is not None else mode
            atomic_write(path, content, mode=target_mode)
            written.append(path)
    except OSError:
        for path in reversed(written):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                content, original_mode = original
                atomic_write(path, content, mode=original_mode)
        raise
