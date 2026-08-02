"""Shared TOML config file editing utilities.

Provides atomic, section-aware TOML writes — a single point of truth
for the four config-editing sites in the codebase.

Uses stdlib ``tomllib`` for reading/validation and ``tomli_w`` for writing.
Comment preservation is NOT guaranteed — sites that need comment preservation
should use line-level editing instead (see ``_write_project_id`` for an
example of the pattern).
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def set_key(
    config_path: Path,
    section: str,
    key: str,
    value: object,
) -> None:
    """Atomically set *key* = *value* inside ``[section]`` of *config_path*.

    Creates the file (and parent directories) if missing.  Creates the
    section if it does not exist.  Overwrites an existing key silently.

    Uses ``tomli_w`` for output, so comments in the original file are
    NOT preserved.  All TOML data types are supported — strings are
    automatically quoted.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config (or start fresh)
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not parse %s, starting fresh: %s", config_path, exc)

    # Ensure section exists
    if section not in data:
        data[section] = {}

    section_data = data[section]
    if not isinstance(section_data, dict):
        log.warning(
            "Section [%s] in %s is not a table (type %s), overwriting",
            section, config_path, type(section_data).__name__,
        )
        section_data = {}
        data[section] = section_data

    section_data[key] = value

    # Atomic write
    import tomli_w  # lazy — only needed when actually writing

    new_content = tomli_w.dumps(data)
    _atomic_write(config_path, new_content)


def merge_template(
    config_path: Path,
    template: str,
) -> list[str]:
    """Merge missing keys from a TOML *template* string into *config_path*.

    Keys that already exist (in the correct section) are NOT overwritten.
    Keys commented out with ``#`` in the template are ignored (standard TOML
    comment handling).

    Returns the list of key names that were added.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Parse template
    try:
        template_data = tomllib.loads(template)
    except Exception as exc:
        log.error("Invalid template TOML: %s", exc)
        return []

    # Load existing config
    existing_data: dict[str, Any] = {}
    if config_path.exists():
        try:
            existing_data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("Could not parse %s, treating as empty", config_path)

    # Merge: add missing sections and keys
    added: list[str] = []
    for section, entries in template_data.items():
        if not isinstance(entries, dict):
            continue
        if section not in existing_data:
            existing_data[section] = {}
        target = existing_data[section]
        if not isinstance(target, dict):
            target = {}
            existing_data[section] = target
        for key, value in entries.items():
            if key not in target:
                target[key] = value
                added.append(key)

    if not added:
        return []

    # Atomic write
    import tomli_w  # lazy — only needed when actually writing

    new_content = tomli_w.dumps(existing_data)
    _atomic_write(config_path, new_content)
    return added


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via tempfile + rename."""
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.name + ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
