from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from lol.config import DEFAULTS


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "LOL Tests")
    run_git(root, "config", "user.email", "lol@example.invalid")
    (root / "Jenkinsfile").write_text(
        "pipeline { agent any; stages { stage('test') { steps { sh 'true' } } } }\n",
        encoding="utf-8",
    )
    (root / "lol.yaml").write_text(yaml.safe_dump(DEFAULTS, sort_keys=False), encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "fixture")
    return root
