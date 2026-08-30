from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lol.config import load_effective
from lol.doctor import Finding, _java, _podman, _writable, analyze, final_state


def _pass(check: str) -> Finding:
    return Finding(check, "pass", "ok")


def _isolate_host_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lol.doctor._command", lambda name: _pass(f"command.{name}"))
    monkeypatch.setattr("lol.doctor._java", lambda: _pass("java.runtime"))
    monkeypatch.setattr("lol.doctor._writable", lambda path: _pass("state.writable"))
    monkeypatch.setattr("lol.doctor._port", lambda: _pass("network.loopback"))


def test_doctor_reports_missing_lock(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_host_checks(monkeypatch)

    findings = analyze(load_effective(repository), repository / ".state")

    lock = next(item for item in findings if item.check == "plugins.lock")
    assert lock.status == "blocker"
    assert lock.repair_scope == "lol-owned"
    assert final_state(findings) == "Blocked"


def test_doctor_reports_uncached_locked_artifacts_as_recommendation(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_host_checks(monkeypatch)
    monkeypatch.setattr("lol.doctor.load_lock", lambda config: {"version": 1})
    monkeypatch.setattr(
        "lol.doctor.verify_lock_cache", lambda lock: ["Jenkins WAR", "plugin git:1.0"]
    )

    findings = analyze(load_effective(repository), repository / ".state")

    lock = next(item for item in findings if item.check == "plugins.lock")
    assert lock.status == "recommendation"
    assert lock.evidence == "Jenkins WAR, plugin git:1.0"
    assert final_state(findings) == "Ready with recommendations"


def test_doctor_reports_unreadable_cache_without_crashing(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_host_checks(monkeypatch)
    monkeypatch.setattr("lol.doctor.load_lock", lambda config: {"version": 1})

    def unreadable(lock: dict[str, object]) -> list[str]:
        raise OSError("permission denied")

    monkeypatch.setattr("lol.doctor.verify_lock_cache", unreadable)

    findings = analyze(load_effective(repository), repository / ".state")

    lock = next(item for item in findings if item.check == "plugins.lock")
    assert lock.status == "blocker"
    assert lock.repair_scope == "manual"
    assert "permission denied" in lock.evidence


def test_doctor_rejects_snapshot_features_that_v1_cannot_reproduce(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_host_checks(monkeypatch)
    monkeypatch.setattr("lol.doctor.load_lock", lambda config: {"version": 1})
    monkeypatch.setattr("lol.doctor.verify_lock_cache", lambda lock: [])
    (repository / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
    (repository / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", encoding="utf-8")

    findings = analyze(load_effective(repository), repository / ".state")

    assert {item.check for item in findings if item.status == "unsupported"} == {
        "scm.lfs",
        "scm.submodules",
    }
    assert final_state(findings) == "Unsupported"


def test_final_state_precedence() -> None:
    assert final_state([Finding("a", "pass", "ok")]) == "Ready"
    assert final_state([Finding("a", "recommendation", "advice")]) == ("Ready with recommendations")
    assert final_state([Finding("a", "blocker", "bad")]) == "Blocked"
    assert final_state([Finding("a", "blocker", "bad"), Finding("b", "unsupported", "bad")]) == (
        "Unsupported"
    )


@pytest.mark.parametrize(
    "version, expected",
    [("17.0.12", "blocker"), ("21.0.8", "pass"), ("25.0.0", "pass")],
)
def test_java_compatibility_matches_pinned_jenkins(
    monkeypatch: pytest.MonkeyPatch, version: str, expected: str
) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/java")
    monkeypatch.setattr(
        "lol.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "", f'openjdk version "{version}"\n'
        ),
    )

    assert _java().status == expected


def test_java_timeout_is_a_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/java")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["java", "-version"], 10)

    monkeypatch.setattr("lol.doctor.subprocess.run", timeout)

    assert _java().summary == "Java version detection timed out"


def test_java_empty_success_output_is_a_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/java")
    monkeypatch.setattr(
        "lol.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )

    finding = _java()

    assert finding.status == "blocker"
    assert finding.summary == "Java version could not be determined"


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {
                "host": {"security": {"rootless": True}},
                "store": {
                    "graphRoot": "/home/user/.local/share/containers",
                    "runRoot": "/run/user/1",
                },
            },
            "pass",
        ),
        ({"host": {"security": {"rootless": False}}}, "unsupported"),
    ],
)
def test_podman_requires_rootless_runtime(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], expected: str
) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/podman")
    monkeypatch.setattr("lol.doctor.os.access", lambda path, mode: True)
    monkeypatch.setattr(
        "lol.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(payload), ""),
    )

    assert _podman().status == expected


def test_podman_requires_writable_rootless_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "host": {"security": {"rootless": True}},
        "store": {"graphRoot": "/home/user/.local/share/containers", "runRoot": "/run/user/1"},
    }
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/podman")
    monkeypatch.setattr(
        "lol.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps(payload), ""),
    )
    monkeypatch.setattr("lol.doctor.os.access", lambda path, mode: str(path) == "/run/user/1")

    finding = _podman()

    assert finding.status == "blocker"
    assert finding.summary == "rootless Podman storage is not writable"
    assert finding.evidence == "/home/user/.local/share/containers"


def test_podman_requires_storage_paths_in_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/podman")
    monkeypatch.setattr(
        "lol.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps({"host": {"security": {"rootless": True}}}), ""
        ),
    )

    finding = _podman()

    assert finding.status == "blocker"
    assert finding.summary == "Podman storage paths could not be determined"


def test_podman_timeout_is_a_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lol.doctor.shutil.which", lambda _: "/usr/bin/podman")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["podman", "info"], 10)

    monkeypatch.setattr("lol.doctor.subprocess.run", timeout)

    assert _podman().summary == "Podman diagnostic timed out"


def test_state_path_must_be_a_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.write_text("not a directory", encoding="utf-8")

    finding = _writable(state)

    assert finding.status == "blocker"
    assert finding.summary == "state path is not a directory"


def test_state_path_must_not_be_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    state = tmp_path / "state"
    state.symlink_to(real, target_is_directory=True)

    finding = _writable(state)

    assert finding.status == "blocker"
    assert finding.summary == "state path is a symbolic link"


def test_doctor_rejects_pipeline_symlink_outside_repository(
    repository: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_host_checks(monkeypatch)
    monkeypatch.setattr("lol.doctor.load_lock", lambda config: {"version": 1})
    monkeypatch.setattr("lol.doctor.verify_lock_cache", lambda lock: [])
    pipeline = repository / "Jenkinsfile"
    pipeline.unlink()
    external = tmp_path / "external-Jenkinsfile"
    external.write_text("node { sh 'unsafe' }\n", encoding="utf-8")
    pipeline.symlink_to(external)

    findings = analyze(load_effective(repository), repository / ".state")

    finding = next(item for item in findings if item.check == "pipeline.file")
    assert finding.status == "blocker"
    assert "outside the repository" in finding.summary
