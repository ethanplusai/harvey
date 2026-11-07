"""Secret store: the system keychain, with .env values as a read fallback.

Sign-ins and passwords for extra sending inboxes must never land in SQLite
or plain files, so they live in the macOS Keychain, Windows Credential
Manager or Secret Service (via `keyring`). A headless box (Linux CI, Docker)
has no keychain: reads fall back to environment values when a caller names
one, and writes raise one plain error instead of a keyring traceback.

A home can bring its own backend (``Home.secret_backend``, anything with
keyring's get_password / set_password): tests use MemorySecretBackend, and a
server running several workspaces brings one per workspace. A hosted home
never falls back to the machine's own keychain.

There is no delete here on purpose: nothing in the engine removes a
credential.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

import keyring
import keyring.errors

from sellbird.workspace_home import current_home

SERVICE = "sellbird"

_REF = re.compile(r"^[a-z0-9:_-]{1,128}$")
_KEYRING_ERRORS = (keyring.errors.KeyringError,)


class SecretStoreUnavailable(Exception):
    """No keychain here. The message is safe to show to a user."""

    def __init__(self, hosted: bool = False) -> None:
        super().__init__(
            "This workspace has no secret storage configured."
            if hosted
            else "No system keychain is available here (headless or Docker), so this "
            "sign-in can't be stored safely."
        )


class SecretStoreLocked(SecretStoreUnavailable):
    """A keychain exists but is locked or refused the request."""

    def __init__(self) -> None:
        Exception.__init__(
            self, "Your system keychain is locked or refused the request. Unlock it and try again.")


def _check_ref(ref: str) -> None:
    if not isinstance(ref, str) or not _REF.match(ref):
        raise ValueError("secret ref must match ^[a-z0-9:_-]{1,128}$")


class MemorySecretBackend:
    """In-memory backend with keyring's get/set/delete_password. No persistence."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, ref: str) -> str | None:
        return self._data.get((service, ref))

    def set_password(self, service: str, ref: str, value: str) -> None:
        self._data[(service, ref)] = value

    def delete_password(self, service: str, ref: str) -> None:
        self._data.pop((service, ref), None)

    def __repr__(self) -> str:
        return "MemorySecretBackend()"

    __str__ = __repr__


class SecretStore:
    """The backend and env resolve per call, from the current home, unless
    passed explicitly (an explicit one always wins)."""

    def __init__(self, backend=None, env: Mapping[str, str] | None = None):
        self._explicit_backend = backend
        self._explicit_env = env

    @property
    def _backend(self):
        if self._explicit_backend is not None:
            return self._explicit_backend
        home = current_home()
        if home.secret_backend is not None:
            return home.secret_backend
        if home.hosted:
            raise SecretStoreUnavailable(hosted=True)
        return keyring  # the module itself has get/set_password

    @property
    def _env(self) -> Mapping[str, str]:
        if self._explicit_env is not None:
            return self._explicit_env
        return {} if current_home().hosted else os.environ

    def get(self, ref: str, env_var: str | None = None) -> str | None:
        _check_ref(ref)
        try:
            value = self._backend.get_password(SERVICE, ref)
        except (SecretStoreUnavailable, *_KEYRING_ERRORS):
            value = None
        if value:
            return value
        if env_var:
            return self._env.get(env_var) or None
        return None

    def set(self, ref: str, value: str) -> None:
        _check_ref(ref)
        backend = self._backend
        try:
            backend.set_password(SERVICE, ref, value)
        except (keyring.errors.NoKeyringError, keyring.errors.InitError):
            # `from None`: the traceback never carries the value around.
            raise SecretStoreUnavailable(hosted=current_home().hosted) from None
        except keyring.errors.KeyringError:
            raise SecretStoreLocked() from None

    def available(self) -> bool:
        try:
            self._backend.get_password(SERVICE, "probe")
        except (SecretStoreUnavailable, *_KEYRING_ERRORS):
            return False
        return True

    def __repr__(self) -> str:
        return f"SecretStore(service={SERVICE!r})"

    __str__ = __repr__
