"""Where the running dashboard is: data/dashboard.json.

`sellbird dashboard` writes it once it knows its address, and removes it when
it stops. The MCP server reads it to find the dashboard, then asks
``/api/instance`` and talks to that address only if the id there matches the
file's, so a stale file never sends the agent's token to some other program
that took the port. The file holds no secrets.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

INFO_NAME = "dashboard.json"


@dataclass(frozen=True)
class DashboardInfo:
    host: str
    port: int
    instance: str
    pid: int


def write(data_dir: Path, *, host: str, port: int, instance: str) -> None:
    """Publish the dashboard's address atomically (owner-only where it means anything)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"host": host, "port": int(port), "instance": instance,
                          "pid": os.getpid()})
    fd, tmp = tempfile.mkstemp(prefix=".dashboard-", suffix=".json", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, data_dir / INFO_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read(data_dir: Path) -> DashboardInfo | None:
    """The published address, or None when there's no readable, well-formed file."""
    try:
        raw = json.loads((Path(data_dir) / INFO_NAME).read_text(encoding="utf-8"))
        info = DashboardInfo(host=str(raw["host"]), port=int(raw["port"]),
                             instance=str(raw["instance"]), pid=int(raw["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (0 < info.port < 65536) or not info.instance or not info.host:
        return None
    return info


def remove(data_dir: Path, *, instance: str) -> None:
    """Remove the file if it is still this dashboard's (a newer one may have replaced it)."""
    info = read(data_dir)
    if info is not None and info.instance == instance:
        (Path(data_dir) / INFO_NAME).unlink(missing_ok=True)
