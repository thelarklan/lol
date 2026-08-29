from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click
import pytest
import yaml
from click.testing import CliRunner

from lol.cli import cli, main
from lol.config import EffectiveConfig, load_effective
from lol.constants import (
    PINNED_JENKINS_SHA256,
    PINNED_JENKINS_URL,
    PLUGIN_MANAGER_SHA256,
    PLUGIN_MANAGER_URL,
    PLUGIN_MANAGER_VERSION,
)
from lol.io import write_yaml
from lol.lockfile import load_lock, normalized_manifest_digest


def _fake_create_lock(config: EffectiveConfig, destination: Path | None = None) -> dict[str, Any]:
    assert destination is not None
    plugins = []
    for value in sorted(config.values["jenkins"]["plugins"]):
        plugin_id = str(value).split(":", 1)[0]
        plugins.append(
            {
                "id": plugin_id,
                "version": "1.0",
                "requested": True,
                "url": (
                    f"https://updates.jenkins.io/download/plugins/{plugin_id}/1.0/{plugin_id}.hpi"
                ),
                "sha256": "0" * 64,
            }
        )
    lock = {
        "version": 1,
        "manifest_digest": normalized_manifest_digest(config),
        "jenkins": {
            "version": config.values["jenkins"]["version"],
            "url": PINNED_JENKINS_URL,
            "sha256": PINNED_JENKINS_SHA256,
        },
        "resolver": {
            "version": PLUGIN_MANAGER_VERSION,
            "url": PLUGIN_MANAGER_URL,
            "sha256": PLUGIN_MANAGER_SHA256,
        },
        "plugins": plugins,
    }
    write_yaml(destination, lock)
    return lock


def test_config_show_json(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user = tmp_path / "config" / "lol"
    user.mkdir(parents=True)
    (user / "config.yaml").write_text(
        "jenkins:\n  version: 9.9.9\n  plugins: [git, junit]\n", encoding="utf-8"
    )
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = CliRunner().invoke(cli, ["--format", "json", "config", "show"])
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["configuration"]["version"] == 1
    assert value["configuration"]["pipeline"]["file"] == "Jenkinsfile"
    assert value["configuration"]["jenkins"] == {
        "version": "9.9.9",
        "plugins": ["git", "junit"],
    }
    assert value["lock_inputs"]["keys"] == ["jenkins.version", "jenkins.plugins"]
    assert "ignore user" in value["lock_inputs"]["note"]


def test_config_show_text_redacts_secret_shaped_values(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = repository / "lol.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["environment"]["set"]["API_TOKEN"] = "do-not-print"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    result = CliRunner().invoke(cli, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert "API_TOKEN: <redacted>" in result.output
    assert "do-not-print" not in result.output


def test_main_reports_non_repository_as_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["lol", "config", "show"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 2
    assert "not inside a Git repository" in capsys.readouterr().err


def test_lock_writes_previewed_lock_and_check_detects_no_drift(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    created = CliRunner().invoke(cli, ["lock", "--yes"])

    assert created.exit_code == 0, created.output
    assert "--- " in created.output
    assert "+++ " in created.output
    assert "lol.plugins.lock.yaml" in created.output
    assert (repository / "lol.plugins.lock.yaml").is_file()

    checked = CliRunner().invoke(cli, ["lock", "--check"])
    assert checked.exit_code == 0, checked.output
    assert checked.output == "Plugin lock matches lol.yaml.\n"


def test_lock_skips_write_when_generated_lock_is_unchanged(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    assert CliRunner().invoke(cli, ["lock", "--yes"]).exit_code == 0

    result = CliRunner().invoke(cli, ["lock"])

    assert result.exit_code == 0, result.output
    assert result.output == "Plugin lock is already up to date.\n"


def test_lock_cancellation_leaves_repository_unchanged(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: True)

    result = CliRunner().invoke(cli, ["lock"], input="n\n")

    assert result.exit_code != 0
    assert "lock update cancelled" in str(result.exception)
    assert not (repository / "lol.plugins.lock.yaml").exists()


def test_lock_requires_yes_when_terminal_is_not_interactive(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: False)
    monkeypatch.setattr(sys, "argv", ["lol", "lock"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "lock update requires an interactive terminal or --yes" in captured.err
    assert "Traceback" not in captured.err
    assert not (repository / "lol.plugins.lock.yaml").exists()


def test_main_maps_click_abort_to_interrupted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def abort(*args: object, **kwargs: object) -> None:
        raise click.Abort()

    monkeypatch.setattr("lol.cli.cli", abort)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 130
    assert capsys.readouterr().err == "Interrupted.\n"


def _remove_configuration(repository: Path) -> None:
    (repository / "lol.yaml").unlink()
    (repository / "lol.plugins.lock.yaml").unlink(missing_ok=True)


def test_init_creates_previewed_manifest_and_lock_noninteractively(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remove_configuration(repository)
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    result = CliRunner().invoke(cli, ["init", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert "Found repository: repo" in result.output
    assert result.output.count("--- ") == 2
    assert result.output.count("+++ ") == 2
    manifest = yaml.safe_load((repository / "lol.yaml").read_text(encoding="utf-8"))
    assert manifest["pipeline"]["file"] == "Jenkinsfile"
    assert manifest["node"] == {"executors": 1, "labels": ["linux", "lol-local"]}
    assert (repository / "lol.plugins.lock.yaml").is_file()


def test_init_requires_force_before_resolving_existing_configuration(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repository)

    def unexpected(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("lock resolution must not run")

    monkeypatch.setattr("lol.cli.create_lock", unexpected)
    result = CliRunner().invoke(cli, ["init", "--yes"])

    assert result.exit_code != 0
    assert "use --force" in str(result.exception)


def test_init_force_refuses_configuration_symlink(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = repository / "lol.yaml"
    manifest_path.unlink()
    external = tmp_path / "external.yaml"
    external.write_text("external: must-not-change\n", encoding="utf-8")
    manifest_path.symlink_to(external)
    monkeypatch.chdir(repository)
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    result = CliRunner().invoke(cli, ["init", "--force", "--yes"])

    assert result.exit_code != 0
    assert "refusing to replace symbolic link" in str(result.exception)
    assert manifest_path.is_symlink()
    assert external.read_text(encoding="utf-8") == "external: must-not-change\n"


def test_init_noninteractive_rejects_ambiguous_jenkinsfile(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_configuration(repository)
    (repository / "ci").mkdir()
    (repository / "ci" / "Jenkinsfile.release").write_text("node('release') {}\n")
    monkeypatch.chdir(repository)

    result = CliRunner().invoke(cli, ["init", "--non-interactive", "--yes"])

    assert result.exit_code != 0
    assert "multiple Jenkinsfiles" in str(result.exception)
    assert "--jenkinsfile" in str(result.exception)
    assert not (repository / "lol.yaml").exists()


def test_init_noninteractive_requires_explicit_detected_contract_choices(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remove_configuration(repository)
    (repository / "Jenkinsfile").write_text(
        "pipeline { agent { label 'firmware' }; "
        "stages { stage('x') { steps { sh 'podman run x' } } } }\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repository)

    labels = CliRunner().invoke(cli, ["init", "--non-interactive"])
    assert labels.exit_code != 0
    assert "pipeline labels were detected" in str(labels.exception)

    podman = CliRunner().invoke(
        cli,
        ["init", "--non-interactive", "--detected-labels"],
    )
    assert podman.exit_code != 0
    assert "Podman usage was detected" in str(podman.exception)
    assert not (repository / "lol.yaml").exists()


def test_init_explicit_automation_choices_and_force_are_honored(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (repository / "Jenkinsfile").write_text(
        "pipeline { agent { label 'firmware' }; "
        "stages { stage('x') { steps { sh 'podman run x' } } } }\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--force",
            "--non-interactive",
            "--detected-labels",
            "--require",
            "podman",
            "--jenkins-version",
            "2.568.2",
            "--executors",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = yaml.safe_load((repository / "lol.yaml").read_text(encoding="utf-8"))
    assert manifest["node"] == {
        "executors": 2,
        "labels": ["firmware", "linux", "lol-local"],
    }
    assert manifest["requirements"]["podman"] is True


def test_init_rejects_conflicting_podman_requirements(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repository)

    result = CliRunner().invoke(
        cli,
        ["init", "--force", "--require", "podman", "--no-require-podman"],
    )

    assert result.exit_code != 0
    assert "conflicts with --no-require-podman" in str(result.exception)


def test_init_cancellation_previews_both_files_and_writes_neither(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remove_configuration(repository)
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: True)

    result = CliRunner().invoke(cli, ["init"], input="\n\nn\n")

    assert result.exit_code != 0
    assert result.output.count("--- ") == 2
    assert "configuration update cancelled" in str(result.exception)
    assert not (repository / "lol.yaml").exists()
    assert not (repository / "lol.plugins.lock.yaml").exists()


def test_config_edit_preserves_repository_values_without_materializing_user_overrides(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = repository / "lol.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["jenkins"]["version"] = "pinned-lts"
    manifest["jenkins"]["plugins"].append("mailer")
    manifest["pipeline"]["parameters"] = {"RELEASE": False}
    manifest["artifacts"]["patterns"] = ["dist/**"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    user = tmp_path / "config" / "lol"
    user.mkdir(parents=True)
    (user / "config.yaml").write_text(
        "node:\n  executors: 7\nenvironment:\n  set:\n    API_TOKEN: do-not-commit\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)

    result = CliRunner().invoke(cli, ["config", "edit", "--yes"])

    assert result.exit_code == 0, result.output
    updated = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert updated["jenkins"]["version"] == "pinned-lts"
    assert updated["node"]["executors"] == 1
    assert "API_TOKEN" not in updated["environment"]["set"]
    assert updated["jenkins"]["plugins"][-1] == "mailer"
    assert updated["pipeline"]["parameters"] == {"RELEASE": False}
    assert updated["artifacts"]["patterns"] == ["dist/**"]
    effective = load_effective(repository)
    assert load_lock(effective)["manifest_digest"] == normalized_manifest_digest(effective)


def test_config_edit_guides_all_editable_repository_choices(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (repository / "ci").mkdir()
    (repository / "ci" / "Jenkinsfile").write_text("node('custom') {}\n", encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: True)

    result = CliRunner().invoke(
        cli,
        ["config", "edit"],
        input="ci/Jenkinsfile\n2.567.3\ncustom\ny\n2\ny\n",
    )

    assert result.exit_code == 0, result.output
    manifest = yaml.safe_load((repository / "lol.yaml").read_text(encoding="utf-8"))
    assert manifest["pipeline"]["file"] == "ci/Jenkinsfile"
    assert manifest["jenkins"]["version"] == "2.567.3"
    assert manifest["node"] == {
        "executors": 2,
        "labels": ["custom", "lol-local"],
    }
    assert manifest["requirements"]["podman"] is True


def test_config_edit_preserves_podman_command_and_honors_explicit_no(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = repository / "lol.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["requirements"] = {"commands": ["git", "podman"], "podman": True}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("lol.cli.create_lock", _fake_create_lock)
    monkeypatch.setattr("lol.cli._is_interactive", lambda: True)

    result = CliRunner().invoke(
        cli,
        ["config", "edit"],
        input="\n\n\nn\n\ny\n",
    )

    assert result.exit_code == 0, result.output
    updated = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert updated["requirements"] == {
        "commands": ["git", "podman"],
        "podman": False,
    }
