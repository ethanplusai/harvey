"""The heartbeat loop's PID file, and a liveness check that never kills.

``os.kill(pid, 0)`` is the POSIX idiom for "does this process exist?", but on
Windows ``os.kill`` with any signal other than CTRL_C_EVENT/CTRL_BREAK_EVENT
calls TerminateProcess — the "check" would kill the loop it was checking. So
Windows asks the kernel instead (OpenProcess + GetExitCodeProcess).

A PID file can outlive its loop (a crash, a reboot) and name whatever process
later got that number, so the running loop also holds an OS lock on
data/harvey.lock. The OS drops the lock when the loop dies, however it dies:
a live PID with nobody holding the lock is not Harvey, and is never signalled.
"""

import logging
import os
import sys
import time
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.pidfile")

PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOCK_FILE = PROJECT_ROOT / "data" / "harvey.lock"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """True if a process with this PID is running. Never signals or kills it."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def read_pid(path: Path | None = None) -> int | None:
    path = path or PID_FILE
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_pid_file(path: Path | None = None) -> None:
    """Record this process as the running loop (best effort)."""
    path = path or PID_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)


def remove_pid_file(path: Path | None = None) -> None:
    """Remove the PID file — only if it still names this process, so a loop
    exiting late never erases a newer loop's record."""
    path = path or PID_FILE
    if read_pid(path) == os.getpid():
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not remove PID file: %s", e)


def _try_lock(handle) -> bool:
    """Take an exclusive lock on ``handle`` without waiting."""
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def hold_loop_lock(path: Path | None = None):
    """Take the loop lock for this process's lifetime, or None if another
    loop already holds it. Keep the returned handle open while running."""
    path = path or LOCK_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError as e:
        logger.warning("Could not open the loop lock: %s", e)
        return None
    # The dashboard's status check holds the lock for an instant; a loop
    # starting in that instant waits it out rather than refusing to run.
    for attempt in range(10):
        if _try_lock(handle):
            return handle
        time.sleep(0.01 * (attempt + 1))
    handle.close()
    return None


def release_loop_lock(handle) -> None:
    if handle is None:
        return
    _unlock(handle)
    handle.close()


def loop_lock_held(path: Path | None = None) -> bool:
    """True while some Harvey loop holds the lock. Probing never keeps it."""
    path = path or LOCK_FILE
    if not path.exists():
        return False
    try:
        handle = open(path, "a+b")
    except OSError:
        return False
    try:
        if _try_lock(handle):
            _unlock(handle)
            return False
        return True
    finally:
        handle.close()
