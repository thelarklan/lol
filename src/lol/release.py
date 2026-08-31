from __future__ import annotations

import sys
import tomllib
from pathlib import Path


def project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as handle:
        value = tomllib.load(handle)
    version = value.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml has no project.version")
    return version


def check_release_ref(root: Path, ref_type: str, ref_name: str) -> None:
    if ref_type != "tag":
        return
    expected = f"v{project_version(root)}"
    if ref_name != expected:
        raise ValueError(f"release tag {ref_name!r} does not match package version {expected!r}")


def main(arguments: list[str] | None = None) -> int:
    values = arguments if arguments is not None else sys.argv[1:]
    if len(values) != 2:
        print("usage: python -m lol.release REF_TYPE REF_NAME", file=sys.stderr)
        return 2
    try:
        check_release_ref(Path.cwd(), values[0], values[1])
    except (OSError, ValueError) as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
