from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from lol.config import EffectiveConfig
from lol.constants import LOCK_NAME, SUPPORTED_JAVA_VERSIONS
from lol.errors import LolError
from lol.lockfile import load_lock, verify_lock_cache

Status = Literal["pass", "recommendation", "warning", "blocker", "unsupported"]


@dataclass(frozen=True, slots=True)
class Finding:
    check: str
    status: Status
    summary: str
    evidence: str = ""
    recommendation: str = ""
    repair_scope: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _command(name: str) -> Finding:
    path = shutil.which(name)
    if path:
        return Finding(f"command.{name}", "pass", f"{name} is available", path)
    return Finding(
        f"command.{name}",
        "blocker",
        f"required command is missing: {name}",
        recommendation=f"Install {name} using the host package manager.",
        repair_scope="manual",
    )


def _java() -> Finding:
    executable = shutil.which("java")
    if not executable:
        return Finding(
            "java.runtime",
            "blocker",
            "Java is unavailable",
            recommendation="Install a Java runtime supported by the pinned Jenkins LTS.",
            repair_scope="manual",
        )
    try:
        result = subprocess.run(
            [executable, "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return Finding("java.runtime", "blocker", "Java version detection timed out", executable)
    except OSError as exc:
        return Finding("java.runtime", "blocker", "Java could not be executed", str(exc))
    output = (result.stderr or result.stdout).splitlines()
    evidence = f"{executable}: {output[0]}" if result.returncode == 0 and output else ""
    version = re.search(r'version "?(\d+)', evidence)
    if result.returncode or not version:
        return Finding("java.runtime", "blocker", "Java version could not be determined", evidence)
    major = int(version.group(1))
    if major not in SUPPORTED_JAVA_VERSIONS:
        supported = " or ".join(str(item) for item in SUPPORTED_JAVA_VERSIONS)
        return Finding(
            "java.runtime",
            "blocker",
            f"Java {major} is not supported by the pinned Jenkins release",
            evidence,
            f"Install Java {supported}.",
            "manual",
        )
    return Finding("java.runtime", "pass", f"Java {major} is compatible", evidence)


def _podman() -> Finding:
    executable = shutil.which("podman")
    if not executable:
        return Finding(
            "podman.rootless",
            "blocker",
            "rootless Podman is required but unavailable",
            recommendation="Install and initialize rootless Podman.",
            repair_scope="manual",
        )
    try:
        result = subprocess.run(
            [executable, "info", "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return Finding("podman.rootless", "blocker", "Podman diagnostic timed out", executable)
    except OSError as exc:
        return Finding("podman.rootless", "blocker", "Podman could not be executed", str(exc))
    if result.returncode:
        return Finding(
            "podman.rootless",
            "blocker",
            "Podman is not responsive",
            result.stderr.strip(),
            "Repair the user Podman runtime.",
            "manual",
        )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Finding("podman.rootless", "blocker", "Podman returned invalid diagnostic data")
    if not isinstance(info, dict):
        return Finding("podman.rootless", "blocker", "Podman returned invalid diagnostic data")
    host = info.get("host")
    security = host.get("security") if isinstance(host, dict) else None
    rootless = bool(security.get("rootless")) if isinstance(security, dict) else False
    if not rootless:
        return Finding(
            "podman.rootless",
            "unsupported",
            "Podman is not running rootlessly",
            recommendation="Configure rootless Podman for the current user.",
            repair_scope="manual",
        )
    store = info.get("store")
    storage_paths = (
        [store.get("graphRoot"), store.get("runRoot")] if isinstance(store, dict) else []
    )
    if len(storage_paths) != 2 or not all(
        isinstance(path, str) and Path(path).is_absolute() for path in storage_paths
    ):
        return Finding(
            "podman.rootless",
            "blocker",
            "Podman storage paths could not be determined",
            recommendation="Repair the rootless Podman storage configuration.",
            repair_scope="manual",
        )
    unwritable = [
        str(path) for path in storage_paths if not os.access(str(path), os.W_OK | os.X_OK)
    ]
    if unwritable:
        return Finding(
            "podman.rootless",
            "blocker",
            "rootless Podman storage is not writable",
            ", ".join(unwritable),
            "Restore write access to the rootless Podman storage paths.",
            "manual",
        )
    return Finding(
        "podman.rootless",
        "pass",
        "rootless Podman is responsive and its storage is writable",
        ", ".join(str(path) for path in storage_paths),
    )


def _writable(path: Path) -> Finding:
    if path.is_symlink():
        return Finding("state.writable", "blocker", "state path is a symbolic link", str(path))
    if path.exists() and not path.is_dir():
        return Finding("state.writable", "blocker", "state path is not a directory", str(path))
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    writable = candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)
    return Finding(
        "state.writable",
        "pass" if writable else "blocker",
        f"state parent {'is' if writable else 'is not'} writable",
        str(candidate),
    )


def _port() -> Finding:
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    except OSError as exc:
        return Finding("network.loopback", "blocker", "cannot allocate a loopback port", str(exc))
    return Finding("network.loopback", "pass", "a loopback port is available", str(port))


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def analyze(config: EffectiveConfig, state_path: Path) -> list[Finding]:
    linux = sys.platform.startswith("linux")
    findings = [
        Finding(
            "host.platform",
            "pass" if linux else "unsupported",
            (
                "Linux host detected"
                if linux
                else f"host platform is unsupported in v1: {sys.platform}"
            ),
        ),
        _command("git"),
        _java(),
        _writable(state_path),
        _port(),
    ]
    for command in config.values["requirements"]["commands"]:
        if command != "git":
            findings.append(_command(str(command)))
    pipeline = config.root / str(config.values["pipeline"]["file"])
    pipeline_safe = pipeline.is_file() and _inside(config.root, pipeline)
    findings.append(
        Finding(
            "pipeline.file",
            "pass" if pipeline_safe else "blocker",
            (
                "Jenkinsfile exists"
                if pipeline_safe
                else "configured Jenkinsfile is missing or outside the repository"
            ),
            str(pipeline),
        )
    )
    if (config.root / ".gitmodules").exists():
        findings.append(
            Finding(
                "scm.submodules",
                "unsupported",
                "Git submodules are not supported in v1 snapshots",
            )
        )
    attributes = config.root / ".gitattributes"
    try:
        attribute_text = (
            attributes.read_text(encoding="utf-8", errors="replace") if attributes.exists() else ""
        )
    except OSError as exc:
        findings.append(
            Finding(
                "scm.attributes",
                "blocker",
                "Git attributes could not be inspected",
                str(exc),
                "Restore read access to .gitattributes.",
                "manual",
            )
        )
        attribute_text = ""
    if "filter=lfs" in attribute_text:
        findings.append(
            Finding("scm.lfs", "unsupported", "Git LFS hydration is not supported in v1")
        )
    try:
        lock = load_lock(config)
    except LolError as exc:
        findings.append(
            Finding(
                "plugins.lock",
                "blocker",
                str(exc),
                str(config.root / LOCK_NAME),
                "Run `lol lock`.",
                "lol-owned",
            )
        )
    else:
        try:
            missing = verify_lock_cache(lock)
        except (LolError, OSError) as exc:
            findings.append(
                Finding(
                    "plugins.lock",
                    "blocker",
                    "locked artifacts could not be verified",
                    str(exc),
                    "Restore access to the LOL cache and rerun doctor.",
                    "manual",
                )
            )
        else:
            findings.append(
                Finding(
                    "plugins.lock",
                    "pass" if not missing else "recommendation",
                    "plugin lock is valid" if not missing else "locked artifacts are not cached",
                    ", ".join(missing),
                    "Run `lol doctor --fix` to seed the cache." if missing else "",
                    "lol-owned" if missing else None,
                )
            )
    if config.values["requirements"]["podman"]:
        findings.append(_podman())
    return findings


def final_state(findings: list[Finding]) -> str:
    statuses = {finding.status for finding in findings}
    if "unsupported" in statuses:
        return "Unsupported"
    if "blocker" in statuses:
        return "Blocked"
    if statuses & {"warning", "recommendation"}:
        return "Ready with recommendations"
    return "Ready"
