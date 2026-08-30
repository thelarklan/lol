from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from lol.errors import HarnessError


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HarnessError(f"{' '.join(command)} failed: {detail}")
    return result.stdout.strip()


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(slots=True)
class Snapshot:
    repository: Path
    commit: str
    tree: str
    branch: str = "lol-run"
    process: subprocess.Popen[bytes] | None = None
    url: str | None = None

    def serve(self, timeout: float = 5.0) -> str:
        if self.process and self.process.poll() is None and self.url:
            return self.url
        port = _port()
        base = self.repository.parent
        log = (base / "git-daemon.log").open("ab", buffering=0)
        command = [
            "git",
            "daemon",
            "--reuseaddr",
            "--export-all",
            "--strict-paths",
            f"--base-path={base}",
            "--listen=127.0.0.1",
            f"--port={port}",
            str(self.repository),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HarnessError("run-scoped Git daemon exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    self.url = f"git://127.0.0.1:{port}/{self.repository.name}"
                    return self.url
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise HarnessError("run-scoped Git daemon did not become ready")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def __enter__(self) -> Snapshot:
        self.serve()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def create_snapshot(root: Path, run_dir: Path, revision: str | None = None) -> Snapshot:
    if (root / ".gitmodules").exists():
        raise HarnessError("Git submodules are unsupported in v1 snapshots")
    attributes = root / ".gitattributes"
    if attributes.exists() and "filter=lfs" in attributes.read_text(errors="replace"):
        raise HarnessError("Git LFS hydration is unsupported in v1 snapshots")
    run_dir.mkdir(parents=True, exist_ok=True)
    repository = run_dir / "snapshot.git"
    if repository.exists():
        shutil.rmtree(repository)
    _run(["git", "clone", "--bare", "--no-hardlinks", str(root), str(repository)])
    selected = revision or "HEAD"
    base = _run(["git", "-C", str(root), "rev-parse", f"{selected}^{{commit}}"])
    if revision:
        commit = base
        tree = _run(["git", f"--git-dir={repository}", "show", "-s", "--format=%T", commit])
    else:
        with tempfile.NamedTemporaryFile(prefix="lol-index-", dir=run_dir, delete=False) as handle:
            index = Path(handle.name)
        index.unlink(missing_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "GIT_DIR": str(repository),
                "GIT_WORK_TREE": str(root),
                "GIT_INDEX_FILE": str(index),
            }
        )
        try:
            _run(["git", "read-tree", base], env=env)
            _run(["git", "add", "-A", "--", "."], cwd=root, env=env)
            tree = _run(["git", "write-tree"], env=env)
            commit_env = env | {
                "GIT_AUTHOR_NAME": "LOL Snapshot",
                "GIT_AUTHOR_EMAIL": "lol@localhost",
                "GIT_COMMITTER_NAME": "LOL Snapshot",
                "GIT_COMMITTER_EMAIL": "lol@localhost",
            }
            commit = _run(
                ["git", "commit-tree", tree, "-p", base, "-m", "LOL run snapshot"],
                env=commit_env,
            )
        finally:
            index.unlink(missing_ok=True)
    _run(["git", f"--git-dir={repository}", "update-ref", "refs/heads/lol-run", commit])
    _run(["git", f"--git-dir={repository}", "symbolic-ref", "HEAD", "refs/heads/lol-run"])
    (repository / "git-daemon-export-ok").touch(mode=0o600)
    return Snapshot(repository=repository, commit=commit, tree=tree)
