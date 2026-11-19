from __future__ import annotations

import errno
import fcntl
import os
import select
import signal
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from lol.errors import HarnessError


class LockBusyError(HarnessError):
    pass


def boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise HarnessError(f"cannot read kernel boot ID: {exc}") from exc
    return value or None


def pidfd_supported() -> bool:
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return False
    fd = -1
    try:
        fd = pidfd_open(os.getpid(), 0)
        pidfd_send_signal(fd, 0)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EPERM, errno.EOPNOTSUPP, errno.EINVAL}:
            return False
        raise HarnessError(f"cannot verify Linux pidfd support: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return True


def process_start_time(pid: int) -> str | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return None
        raise HarnessError(f"cannot inspect process {pid}: {exc}") from exc
    tail = value.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) <= 19 or fields[0] == "Z":
        return None
    return fields[19]


def process_matches(
    pid: int,
    start_time: str,
    *,
    expected_boot_id: str | None = None,
) -> bool:
    if expected_boot_id is not None and boot_id() != expected_boot_id:
        return False
    return process_start_time(pid) == start_time


@dataclass(slots=True)
class VerifiedProcess:
    pid: int
    start_time: str
    _pidfd: int

    def send_signal(self, sig: int) -> bool:
        if self._pidfd < 0:
            return False
        try:
            signal.pidfd_send_signal(self._pidfd, sig)
        except ProcessLookupError:
            return False
        except OSError as exc:
            raise HarnessError(f"cannot signal process {self.pid}: {exc}") from exc
        return True

    def is_running(self) -> bool:
        if self._pidfd < 0:
            return False
        poller = select.poll()
        poller.register(self._pidfd, select.POLLIN)
        try:
            events = poller.poll(0)
        except OSError as exc:
            raise HarnessError(f"cannot inspect process {self.pid}: {exc}") from exc
        return not events

    def close(self) -> None:
        if self._pidfd >= 0:
            fd = self._pidfd
            self._pidfd = -1
            os.close(fd)


@contextmanager
def open_verified_process(
    pid: int,
    start_time: str,
    *,
    expected_boot_id: str | None = None,
) -> Iterator[VerifiedProcess | None]:
    if pid <= 0:
        yield None
        return
    if expected_boot_id is not None and boot_id() != expected_boot_id:
        yield None
        return
    try:
        pidfd = os.pidfd_open(pid, 0)
    except ProcessLookupError:
        yield None
        return
    except (AttributeError, OSError) as exc:
        raise HarnessError(f"cannot open process handle for PID {pid}: {exc}") from exc
    process = VerifiedProcess(pid, start_time, pidfd)
    try:
        if process_start_time(pid) != start_time or not process.is_running():
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
            raise LockBusyError(f"another LOL operation owns {path}") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
