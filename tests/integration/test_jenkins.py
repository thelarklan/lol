from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import requests
import yaml
from click.testing import CliRunner

from lol.cli import cli
from lol.config import DEFAULTS, load_effective
from lol.controller import credentials as controller_credentials
from lol.controller import down
from lol.controller import reset as reset_controller
from lol.controller import status as controller_status
from lol.jenkins import JenkinsClient
from lol.lockfile import create_lock
from lol.paths import ProjectPaths
from lol.project import project_identity
from lol.runner import TemporaryCredential, execute
from lol.runs import RunRecord, list_runs

pytestmark = pytest.mark.integration


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _require_rootless_podman() -> str:
    executable = shutil.which("podman")
    if not executable:
        pytest.fail("LOL_PODMAN_INTEGRATION requires podman")
    result = subprocess.run(
        [executable, "info", "--format", "{{.Host.Security.Rootless}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode or result.stdout.strip().lower() != "true":
        pytest.fail(f"rootless Podman is unavailable: {result.stderr.strip()}")
    return executable


def _prepare_podman_image(tmp_path: Path) -> tuple[str, str]:
    podman = _require_rootless_podman()
    busybox = shutil.which("busybox")
    if not busybox:
        pytest.fail("LOL_PODMAN_INTEGRATION requires static busybox")
    context = tmp_path / "podman-image"
    context.mkdir()
    shutil.copy2(busybox, context / "busybox")
    (context / "Containerfile").write_text(
        'FROM scratch\nCOPY busybox /busybox\nENTRYPOINT ["/busybox"]\n',
        encoding="utf-8",
    )
    image = "localhost/lol-fixture:latest"
    subprocess.run(
        [podman, "build", "--tag", image, str(context)],
        check=True,
        capture_output=True,
        text=True,
    )
    return podman, image


@pytest.mark.skipif(
    not os.environ.get("LOL_JENKINS_INTEGRATION") or not shutil.which("java"),
    reason="set LOL_JENKINS_INTEGRATION=1 on a host with Java",
)
def test_pinned_jenkins_acceptance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "LOL Integration")
    _git(root, "config", "user.email", "lol@example.invalid")
    fixtures = Path(__file__).parents[1] / "fixtures"
    (root / "Jenkinsfile").write_text(
        (fixtures / "declarative" / "Jenkinsfile").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    manifest = copy.deepcopy(DEFAULTS)
    manifest["requirements"]["podman"] = bool(os.environ.get("LOL_PODMAN_INTEGRATION"))
    (root / "lol.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    monkeypatch.chdir(root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = os.environ.get("LOL_INTEGRATION_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CACHE_HOME", cache)
    config = load_effective(root)
    create_lock(config)
    identity = project_identity(root, config.values)
    paths = ProjectPaths.from_id(root, identity.project_id)
    podman: str | None = None
    podman_image: str | None = None

    def run_fixture(
        *,
        revision: str | None = "HEAD",
        parameters: dict[str, str] | None = None,
        secret_parameters: dict[str, str] | None = None,
        temporary_credentials: list[TemporaryCredential] | None = None,
    ) -> tuple[int, RunRecord]:
        return execute(
            config,
            identity,
            paths,
            revision=revision,
            jenkinsfile=None,
            parameters=parameters or {},
            secret_parameters=secret_parameters or {},
            temporary_credentials=temporary_credentials or [],
            emit=lambda _: None,
        )

    def install_fixture(name: str) -> None:
        source = fixtures / name / "Jenkinsfile"
        (root / "Jenkinsfile").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        _git(root, "add", "Jenkinsfile")
        _git(root, "commit", "-m", name)

    try:
        doctor = CliRunner().invoke(cli, ["doctor", "--check"])
        assert doctor.exit_code == 0, doctor.output
        first = CliRunner().invoke(
            cli,
            ["run", "--revision", "HEAD", "--trust-repository", "--non-interactive"],
        )
        assert first.exit_code == 0, first.output
        record = list_runs(paths)[0]
        assert record.metadata["result"] == "SUCCESS"
        assert (record.artifacts_path / "result.txt").read_text(encoding="utf-8").strip() == (
            "fixture"
        )

        dirty = (root / "Jenkinsfile").read_text(encoding="utf-8").replace("fixture", "dirty")
        (root / "Jenkinsfile").write_text(dirty, encoding="utf-8")
        code, record = run_fixture(revision=None)
        assert code == 0
        assert (record.artifacts_path / "result.txt").read_text(encoding="utf-8").strip() == (
            "dirty"
        )

        install_fixture("scripted")
        assert run_fixture()[0] == 0

        install_fixture("parameters")
        code, record = run_fixture(
            parameters={"GREETING": "hola", "ENABLED": "false"},
            secret_parameters={"TOKEN": "fixture-secret-value"},
        )
        assert code == 0
        console = record.console_path.read_text(encoding="utf-8")
        assert "hola:false" in console
        assert "fixture-secret-value" not in console

        install_fixture("credentials")
        code, record = run_fixture(
            temporary_credentials=[
                TemporaryCredential("fixture-text", "secret-text", "text-value"),
                TemporaryCredential(
                    "fixture-userpass",
                    "username-password",
                    "password-value",
                    "fixture-user",
                ),
            ]
        )
        assert code == 0
        console = record.console_path.read_text(encoding="utf-8")
        assert "text-value" not in console
        assert "password-value" not in console
        assert "fixture-user" not in console
        current = controller_status(paths)
        assert current.endpoint
        username, password = controller_credentials(paths)
        with JenkinsClient(current.endpoint, username, password) as client:
            descriptions = client.credential_descriptions()
        assert "fixture-text" not in descriptions
        assert "fixture-userpass" not in descriptions

        for fixture, expected in (
            ("failure", "FAILURE"),
            ("unstable", "UNSTABLE"),
            ("aborted", "ABORTED"),
            ("timeout", "ABORTED"),
        ):
            install_fixture(fixture)
            code, record = run_fixture()
            assert code == 1
            assert record.metadata["result"] == expected

        if os.environ.get("LOL_PODMAN_INTEGRATION"):
            podman, podman_image = _prepare_podman_image(tmp_path)
            install_fixture("podman-parallel")
            assert run_fixture()[0] == 0

        # Prove restart and reset use only the already seeded lock/cache.
        install_fixture("declarative")
        down(paths)

        class OfflineRequests:
            RequestException = requests.RequestException

            @staticmethod
            def get(*_: object, **__: object) -> object:
                raise AssertionError("cache attempted an external download")

        monkeypatch.setattr("lol.cache.requests", OfflineRequests)
        assert run_fixture()[0] == 0
        down(paths)
        reset_controller(paths)
        assert run_fixture()[0] == 0
    finally:
        down(paths)
        if podman and podman_image:
            subprocess.run(
                [podman, "image", "rm", "--force", podman_image],
                check=False,
                capture_output=True,
                text=True,
            )


def test_rootless_podman_acceptance_is_explicitly_opt_in() -> None:
    if not os.environ.get("LOL_PODMAN_INTEGRATION"):
        pytest.skip("set LOL_PODMAN_INTEGRATION=1 to require rootless Podman")
    assert _require_rootless_podman()
