from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from lol.artifacts import copy_artifacts, download_artifacts, matches, safe_relative
from lol.errors import HarnessError
from lol.jenkins import JenkinsClient
from lol.paths import AppPaths, ProjectPaths
from lol.redaction import StreamingRedactor
from lol.runs import create_run, list_runs, load_run, select_run


def paths(tmp_path: Path) -> ProjectPaths:
    return ProjectPaths.from_id(
        tmp_path / "repo",
        "fixture",
        AppPaths(tmp_path / "config", tmp_path / "state", tmp_path / "cache"),
    )


def test_run_records_round_trip_privately_and_cannot_change_identity(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "running", "run_id": "caller-value"})
    record.update(status="completed", result="SUCCESS")

    loaded = load_run(project, record.run_id)
    assert loaded.metadata["result"] == "SUCCESS"
    assert loaded.metadata["run_id"] == record.run_id
    assert list_runs(project)[0].run_id == record.run_id
    assert select_run(project).run_id == record.run_id
    assert record.directory.stat().st_mode & 0o777 == 0o700
    assert record.metadata_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HarnessError, match="cannot be changed"):
        record.update(run_id="different")


@pytest.mark.parametrize("run_id", ["../secret", "/absolute", "run-1", ""])
def test_load_run_rejects_unsafe_identifier(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(HarnessError, match="invalid LOL run ID"):
        load_run(paths(tmp_path), run_id)


def test_load_run_rejects_public_or_symbolic_metadata(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "running"})
    record.metadata_path.chmod(0o644)
    with pytest.raises(HarnessError, match="unsafe LOL run metadata"):
        load_run(project, record.run_id)

    record.metadata_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"run_id": record.run_id}), encoding="utf-8")
    record.metadata_path.symlink_to(outside)
    with pytest.raises(HarnessError, match="cannot read LOL run metadata"):
        load_run(project, record.run_id)


def test_list_runs_skips_malformed_and_symbolic_entries(tmp_path: Path) -> None:
    project = paths(tmp_path)
    valid = create_run(project, {"status": "running"})
    (project.runs / "not-a-run").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.runs / "20260831T000000Z-aaaaaa").symlink_to(outside, target_is_directory=True)

    assert [record.run_id for record in list_runs(project)] == [valid.run_id]


@pytest.mark.parametrize("value", ["../secret", "/absolute", "a/../../b", "", "."])
def test_unsafe_artifact_paths_are_rejected(value: str) -> None:
    with pytest.raises(HarnessError, match="unsafe Jenkins artifact path"):
        safe_relative(value)


class ArtifactResponse:
    def __init__(self, payload: Any = None, chunks: tuple[bytes, ...] = ()) -> None:
        self.payload = {} if payload is None else payload
        self.chunks = chunks
        self.closed = False

    def json(self) -> Any:
        return self.payload

    def iter_content(self, _: int) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class ArtifactClient:
    def __init__(self, listing: list[dict[str, str]]) -> None:
        self.listing = listing
        self.requests: list[str] = []
        self.downloads: list[ArtifactResponse] = []

    def get_response(self, target: str, *, action: str, stream: bool = False) -> ArtifactResponse:
        self.requests.append(target)
        if not stream:
            return ArtifactResponse({"artifacts": self.listing})
        response = ArtifactResponse(chunks=(b"first", b"-second"))
        self.downloads.append(response)
        return response


def test_download_artifacts_filters_encodes_hashes_and_closes_response(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "running"})
    client = ArtifactClient(
        [
            {"relativePath": "reports/result one.txt"},
            {"relativePath": "ignored/data.bin"},
        ]
    )

    index = download_artifacts(
        cast(JenkinsClient, client),
        "http://127.0.0.1:8080/job/fixture/1/",
        record,
        ["reports/*.txt"],
    )

    artifact = record.artifacts_path / "reports" / "result one.txt"
    assert artifact.read_bytes() == b"first-second"
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert index == [
        {
            "path": "reports/result one.txt",
            "size": 12,
            "sha256": "79be082f29fd48f6922ef9e7c161190ba1e076790e82e9fa490b58205b5e9a44",
        }
    ]
    assert "/artifact/reports/result%20one.txt" in client.requests[-1]
    assert client.downloads[0].closed
    assert matches("reports/result one.txt", ["reports/*.txt"])


def test_download_rejects_duplicate_or_traversing_artifact_paths(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "running"})
    duplicate = ArtifactClient([{"relativePath": "same"}, {"relativePath": "same"}])
    with pytest.raises(HarnessError, match="duplicate artifact path"):
        download_artifacts(cast(JenkinsClient, duplicate), "http://127.0.0.1:8080/b/1/", record, [])

    traversing = ArtifactClient([{"relativePath": "../outside"}])
    with pytest.raises(HarnessError, match="unsafe Jenkins artifact path"):
        download_artifacts(
            cast(JenkinsClient, traversing), "http://127.0.0.1:8080/b/1/", record, []
        )


def test_download_rejects_symlinked_artifact_parent_without_touching_target(
    tmp_path: Path,
) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "running"})
    record.artifacts_path.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (record.artifacts_path / "reports").symlink_to(outside, target_is_directory=True)
    client = ArtifactClient([{"relativePath": "reports/result.txt"}])

    with pytest.raises(HarnessError, match="artifact directory is a symbolic link"):
        download_artifacts(cast(JenkinsClient, client), "http://127.0.0.1:8080/b/1/", record, [])

    assert list(outside.iterdir()) == []


def test_copy_artifacts_preserves_paths_and_rejects_symlinks(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "completed"})
    artifact = record.artifacts_path / "reports" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("result", encoding="utf-8")
    output = tmp_path / "output"

    copied = copy_artifacts(record, output)

    assert copied == [output / "reports" / "result.txt"]
    assert copied[0].read_text(encoding="utf-8") == "result"
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, record.artifacts_path / "link")
    with pytest.raises(HarnessError, match="unsafe stored artifact"):
        copy_artifacts(record, tmp_path / "second-output")


def test_copy_artifacts_rejects_symlinked_output_parent(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "completed"})
    artifact = record.artifacts_path / "reports" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("result", encoding="utf-8")
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "reports").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HarnessError, match="unsafe artifact copy destination"):
        copy_artifacts(record, output)

    assert list(outside.iterdir()) == []


def test_copy_artifacts_rejects_output_overlapping_stored_artifacts(tmp_path: Path) -> None:
    project = paths(tmp_path)
    record = create_run(project, {"status": "completed"})
    artifact = record.artifacts_path / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("result", encoding="utf-8")

    with pytest.raises(HarnessError, match="output overlaps stored artifacts"):
        copy_artifacts(record, record.artifacts_path)


def test_streaming_redaction_never_leaks_secret_across_chunk_boundaries() -> None:
    text = "before-abcdef-middle-token-after"
    expected = "before-****-middle-****-after"
    for first in range(len(text) + 1):
        for second in range(first, len(text) + 1):
            redactor = StreamingRedactor(["abcdef", "token", "", "abcdef"])
            output = (
                redactor.feed(text[:first])
                + redactor.feed(text[first:second])
                + redactor.feed(text[second:])
                + redactor.finish()
            )
            assert output == expected
            assert "abcdef" not in output
            assert "token" not in output


def test_redactor_without_overlap_streams_immediately() -> None:
    plain = StreamingRedactor([])
    assert plain.feed("plain text") == "plain text"
    assert plain.finish() == ""

    single = StreamingRedactor(["x"])
    assert single.feed("axb") == "a****b"
    assert single.finish() == ""


def test_streaming_redaction_handles_overlapping_secrets_deterministically() -> None:
    text = "ABCDEFXY"
    secrets = ["AB", "BC", "CDEF"]
    for first in range(len(text) + 1):
        for second in range(first, len(text) + 1):
            redactor = StreamingRedactor(secrets)
            output = (
                redactor.feed(text[:first])
                + redactor.feed(text[first:second])
                + redactor.feed(text[second:])
                + redactor.finish()
            )
            assert output == "********XY"
            assert all(secret not in output for secret in secrets)
