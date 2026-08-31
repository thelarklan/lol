from __future__ import annotations

from pathlib import Path

import pytest

from lol.release import check_release_ref, main, project_version


def test_release_tag_must_match_project_version(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    assert project_version(tmp_path) == "1.2.3"
    check_release_ref(tmp_path, "tag", "v1.2.3")
    check_release_ref(tmp_path, "branch", "main")
    with pytest.raises(ValueError, match="does not match"):
        check_release_ref(tmp_path, "tag", "v1.2.4")


def test_release_check_reports_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err
