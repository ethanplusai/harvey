"""The Home: every path and mode one Sellbird workspace needs.

One process can serve many homes (a server running several workspaces, each
with its own files and database). The current home
lives in a ContextVar, so concurrent asyncio tasks each see their own and
``asyncio.to_thread`` inherits it. With no home set, ``default_home()`` rebuilds
one from the legacy module globals on every call, so tests and callers that
patch those globals keep working.
"""

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, Literal

Mode = Literal["self_hosted", "hosted"]


@dataclass(frozen=True)
class Home:
    root: Path
    data_dir: Path
    db_path: Path
    env_file: Path
    prompts_dir: Path
    skills_dir: Path
    m365_token_file: Path
    gmail_token_file: Path
    dashboard_token_file: Path
    agent_token_file: Path
    # None means "use the legacy config search".
    config_file: Path | None
    mode: Mode = "self_hosted"
    # Object with keyring-style get_password / set_password; None means the default.
    secret_backend: object | None = None

    @classmethod
    def for_root(
        cls, root: Path, *, mode: Mode = "self_hosted", secret_backend=None
    ) -> "Home":
        data = root / "data"
        return cls(
            root=root,
            data_dir=data,
            db_path=data / "sellbird.db",
            env_file=root / ".env",
            prompts_dir=root / "prompts",
            skills_dir=root / "skills",
            m365_token_file=data / "m365_token.json",
            gmail_token_file=data / "gmail_token.json",
            dashboard_token_file=data / "dashboard-token",
            agent_token_file=data / "agent-token",
            config_file=root / "sellbird.yaml",
            mode=mode,
            secret_backend=secret_backend,
        )

    @property
    def hosted(self) -> bool:
        return self.mode == "hosted"


_HOME: ContextVar["Home | None"] = ContextVar("sellbird_home", default=None)


def default_home() -> Home:
    """The self-hosted home, read lazily from the legacy globals.

    Never imports sellbird.dashboard or sellbird.integrations.microsoft (heavy, and
    they would create import cycles); it only reads them if already loaded.
    """
    from sellbird import paths, state

    dash = sys.modules.get("sellbird.dashboard")
    db_path = Path(dash.DB_PATH if dash is not None else state.DB_PATH)
    data_dir = db_path.parent
    root = Path(dash.PROJECT_ROOT) if dash is not None else paths.PROJECT_ROOT
    env_file = Path(dash.ENV_FILE) if dash is not None else root / ".env"
    ms = sys.modules.get("sellbird.integrations.microsoft")
    gm = sys.modules.get("sellbird.integrations.gmail")
    brain = sys.modules.get("sellbird.brain")
    return Home(
        root=root,
        data_dir=data_dir,
        db_path=db_path,
        env_file=env_file,
        prompts_dir=Path(brain.PROMPTS_DIR) if brain is not None else root / "prompts",
        skills_dir=Path(brain.SKILLS_DIR) if brain is not None else root / "skills",
        m365_token_file=Path(ms.TOKEN_FILE) if ms is not None else data_dir / "m365_token.json",
        gmail_token_file=Path(gm.TOKEN_FILE) if gm is not None else data_dir / "gmail_token.json",
        dashboard_token_file=Path(dash.TOKEN_FILE) if dash is not None else data_dir / "dashboard-token",
        agent_token_file=Path(getattr(dash, "AGENT_TOKEN_FILE", data_dir / "agent-token")
                              if dash is not None else data_dir / "agent-token"),
        config_file=None,
    )


def current_home() -> Home:
    return _HOME.get() or default_home()


@contextmanager
def use_home(home: Home) -> Iterator[Home]:
    token = _HOME.set(home)
    try:
        yield home
    finally:
        _HOME.reset(token)


# What only runs on the user's own machine, by require_self_hosted's feature
# name. A hosted home refuses these because they reach into that machine (its
# Claude or Codex login, its files, its processes), not because hosted lacks
# the capability: hosted runs its own brain, training and discovery on the
# server, through the same engine.
LOCAL_ONLY: dict[str, str] = {
    "local_ai_loop": "the heartbeat calling the Claude or Codex CLI signed in on this computer",
    "local_training": "training through the Claude or Codex CLI on this computer",
    "local_discovery": "discovery runs started from this computer's dashboard",
    "desktop": "the menu-bar app and starting at login",
    "mcp_install": "adding Sellbird to this computer's coding-agent config",
    "env_settings": "editing this install's .env file",
    "process_logs": "this computer's process log",
    "demo_vendors": "the offline demo vendors",
}
HOSTED_REFUSED = tuple(LOCAL_ONLY)


class NotOnHosted(Exception):
    def __init__(self, feature: str = ""):
        super().__init__(feature)
        self.feature = feature
        what = LOCAL_ONLY.get(feature)
        self.payload = {
            "error": "not_on_hosted",
            "message": (f"This runs only on your own computer ({what}). Hosted Sellbird does "
                        "this on the server instead." if what else
                        "This runs only on your own computer, not on hosted Sellbird."),
        }


def require_self_hosted(feature: str) -> None:
    if feature not in LOCAL_ONLY:
        raise ValueError(f"unknown local-only feature: {feature!r}")
    if current_home().hosted:
        raise NotOnHosted(feature)
