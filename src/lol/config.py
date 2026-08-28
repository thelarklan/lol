from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import jsonschema
import yaml

from lol.constants import (
    DEFAULT_PLUGINS,
    MANIFEST_NAME,
    PINNED_JENKINS_VERSION,
    SCHEMA_VERSION,
)
from lol.errors import ConfigError
from lol.paths import AppPaths
from lol.project import find_repository

JSON = dict[str, Any]
__all__ = [
    "ConfigError",
    "DEFAULTS",
    "EffectiveConfig",
    "deep_merge",
    "dump_yaml",
    "load_effective",
    "read_yaml",
    "redact",
    "validate_manifest",
]
SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|credential|api[_-]?key)")

MANIFEST_SCHEMA = cast(
    JSON,
    json.loads(files("lol").joinpath("schemas/lol-v1.schema.json").read_text(encoding="utf-8")),
)

DEFAULTS: JSON = {
    "version": SCHEMA_VERSION,
    "jenkins": {"version": PINNED_JENKINS_VERSION, "plugins": list(DEFAULT_PLUGINS)},
    "pipeline": {"file": "Jenkinsfile", "parameters": {}},
    "node": {"executors": 1, "labels": ["lol-local"]},
    "requirements": {"commands": ["git"], "podman": False},
    "environment": {"pass": [], "set": {}},
    "artifacts": {"patterns": []},
}


def deep_merge(base: JSON, overlay: Mapping[str, Any]) -> JSON:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(dict(result[key]), value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_yaml(path: Path, *, required: bool = True) -> JSON:
    if not path.exists():
        if required:
            raise ConfigError(f"configuration file not found: {path}")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return loaded


def validate_manifest(config: JSON, path: Path | None = None) -> None:
    try:
        jsonschema.Draft202012Validator(MANIFEST_SCHEMA).validate(config)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(part) for part in exc.absolute_path) or "root"
        prefix = f"{path}: " if path else ""
        raise ConfigError(f"{prefix}{where}: {exc.message}") from exc
    pipeline = Path(str(config["pipeline"]["file"]))
    if pipeline.is_absolute() or ".." in pipeline.parts:
        raise ConfigError("pipeline.file must be a repository-relative path without '..'")


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    root: Path
    manifest_path: Path
    values: JSON
    sources: dict[str, str]

    @property
    def digest(self) -> str:
        payload = json.dumps(self.values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_effective(
    start: Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    require_manifest: bool = True,
) -> EffectiveConfig:
    root = find_repository(start)
    manifest_path = root / MANIFEST_NAME
    project = read_yaml(manifest_path, required=require_manifest)
    if manifest_path.exists():
        validate_manifest(project, manifest_path)
    user_path = AppPaths.discover().config / "config.yaml"
    user = read_yaml(user_path, required=False)
    values = deep_merge(DEFAULTS, project)
    values = deep_merge(values, user)
    if overrides:
        values = deep_merge(values, overrides)
    if values["jenkins"]["version"] in {"pinned-lts", "lts"}:
        values["jenkins"]["version"] = PINNED_JENKINS_VERSION
    validate_manifest(values)
    return EffectiveConfig(
        root=root,
        manifest_path=manifest_path,
        values=values,
        sources={
            "built_in": "LOL defaults",
            "repository": str(manifest_path) if manifest_path.exists() else "not present",
            "user": str(user_path) if user_path.exists() else "not present",
            "cli": "provided" if overrides else "not provided",
        },
    )


def redact(value: Any, key: str = "") -> Any:
    if SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value


def dump_yaml(value: Any) -> str:
    return yaml.safe_dump(value, sort_keys=False, default_flow_style=False)
