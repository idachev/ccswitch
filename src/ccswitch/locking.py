"""Directory locks compatible with Claude Code 2.1.306's proper-lockfile paths.

Never break a foreign/stale lock. Heartbeats keep our locks younger than Claude's
shortest (10s) stale interval. Timeouts abort our operation, not another writer.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from pathlib import Path
import os
import json
import threading
import time

from ccswitch.paths import get_claude_config_path, get_secure_storage_dir
from ccswitch.jsonio import write_json_atomic


class LockError(RuntimeError):
    pass


class DirectoryLock:
    def __init__(self, path: Path, timeout: float = 10, heartbeat: float = 1):
        self.path, self.timeout, self.heartbeat = path, timeout, heartbeat
        self._stop = threading.Event()
        self._lost = False
        self._owner = Path(str(path) + ".ccswitch-owner")

    def _reap_own_dead_writer(self):
        # Native locks have no such record. Never steal them, even when old.
        try:
            record = json.loads(self._owner.read_text())
            stat = self.path.lstat()
            if [stat.st_dev, stat.st_ino] != record["identity"]:
                return
            pid = record["pid"]
            if not isinstance(pid, int) or pid <= 0:
                return
            try:
                os.kill(pid, 0)
                return
            except ProcessLookupError:
                pass
            stat = self.path.lstat()
            if [stat.st_dev, stat.st_ino] == record["identity"]:
                self.path.rmdir()
        except (OSError, ValueError, KeyError, TypeError):
            return

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.path.mkdir(mode=0o700)
                break
            except FileExistsError:
                self._reap_own_dead_writer()
                if time.monotonic() >= deadline:
                    raise LockError(f"Another writer holds {self.path}; retry when it finishes.") from None
                time.sleep(0.05)
        stat = self.path.lstat()
        self._identity = (stat.st_dev, stat.st_ino)
        try:
            write_json_atomic(self._owner, {"pid": os.getpid(), "identity": list(self._identity)})
        except BaseException:
            self.path.rmdir()
            raise
        self._thread = threading.Thread(target=self._pulse, daemon=True)
        self._thread.start()
        return self

    def check(self):
        try:
            stat = self.path.lstat()
            intact = (stat.st_dev, stat.st_ino) == self._identity
        except OSError:
            intact = False
        if self._lost or not intact:
            self._lost = True
            raise LockError(f"Lock ownership changed at {self.path}; aborting.")

    def _pulse(self):
        while not self._stop.wait(self.heartbeat):
            try:
                self.check()
                os.utime(self.path, follow_symlinks=False)
            except (OSError, LockError):
                self._lost = True
                return

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()
        self.check()
        # Remove our sidecar before releasing ownership, not after a new writer can acquire it.
        self._owner.unlink(missing_ok=True)
        self.path.rmdir()


@contextmanager
def refresh_locks():
    root = get_secure_storage_dir()
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        locks = [stack.enter_context(DirectoryLock(path)) for path in (
            root / ".oauth_refresh.lock", Path(str(root.resolve()) + ".lock")
        )]
        yield lambda: [lock.check() for lock in locks]


@contextmanager
def storage_and_config_locks():
    # Never hold config locks across a prompt or HTTP request.
    with ExitStack() as stack:
        locks = [stack.enter_context(DirectoryLock(path)) for path in (
            get_secure_storage_dir() / ".storage-write.lock",
            Path(str(get_claude_config_path()) + ".lock"),
        )]
        yield lambda: [lock.check() for lock in locks]
