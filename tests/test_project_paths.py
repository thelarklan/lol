from __future__ import annotations

from pathlib import Path

import pytest

from lol.errors import ConfigError
from lol.paths import AppPaths, ProjectPaths
from lol.project import find_repository, git, project_identity


def test_project_identity_is_stable_and_can_be_explicit(repository: Path) -> None:
    first = project_identity(repository)
    second = project_identity(repository)
    assert first == second
    assert first.project_id.startswith("repo-")
    explicit = project_identity(repository, {"project": {"id": "firmware"}})
    assert explicit.project_id == "firmware"


def test_repository_marker_supports_git_missing_doctor(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = repository / "nested"
    nested.mkdir()

    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr("lol.project.subprocess.run", unavailable)

    assert find_repository(nested) == repository
    assert git(repository, "status", check=False) == ""
    with pytest.raises(ConfigError, match="git status failed"):
        git(repository, "status")


def test_xdg_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = AppPaths.discover()
    assert app.state == tmp_path / "state" / "lol"
    project = ProjectPaths.from_id(tmp_path, "example", app)
    assert project.runs == tmp_path / "state" / "lol" / "example" / "runs"
