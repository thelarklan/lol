from __future__ import annotations

import copy
import hashlib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from lol.cache import download_verified, sha256_file, store_verified, verify_file
from lol.config import ConfigError, EffectiveConfig, load_effective
from lol.constants import (
    PINNED_JENKINS_SHA256,
    PINNED_JENKINS_URL,
    PINNED_JENKINS_VERSION,
    PLUGIN_MANAGER_SHA256,
    PLUGIN_MANAGER_URL,
    PLUGIN_MANAGER_VERSION,
)
from lol.errors import HarnessError
from lol.io import write_yaml
from lol.lockfile import (
    LOCK_SCHEMA,
    _jenkins_coordinates,
    _plugin_version,
    _repository_lock_config,
    create_lock,
    ensure_lock_cache,
    load_lock,
    normalized_manifest_digest,
    verify_lock_cache,
)


class Response:
    status_code = 200

    def __init__(self, content: bytes = b"content", text: str = "") -> None:
        self.content = content
        self.text = text

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, _: int) -> Iterator[bytes]:
        yield self.content


def _valid_lock(config: EffectiveConfig) -> dict[str, Any]:
    plugins = [
        {
            "id": item.split(":", 1)[0],
            "version": "1.0",
            "requested": True,
            "url": (
                "https://updates.jenkins.io/download/plugins/"
                f"{item.split(':', 1)[0]}/1.0/{item.split(':', 1)[0]}.hpi"
            ),
            "sha256": "0" * 64,
        }
        for item in sorted(config.values["jenkins"]["plugins"])
    ]
    return {
        "version": 1,
        "manifest_digest": normalized_manifest_digest(config),
        "jenkins": {
            "version": PINNED_JENKINS_VERSION,
            "url": PINNED_JENKINS_URL,
            "sha256": PINNED_JENKINS_SHA256,
        },
        "resolver": {
            "version": PLUGIN_MANAGER_VERSION,
            "url": PLUGIN_MANAGER_URL,
            "sha256": PLUGIN_MANAGER_SHA256,
        },
        "plugins": plugins,
    }


def _write_plugin(path: Path, version: str = "1.0") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\r\nPlugin-Version: {version}\r\n",
        )


def test_download_verified_reuses_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cache" / "artifact"
    digest = hashlib.sha256(b"content").hexdigest()
    calls = 0

    def get(*_: object, **__: object) -> Response:
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr("lol.cache.requests.get", get)
    assert download_verified("https://example.invalid/artifact", target, digest) == target
    assert download_verified("https://example.invalid/artifact", target, digest) == target
    assert calls == 1
    assert verify_file(target, digest)
    assert sha256_file(target) == digest


def test_download_replaces_corrupt_cache_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cache" / "artifact"
    target.parent.mkdir()
    target.write_bytes(b"corrupt")
    digest = hashlib.sha256(b"content").hexdigest()
    monkeypatch.setattr("lol.cache.requests.get", lambda *_args, **_kwargs: Response())

    download_verified("https://example.invalid/artifact", target, digest)

    assert target.read_bytes() == b"content"
    assert not list(target.parent.glob(".download-*"))


def test_download_replaces_cache_symlink_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    external = tmp_path / "external"
    external.write_bytes(b"content")
    target = tmp_path / "cache" / "artifact"
    target.parent.mkdir()
    target.symlink_to(external)
    digest = hashlib.sha256(b"content").hexdigest()
    monkeypatch.setattr("lol.cache.requests.get", lambda *_args, **_kwargs: Response())

    download_verified("https://example.invalid/artifact", target, digest)

    assert target.read_bytes() == b"content"
    assert not target.is_symlink()
    assert external.read_bytes() == b"content"


def test_download_rejects_checksum_mismatch_without_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "artifact"
    monkeypatch.setattr("lol.cache.requests.get", lambda *_args, **_kwargs: Response())

    with pytest.raises(HarnessError, match="checksum mismatch"):
        download_verified("https://example.invalid/artifact", target, "0" * 64)

    assert not target.exists()
    assert not list(tmp_path.glob(".download-*"))


def test_download_wraps_transport_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def get(*_: object, **__: object) -> Response:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr("lol.cache.requests.get", get)
    with pytest.raises(HarnessError, match="download failed.*offline"):
        download_verified("https://example.invalid/artifact", tmp_path / "artifact", "0" * 64)


def test_download_rejects_invalid_expected_digest(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="invalid SHA-256"):
        download_verified("https://example.invalid/artifact", tmp_path / "artifact", "not-a-hash")


def test_store_verified_is_atomic_and_rejects_bad_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "cache" / "artifact"
    source.write_bytes(b"content")
    digest = hashlib.sha256(b"content").hexdigest()

    assert store_verified(source, destination, digest) == destination
    assert destination.read_bytes() == b"content"
    assert not list(destination.parent.glob(".cache-*"))

    source.write_bytes(b"tampered")
    with pytest.raises(HarnessError, match="local artifact"):
        store_verified(source, tmp_path / "other", digest)


def test_manifest_lock_digest_tracks_only_the_jenkins_contract(repository: Path) -> None:
    config = load_effective(repository)
    base_digest = normalized_manifest_digest(config)
    repository_values = copy.deepcopy(config.values)

    values = copy.deepcopy(repository_values)
    values["node"]["labels"].append("extra")
    write_yaml(config.manifest_path, values)
    assert normalized_manifest_digest(load_effective(repository)) == base_digest

    values = copy.deepcopy(repository_values)
    values["jenkins"]["plugins"].append("mailer")
    write_yaml(config.manifest_path, values)
    assert normalized_manifest_digest(load_effective(repository)) != base_digest

    values = copy.deepcopy(repository_values)
    values["jenkins"]["plugins"].reverse()
    write_yaml(config.manifest_path, values)
    assert normalized_manifest_digest(load_effective(repository)) == base_digest


def test_user_jenkins_overrides_do_not_invalidate_repository_lock(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_config = load_effective(repository)
    expected = _valid_lock(repository_config)
    write_yaml(repository / "lol.plugins.lock.yaml", expected)
    user = tmp_path / "config" / "lol"
    user.mkdir(parents=True)
    write_yaml(user / "config.yaml", {"jenkins": {"plugins": ["git", "junit"]}})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    effective = load_effective(repository)

    assert effective.values["jenkins"]["plugins"] == ["git", "junit"]
    assert normalized_manifest_digest(effective) == expected["manifest_digest"]
    assert load_lock(effective) == expected
    repository_only = _repository_lock_config(effective)
    assert repository_only.values["jenkins"] == repository_config.values["jenkins"]


def test_packaged_lock_schema_is_the_validation_source() -> None:
    assert (
        LOCK_SCHEMA["$id"] == "https://thelarklan.github.io/lol/schemas/plugin-lock-v1.schema.json"
    )
    assert LOCK_SCHEMA["properties"]["plugins"]["items"]["additionalProperties"] is False


def test_load_lock_accepts_valid_lock(repository: Path) -> None:
    config = load_effective(repository)
    expected = _valid_lock(config)
    write_yaml(repository / "lol.plugins.lock.yaml", expected)
    assert load_lock(config) == expected


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda lock: lock.update(manifest_digest="f" * 64), "does not match lol.yaml"),
        (
            lambda lock: lock["resolver"].update(url="https://example.invalid/manager.jar"),
            "resolver artifact",
        ),
        (lambda lock: lock["jenkins"].update(sha256="f" * 64), "Jenkins artifact"),
        (
            lambda lock: lock["plugins"][0].update(url="https://example.invalid/plugin.hpi"),
            "URL is not canonical",
        ),
        (lambda lock: lock["plugins"][0].update(requested=False), "requested-plugin markers"),
        (lambda lock: lock["plugins"].reverse(), "unique and sorted"),
    ],
)
def test_load_lock_rejects_drift_and_tampering(
    repository: Path,
    mutation: Any,
    message: str,
) -> None:
    config = load_effective(repository)
    lock = _valid_lock(config)
    mutation(lock)
    write_yaml(repository / "lol.plugins.lock.yaml", lock)
    with pytest.raises(ConfigError, match=message):
        load_lock(config)


def test_load_lock_rejects_unknown_fields(repository: Path) -> None:
    config = load_effective(repository)
    lock = _valid_lock(config)
    lock["unexpected"] = True
    write_yaml(repository / "lol.plugins.lock.yaml", lock)
    with pytest.raises(ConfigError, match="Additional properties"):
        load_lock(config)


def test_custom_jenkins_checksum_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = "a" * 64
    monkeypatch.setattr(
        "lol.lockfile.requests.get",
        lambda *_args, **_kwargs: Response(text=f"{digest}  jenkins.war"),
    )
    url, resolved = _jenkins_coordinates("2.567.0")
    assert url == "https://get.jenkins.io/war-stable/2.567.0/jenkins.war"
    assert resolved == digest
    with pytest.raises(ConfigError, match="three-part numeric"):
        _jenkins_coordinates("../../latest")


def test_plugin_version_reads_folded_manifest(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.jpi"
    with zipfile.ZipFile(plugin, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            "Manifest-Version: 1.0\r\nPlugin-Version: 1.2.\r\n 3\r\n",
        )
    assert _plugin_version(plugin) == "1.2.3"


def test_create_lock_caches_sorted_resolver_output(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    war = tmp_path / "jenkins.war"
    manager = tmp_path / "manager.jar"
    war.write_bytes(b"war")
    manager.write_bytes(b"manager")
    monkeypatch.setattr(
        "lol.lockfile.ensure_tools",
        lambda _config: (
            war,
            manager,
            PINNED_JENKINS_URL,
            PINNED_JENKINS_SHA256,
        ),
    )

    def resolve(_config: EffectiveConfig, _war: Path, _manager: Path, output: Path) -> None:
        for plugin_id in reversed([*config.values["jenkins"]["plugins"], "structs"]):
            _write_plugin(output / f"{plugin_id}.jpi")

    monkeypatch.setattr("lol.lockfile._run_resolver", resolve)
    destination = tmp_path / "lock.yaml"
    lock = create_lock(config, destination)

    identifiers = [item["id"] for item in lock["plugins"]]
    assert identifiers == sorted(identifiers)
    assert {item["id"] for item in lock["plugins"] if item["requested"]} == set(
        config.values["jenkins"]["plugins"]
    )
    assert next(item for item in lock["plugins"] if item["id"] == "structs")["requested"] is False
    for item in lock["plugins"]:
        cached = cache_root / "lol" / "plugins" / item["id"] / item["version"] / f"{item['id']}.jpi"
        assert verify_file(cached, item["sha256"])
    assert destination.is_file()


def test_create_lock_rejects_missing_requested_artifact(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_effective(repository)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(
        "lol.lockfile.ensure_tools",
        lambda _config: (
            tmp_path / "jenkins.war",
            tmp_path / "manager.jar",
            PINNED_JENKINS_URL,
            PINNED_JENKINS_SHA256,
        ),
    )
    monkeypatch.setattr("lol.lockfile._run_resolver", lambda *_args: None)
    with pytest.raises(HarnessError, match="did not return requested artifacts"):
        create_lock(config, tmp_path / "lock.yaml")


def test_verify_and_populate_lock_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-root"))
    contents = {"jenkins": b"war", "manager": b"manager", "plugin": b"plugin"}
    lock: dict[str, Any] = {
        "jenkins": {
            "version": "1.2.3",
            "url": "https://example.invalid/jenkins",
            "sha256": hashlib.sha256(contents["jenkins"]).hexdigest(),
        },
        "resolver": {
            "version": PLUGIN_MANAGER_VERSION,
            "url": "https://example.invalid/manager",
            "sha256": hashlib.sha256(contents["manager"]).hexdigest(),
        },
        "plugins": [
            {
                "id": "example",
                "version": "1.0",
                "requested": True,
                "url": "https://example.invalid/plugin",
                "sha256": hashlib.sha256(contents["plugin"]).hexdigest(),
            }
        ],
    }
    assert verify_lock_cache(lock) == [
        "Jenkins WAR",
        "plugin manager",
        "plugin example:1.0",
    ]

    def download(url: str, destination: Path, _digest: str) -> Path:
        key = url.rsplit("/", 1)[-1]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents[key])
        return destination

    monkeypatch.setattr("lol.lockfile.download_verified", download)
    ensure_lock_cache(lock)
    assert verify_lock_cache(lock) == []
