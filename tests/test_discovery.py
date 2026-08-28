from __future__ import annotations

from pathlib import Path

import pytest

from lol.constants import PINNED_JENKINS_VERSION
from lol.discovery import discover, initial_manifest
from lol.errors import ConfigError


def test_discovery_finds_safe_jenkinsfiles_labels_podman_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Jenkinsfile").write_text(
        "pipeline { agent { label 'linux && firmware-builder' } }\n",
        encoding="utf-8",
    )
    (root / "ci").mkdir()
    (root / "ci" / "Jenkinsfile.release").write_text(
        "node('release') { sh 'podman build .' }\n",
        encoding="utf-8",
    )
    (root / ".git").mkdir()
    (root / ".git" / "Jenkinsfile").write_text("node('ignored') {}\n", encoding="utf-8")
    external = tmp_path / "Jenkinsfile.external"
    external.write_text("node('outside') {}\n", encoding="utf-8")
    (root / "Jenkinsfile.link").symlink_to(external)
    monkeypatch.setattr("lol.discovery.shutil.which", lambda command: f"/tools/{command}")

    found = discover(root)

    assert found.jenkinsfiles == (Path("Jenkinsfile"), Path("ci/Jenkinsfile.release"))
    assert found.labels == ("firmware-builder", "linux", "release")
    assert found.podman is True
    assert found.java == "/tools/java"
    assert found.git == "/tools/git"


def test_initial_manifest_is_deterministic_and_rejects_unsafe_pipeline() -> None:
    manifest = initial_manifest(
        Path("ci/Jenkinsfile"),
        jenkins_version="pinned-lts",
        labels=("z", "linux", "z"),
        require_podman=True,
        executors=2,
        commands=("make", "git", "make"),
    )

    assert manifest["pipeline"]["file"] == "ci/Jenkinsfile"
    assert manifest["node"] == {"executors": 2, "labels": ["linux", "lol-local", "z"]}
    assert manifest["requirements"] == {
        "commands": ["git", "make"],
        "podman": True,
    }
    assert manifest["jenkins"]["version"] == PINNED_JENKINS_VERSION

    with pytest.raises(ConfigError, match="repository-relative"):
        initial_manifest(Path("../Jenkinsfile"))
