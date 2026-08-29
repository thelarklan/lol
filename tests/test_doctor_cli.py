from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lol.cli import cli, main
from lol.doctor import Finding
from lol.errors import InteractionError


def _prepare(repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(repository)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_doctor_check_emits_machine_readable_findings(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lol.cli.analyze",
        lambda config, state: [Finding("host.platform", "pass", "Linux host detected")],
    )

    result = CliRunner().invoke(cli, ["--format", "json", "doctor", "--check"])

    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["state"] == "Ready"
    assert value["project_id"].startswith("repo-")
    assert value["findings"] == [
        {
            "check": "host.platform",
            "evidence": "",
            "recommendation": "",
            "repair_scope": None,
            "status": "pass",
            "summary": "Linux host detected",
        }
    ]


def test_doctor_check_uses_stable_host_exit_code(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lol.cli.analyze",
        lambda config, state: [Finding("java.runtime", "blocker", "Java is unavailable")],
    )
    monkeypatch.setattr(sys, "argv", ["lol", "doctor", "--check"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 3
    assert "Doctor result: Blocked" in captured.out
    assert "Error: host is blocked" in captured.err


def test_doctor_reports_missing_git_before_project_identity(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare(repository, monkeypatch, tmp_path)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr("lol.project.subprocess.run", unavailable)
    monkeypatch.setattr(
        "lol.doctor._command",
        lambda name: (
            Finding("command.git", "blocker", "required command is missing: git")
            if name == "git"
            else Finding(f"command.{name}", "pass", f"{name} is available")
        ),
    )
    monkeypatch.setattr("lol.doctor._java", lambda: Finding("java.runtime", "pass", "ok"))
    monkeypatch.setattr(
        "lol.doctor._writable", lambda path: Finding("state.writable", "pass", "ok")
    )
    monkeypatch.setattr("lol.doctor._port", lambda: Finding("network.loopback", "pass", "ok"))
    monkeypatch.setattr("lol.doctor.load_lock", lambda config: {"version": 1})
    monkeypatch.setattr("lol.doctor.verify_lock_cache", lambda lock: [])
    monkeypatch.setattr(sys, "argv", ["lol", "doctor", "--check"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 3
    assert "required command is missing: git" in captured.out


def test_doctor_fix_yes_regenerates_missing_lock_and_revalidates(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    rounds = iter(
        [
            [Finding("plugins.lock", "blocker", "lock is missing", repair_scope="lol-owned")],
            [Finding("plugins.lock", "pass", "plugin lock is valid")],
        ]
    )
    monkeypatch.setattr("lol.cli.analyze", lambda config, state: next(rounds))
    calls: list[object] = []
    monkeypatch.setattr("lol.cli.create_lock", lambda config: calls.append(config))

    result = CliRunner().invoke(cli, ["doctor", "--fix", "--yes"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "Revalidated:" in result.output
    assert "Doctor result: Ready" in result.output


def test_doctor_fix_seeds_exact_lock_cache_without_resolving(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    rounds = iter(
        [
            [
                Finding(
                    "plugins.lock",
                    "recommendation",
                    "locked artifacts are not cached",
                    repair_scope="lol-owned",
                )
            ],
            [Finding("plugins.lock", "pass", "plugin lock is valid")],
        ]
    )
    monkeypatch.setattr("lol.cli.analyze", lambda config, state: next(rounds))
    lock = {"version": 1}
    monkeypatch.setattr("lol.cli.load_lock", lambda config: lock)
    cached: list[dict[str, Any]] = []
    monkeypatch.setattr("lol.cli.ensure_lock_cache", lambda value: cached.append(value))
    monkeypatch.setattr(
        "lol.cli.create_lock", lambda config: pytest.fail("lock must not be re-resolved")
    )

    result = CliRunner().invoke(cli, ["doctor", "--fix", "--yes"])

    assert result.exit_code == 0, result.output
    assert cached == [lock]


def test_doctor_refuses_noninteractive_repair_without_yes(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lol.cli.analyze",
        lambda config, state: [
            Finding("plugins.lock", "blocker", "lock is missing", repair_scope="lol-owned")
        ],
    )
    monkeypatch.setattr("lol.cli._is_interactive", lambda: False)

    result = CliRunner().invoke(cli, ["doctor", "--fix"])

    assert result.exit_code != 0
    assert isinstance(result.exception, InteractionError)
    assert "interactive terminal or --yes" in str(result.exception)


def test_doctor_verbose_includes_evidence(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "lol.cli.analyze",
        lambda config, state: [Finding("java.runtime", "pass", "Java is compatible", "21.0.8")],
    )

    result = CliRunner().invoke(cli, ["doctor", "--check", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "21.0.8" in result.output


def test_doctor_rejects_conflicting_modes(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prepare(repository, monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["doctor", "--check", "--fix"])

    assert result.exit_code != 0
    assert "cannot be combined" in str(result.exception)
