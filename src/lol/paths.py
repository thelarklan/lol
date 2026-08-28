from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _xdg(name: str, fallback: str) -> Path:
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser()
    return Path.home() / fallback


@dataclass(frozen=True, slots=True)
class AppPaths:
    config: Path
    state: Path
    cache: Path

    @classmethod
    def discover(cls) -> AppPaths:
        return cls(
            config=_xdg("XDG_CONFIG_HOME", ".config") / "lol",
            state=_xdg("XDG_STATE_HOME", ".local/state") / "lol",
            cache=_xdg("XDG_CACHE_HOME", ".cache") / "lol",
        )


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    root: Path
    state: Path
    controller: Path
    jenkins_home: Path
    runtime: Path
    runs: Path

    @classmethod
    def from_id(
        cls, project_root: Path, project_id: str, app: AppPaths | None = None
    ) -> ProjectPaths:
        app = app or AppPaths.discover()
        state = app.state / project_id
        return cls(
            root=project_root,
            state=state,
            controller=state / "controller",
            jenkins_home=state / "controller" / "jenkins-home",
            runtime=state / "runtime",
            runs=state / "runs",
        )
