from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest
from conftest import run_git

from lol.errors import HarnessError
from lol.snapshot import create_snapshot


def snapshot_file(snapshot: Path, commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", f"--git-dir={snapshot}", "show", f"{commit}:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def test_working_tree_snapshot_preserves_changes_without_touching_index(
    repository: Path, tmp_path: Path
) -> None:
    index_before = run_git(repository, "write-tree")
    branch_before = run_git(repository, "branch", "--show-current")
    (repository / "Jenkinsfile").write_text("changed\n", encoding="utf-8")
    executable = repository / "run.sh"
    executable.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
    executable.chmod(0o755)
    os.symlink("run.sh", repository / "run-link")
    (repository / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    snapshot = create_snapshot(repository, tmp_path / "run")

    assert snapshot_file(snapshot.repository, snapshot.commit, "Jenkinsfile") == b"changed\n"
    assert snapshot_file(snapshot.repository, snapshot.commit, "run.sh").startswith(b"#!/bin/sh")
    assert snapshot_file(snapshot.repository, snapshot.commit, "run-link") == b"run.sh"
    tree = run_git(snapshot.repository, "ls-tree", snapshot.commit, "run.sh")
    assert tree.startswith("100755")
    missing = subprocess.run(
        [
            "git",
            f"--git-dir={snapshot.repository}",
            "cat-file",
            "-e",
            f"{snapshot.commit}:ignored.txt",
        ],
        check=False,
    )
    assert missing.returncode != 0
    assert run_git(repository, "write-tree") == index_before
    assert run_git(repository, "branch", "--show-current") == branch_before
    source_has_commit = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", snapshot.commit], check=False
    )
    assert source_has_commit.returncode != 0


def test_revision_snapshot_excludes_working_changes(repository: Path, tmp_path: Path) -> None:
    (repository / "Jenkinsfile").write_text("changed\n", encoding="utf-8")
    snapshot = create_snapshot(repository, tmp_path / "run", "HEAD")
    assert b"pipeline" in snapshot_file(snapshot.repository, snapshot.commit, "Jenkinsfile")


def test_working_tree_snapshot_is_deterministic(repository: Path, tmp_path: Path) -> None:
    (repository / "Jenkinsfile").write_text("changed\n", encoding="utf-8")

    first = create_snapshot(repository, tmp_path / "first")
    second = create_snapshot(repository, tmp_path / "second")

    assert first.tree == second.tree
    assert first.commit == second.commit


def test_snapshot_exports_only_the_run_branch(repository: Path, tmp_path: Path) -> None:
    run_git(repository, "branch", "private-branch")
    run_git(repository, "tag", "private-tag")

    snapshot = create_snapshot(repository, tmp_path / "run")

    refs = run_git(snapshot.repository, "for-each-ref", "--format=%(refname)").splitlines()
    assert refs == ["refs/heads/lol-run"]


def test_snapshot_git_daemon_is_loopback_only(repository: Path, tmp_path: Path) -> None:
    snapshot = create_snapshot(repository, tmp_path / "run", "HEAD")
    try:
        try:
            url = snapshot.serve()
        except PermissionError:
            pytest.skip("test sandbox does not permit loopback sockets")
        assert url.startswith("git://127.0.0.1:")
        assert snapshot.process is not None and snapshot.process.poll() is None
        checkout = tmp_path / "checkout"
        subprocess.run(
            ["git", "clone", "--branch", snapshot.branch, "--single-branch", url, str(checkout)],
            check=True,
            capture_output=True,
        )
        assert b"pipeline" in (checkout / "Jenkinsfile").read_bytes()
        assert run_git(checkout, "rev-parse", "HEAD") == snapshot.commit
    finally:
        snapshot.stop()
    assert snapshot.process is None


def test_snapshot_git_daemon_retries_a_port_race(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = create_snapshot(repository, tmp_path / "run", "HEAD")
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    occupied.listen()
    blocked_port = int(occupied.getsockname()[1])
    ports = iter([blocked_port, 0])

    def port() -> int:
        selected = next(ports)
        if selected:
            return selected
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    monkeypatch.setattr("lol.snapshot._port", port)
    try:
        assert snapshot.serve().startswith("git://127.0.0.1:")
    finally:
        occupied.close()
        snapshot.stop()


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        (".gitmodules", "Git submodules are unsupported"),
        (".gitattributes", "Git LFS hydration is unsupported"),
    ],
)
def test_snapshot_rejects_unsupported_git_features(
    repository: Path, tmp_path: Path, marker: str, message: str
) -> None:
    content = "[submodule]\n" if marker == ".gitmodules" else "*.bin filter=lfs\n"
    (repository / marker).write_text(content, encoding="utf-8")

    with pytest.raises(HarnessError, match=message):
        create_snapshot(repository, tmp_path / "run")


def test_snapshot_rejects_nested_lfs_attributes(repository: Path, tmp_path: Path) -> None:
    nested = repository / "assets"
    nested.mkdir()
    (nested / ".gitattributes").write_text("*.bin filter=lfs diff=lfs\n", encoding="utf-8")
    (nested / "payload.bin").write_bytes(b"content")

    with pytest.raises(HarnessError, match="Git LFS hydration is unsupported"):
        create_snapshot(repository, tmp_path / "run")


def test_snapshot_ignores_commented_lfs_attributes(repository: Path, tmp_path: Path) -> None:
    (repository / ".gitattributes").write_text("# *.bin filter=lfs diff=lfs\n", encoding="utf-8")

    snapshot = create_snapshot(repository, tmp_path / "run")

    assert snapshot.repository.is_dir()
