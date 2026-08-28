from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lol import io


def test_write_yaml_bundle_rolls_back_partial_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text("old: first\n", encoding="utf-8")
    second.write_text("old: second\n", encoding="utf-8")
    real_atomic_write = io.atomic_write
    calls = 0

    def fail_second(path: Path, content: str, *, mode: int = 0o644) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(io, "atomic_write", fail_second)

    with pytest.raises(OSError, match="simulated write failure"):
        io.write_yaml_bundle(((first, {"new": "first"}), (second, {"new": "second"})))

    assert first.read_text(encoding="utf-8") == "old: first\n"
    assert second.read_text(encoding="utf-8") == "old: second\n"


def test_write_yaml_bundle_refuses_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external.yaml"
    external.write_text("safe: true\n", encoding="utf-8")
    link = tmp_path / "link.yaml"
    link.symlink_to(external)
    entries: tuple[tuple[Path, Any], ...] = ((link, {"safe": False}),)

    with pytest.raises(OSError, match="symbolic link"):
        io.write_yaml_bundle(entries)

    assert link.is_symlink()
    assert external.read_text(encoding="utf-8") == "safe: true\n"


def test_write_yaml_bundle_preserves_existing_mode(tmp_path: Path) -> None:
    path = tmp_path / "lol.yaml"
    path.write_text("old: value\n", encoding="utf-8")
    path.chmod(0o600)

    io.write_yaml_bundle(((path, {"new": "value"}),))

    assert path.read_text(encoding="utf-8") == "new: value\n"
    assert path.stat().st_mode & 0o777 == 0o600
