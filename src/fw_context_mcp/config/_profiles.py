"""Write an LLM profile (``[llm.profiles.<name>]``) to the global config, and keep its comments.

The line editor of ``settings._edit_llm_table`` finds only the table
``[llm]``, thus it cannot write a sub-table.  ``tomlkit`` keeps each
comment, blank line and key order of the file, as ``housekeeping`` does
for its edits.

WHY the global config only: a profile names the server that gets the
source code; a project selects one with ``[llm] profile``
(``config_value_problems``).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import tomlkit

from . import settings
from ._toml_editor import _atomic_write

#: A profile name: a TOML bare key, thus ``[llm.profiles.<name>]`` needs no quotes.
_NAME = re.compile(r"[A-Za-z0-9_-]+")


def profile_names() -> list[str]:
    """Return the names of the profiles of the global config, in the order of the file.

    Raises:
        ConfigError: the global config cannot be read, or it is not valid TOML.
    """
    llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm")
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    return list(profiles) if isinstance(profiles, dict) else []


def project_profile_name(project_root: Path) -> str:
    """Return a profile name from the name of the project directory (a bare TOML key)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", project_root.name).strip("-") or "project"


def replaces_other_values(name: str, values: dict[str, object]) -> bool:
    """Return True when the global config has the profile *name* with values that are not *values*.

    Each project that selects the profile uses it, thus a writer asks
    before it changes one.  A global config that cannot be read gives
    False: ``set_profile`` then reports the problem.
    """
    try:
        llm = settings.read_config_file(settings._GLOBAL_CONFIG_PATH).get("llm")
    except settings.ConfigError:
        return False
    profiles = llm.get("profiles") if isinstance(llm, dict) else None
    earlier = profiles.get(name) if isinstance(profiles, dict) else None
    if earlier is None:
        return False
    return earlier != {key: value for key, value in values.items() if value is not None}


def put_profile(document: tomlkit.TOMLDocument, name: str, values: dict[str, object]) -> None:
    """Put the profile *name* with exactly *values* into *document* (a ``None`` value is left out).

    Makes ``[llm]`` and ``[llm.profiles]`` when they are missing; the
    profiles table is a super-table, thus the file gets only the headers
    ``[llm.profiles.<name>]``.
    """
    llm = document.get("llm")
    if llm is None:
        llm = tomlkit.table()
        document["llm"] = llm
    profiles = llm.get("profiles")
    if profiles is None:
        profiles = tomlkit.table(is_super_table=True)
        llm["profiles"] = profiles
    table = tomlkit.table()
    for key, value in values.items():
        if value is not None:
            table[key] = value
    profiles[name] = table


def set_profile(name: str, values: dict[str, object], *, select: bool = False) -> Path:
    """Write the profile *name* with *values* to the global config; with *select*, make it ``[llm] profile``.

    The profile gets exactly *values* (a ``None`` value is left out): a
    key of an earlier profile of that name does not stay behind, because
    it can belong to a different provider.  The file does not change when
    the new profile has a problem (``config_value_problems``).

    Returns the path of the global config.

    Raises:
        ValueError: *name* is not a bare TOML key (letters, digits, ``_``
            and ``-``).
        ConfigError: the global config cannot be read or is not valid TOML,
            or the new profile has a problem.  The file is not changed.
        OSError: the file cannot be written.
    """
    if not _NAME.fullmatch(name):
        raise ValueError(f"profile name {name!r} must have only letters, digits, '_' and '-'")
    path = settings._ensure_global_config()
    # Before the edit: a file that is not valid TOML gets no more text.
    settings.read_config_file(path)
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    put_profile(document, name, values)
    if select:
        document["llm"]["profile"] = name

    text = tomlkit.dumps(document)
    problems = [
        problem.message
        for problem in settings.config_value_problems(tomllib.loads(text), global_file=True)
        if problem.keys[:3] == ("llm", "profiles", name)
    ]
    if problems:
        raise settings.ConfigError(path, f"the profile {name!r} is not written: {problems[0]}")
    _atomic_write(path.resolve(), text)
    return path


def remove_profile(name: str) -> Path:
    """Remove the profile *name* from the global config, and keep the comments of the file.

    Refused when ``[llm] profile`` or ``[llm] embed_profile`` of the global
    config selects it: each project without a selection of its own would
    get an error.  A project that selects it in its own ``local.toml``
    gets an error that names the profile; ``fw-context llm default`` there
    selects another one.

    Returns the path of the global config.

    Raises:
        ConfigError: the global config cannot be read, has no such
            profile, or selects it.  The file is not changed.
        OSError: the file cannot be written.
    """
    path = settings._ensure_global_config()
    settings.read_config_file(path)
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    llm = document.get("llm")
    if llm is None or name not in (llm.get("profiles") or {}):
        raise settings.ConfigError(path, f"the global config has no LLM profile {name!r}")
    for key in ("profile", "embed_profile"):
        if llm.get(key) == name:
            raise settings.ConfigError(
                path, f"[llm] {key} of the global config selects {name!r}: select another profile first "
                f"('fw-context llm default <profile> --global')",
            )
    del llm["profiles"][name]
    _atomic_write(path.resolve(), tomlkit.dumps(document))
    return path


def select_global_profile(name: str | None, *, ollama_profile: str | None = None) -> Path:
    """Write ``[llm] profile`` (*name*) and ``[llm] ollama_profile`` of the global config, in one write.

    *name* is the profile of each project that selects none; None keeps
    it.  *ollama_profile* is the Ollama profile of this machine
    (``llm.profile_state.ollama_profile_name``); None keeps the key as it
    is.  The caller decides which profile is an Ollama one: a cloud model
    of Ollama needs the server to tell it.

    Raises:
        ConfigError: the global config cannot be read, or has no such profile.
        OSError: the file cannot be written.
    """
    path = settings._ensure_global_config()
    settings.read_config_file(path)
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    llm = document.get("llm")
    for wanted in (name, ollama_profile):
        if wanted is not None and (llm is None or wanted not in (llm.get("profiles") or {})):
            raise settings.ConfigError(path, f"the global config has no LLM profile {wanted!r}")
    if llm is None:
        return path  # Nothing to write: no profile is named.
    if name is not None:
        llm["profile"] = name
    if ollama_profile is not None:
        llm["ollama_profile"] = ollama_profile
    _atomic_write(path.resolve(), tomlkit.dumps(document))
    return path
