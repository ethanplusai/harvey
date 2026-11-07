"""Extensions: code outside this package that adds to Sellbird without patching it.

An extension is a Python package that declares an entry point in the
``sellbird.extensions`` group. Its value is a function taking the registry:

    # the extension's pyproject.toml
    [project.entry-points."sellbird.extensions"]
    myext = "myext.sellbird_plugin:install"

    # myext/sellbird_plugin.py
    def install(registry):
        registry.add_router(router)                     # dashboard routes
        registry.add_tick_step("myext.sync", sync)      # runs every heartbeat cycle
        registry.add_cli_command("myext", configure)    # a `sellbird myext` subcommand

Extensions load once per process, on first use, in entry-point name order. A
failing ``install`` stops Sellbird with the extension's name, rather than
running with half an extension. An install with no extensions (every
self-hosted install) loads nothing and behaves exactly as before.

Extensions are trusted code: they run in the same process with the same
access as Sellbird itself. Install only ones you'd install as a Python package.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "sellbird.extensions"
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
TICK_STEP_TIMEOUT_SECONDS = 120.0

TickStep = Callable[["TickContext"], Awaitable[None]]
ConfigureCommand = Callable[[argparse.ArgumentParser], Callable[[argparse.Namespace], Any]]


class ExtensionError(RuntimeError):
    """An extension couldn't be installed, or asked for something invalid."""


@dataclass(frozen=True)
class TickContext:
    """What a tick step gets: the cycle's config and state (a StateManager)."""
    config: Any
    state: Any


@dataclass
class Registry:
    routers: list[Any] = field(default_factory=list)
    tick_steps: dict[str, TickStep] = field(default_factory=dict)
    cli_commands: dict[str, tuple[str, ConfigureCommand]] = field(default_factory=dict)

    def add_router(self, router) -> None:
        """A FastAPI ``APIRouter`` served by the dashboard, behind its guard,
        after the built-in routes (an extension can add paths, not replace them)."""
        self.routers.append(router)

    def add_tick_step(self, name: str, step: TickStep) -> None:
        """An async ``step(TickContext)`` run once each heartbeat cycle, after the
        cycle's own work. A step that fails or runs past the timeout is logged
        and skipped; it never stops the cycle."""
        _check_name(name, "tick step")
        if name in self.tick_steps:
            raise ExtensionError(f"tick step {name!r} is already registered")
        if not asyncio.iscoroutinefunction(step):
            raise ExtensionError(f"tick step {name!r} must be an async function")
        self.tick_steps[name] = step

    def add_cli_command(self, name: str, configure: ConfigureCommand, *, help: str = "") -> None:
        """A ``sellbird <name>`` subcommand. ``configure(parser)`` adds its
        arguments and returns the function that runs it with the parsed args.
        A name the built-in CLI already uses is refused when the CLI is built."""
        _check_name(name, "command")
        if name in self.cli_commands:
            raise ExtensionError(f"command {name!r} is already registered")
        self.cli_commands[name] = (help, configure)


def _check_name(name: str, what: str) -> None:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ExtensionError(f"{what} name {name!r} must be lowercase letters, digits, . _ or -")


_REGISTRY = Registry()
_LOADED = False


def registry() -> Registry:
    """The process's registry, with every installed extension loaded."""
    load()
    return _REGISTRY


def load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        try:
            ep.load()(_REGISTRY)
        except Exception as e:
            raise ExtensionError(f"the Sellbird extension {ep.name!r} failed to install: {e}") from e
        logger.info(f"Loaded the Sellbird extension {ep.name!r}.")


def reset_for_tests() -> Registry:
    """Empty the registry and mark it loaded, so a test registers exactly what it needs."""
    global _REGISTRY, _LOADED
    _REGISTRY = Registry()
    _LOADED = True
    return _REGISTRY


async def run_tick_steps(config, state, *, timeout: float = TICK_STEP_TIMEOUT_SECONDS) -> dict[str, str]:
    """Run every registered tick step once, in registration order. Returns
    ``{name: "ok" | "failed" | "timed out"}``."""
    results: dict[str, str] = {}
    context = TickContext(config=config, state=state)
    for name, step in list(registry().tick_steps.items()):
        try:
            await asyncio.wait_for(step(context), timeout=timeout)
            results[name] = "ok"
        except asyncio.TimeoutError:
            logger.error(f"Extension step {name!r} ran past {timeout:.0f}s and was stopped.")
            results[name] = "timed out"
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as e:
            logger.error(f"Extension step {name!r} failed: {e}", exc_info=True)
            results[name] = "failed"
    return results
