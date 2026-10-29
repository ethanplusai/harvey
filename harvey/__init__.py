"""Harvey — an autonomous sales agent that runs on your Claude subscription."""

import sys

# Fail with a sentence someone can act on, not a SyntaxError from deep inside
# a module. `python3 -m venv` on macOS builds from the system 3.9, so this is
# the single most likely first-run problem.
if sys.version_info < (3, 11):  # pragma: no cover - version guard
    raise RuntimeError(
        f"Harvey needs Python 3.11+, but this is "
        f"{sys.version_info.major}.{sys.version_info.minor} "
        f"({sys.executable}). On macOS `python3` is the system 3.9 — rebuild "
        f"the venv from a newer Python:\n"
        f"    brew install python@3.13\n"
        f"    rm -rf .venv && $(brew --prefix)/bin/python3.13 -m venv .venv\n"
        f"    source .venv/bin/activate && pip install -e ."
    )

# Credential query values must stay out of log files even when a standalone
# script imports harvey and configures logging itself, so the redaction is
# installed at package import rather than in each entry point.
from harvey.log_redaction import install_log_redaction

install_log_redaction()

"""Harvey — Autonomous Sales Agent. Always Be Closing."""
