from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lol.constants import DEFAULT_PLUGINS, PINNED_JENKINS_VERSION
from lol.errors import ConfigError

LABEL_PATTERNS = (
    re.compile(r"agent\s*\{\s*label\s+['\"]([^'\"]+)['\"]", re.DOTALL),
    re.compile(r"node\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(
        r"agent\s*\{\s*node\s*\{[^{}]*?\blabel\s+['\"]([^'\"]+)['\"]",
        re.DOTALL,
    ),
)
LABEL_TOKEN = re.compile(r"!|[()]|[A-Za-z0-9_.-]+")
IGNORED_DISCOVERY_DIRECTORIES = frozenset({".git", "node_modules"})


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
    if any(marker in expression for marker in ("$", "{", "}")):
        return set()
    labels: set[str] = set()
    negation_stack = [False]
    pending_negation = False
    for match in LABEL_TOKEN.finditer(expression):
        token = match.group()
        if token == "!":
            pending_negation = not pending_negation
        elif token == "(":
            negation_stack.append(negation_stack[-1] ^ pending_negation)
            pending_negation = False
        elif token == ")":
            if len(negation_stack) > 1:
                negation_stack.pop()
            pending_negation = False
        else:
            if not (negation_stack[-1] ^ pending_negation):
                labels.add(token)
            pending_negation = False
    return labels


def _jenkinsfiles(root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []

    def walk_error(exc: OSError) -> None:
        raise ConfigError(f"cannot inspect repository: {exc}") from exc

    for directory, names, files in os.walk(root, topdown=True, onerror=walk_error):
        names[:] = sorted(name for name in names if name not in IGNORED_DISCOVERY_DIRECTORIES)
        for name in sorted(files):
            if not name.startswith("Jenkinsfile"):
                continue
            path = Path(directory) / name
            if path.is_file() and _inside(root, path):
                candidates.append(path.relative_to(root))
    return tuple(sorted(candidates))


def discover(root: Path) -> Discovery:
    jenkinsfiles = _jenkinsfiles(root)
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
