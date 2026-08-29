from __future__ import annotations

from pathlib import Path

import pytest

from lol.constants import PINNED_JENKINS_VERSION
from lol.discovery import discover, initial_manifest
from lol.errors import ConfigError


def test_discovery_finds_safe_jenkinsfiles_labels_and_podman(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Jenkinsfile").write_text(
        "pipeline { agent { label 'linux && firmware-builder && !windows' } }\n",
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
    found = discover(root)

    assert found.jenkinsfiles == (Path("Jenkinsfile"), Path("ci/Jenkinsfile.release"))
    assert found.labels == ("firmware-builder", "linux", "release")
    assert found.podman is True


def test_discovery_handles_nested_node_labels_negated_groups_and_interpolation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Jenkinsfile").write_text(
        """
pipeline {
  agent { node { label 'linux-big && !(windows || mac)' } }
  stages { stage('dynamic') { agent { label "linux-${env.ARCH}" } } }
}
""",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "Jenkinsfile.package").write_text(
        "node('vendored') {}\n", encoding="utf-8"
    )

    found = discover(root)

    assert found.jenkinsfiles == (Path("Jenkinsfile"),)
    assert found.labels == ("linux-big",)


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

    without_linux = initial_manifest(Path("Jenkinsfile"), labels=("custom",))
    assert without_linux["node"]["labels"] == ["custom", "lol-local"]

    with pytest.raises(ConfigError, match="repository-relative"):
        initial_manifest(Path("../Jenkinsfile"))


def test_discovery_wraps_unreadable_jenkinsfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    pipeline = root / "Jenkinsfile"
    pipeline.write_text("node('linux') {}\n", encoding="utf-8")
    original = Path.read_text

    def read_text(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == pipeline:
            raise PermissionError("permission denied")
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(ConfigError, match="cannot read Jenkinsfile.*permission denied"):
        discover(root)
