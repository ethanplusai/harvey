"""How Harvey starts the Antigravity CLI (agy).

Connects to the operator's local Antigravity installation using `agy -p` (print mode).
No API keys required; runs against the operator's authenticated Antigravity session
and supports Gemini models (such as gemini-3.8-flash-high).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("harvey.antigravity_runtime")

DEFAULT_MODEL = "gemini-3.8-flash-high"


class AntigravityRuntimeError(Exception):
    """The Antigravity CLI ('agy') cannot be used until the operator acts."""


def find_agy(env: dict | None = None) -> str | None:
    """Full path of the `agy` executable on PATH or in standard user bins."""
    path = (env if env is not None else os.environ).get("PATH")
    found = shutil.which("agy", path=path)
    if found:
        return found
    default_bin = Path.home() / ".local" / "bin" / "agy"
    if default_bin.is_file() and os.access(default_bin, os.X_OK):
        return str(default_bin)
    return None


def check_cli(env: dict | None = None) -> str:
    """Verify that agy is installed and return its path."""
    found = find_agy(env)
    if not found:
        raise AntigravityRuntimeError(
            "Antigravity CLI ('agy') not found on PATH or in ~/.local/bin/agy. "
            "Please ensure Antigravity is installed."
        )
    return found


def build_command(
    executable: str, model: str = "", prompt: str = ""
) -> list[str]:
    """argv for one isolated, print-mode call to agy."""
    selected_model = model.strip() if model else DEFAULT_MODEL
    cmd = [
        executable,
        "--model",
        selected_model,
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--print",
        prompt,
    ]
    return cmd


def parse_result_payload(raw: str) -> tuple[str, dict | None]:
    """Parse agy CLI JSON output into (response_text, payload_dict)."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, None
    if not isinstance(payload, dict):
        return raw, None
    # agy returns {"conversation_id": "...", "status": "SUCCESS", "response": "...", "usage": {...}}
    response_text = payload.get("response")
    if response_text is not None:
        return str(response_text).strip(), payload
    return raw, payload


@contextmanager
def isolated_cwd():
    """A fresh empty directory so agy does not scan local repo files."""
    with tempfile.TemporaryDirectory(
        prefix="harvey-agy-", ignore_cleanup_errors=True
    ) as tmp:
        yield str(Path(tmp).resolve())
