"""Store and find the API key of an LLM profile.

A profile of an API needs a key, and a key in a TOML file is a secret in
plain text.  ``get_api_key`` looks in this order:

1. The environment variable that ``api_key_env`` of the profile names.
   WHY first: CI and a shell of one task can set a key without a change
   of a stored one.
2. The keyring of the system (``keyring``: macOS Keychain, Windows
   Credential Manager, Secret Service on Linux), service ``fw-context``,
   user = the name of the profile.
3. ``secrets.toml`` in the global directory of fw-context
   (``$FW_CONTEXT_HOME``, else ``~/.fw-context``), mode 0600 on Linux and
   macOS.  WHY a file: a Linux machine without a Secret Service (no
   D-Bus session, WSL, a container) has no keyring, and the user must
   still be able to store a key.  On Windows the mode cannot keep other
   users out: the keyring (Credential Manager) is always there.

A key goes into an HTTP header: a key with a space, a control character
or a character that is not ASCII is not usable.  httpx then raised an
error whose text held the key, thus such a key is refused when it is
stored, and is a missing key when it is read (``_usable``).

``set_api_key`` uses the keyring, and the file only when the keyring
cannot be used.  No function writes the value of a key to a log, an
exception or a message.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from pathlib import Path

import tomli_w

from ..utils import fw_context_home, write_text_atomic

log = logging.getLogger(__name__)

#: The service name of the keyring entries.
SERVICE = "fw-context"

#: The name of the file of the keys in the global directory.
SECRETS_FILE = "secrets.toml"

#: The table of the keys in the file: ``<profile> = "<key>"``.
_TABLE = "api_keys"

_FILE_HEADER = (
    "# The API keys of the LLM profiles of fw-context, for a machine without a\n"
    "# keyring. Mode 0600: only the owner can read this file. Do not commit it.\n"
    "# Change it with 'fw-context llm key set|delete <profile>'.\n"
)

#: The keys that were found, for each (profile, variable).  A keyring
#: lookup takes about 0.1 s (Secret Service, measured 2026-10-20), and each
#: chat request needs the key.  Only a found key is kept: a key that the
#: user stores later must apply without a restart of the MCP server.  A
#: key that another process changed applies after the next HTTP 401 or
#: 403: the chat client then calls ``forget``.
_found: dict[tuple[str, str, str], str] = {}

#: The profiles without a key that this process told about.
_told: set[str] = set()


class SecretStoreError(RuntimeError):
    """The key cannot be stored or removed: the keyring and the file both failed."""


def secrets_path() -> Path:
    """Return the path of ``secrets.toml``, read at each call (tests move the global directory)."""
    return fw_context_home() / SECRETS_FILE


def get_api_key(profile: str, env_name: str = "", shared: str = "") -> str | None:
    """Return the API key of *profile*, or None when no place holds one.

    *env_name* is the value of ``api_key_env`` of the profile, or empty.
    *shared* is the name of the provider of the profile (``openrouter``,
    ``deepseek``): without a key of the profile, the key stored as
    ``provider:<name>`` applies.  WHY: two profiles of one provider (two
    models of OpenRouter) use one account, and the user stores its key one
    time.  WHY the prefix: a profile name has no ``:``, thus a profile
    ``deepseek`` of OpenRouter cannot give its key to the provider DeepSeek.
    When no place holds a key, a notice goes to stderr one time for each
    profile: the requests then go without a key, and the server replies
    HTTP 401.
    """
    entry = (profile, env_name, shared)
    cached = _found.get(entry)
    if cached is not None:
        return cached
    key = _from_env(env_name, profile) or _from_keyring(profile) or _from_file(profile)
    if not key and shared:
        key = _from_keyring(provider_entry(shared)) or _from_file(provider_entry(shared))
    if key:
        _found[entry] = key
        return key
    if profile not in _told:
        _told.add(profile)
        where = f"the environment variable {env_name} is not set, and " if env_name else ""
        print(
            f"⚠ LLM profile {profile!r}: {where}no API key is stored for it: the requests go without a key. "
            f"Store one with 'fw-context llm key set {profile}'",
            file=sys.stderr,
        )
    return None


def api_key_source(profile: str, env_name: str = "", shared: str = "") -> str | None:
    """Return the place that gives the key of *profile* (``env:<name>``, ``keyring``, ``file``), or None.

    A key of the provider (*shared*, see ``get_api_key``) gives
    ``keyring:<provider>`` or ``file:<provider>``.  For a status line: the
    value itself is never returned.
    """
    if _from_env(env_name, profile):
        return f"env:{env_name}"
    if _from_keyring(profile):
        return "keyring"
    if _from_file(profile):
        return "file"
    if shared:
        if _from_keyring(provider_entry(shared)):
            return f"keyring:{shared}"
        if _from_file(provider_entry(shared)):
            return f"file:{shared}"
    return None


def provider_entry(provider: str) -> str:
    """Return the name of the stored key of *provider* (``provider:<name>``), which no profile can have."""
    return f"provider:{provider}"


def stored_api_key(profile: str) -> str | None:
    """Return the key of *profile* from the keyring or the file, without the environment and without a notice.

    For a copy of the key to a new profile (the conversion of the old
    config keys).
    """
    return _from_keyring(profile) or _from_file(profile)


def set_api_key(profile: str, value: str) -> str:
    """Store *value* as the API key of *profile*; return where it went (``keyring`` or ``file``).

    The keyring first.  When it cannot be used, the key goes to
    ``secrets.toml`` (mode 0600), and a key of the profile that the keyring
    holds from before is not there to win over it: the keyring cannot be
    used.  When the keyring takes the key, a key of the profile in the file
    is removed, else the two places would hold two keys.

    Raises:
        ValueError: *value* or *profile* is empty, or *value* has a
            character that an HTTP header cannot hold.  The message does
            not hold the key.
        SecretStoreError: the keyring and the file both failed, or the
            file is not valid TOML (a write would remove its other keys).
    """
    value = value.strip()
    if not profile or not value:
        raise ValueError("a profile name and a key that is not empty are necessary")
    if not _header_safe(value):
        raise ValueError(
            "the key has a character that an HTTP header cannot hold (a space, a control character, "
            "or a character that is not ASCII): nothing was stored"
        )
    forget(profile)
    if _keyring_call("set_password", profile, value):
        try:
            _file_remove(profile)
        except (OSError, SecretStoreError) as exc:
            # The key is stored: an old copy in the file is not a reason to
            # report a failure, but the keyring wins over it anyway.
            log.warning("The old API key of profile %r stays in %s: %s", profile, secrets_path(), exc)
        return "keyring"
    try:
        _file_write(profile, value)
    except OSError as exc:
        raise SecretStoreError(
            f"the API key of profile {profile!r} cannot be stored: no keyring can be used, "
            f"and {secrets_path()} cannot be written ({exc.strerror or exc})"
        ) from None
    return "file"


def delete_api_key(profile: str) -> list[str]:
    """Remove the API key of *profile* from the keyring and from the file; return the places that held one.

    Raises:
        SecretStoreError: the file holds the key and cannot be written.
    """
    forget(profile)
    removed = []
    # One call: delete_password of a missing entry raises PasswordDeleteError,
    # a KeyringError, thus False.
    if _keyring_call("delete_password", profile):
        removed.append("keyring")
    try:
        if _file_remove(profile):
            removed.append("file")
    except OSError as exc:
        raise SecretStoreError(
            f"the API key of profile {profile!r} cannot be removed from {secrets_path()} ({exc.strerror or exc})"
        ) from None
    return removed


def forget(profile: str) -> None:
    """Remove each cached key, and allow a new notice for *profile*.

    The chat client calls it after HTTP 401 or 403: another process
    (``fw-context llm key set``) can have changed the key.  WHY each key
    and not only the key of *profile*: the key of a provider applies to
    each profile of that provider, and the cache holds a few keys only.
    """
    _found.clear()
    _told.discard(profile)


def _header_safe(value: str) -> bool:
    """Return True when each character of *value* is printable ASCII without a space (``!`` to ``~``)."""
    return all("!" <= char <= "~" for char in value)


def _usable(value: object, profile: str, place: str) -> str | None:
    """Return *value* without leading and trailing white space, or None when it is not a usable key.

    A key with a character that an HTTP header cannot hold gives a
    warning that names the place, never the key.  A trailing newline (a
    variable from a ``.env`` file) is removed, not refused.
    """
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not key:
        return None
    if not _header_safe(key):
        log.warning(
            "The API key of profile %r in %s has a character that an HTTP header cannot hold: it is not used",
            profile, place,
        )
        return None
    return key


def _from_env(env_name: str, profile: str) -> str | None:
    return _usable(os.environ.get(env_name), profile, f"the environment variable {env_name}") if env_name else None


def _from_keyring(profile: str) -> str | None:
    return _usable(_keyring_call("get_password", profile), profile, "the keyring")


def _keyring_call(name: str, profile: str, *args: str) -> object:
    """Call ``keyring.<name>(SERVICE, profile, *args)``; return its result, or False when no keyring can be used.

    ``keyring`` is imported here: its import finds the backend, and a
    profile without an API needs none.  The message names the type of the
    error, never a key.

    WHY each error that is not fatal, and not only ``KeyringError``: this
    is the boundary to a backend of the system.  keyring 25 lets the
    errors of a backend pass: a Secret Service whose D-Bus socket went
    away (an SSH or tmux session after the logout) raises
    ``FileNotFoundError``, the Windows backend ``pywintypes.error``.  Such
    an error stopped the request, and the key file was not used.
    """
    from ..utils import is_fatal

    try:
        import keyring
    except ImportError as exc:
        log.warning("The keyring cannot be used: %s", exc)
        return False
    try:
        result = getattr(keyring, name)(SERVICE, profile, *args)
    except Exception as exc:
        if is_fatal(exc):
            raise
        log.info("The keyring cannot be used (%s)", type(exc).__name__)
        return False
    return True if result is None and name != "get_password" else result


def _file_keys(*, for_write: bool = False) -> dict[str, str]:
    """Return the keys of ``secrets.toml``; a missing file gives none.

    A file that cannot be read gives none and a warning: a key that is
    not readable is a missing key, and the request says so.  With
    *for_write* it raises instead: a write of the keys that were read
    would remove the other keys of the file.

    Raises:
        SecretStoreError: with *for_write*, the file cannot be read or is
            not valid TOML.  The message names the error, not a line of
            the file.
    """
    path = secrets_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        if for_write:
            raise SecretStoreError(
                f"{path} cannot be read ({type(exc).__name__}): correct or remove the file, then store the key again"
            ) from None
        log.warning("%s cannot be read: %s", path, type(exc).__name__)
        return {}
    table = data.get(_TABLE)
    if not isinstance(table, dict):
        return {}
    return {name: value for name, value in table.items() if isinstance(value, str)}


def _from_file(profile: str) -> str | None:
    return _usable(_file_keys().get(profile), profile, str(secrets_path()))


def _file_write(profile: str, value: str) -> None:
    keys = _file_keys(for_write=True)
    keys[profile] = value
    _file_save(keys)


def _file_remove(profile: str) -> bool:
    keys = _file_keys(for_write=True)
    if profile not in keys:
        return False
    del keys[profile]
    _file_save(keys)
    return True


def _file_save(keys: dict[str, str]) -> None:
    """Write *keys* to ``secrets.toml`` with mode 0600 (``write_text_atomic`` starts the new file at 0600)."""
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, _FILE_HEADER + tomli_w.dumps({_TABLE: keys}), mode=0o600)
