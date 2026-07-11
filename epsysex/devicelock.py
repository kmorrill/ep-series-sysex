"""Cross-process device lock for EP FILE I/O.

The EP FILE subsystem is globally single-threaded on the device:
overlapping FILE sessions (even two *read-only* probes) reproducibly push the
firmware into an ``err lfs`` debug loop that requires a power cycle. Every
tool that touches the device must therefore hold this flock-based lock for
the duration of its FILE traffic, and FILE operations must never be
parallelized.

The default lock path is ``/tmp/ep-device.lock`` — deliberately kept
identical to the path used by the original koii research-repo tooling so that
this library and those tools serialize against *each other* when both are
installed on the same machine. Pass a different ``path`` only if you know no
legacy tooling will run concurrently.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

# Kept byte-for-byte compatible with the koii research tooling (see module
# docstring). Do not change casually.
LOCK_PATH = "/tmp/ep-device.lock"


class DeviceLockError(RuntimeError):
    pass


class DeviceLock:
    """flock-based advisory lock. Also usable as a context manager."""

    def __init__(self, owner, path=LOCK_PATH):
        self.owner = owner
        self.path = Path(path)
        self.file = None

    def acquire(self, timeout=0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.file.seek(0)
                self.file.truncate()
                self.file.write(
                    f"{self.owner} pid={os.getpid()} acquired={time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
                )
                self.file.flush()
                return self
            except BlockingIOError:
                if timeout <= 0 or time.monotonic() >= deadline:
                    holder = self.current_holder()
                    self.file.close()
                    self.file = None
                    raise DeviceLockError(f"EP device lock is busy: {holder}".strip())
                time.sleep(0.1)

    def release(self):
        if self.file is None:
            return
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None

    def current_holder(self):
        try:
            with self.path.open("r") as file:
                return file.read().strip()
        except FileNotFoundError:
            return "unknown holder"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def acquire_device_lock(owner, timeout=0, path=LOCK_PATH):
    """Acquire the shared device lock, waiting up to ``timeout`` seconds."""
    return DeviceLock(owner, path=path).acquire(timeout=timeout)
