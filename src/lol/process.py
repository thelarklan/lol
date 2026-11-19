from __future__ import annotations

import fcntl
import os
import signal
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lol.errors import HarnessError


def process_start_time(pid: int) -> str | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    tail = value.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) <= 19 or fields[0] == "Z":
        return None
    return fields[19]


def process_matches(pid: int, start_time: str) -> bool:
    return process_start_time(pid) == start_time


@dataclass(slots=True)
class VerifiedProcess:
    pid: int
    start_time: str
    _pidfd: int

    def send_signal(self, sig: int) -> bool:
        try:
            signal.pidfd_send_signal(self._pidfd, sig)
        except ProcessLookupError:
            return False
        return True

    def is_running(self) -> bool:
        return process_matches(self.pid, self.start_time)

    def close(self) -> None:
        if self._pidfd >= 0:
            fd = self._pidfd
            self._pidfd = -1
            os.close(fd)


@contextmanager
def open_verified_process(pid: int, start_time: str) -> Iterator[VerifiedProcess | None]:
    if not process_matches(pid, start_time):
        yield None
        return
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        yield None
        return
    except OSError as exc:
        raise HarnessError(f"cannot open process handle for PID {pid}: {exc}") from exc
    process = VerifiedProcess(pid, start_time, pidfd)
    try:
        if not process.is_running():
            yield None
            return
        yield process
    finally:
        process.close()


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HarnessError(f"cannot open LOL operation lock {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HarnessError(f"LOL operation lock is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HarnessError(f"another LOL operation owns {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
