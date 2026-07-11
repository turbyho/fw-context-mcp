"""Build system abstraction — detection, build, validation, and auto-fix.

Each supported build system has a class implementing the ``BuildSystem``
protocol.  ``BuildSystemRegistry`` discovers the active system from project
markers and holds the corresponding builder instance.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .protocol import BuildSystem

log = logging.getLogger(__name__)


class BuildSystemRegistry:
    """Holds registered build systems and delegates detection to them.

    Each builder class is registered with its ``config_key`` (the string
    used in ``[build] system = "..."`` config).  Detection iterates all
    registered builders and returns the first match.
    """

    def __init__(self) -> None:
        self._builders: dict[str, type[BuildSystem]] = {}

    def register(self, builder_cls: type[BuildSystem]) -> None:
        """Register a build system class.

        The class must have a ``config_key`` attribute and implement the
        ``BuildSystem`` protocol.
        """
        self._builders[builder_cls.config_key] = builder_cls

    def detect(self, project_root: Path) -> str | None:
        """Return the config_key of the first matching build system, or None."""
        root = project_root.resolve()
        for key, builder_cls in self._builders.items():
            try:
                if builder_cls.detect(root):
                    log.debug("Detected build system: %s", key)
                    return key
            except Exception:
                log.debug("Builder %s raised during detection", key, exc_info=True)
                continue
        return None

    def get(self, config_key: str) -> type[BuildSystem] | None:
        """Return the builder class for *config_key*, or None."""
        return self._builders.get(config_key)

    def keys(self) -> list[str]:
        """Return all registered config keys."""
        return list(self._builders.keys())


# Singleton registry — populated by builder modules on import.
registry = BuildSystemRegistry()

# Import builder modules so they self-register via ``registry.register()``.
# isort: split
from . import mbed_os  # noqa: F401, E402, I001
from . import platformio  # noqa: F401, E402, I001
from . import zephyr  # noqa: F401, E402, I001
