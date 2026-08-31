from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml


def _workflow(name: str) -> dict[str, Any]:
    path = Path(__file__).parents[1] / ".github" / "workflows" / name
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_ci_requires_real_jenkins_and_rootless_podman_acceptance() -> None:
    workflow = _workflow("ci.yml")
    jobs = workflow["jobs"]
    integration = jobs["jenkins-integration"]
    rendered = yaml.safe_dump(integration)
    assert "timeout-minutes" in integration
    assert "LOL_JENKINS_INTEGRATION=1" in rendered
    assert "LOL_PODMAN_INTEGRATION=1" in rendered
    assert "setup-java" in rendered
    assert "podman" in rendered
    assert "actions/cache" in rendered


def test_release_is_tag_gated_reproducible_and_uses_trusted_publishing() -> None:
    workflow = _workflow("release.yml")
    jobs = workflow["jobs"]
    assert set(jobs) == {"acceptance", "build", "github-release", "pypi"}
    rendered = yaml.safe_dump(workflow)
    assert "python -m lol.release" in rendered
    assert "SOURCE_DATE_EPOCH" in rendered
    assert "/tmp/lol-release-dist-a" in rendered
    assert "cmp " in rendered
    assert "SHA256SUMS" in rendered
    assert "gh release" in rendered
    assert "GH_REPO" in rendered
    assert "pypa/gh-action-pypi-publish@release/v1" in rendered
    assert jobs["pypi"]["permissions"] == {"id-token": "write"}
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
