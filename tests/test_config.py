from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lol.config import ConfigError, deep_merge, load_effective, redact, validate_manifest
from lol.constants import PINNED_JENKINS_VERSION


def test_deep_merge_replaces_scalars_and_preserves_siblings() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}


def test_load_effective_applies_user_precedence(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "config"
    (config_home / "lol").mkdir(parents=True)
    (config_home / "lol" / "config.yaml").write_text("node:\n  executors: 2\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    effective = load_effective(repository)
    assert effective.values["node"]["executors"] == 2
    assert effective.values["node"]["labels"] == ["lol-local"]


def test_invalid_user_override_does_not_blame_repository_manifest(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_home = tmp_path / "config"
    (config_home / "lol").mkdir(parents=True)
    (config_home / "lol" / "config.yaml").write_text(
        "node:\n  executors: invalid\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(ConfigError) as exc_info:
        load_effective(repository)

    assert str(repository / "lol.yaml") not in str(exc_info.value)


def test_pinned_lts_alias_is_resolved(repository: Path) -> None:
    path = repository / "lol.yaml"
    value = yaml.safe_load(path.read_text())
    value["jenkins"]["version"] = "pinned-lts"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    assert load_effective(repository).values["jenkins"]["version"] == PINNED_JENKINS_VERSION


def test_repository_manifest_is_validated_before_defaults_are_applied(repository: Path) -> None:
    (repository / "lol.yaml").write_text("node:\n  executors: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="'version' is a required property"):
        load_effective(repository)


def test_manifest_rejects_parent_pipeline_path(repository: Path) -> None:
    config = load_effective(repository).values
    config["pipeline"]["file"] = "../Jenkinsfile"
    with pytest.raises(ConfigError, match="repository-relative"):
        validate_manifest(config)


def test_redact_recurses() -> None:
    assert redact({"token": "value", "nested": {"password": "other"}, "safe": 1}) == {
        "token": "<redacted>",
        "nested": {"password": "<redacted>"},
        "safe": 1,
    }
