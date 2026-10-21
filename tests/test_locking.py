"""Concurrency tests use temporary directories and real child processes, never Claude state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from ccswitch.locking import DirectoryLock, LockError, refresh_locks, storage_and_config_locks


def test_foreign_lock_is_not_stolen_even_when_old(tmp_path):
    lock = tmp_path / "foreign.lock"
    lock.mkdir()
    os.utime(lock, (1, 1))
    with pytest.raises(LockError, match="Another writer"):
        with DirectoryLock(lock, timeout=0.02):
            pytest.fail("Acquired a foreign lock")
    assert lock.is_dir()


def test_heartbeat_and_release(tmp_path):
    path = tmp_path / "lease.lock"
    with DirectoryLock(path, heartbeat=0.02):
        first = path.stat().st_mtime_ns
        deadline = time.monotonic() + 1
        while path.stat().st_mtime_ns == first and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.stat().st_mtime_ns > first
    assert not path.exists()
    assert not Path(str(path) + ".ccswitch-owner").exists()


def test_lock_replacement_is_detected_and_not_removed(tmp_path):
    path = tmp_path / "lease.lock"
    with pytest.raises(LockError, match="ownership"):
        with DirectoryLock(path, heartbeat=10) as lock:
            path.rename(tmp_path / "old-lock")
            path.mkdir()
            lock.check()
    assert path.is_dir()


def child_env():
    return dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                PYTHONDONTWRITEBYTECODE="1")


def test_abrupt_exit_of_our_writer_can_be_recovered(tmp_path):
    path = tmp_path / "crashed.lock"
    code = "from pathlib import Path; import os,sys; from ccswitch.locking import DirectoryLock; " \
           "lock=DirectoryLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)"
    subprocess.run([sys.executable, "-c", code, str(path)], env=child_env(), check=True)
    assert path.is_dir()
    with DirectoryLock(path, timeout=1):
        assert path.is_dir()
    assert not path.exists()


def test_multiple_processes_serialize_read_modify_write(tmp_path):
    path = tmp_path / "counter.json"
    path.write_text('{"count": 0}')
    code = """
from pathlib import Path
import json,sys,time
from ccswitch.locking import DirectoryLock
p=Path(sys.argv[1])
for _ in range(4):
    with DirectoryLock(Path(str(p)+'.lock')):
        n=json.loads(p.read_text())['count']
        time.sleep(.01)
        p.write_text(json.dumps({'count':n+1}))
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path)], env=child_env()) for _ in range(3)]
    for process in processes:
        assert process.wait(timeout=10) == 0
    assert json.loads(path.read_text())["count"] == 12


def test_claude_compatible_paths_and_cleanup(tmp_home):
    root = tmp_home / ".claude"
    with refresh_locks() as check_refresh, storage_and_config_locks() as check_storage:
        paths = [root / ".oauth_refresh.lock", tmp_home / ".claude.lock",
                 root / ".storage-write.lock", tmp_home / ".claude.json.lock"]
        assert all(path.is_dir() for path in paths)
        check_refresh()
        check_storage()
    assert all(not path.exists() for path in paths)
