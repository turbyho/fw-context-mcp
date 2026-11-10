"""The text of a ``[build]`` setting for the help of a builder.

``environment_help()`` of a builder tells the user what to write into
``.fw-context/local.toml``.  The user copies the lines, thus they must be
valid TOML.  A Windows path in a basic string (``"C:\\Users\\..."``) is not:
TOML reads each backslash as the start of an escape.  tomli-w writes the
value with each escape that TOML needs.
"""

from __future__ import annotations

import textwrap

import tomli_w


def build_setting(key: str, value: str) -> str:
    """Return the ``[build]`` table with *key* = *value*, indented by two spaces."""
    return textwrap.indent(tomli_w.dumps({"build": {key: value}}).rstrip("\n"), "  ")
