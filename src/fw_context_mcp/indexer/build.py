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
import shutil
import subprocess
from dataclasses import dataclass, field, fields
from pathlib import Path

from fw_context_mcp.utils import cc_output_path

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

    # ── Zephyr sysbuild (multi-image) + multi-variant ──
    source_dir: str | None = None  # sysbuild source app dir (e.g. "proj/app")
    sysbuild: bool = False  # use `west build --sysbuild`
    build_dir: str | None = None  # build output dir override (default "build")

    # Multi-variant build configuration (opt-in).  When empty, every builder
    # produces a single compile_commands.json (variant='' image='' board='').
    default_variant: str | None = None  # name of default variant (fail-closed queries)
    default_image: str | None = None  # name of default image within default_variant
    variants: list[BuildVariant] = field(default_factory=list)

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

    # ── Build environment (compile-affecting, stored in config.toml) ──
    # env vars passed to the build command and folded into config_hash.  Must
    # mirror exactly the build-affecting variables of the project — machine-
    # specific values go in extra_env (local.toml) instead.
    env: dict[str, str] = field(default_factory=dict)

    # ── Build environment (machine-specific, stored in local.toml) ──
    activate: str | None = None  # shell script sourced before build (Zephyr, ESP-IDF, etc.)
    python: str | None = None  # Python interpreter for pip-based CLI tools (mbed-cli, pio, etc.)
    extra_env: dict[str, str] = field(default_factory=dict)  # extra environment variables
    extra_path: list[str] = field(default_factory=list)  # directories prepended to PATH

    # ── Pre-build hooks ──
    pre_build: str | None = None  # shell command run before build/convert/generate
    timeout: float = 7200  # build timeout in seconds — long first builds (Zephyr, ESP-IDF) must not be killed at 10 min


@dataclass(slots=True)
class BuildImage:
    """One sysbuild image — a sub-project of a multi-image build.

    ``image`` and sub-project are 1:1 — an image IS a part of the project.
    ``name`` is the mapping key (= basename of the per-image build dir);
    ``dir`` is the SOURCE dir (may point outside project_root, e.g. an SDK
    image ``mcuboot``), kept only for LLM orientation.  ``type`` is a display
    hint for the LLM (``project`` vs ``sdk``) — it does NOT change is_project
    classification nor exclude the image from indexing.
    """

    name: str
    description: str = ""
    dir: str = ""
    type: str = "project"  # "project" | "sdk" — display hint only
    board: str | None = None  # per-image board override (e.g. FLPR -> cpuflpr)


@dataclass(slots=True)
class BuildVariant:
    """A named build configuration within a project.

    One project may build N variants (different boards/envs/flags) × M images.
    Each (variant, image) pair produces one compile_commands.json → one
    config_hash.  ``board`` is the variant default board; individual images
    may override it (FLPR asymmetry).  ``overrides`` holds arbitrary
    ``[build]`` keys that override the shared top-level defaults per the
    scalar-override / list-replace / dict-merge rules.
    """

    name: str
    description: str = ""
    board: str | None = None
    build_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    images: list[BuildImage] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)


# BuildConfig fields classified by per-variant merge semantics.
#
# WHY explicit lists: TOML variant tables may override any [build] key, but
# the merge rule depends on the field type — scalars override, lists replace,
# dicts merge per-key.  Classification lives here (not in settings.py) so the
# merge is next to the dataclass that owns the fields.
_SCALAR_FIELDS: frozenset[str] = frozenset({
    "system", "clean", "command", "target", "toolchain", "profile",
    "app_config", "board", "idf_path", "fqbn", "cmake_generator",
    "keil_project", "keil_target", "keil_cmsis_path",
    "iar_project", "iar_target", "makefile", "make_target",
    "make_dry_run", "toolchain_path", "toolchain_prefix", "compiler",
    "activate", "python", "pre_build", "timeout",
    "source_dir", "sysbuild", "build_dir",
})
_LIST_FIELDS: frozenset[str] = frozenset({
    "extra_profiles", "defines", "include_dirs", "system_include_dirs",
    "extra_flags", "source_dirs", "extra_path",
})
_DICT_FIELDS: frozenset[str] = frozenset({
    "env", "make_vars", "extra_env",
})


def build_variant_config(base: BuildConfig, variant: BuildVariant) -> BuildConfig:
    """Construct the effective per-variant ``BuildConfig``.

    WHY this function: a variant is a partial ``[build]`` override, not a full
    config.  The effective config is the top-level ``[build]`` with the
    variant's values applied according to the merge table — scalars override,
    lists replace (authoritative), dicts merge per-key.  This gives the user
    full control, including REMOVING a shared list item (a list replace, not
    an append, allows dropping an entry from ``[build]``).

    ``env`` from the variant is merged per-key over the shared ``[build] env``
    — build env vars are the dict case, never replaced wholesale.

    The result is a fresh ``BuildConfig`` (shallow copy of *base*) so the
    shared top-level config is never mutated across variants.
    """
    cfg = BuildConfig()
    for f in fields(BuildConfig):
        if not f.init:
            continue
        val = getattr(base, f.name)
        if f.name in _LIST_FIELDS:
            val = list(val)
        elif f.name in _DICT_FIELDS:
            val = dict(val)
        setattr(cfg, f.name, val)

    # Explicit variant fields (board/build_dir) are scalar overrides; env is
    # a dict merge.  images/default_* are not part of the effective build.
    if variant.board is not None:
        cfg.board = variant.board
    if variant.build_dir is not None:
        cfg.build_dir = variant.build_dir
    if variant.env:
        merged_env = dict(base.env)
        merged_env.update(variant.env)
        cfg.env = merged_env

    # Arbitrary [build] overrides, classified by field type.
    for attr, value in variant.overrides.items():
        if attr in _SCALAR_FIELDS:
            setattr(cfg, attr, value)
        elif attr in _LIST_FIELDS:
            setattr(cfg, attr, list(value))
        elif attr in _DICT_FIELDS:
            merged = dict(getattr(base, attr))
            merged.update(value)
            setattr(cfg, attr, merged)
        else:
            # A key that no field table knows was dropped in silence, so a
            # typo in [[build.variants]] had no effect and no message.  The
            # user then reads the config as applied when it is not.
            log.warning(
                "Variant %r: unknown [build] key %r — it has no effect",
                variant.name, attr,
            )

    return cfg


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
    result = subprocess.run(shlex.split(cfg.pre_build), shell=False, cwd=cwd, timeout=cfg.timeout)
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
        # A custom command runs an arbitrary build tool in the project root;
        # its native output lands at ``compile_commands.json``.  Copy it to
        # the gitignored fw-context build dir for a stable, reusable location.
        native_cc = root / "compile_commands.json"
        if not native_cc.exists():
            raise RuntimeError("compile_commands.json was not generated")
        cc_path = cc_output_path(root)
        shutil.copy2(native_cc, cc_path)
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


def resolve_reuse_compile_commands(project_root: Path, configured: Path) -> Path:
    """Return the compile_commands.json to reuse when not rebuilding.

    WHY this self-heal: every builder writes the generated database to the
    canonical fw-context-owned location ``cc_output_path()``
    (``.fw-context/build/compile_commands.json``).  Projects initialised
    before that location was introduced still carry a legacy
    ``[index] compile_commands = "compile_commands.json"`` value that points
    at the project root — a file ``--build`` no longer produces.  Reusing that
    stale root file instead of the freshly generated one changes the
    config_hash and silently drops the injected ``-D`` defines (e.g.
    ``SKEY_INIT_BASE64``) and any newly listed translation units.

    When the configured path is exactly the legacy root file and the canonical
    file exists, prefer the canonical file.  All other configured paths
    (explicit user overrides) are returned unchanged.
    """
    root = project_root.resolve()
    if not configured.is_absolute():
        configured = (root / configured).resolve()
    legacy = root / "compile_commands.json"
    canonical = cc_output_path(root)
    if configured == legacy and canonical.exists():
        return canonical
    return configured


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
