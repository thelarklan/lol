from __future__ import annotations

import json
from pathlib import Path

from lol.project import ProjectIdentity
from lol.trust import is_trusted, trust


def identity(
    root: Path, *, fingerprint: str = "fingerprint", origin: str | None = None
) -> ProjectIdentity:
    return ProjectIdentity("project", fingerprint, root, origin)


def test_trust_is_scoped_to_repository_identity(tmp_path: Path) -> None:
    state = tmp_path / "state"
    trusted = identity(tmp_path / "repository", origin="git@example.invalid:org/repo.git")

    assert not is_trusted(state, trusted)

    trust(state, trusted)

    assert is_trusted(state, trusted)
    assert not is_trusted(
        state, identity(trusted.root, fingerprint="changed", origin=trusted.origin)
    )
    assert not is_trusted(state, identity(trusted.root, origin="git@example.invalid:org/other.git"))


def test_trust_record_is_private_and_malformed_records_are_untrusted(tmp_path: Path) -> None:
    state = tmp_path / "state"
    repository = tmp_path / "repository"
    trusted = identity(repository)

    trust(state, trusted)

    path = state / "trust.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "fingerprint": "fingerprint",
        "origin": None,
        "root": str(repository),
    }

    path.write_text("[]\n", encoding="utf-8")
    assert not is_trusted(state, trusted)
    path.write_text("not json\n", encoding="utf-8")
    assert not is_trusted(state, trusted)
