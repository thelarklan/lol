from __future__ import annotations

import os
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
