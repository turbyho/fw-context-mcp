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

from ._linker import LinkRecord
from .protocol import BuildSystem

log = logging.getLogger(__name__)


def background_build_safe(builder: BuildSystem | None, cfg) -> bool:
    """Tell whether fw-context may start a build of *builder* on its own.

    The question is whether the build can DAMAGE the build of the user, not
    whether it writes no file at all.  A backend qualifies when it keeps its
    object files out of the directory the build of the user owns, or when it
    compiles nothing.  fw-context cannot lock the build of an IDE, so two
    builds sharing one output directory is the case this rule prevents.

    A generated file the project already treats as build output is not a
    violation.  ``fw-context init`` gitignores ``compile_commands.json`` for
    exactly that reason — see ``cli/_init.py:_ensure_gitignore`` — and
    PlatformIO rewrites it in the project root on every ``-t compiledb``.
    The rule used to read "nothing reaches the tree of the user", which that
    backend has never satisfied; stating an invariant the code does not hold
    is how the next backend gets written against a promise that is not kept.

    A backend that does not implement ``background_build_safe`` answers no.
    The concrete classes implement the ``BuildSystem`` protocol structurally,
    not by inheritance, thus a default on the protocol would never reach
    them, and the safe answer is the negative one.
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


def output_compile_commands(builder: BuildSystem | None, out_dir: Path, cfg) -> dict[str, Path]:
    """Return the compilation databases that the build in *out_dir* produced.

    The key is the image name, and "" names the only program of a build that
    makes one.  Only files that exist are in the result, thus an empty result
    means that the build is not there.  *cfg* is the ``BuildConfig`` of the
    build, because the configuration selects the command, and the command
    selects where the database is (``west build`` or ``west build
    --sysbuild``).

    ``output_compile_commands`` is an OPTIONAL method of a backend, for a
    build system that puts the database somewhere else than
    ``out/compile_commands.json``: one database per image (Zephyr sysbuild),
    or one per environment (PlatformIO).  A backend without the method writes
    ``out/compile_commands.json``.  The concrete classes implement the
    ``BuildSystem`` protocol structurally, thus a default on the protocol
    would never reach them.
    """
    from ..build_layout import COMPILE_COMMANDS_NAME

    probe = getattr(builder, "output_compile_commands", None) if builder is not None else None
    if probe is not None:
        return dict(probe(out_dir, cfg))
    single = out_dir / COMPILE_COMMANDS_NAME
    return {"": single} if single.is_file() else {}


def application_database(builder: BuildSystem | None, out_dir: Path) -> Path | None:
    """Return the database of the application of a build in *out_dir*, or None.

    A query without ``image`` gets the image whose index read this database,
    when ``[build] default_image`` names none of the images (see
    ``mcp.shared.variants.resolve_build``).  ``application_database`` is an
    OPTIONAL method of a backend whose build makes the application and other
    programs of lower rank: ESP-IDF makes the application and its
    bootloader, and a Zephyr sysbuild names its default image in
    ``domains.yaml``.  None when the backend does not name one, and then a
    query must name the image.  The database need not exist: ESP-IDF answers
    with a path alone, thus the answer holds while a clean build has removed
    *out_dir*.
    """
    probe = getattr(builder, "application_database", None) if builder is not None else None
    if probe is None:
        return None
    # A backend can answer None for one build: Zephyr without sysbuild.
    database = probe(out_dir)
    return Path(database) if database is not None else None


def implicit_variants(builder: BuildSystem | None, project_root: Path, cfg) -> list:
    """Return the variants that the project file of the build system declares.

    ``implicit_variants`` is an OPTIONAL method of a backend whose project
    file holds more than one build: each ``[env:<name>]`` of
    ``platformio.ini`` is a build of its own.  The caller asks only for a
    project without ``[[build.variants]]``.  The method returns a list of
    ``BuildVariant``, an empty list for one build, and it raises
    RuntimeError when the build system cannot answer.  It may also set the
    one build on *cfg*, as the PlatformIO backend sets ``environment``.
    """
    probe = getattr(builder, "implicit_variants", None) if builder is not None else None
    if probe is None:
        return []
    return list(probe(project_root, cfg))


def linker_scripts(
    builder: BuildSystem | None,
    project_root: Path,
    *,
    compile_commands: Path | None = None,
    variant: str = "",
    units: list | None = None,
) -> list[Path]:
    """Return the linker scripts of *builder*, or an empty list.

    A backend that does not implement ``get_linker_scripts`` answers with
    nothing, which is correct: the concrete classes implement the
    ``BuildSystem`` protocol structurally and not by inheritance, thus a
    default on the protocol never reaches them.

    A backend that raises answers with nothing too.  A missing memory map
    must not stop an index run, and a partial index is better than none.
    """
    if builder is None:
        return []
    probe = getattr(builder, "get_linker_scripts", None)
    if probe is None:
        return []
    try:
        found = probe(
            project_root,
            compile_commands=compile_commands,
            variant=variant,
            units=units,
        )
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
        log.debug("get_linker_scripts failed for %r", builder, exc_info=True)
        return []
    return [Path(item) for item in found or []]


def link_record(
    builder: BuildSystem | None,
    project_root: Path,
    *,
    compile_commands: Path | None = None,
    variant: str = "",
    units: list | None = None,
) -> LinkRecord | None:
    """Return what *builder* knows about the link of this build, or None.

    None means "the backend does not know", and the pass then keeps the
    rows of an earlier run.  A record with no script means "the link names
    no script", and the pass removes them.  `get_linker_scripts` cannot
    give that difference: its empty list means both.  A database whose
    build directory holds no `build.ninja` gives an empty list that means
    "do not know".

    ``get_link_record`` is an OPTIONAL method with the arguments of
    ``get_linker_scripts``.  It is not on the ``BuildSystem`` protocol, for
    the same reason that ``build_multi`` is not: only a backend that records
    the link command has the answer, and a protocol method would make each
    backend write an empty one.  It also gives the ``--defsym`` definitions,
    because a linker script can use a name that only the link command
    defines.

    A backend without the method answers through ``get_linker_scripts``: a
    non-empty list is a record, and an empty list is None.  A backend that
    raises, or that answers with a wrong type, gives None.
    """
    if builder is None:
        return None
    probe = getattr(builder, "get_link_record", None)
    if probe is None:
        scripts = linker_scripts(
            builder, project_root,
            compile_commands=compile_commands, variant=variant, units=units,
        )
        return LinkRecord(scripts=scripts) if scripts else None
    try:
        found = probe(
            project_root,
            compile_commands=compile_commands,
            variant=variant,
            units=units,
        )
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError):
        log.debug("get_link_record failed for %r", builder, exc_info=True)
        return None
    if found is None or not isinstance(found, LinkRecord) or not isinstance(found.defsyms, dict):
        return None
    if not all(isinstance(path, Path) for path in found.scripts) or not all(
        isinstance(name, str) and (expression is None or isinstance(expression, str))
        for name, expression in found.defsyms.items()
    ):
        return None
    return found


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
