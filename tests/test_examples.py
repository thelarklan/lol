from __future__ import annotations

from pathlib import Path

from lol.config import DEFAULTS, deep_merge, read_yaml, validate_manifest


def test_examples_are_complete_valid_repository_contracts() -> None:
    examples = Path(__file__).parents[1] / "examples"
    names = {path.name for path in examples.iterdir() if path.is_dir()}
    assert names == {"basic", "podman-parallel"}
    for directory in examples.iterdir():
        if not directory.is_dir():
            continue
        manifest = read_yaml(directory / "lol.yaml")
        validate_manifest(deep_merge(DEFAULTS, manifest), directory / "lol.yaml")
        pipeline = directory / str(manifest["pipeline"]["file"])
        assert pipeline.is_file()
        assert "pipeline {" in pipeline.read_text(encoding="utf-8")
