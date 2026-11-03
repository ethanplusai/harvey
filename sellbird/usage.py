"""Usage accounting helpers.

Harvey's usage ledger is built entirely from its OWN Claude calls: every
`claude -p` invocation goes through the Brain, which parses the call's
result JSON and writes one usage_events row tagged with the agent and task
(see harvey/brain.py). That path is the single source of truth — it never
sees, and never records, your other Claude Code sessions.

(An earlier version also scanned every transcript in the Claude config
directory to "backfill" usage. That pulled in unrelated projects' sessions
and polluted Harvey's numbers, so it was removed: Harvey tracks only what
Harvey does.)

This module now holds just the shared location helper the quota client
needs.
"""

import os
from pathlib import Path


def claude_config_dir() -> Path:
    """Claude Code's config directory (honors CLAUDE_CONFIG_DIR)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"
