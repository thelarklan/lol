from __future__ import annotations

from pathlib import Path

import pytest

from lol.paths import AppPaths, ProjectPaths
from lol.project import project_identity


def test_project_identity_is_stable_and_can_be_explicit(repository: Path) -> None:
    first = project_identity(repository)
    second = project_identity(repository)
    assert first == second
    assert first.project_id.startswith("repo-")
    explicit = project_identity(repository, {"project": {"id": "firmware"}})
    assert explicit.project_id == "firmware"


def test_xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = AppPaths.discover()
    assert app.state == tmp_path / "state" / "lol"
    project = ProjectPaths.from_id(tmp_path, "example", app)
    assert project.runs == tmp_path / "state" / "lol" / "example" / "runs"
