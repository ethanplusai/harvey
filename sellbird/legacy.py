"""Carrying an install over from Harvey, Sellbird's old name.

People run Sellbird from a clone and update with ``git pull``. A clone made
while the project was called Harvey has its own files under the old names
(``harvey.local.yaml``, ``data/harvey.db`` and its log), and may set
``HARVEY_*`` environment variables. This module moves those across once,
quietly and safely, so an update never looks like lost data:

- A file is renamed only when nothing exists under the new name. Nothing of
  the user's is overwritten or deleted; a conflict is reported and left for a
  person. (A dead process's pidfile is the one thing removed.)
- Nothing moves while a Harvey process is still running from this folder,
  because it would keep writing to the old files.
- ``HARVEY_X`` is honoured when ``SELLBIRD_X`` is unset, and never wins over it.

The file moves are kept permanently: someone may pull years from now. Only
the ``harvey`` command (the package next to this one) is temporary.
"""

from __future__ import annotations

import os
from pathlib import Path

from sellbird.paths import PROJECT_ROOT

# Old path → new path, relative to the project root. SQLite's -wal and -shm
# files belong to the database and move with it, or recent writes are lost.
FILES = [
    ("harvey.local.yaml", "sellbird.local.yaml"),
    ("data/harvey.db", "data/sellbird.db"),
    ("data/harvey.db-wal", "data/sellbird.db-wal"),
    ("data/harvey.db-shm", "data/sellbird.db-shm"),
    ("data/harvey.log", "data/sellbird.log"),
]
OLD_PID = "data/harvey.pid"
ENV_PREFIX_OLD, ENV_PREFIX_NEW = "HARVEY_", "SELLBIRD_"


def adopt_legacy_env(environ=os.environ) -> list[str]:
    """Copy each ``HARVEY_X`` to ``SELLBIRD_X`` where that is unset. Returns
    the new names it set, so a command can mention them once."""
    adopted = []
    for name, value in list(environ.items()):
        if not name.startswith(ENV_PREFIX_OLD):
            continue
        new = ENV_PREFIX_NEW + name[len(ENV_PREFIX_OLD):]
        if new not in environ:
            environ[new] = value
            adopted.append(new)
    return sorted(adopted)


def _old_process_running(root: Path) -> int | None:
    from sellbird.pidfile import pid_alive, read_pid
    pid = read_pid(root / OLD_PID)
    return pid if pid and pid_alive(pid) else None


def migrate_files(root: Path = PROJECT_ROOT) -> list[str]:
    """Rename this install's Harvey-era files. Returns one line per thing
    done or left alone, for the caller to show; empty when there was nothing."""
    pending = [(root / old, root / new) for old, new in FILES if (root / old).exists()]
    if not pending:
        return []
    pid = _old_process_running(root)
    if pid:
        return [f"Harvey is still running here (process {pid}). Stop it, then run "
                f"Sellbird again to move its files over."]
    notes = []
    for old, new in pending:
        old_name, new_name = old.relative_to(root), new.relative_to(root)
        if new.exists():
            notes.append(f"Left {old_name} alone: {new_name} already exists. "
                         f"Compare them and keep the one you want.")
            continue
        try:
            old.rename(new)
        except OSError as e:
            notes.append(f"Couldn't move {old_name} to {new_name}: {e.strerror or e}")
            continue
        notes.append(f"Moved {old_name} to {new_name}.")
    # A stale pidfile from a Harvey that has stopped means nothing now.
    stale = root / OLD_PID
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass
    return notes
