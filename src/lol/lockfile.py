from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import jsonschema
import requests

from lol.cache import download_verified, sha256_file, store_verified, verify_file
from lol.config import DEFAULTS, EffectiveConfig, deep_merge, read_yaml, validate_manifest
from lol.constants import (
    LOCK_NAME,
    PINNED_JENKINS_SHA256,
    PINNED_JENKINS_URL,
    PINNED_JENKINS_VERSION,
    PLUGIN_MANAGER_SHA256,
    PLUGIN_MANAGER_URL,
    PLUGIN_MANAGER_VERSION,
)
from lol.errors import ConfigError, HarnessError
from lol.io import write_yaml
from lol.paths import AppPaths

JSON = dict[str, Any]
JENKINS_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

LOCK_SCHEMA = cast(
    JSON,
    json.loads(
        files("lol").joinpath("schemas/plugin-lock-v1.schema.json").read_text(encoding="utf-8")
    ),
)


def _repository_lock_config(config: EffectiveConfig) -> EffectiveConfig:
    """Reload the committed contract; user and in-memory overrides never define a lock."""
    repository = read_yaml(config.manifest_path)
    validate_manifest(repository, config.manifest_path)
    values = deep_merge(DEFAULTS, repository)
    if values["jenkins"]["version"] in {"pinned-lts", "lts"}:
        values["jenkins"]["version"] = PINNED_JENKINS_VERSION
    validate_manifest(values, config.manifest_path)
    return EffectiveConfig(
        root=config.root,
        manifest_path=config.manifest_path,
        values=values,
        sources={"built_in": "LOL defaults", "repository": str(config.manifest_path)},
    )


def _normalized_manifest_digest(config: EffectiveConfig) -> str:
    jenkins = cast(JSON, config.values["jenkins"])
    contract = {
        "jenkins": {
            "plugins": sorted(str(item) for item in cast(list[Any], jenkins["plugins"])),
            "version": jenkins["version"],
        },
        "schema": config.values["version"],
    }
    payload = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def normalized_manifest_digest(config: EffectiveConfig) -> str:
    return _normalized_manifest_digest(_repository_lock_config(config))


def _plugin_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise HarnessError(f"cannot inspect plugin {path}: {exc}") from exc
    unfolded = manifest.replace("\r\n ", "").replace("\n ", "")
    for line in unfolded.splitlines():
        if line.startswith("Plugin-Version:"):
            version = line.split(":", 1)[1].strip()
            if re.fullmatch(r"[A-Za-z0-9_.+-]+", version):
                return version
            raise HarnessError(f"plugin has an unsafe version: {path.name}")
    raise HarnessError(f"plugin has no Plugin-Version manifest field: {path.name}")


def _artifact_paths(cache: Path, jenkins_version: str) -> tuple[Path, Path]:
    war = cache / "jenkins" / jenkins_version / "jenkins.war"
    manager = cache / "tools" / f"jenkins-plugin-manager-{PLUGIN_MANAGER_VERSION}.jar"
    return war, manager


def _jenkins_url(version: str) -> str:
    if not JENKINS_VERSION.fullmatch(version):
        raise ConfigError("jenkins.version must be a three-part numeric release")
    return f"https://get.jenkins.io/war-stable/{version}/jenkins.war"


def _plugin_url(plugin_id: str, version: str) -> str:
    return f"https://updates.jenkins.io/download/plugins/{plugin_id}/{version}/{plugin_id}.hpi"


def _jenkins_coordinates(version: str) -> tuple[str, str]:
    if version == PINNED_JENKINS_VERSION:
        return PINNED_JENKINS_URL, PINNED_JENKINS_SHA256
    url = _jenkins_url(version)
    try:
        response = requests.get(f"{url}.sha256", timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HarnessError(f"cannot resolve checksum for Jenkins {version}: {exc}") from exc
    fields = response.text.strip().split()
    digest = fields[0].lower() if fields else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HarnessError(f"invalid checksum metadata for Jenkins {version}")
    return url, digest


def ensure_tools(config: EffectiveConfig) -> tuple[Path, Path, str, str]:
    version_value = config.values["jenkins"]["version"]
    if not isinstance(version_value, str):
        raise ConfigError("jenkins.version must be a string")
    jenkins_url, jenkins_sha256 = _jenkins_coordinates(version_value)
    cache = AppPaths.discover().cache
    war, manager = _artifact_paths(cache, version_value)
    download_verified(jenkins_url, war, jenkins_sha256)
    download_verified(PLUGIN_MANAGER_URL, manager, PLUGIN_MANAGER_SHA256)
    return war, manager, jenkins_url, jenkins_sha256


def _run_resolver(config: EffectiveConfig, war: Path, manager: Path, output: Path) -> None:
    java = shutil.which("java")
    if not java:
        raise HarnessError("Java is required to resolve Jenkins plugins")
    plugins = sorted(str(item) for item in config.values["jenkins"]["plugins"])
    command = [
        java,
        "-jar",
        str(manager),
        "--war",
        str(war),
        "--plugin-download-directory",
        str(output),
        "--clean-download-directory",
        "--latest=false",
        "--plugins",
        *plugins,
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HarnessError(f"Jenkins plugin resolution failed: {detail}")


def create_lock(config: EffectiveConfig, destination: Path | None = None) -> JSON:
    destination = destination or config.root / LOCK_NAME
    config = _repository_lock_config(config)
    war, manager, jenkins_url, jenkins_sha256 = ensure_tools(config)
    requested_list = [str(item).split(":", 1)[0] for item in config.values["jenkins"]["plugins"]]
    requested = set(requested_list)
    if len(requested) != len(requested_list):
        raise ConfigError("jenkins.plugins must not request one plugin ID more than once")
    plugin_cache = AppPaths.discover().cache / "plugins"
    with tempfile.TemporaryDirectory(prefix="lol-plugins-") as temporary:
        output = Path(temporary)
        _run_resolver(config, war, manager, output)
        plugins: list[JSON] = []
        resolved: set[str] = set()
        for artifact in sorted((*output.glob("*.jpi"), *output.glob("*.hpi"))):
            plugin_id = artifact.stem
            if plugin_id in resolved:
                raise HarnessError(f"plugin resolver returned duplicate artifact: {plugin_id}")
            resolved.add(plugin_id)
            version = _plugin_version(artifact)
            digest = sha256_file(artifact)
            cached = plugin_cache / plugin_id / version / f"{plugin_id}.jpi"
            store_verified(artifact, cached, digest)
            plugins.append(
                {
                    "id": plugin_id,
                    "version": version,
                    "requested": plugin_id in requested,
                    "url": _plugin_url(plugin_id, version),
                    "sha256": digest,
                }
            )
        missing = requested - resolved
        if missing:
            raise HarnessError(
                "plugin resolver did not return requested artifacts: " + ", ".join(sorted(missing))
            )
    lock: JSON = {
        "version": 1,
        "manifest_digest": _normalized_manifest_digest(config),
        "jenkins": {
            "version": config.values["jenkins"]["version"],
            "url": jenkins_url,
            "sha256": jenkins_sha256,
        },
        "resolver": {
            "version": PLUGIN_MANAGER_VERSION,
            "url": PLUGIN_MANAGER_URL,
            "sha256": PLUGIN_MANAGER_SHA256,
        },
        "plugins": sorted(plugins, key=lambda item: str(item["id"])),
    }
    _validate_lock_schema(lock, destination)
    write_yaml(destination, lock)
    return lock


def _validate_lock_schema(lock: JSON, path: Path | None = None) -> None:
    try:
        jsonschema.Draft202012Validator(LOCK_SCHEMA).validate(lock)
    except jsonschema.ValidationError as exc:
        where = ".".join(str(part) for part in exc.absolute_path) or "root"
        prefix = f"{path}: " if path else ""
        raise ConfigError(f"{prefix}{where}: {exc.message}") from exc


def load_lock(config: EffectiveConfig, *, verify_drift: bool = True) -> JSON:
    config = _repository_lock_config(config)
    path = config.root / LOCK_NAME
    lock = read_yaml(path)
    _validate_lock_schema(lock, path)
    if verify_drift and lock["manifest_digest"] != _normalized_manifest_digest(config):
        raise ConfigError("plugin lock does not match lol.yaml; run `lol lock`")
    jenkins = cast(JSON, lock["jenkins"])
    jenkins_version = str(jenkins["version"])
    if jenkins_version != str(config.values["jenkins"]["version"]):
        raise ConfigError("plugin lock Jenkins version does not match lol.yaml; run `lol lock`")
    expected_jenkins_url = _jenkins_url(jenkins_version)
    if str(jenkins["url"]) != expected_jenkins_url:
        raise ConfigError("plugin lock Jenkins URL is not canonical")
    if (
        jenkins_version == PINNED_JENKINS_VERSION
        and str(jenkins["sha256"]) != PINNED_JENKINS_SHA256
    ):
        raise ConfigError("plugin lock Jenkins artifact does not match this LOL release")
    resolver = cast(JSON, lock["resolver"])
    if (
        str(resolver["version"]) != PLUGIN_MANAGER_VERSION
        or str(resolver["url"]) != PLUGIN_MANAGER_URL
        or str(resolver["sha256"]) != PLUGIN_MANAGER_SHA256
    ):
        raise ConfigError("plugin lock resolver artifact does not match this LOL release")
    plugin_items = cast(list[JSON], lock["plugins"])
    identifiers = [str(item["id"]) for item in plugin_items]
    if identifiers != sorted(set(identifiers)):
        raise ConfigError("plugin lock IDs must be unique and sorted")
    for item in plugin_items:
        expected_url = _plugin_url(str(item["id"]), str(item["version"]))
        if str(item["url"]) != expected_url:
            raise ConfigError(f"plugin lock URL is not canonical: {item['id']}")
    requested = {str(item).split(":", 1)[0] for item in config.values["jenkins"]["plugins"]}
    locked_requested = {str(item["id"]) for item in plugin_items if item["requested"]}
    if requested != locked_requested:
        raise ConfigError(
            "plugin lock requested-plugin markers do not match lol.yaml; run `lol lock`"
        )
    return lock


def verify_lock_cache(lock: JSON) -> list[str]:
    missing: list[str] = []
    cache = AppPaths.discover().cache
    jenkins = cast(JSON, lock["jenkins"])
    resolver = cast(JSON, lock["resolver"])
    war, manager = _artifact_paths(cache, str(jenkins["version"]))
    if not verify_file(war, str(jenkins["sha256"])):
        missing.append("Jenkins WAR")
    if not verify_file(manager, str(resolver["sha256"])):
        missing.append("plugin manager")
    for item in cast(list[JSON], lock["plugins"]):
        path = cache / "plugins" / item["id"] / item["version"] / f"{item['id']}.jpi"
        if not verify_file(path, str(item["sha256"])):
            missing.append(f"plugin {item['id']}:{item['version']}")
    return missing


def ensure_lock_cache(lock: JSON) -> None:
    cache = AppPaths.discover().cache
    jenkins = cast(JSON, lock["jenkins"])
    resolver = cast(JSON, lock["resolver"])
    war, manager = _artifact_paths(cache, str(jenkins["version"]))
    download_verified(str(jenkins["url"]), war, str(jenkins["sha256"]))
    download_verified(str(resolver["url"]), manager, str(resolver["sha256"]))
    for item in cast(list[JSON], lock["plugins"]):
        destination = (
            cache / "plugins" / str(item["id"]) / str(item["version"]) / f"{item['id']}.jpi"
        )
        download_verified(str(item["url"]), destination, str(item["sha256"]))
