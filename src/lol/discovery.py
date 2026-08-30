from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lol.constants import DEFAULT_PLUGINS, PINNED_JENKINS_VERSION
from lol.errors import ConfigError

LABEL_PATTERNS = (
    re.compile(r"agent\s*\{\s*label\s+['\"]([^'\"]+)['\"]", re.DOTALL),
    re.compile(r"node\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
)
LABEL_TOKEN = re.compile(r"(?P<negated>!\s*)?(?P<label>[A-Za-z0-9_.-]+)")


@dataclass(frozen=True, slots=True)
class Discovery:
    jenkinsfiles: tuple[Path, ...]
    labels: tuple[str, ...]
    podman: bool


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _labels(expression: str) -> set[str]:
    return {
        match.group("label")
        for match in LABEL_TOKEN.finditer(expression)
        if match.group("negated") is None
    }


def discover(root: Path) -> Discovery:
    candidates = (
        path
        for path in root.rglob("Jenkinsfile*")
        if ".git" not in path.relative_to(root).parts and path.is_file() and _inside(root, path)
    )
    jenkinsfiles = tuple(sorted(path.relative_to(root) for path in candidates))
    labels: set[str] = set()
    podman = False
    for relative in jenkinsfiles:
        try:
            text = (root / relative).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConfigError(f"cannot read {relative}: {exc}") from exc
        podman = podman or bool(re.search(r"\bpodman\b", text))
        for pattern in LABEL_PATTERNS:
            for match in pattern.finditer(text):
                labels.update(_labels(match.group(1)))
    return Discovery(
        jenkinsfiles=jenkinsfiles,
        labels=tuple(sorted(labels)),
        podman=podman,
    )


def initial_manifest(
    pipeline_file: Path,
    *,
    jenkins_version: str = PINNED_JENKINS_VERSION,
    labels: tuple[str, ...] = (),
    require_podman: bool = False,
    executors: int = 1,
    commands: tuple[str, ...] = ("git",),
) -> dict[str, Any]:
    if pipeline_file.is_absolute() or ".." in pipeline_file.parts:
        raise ConfigError("Jenkinsfile must be repository-relative")
    return {
        "version": 1,
        "jenkins": {
            "version": (
                PINNED_JENKINS_VERSION
                if jenkins_version in {"lts", "pinned-lts"}
                else jenkins_version
            ),
            "plugins": list(DEFAULT_PLUGINS),
        },
        "pipeline": {"file": pipeline_file.as_posix(), "parameters": {}},
        "node": {
            "executors": executors,
            "labels": sorted({"lol-local", *labels}),
        },
        "requirements": {
            "commands": sorted({"git", *commands}),
            "podman": require_podman,
        },
        "environment": {"pass": [], "set": {}},
        "artifacts": {"patterns": []},
    }
