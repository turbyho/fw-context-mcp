"""Build system abstraction — detection, build, validation, and auto-fix.

Each supported build system has a class implementing the ``BuildSystem``
protocol.  ``BuildSystemRegistry`` discovers the active system from project
markers and holds the corresponding builder instance.

WHY registry pattern: each build system is self-contained — it knows its
detection markers, how to run the build, what tools it needs, and how to
auto-detect the environment.  Adding a new build system only requires a
new module and ``registry.register()`` — no changes to the core indexer.

Detection strategy (tiered):
Tier 1 — build: runs the native build tool (bear + cmake, pio run, west build,
  mbed compile, bare compilation) to produce compile_commands.json + .d files.
Tier 2 — detect only: recognizes IDE projects (STM32CubeIDE, TI CCS) and
  instructs the user how to generate compile_commands.json manually.
Tier 3 — generic fallback: tries cmake/make/makefile/compile_commands.json.
Tier 4 — manual: user provides source_dirs and flags in config; fw-context
  runs syntax-only compilation of each source file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .protocol import BuildSystem

log = logging.getLogger(__name__)


def background_build_safe(builder: BuildSystem | None, cfg) -> bool:
    """Tell whether fw-context may start a build of *builder* on its own.

    A backend that does not implement ``background_build_safe`` answers no.
    The concrete classes implement the ``BuildSystem`` protocol structurally,
    not by inheritance, thus a default on the protocol would never reach
    them, and the safe answer is the negative one: an automatic build that
    writes to the usual directory can corrupt the build that the user runs in
    an IDE at the same time, and fw-context cannot lock that build.
    """
    if builder is None:
        return False
    probe = getattr(builder, "background_build_safe", None)
    if probe is None:
        return False
    try:
        return bool(probe(cfg))
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
        # A backend that cannot answer must not be trusted with a build.
        log.debug("background_build_safe failed for %r", builder, exc_info=True)
        return False


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
            except (ValueError, TypeError, RuntimeError, AttributeError):
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
# Order matters: first-registered builder wins ties in the scoring system.
# Tier 1: already-supported build systems
from . import mbed_os  # noqa: F401, E402, I001
from . import platformio  # noqa: F401, E402, I001
from . import zephyr  # noqa: F401, E402, I001
# Tier 2: new first-class builders
from . import arduino  # noqa: F401, E402, I001
from . import esp_idf  # noqa: F401, E402, I001
from . import generic_cmake  # noqa: F401, E402, I001
# Tier 3/4: stubs (detect-only, no automated build)
from . import stubs  # noqa: F401, E402, I001
# Manual / bare mode (flags-driven compile_commands.json generation)
from . import manual  # noqa: F401, E402, I001
# Makefile via compiledb
from . import makefile  # noqa: F401, E402, I001
# Keil MDK and IAR EWARM via keil2clangd
from . import keil  # noqa: F401, E402, I001
from . import iar  # noqa: F401, E402, I001
