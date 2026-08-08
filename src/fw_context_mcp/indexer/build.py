"""Build system detection and compile_commands.json generation.

WHY: Firmware projects use disparate build systems (Mbed OS, Zephyr,
PlatformIO, CMake, Keil, IAR, bare Makefiles).  libclang-based indexing
requires a compile_commands.json — a JSON compilation database listing every
translation unit with its exact compiler flags.  This module provides a
build-system-agnostic interface: detect the system from project markers,
then delegate to the appropriate backend to produce a fresh, complete
compile_commands.json.

Supports Mbed OS, Zephyr, PlatformIO, CMake, Arduino, Keil MDK, IAR EWARM,
bare Makefile (via compiledb), and bare/manual mode.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Import builders package so the registry is populated with all registered
# build system backends before ``detect_build_system()`` is called.
from . import builders  # noqa: F401 — side-effect import
from .builders import registry as _builder_registry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BuildConfig:
    """Build system configuration for compile_commands.json generation.

    Attributes:
        system: Build system name — ``"mbed-os"``, ``"zephyr"``, ``"platformio"``,
            or None (auto-detect from project markers).
        clean: Always clean-build before generating compile_commands.json.
        command: Full shell command override — bypasses all detection when set.
        target: Mbed OS target board name (e.g. ``"P_ECB_BOARD"``).
        toolchain: Mbed OS toolchain (e.g. ``"GCC_ARM"``).
        profile: Mbed OS build profile (default ``"develop"``).
        app_config: Path to Mbed OS app config JSON (default ``"mbed_app.json"``).
        extra_profiles: Additional Mbed OS profiles merged on top (default ``["lto.json"]``).
        defines: Extra ``-D`` preprocessor macros passed to the compiler.
        board: Zephyr board name (e.g. ``"nrf52840dk_nrf52840"``). Required for Zephyr.
        idf_path: Path to ESP-IDF install (usually ``$IDF_PATH``).
        fqbn: Arduino Fully Qualified Board Name (e.g. ``"arduino:avr:uno"``).
        cmake_generator: CMake generator (e.g. ``"Ninja"``, ``"Unix Makefiles"``).
        keil_project: Path to Keil MDK ``.uvprojx`` file (relative to project root).
        keil_target: Keil target name within the project (optional).
        keil_cmsis_path: Path to CMSIS headers for Keil projects.
        iar_project: Path to IAR EWARM ``.ewp`` file (relative to project root).
        iar_target: IAR target name within the project (optional).
        makefile: Path to Makefile (default: ``Makefile`` in project root).
        make_target: Make build target (default: ``"all"``).
        make_vars: Extra variables passed to make (e.g. ``{V: "1"}``).
        make_dry_run: Use ``compiledb -n`` dry-run instead of real build.
        toolchain_path: Path to toolchain binaries (shared by Keil, IAR, Makefile).
        toolchain_prefix: Toolchain prefix (e.g. ``"arm-none-eabi-"``).
        include_dirs: Directories added via ``-I`` (manual/bare mode).
        system_include_dirs: Directories added via ``-isystem`` (manual/bare mode).
        extra_flags: Extra compiler flags (manual/bare mode).
        source_dirs: Directories scanned for ``.c``/``.cpp`` files (manual/bare mode).
        compiler: Compiler executable name (manual/bare mode, default ``"gcc"``).
        pre_build: Shell command run before build/convert/generate.
    """

    system: str | None = None  # "mbed-os", "zephyr", "platformio", or None (auto-detect)
    clean: bool = True  # always clean build before generating
    command: str | None = None  # full override — runs as-is, bypasses all detection

    # Mbed OS overrides (auto-detected from .mbed / custom_targets.json)
    target: str | None = None
    toolchain: str | None = None
    profile: str = "develop"
    app_config: str = "mbed_app.json"
    extra_profiles: list[str] = field(default_factory=lambda: ["lto.json"])
    defines: list[str] = field(default_factory=list)  # extra -D flags for the compiler

    # Zephyr override (required, no safe auto-detection)
    board: str | None = None

    # ESP-IDF (optional — auto-detected from environment)
    idf_path: str | None = None  # Path to ESP-IDF install (usually $IDF_PATH)

    # Arduino (required for build — no safe auto-detection)
    fqbn: str | None = None  # Fully Qualified Board Name, e.g. "arduino:avr:uno"

    # Generic CMake (optional)
    cmake_generator: str | None = None  # e.g. "Ninja", "Unix Makefiles"

    # ── Keil MDK (convert path — no build needed) ──
    keil_project: str | None = None  # path to .uvprojx
    keil_target: str | None = None  # target name within the project
    keil_cmsis_path: str | None = None  # path to CMSIS headers

    # ── IAR EWARM (convert path — no build needed) ──
    iar_project: str | None = None  # path to .ewp
    iar_target: str | None = None  # target name within the project

    # ── Makefile (generate via compiledb) ──
    makefile: str | None = None  # path to Makefile (default: project_root/Makefile)
    make_target: str = "all"  # build target
    make_vars: dict[str, str] = field(default_factory=dict)  # extra vars for make
    make_dry_run: bool = True  # use compiledb -n (dry-run, no real build)

    # ── Toolchain (shared by Keil, IAR, Makefile) ──
    toolchain_path: str | None = None  # path to toolchain bin directory
    toolchain_prefix: str | None = None  # e.g. "arm-none-eabi-"

    # ── Manual / bare mode ──
    include_dirs: list[str] = field(default_factory=list)  # -I directories
    system_include_dirs: list[str] = field(default_factory=list)  # -isystem directories
    extra_flags: list[str] = field(default_factory=list)  # extra compiler flags
    source_dirs: list[str] = field(default_factory=list)  # directories to scan for sources
    compiler: str = "gcc"  # compiler executable name

    # ── Build environment (machine-specific, stored in local.toml) ──
    activate: str | None = None  # shell script sourced before build (Zephyr, ESP-IDF, etc.)
    python: str | None = None  # Python interpreter for pip-based CLI tools (mbed-cli, pio, etc.)
    extra_env: dict[str, str] = field(default_factory=dict)  # extra environment variables
    extra_path: list[str] = field(default_factory=list)  # directories prepended to PATH

    # ── Pre-build hooks ──
    pre_build: str | None = None  # shell command run before build/convert/generate


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_build_system(project_root: Path) -> str | None:
    """Detect the build system from project markers.

    WHY scoring rather than exact match: a project may contain markers from
    multiple build systems (e.g. ``CMakeLists.txt`` + ``.mbed`` in a hybrid
    project).  Scoring by marker count picks the dominant system.

    Delegates to the ``BuildSystemRegistry`` — each registered builder's
    ``markers`` list is scored by how many markers exist in *project_root*.
    The builder with the highest score wins.

    Returns one of ``"mbed-os"``, ``"zephyr"``, ``"platformio"``, or
    ``None`` when nothing is recognised.
    """
    root = project_root.resolve()
    scores: dict[str, int] = {}

    for config_key in _builder_registry.keys():
        builder_cls = _builder_registry.get(config_key)
        if builder_cls is None:
            continue
        markers: list[str] = getattr(builder_cls, "markers", [])
        for marker in markers:
            if (root / marker).exists():
                scores[config_key] = scores.get(config_key, 0) + 1

    if not scores:
        return None

    # Return the system with the most markers matched
    return max(scores, key=lambda k: scores[k])


# ---------------------------------------------------------------------------
# Mbed OS helpers
# ---------------------------------------------------------------------------


def _parse_mbed_dotfile(project_root: Path) -> dict[str, str]:
    """Parse ``.mbed`` into a dict of KEY=VALUE pairs.

    Re-exported from ``MbedOSBuildSystem`` for backward compatibility.
    """
    from .builders.mbed_os import _parse_mbed_dotfile as _fn
    return _fn(project_root)


def _mbed_target_from_custom_targets(project_root: Path) -> str | None:
    """Extract the first board name from custom_targets.json.

    Re-exported from ``MbedOSBuildSystem`` for backward compatibility.
    """
    from .builders.mbed_os import _mbed_target_from_custom_targets as _fn
    return _fn(project_root)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_completeness(cc_path: Path, project_root: Path) -> list[str]:
    """Return a list of warnings if compile_commands.json seems incomplete.

    WHY: a build system may produce a partially-empty compile_commands.json
    (e.g. after ``mbed deploy`` pulls new libraries without a rebuild).
    Catching this early avoids indexing an incomplete project silently.

    Heuristic: count source files (.c, .cpp) in common directories and
    compare with the number of entries in compile_commands.json.
    """
    import json

    warnings: list[str] = []
    try:
        data = json.loads(cc_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ["Cannot parse compile_commands.json"]

    cc_count = len(data)
    if cc_count == 0:
        return ["compile_commands.json is empty — build may have produced nothing"]

    # Count source files in src/, lib/, app/
    source_dirs = ["src", "lib", "app"]
    source_count = 0
    for d in source_dirs:
        sd = project_root / d
        if sd.is_dir():
            source_count += len(list(sd.rglob("*.c")))
            source_count += len(list(sd.rglob("*.cpp")))

    # Heuristic: if there are more source files than compile_commands entries,
    # it's likely incomplete (compile_commands should include OS files too)
    if source_count > 0 and cc_count < source_count:
        warnings.append(
            f"compile_commands.json has {cc_count} entries but there are "
            f"at least {source_count} source files in src/lib/app — "
            f"the index may be incomplete.  Run 'fw-context index --build' "
            f"to regenerate."
        )

    return warnings


def _run_pre_build(cfg: BuildConfig, cwd: Path) -> None:
    """Execute the pre-build hook if configured.

    WHY: some build systems require environment setup scripts (``west zephyr-export``,
    ``idf.py set-target``) before the actual build can proceed.  The pre-build
    hook runs these once, before any build/convert/generate step.
    """
    if not cfg.pre_build:
        return
    log.warning(
        "Running pre-build hook: %s\n"
        "Pre-build hooks execute arbitrary shell commands.  For security, "
        "configure pre_build only in .fw-context/local.toml (gitignored), "
        "not in .fw-context/config.toml (committed).",
        cfg.pre_build,
    )
    import shlex
    result = subprocess.run(shlex.split(cfg.pre_build), shell=False, cwd=cwd, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Pre-build command failed with exit code {result.returncode}")


def generate_compile_commands(
    project_root: Path,
    cfg: BuildConfig,
) -> Path:
    """Generate a fresh compile_commands.json and return its path.

    WHY four paths: different build systems produce compile_commands.json
    differently.  Some require a full build (PlatformIO), others can convert
    their project files statically (Keil, IAR), and bare Makefiles can use
    ``compiledb`` dry-run.  This function picks the cheapest available path.

    Auto-detects the build system when ``cfg.system`` is ``None``.
    The generation path is chosen by builder capability:

    1. **Shell override** — ``cfg.command`` runs as-is, highest priority.
    2. **Convert** — builder has ``convert()`` (Keil, IAR — no build).
    3. **Generate** — builder has ``generate()`` (Makefile/compiledb, manual/bare).
    4. **Build** — builder has ``build()`` (PlatformIO, Zephyr, Mbed OS, …).

    Raises ``RuntimeError`` when detection or generation fails.
    """
    root = project_root.resolve()

    # Full command override — highest priority
    if cfg.command:
        _run_pre_build(cfg, root)
        log.info("Running custom build command: %s", cfg.command)
        import shlex
        result = subprocess.run(shlex.split(cfg.command), shell=False, cwd=root)
        if result.returncode != 0:
            raise RuntimeError(f"Build command failed with exit code {result.returncode}")
        cc_path = root / "compile_commands.json"
        if not cc_path.exists():
            raise RuntimeError("compile_commands.json was not generated")
        return cc_path

    # Detect or validate
    system = cfg.system or detect_build_system(root)
    if not system:
        raise RuntimeError(
            "Cannot detect build system.  Set it explicitly in .fw-context/config.toml:\n"
            "  [build]\n  system = \"mbed-os\"  # or \"zephyr\", \"platformio\"\n"
            "Or provide a custom build command:\n"
            "  [build]\n  command = \"bear -- make\""
        )

    builder_cls = _builder_registry.get(system)
    if builder_cls is None:
        raise RuntimeError(
            f"Unknown build system '{system}'.  Supported: {', '.join(sorted(_builder_registry.keys()))}"
        )

    log.info("Detected build system: %s (clean=%s)", system, cfg.clean)
    builder = builder_cls()

    # Run pre-build hook before any generation path
    _run_pre_build(cfg, root)

    # ── Path 2: Convert (Keil, IAR — no build needed) ──
    if hasattr(builder, "convert") and _can_convert(cfg, system):
        log.info("Using convert path for %s", system)
        return builder.convert(root, cfg)

    # ── Path 3: Generate (Makefile/compiledb, manual/bare) ──
    if hasattr(builder, "generate") and _can_generate(cfg, system):
        log.info("Using generate path for %s", system)
        return builder.generate(root, cfg)

    # ── Path 1: Build (PlatformIO, Zephyr, Mbed OS, ESP-IDF, CMake, Arduino) ──
    return builder.build(root, cfg)


def _can_convert(cfg: BuildConfig, system: str) -> bool:
    """Check whether the configuration supports the convert path."""
    if system == "keil-mdk":
        return cfg.keil_project is not None
    if system == "iar-ewarm":
        return cfg.iar_project is not None
    return False


def _can_generate(cfg: BuildConfig, system: str) -> bool:
    """Check whether the configuration supports the generate path."""
    if system == "makefile":
        # compiledb needs a Makefile
        return True  # Makefile builder always supports generate
    if system == "bare":
        return bool(cfg.source_dirs)
    return False
