"""TOML values for the config files that the tests write as text."""

from __future__ import annotations

import tomli_w


def toml_string(value: object) -> str:
    """Return ``str(value)`` as a TOML basic string, with its quotes.

    WHY: a test that writes ``db_dir = "{path}"`` with an f-string writes a
    Windows path such as ``C:\\Users\\...`` without escapes.  TOML reads
    ``\\U`` as the start of a Unicode escape, and the config is not valid.
    ``tomli_w`` writes each character that TOML requires as an escape.
    """
    line = tomli_w.dumps({"value": str(value)})
    return line.removeprefix("value = ").rstrip("\n")
