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
import os
import shutil
import subprocess
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from fw_context_mcp.utils import (
    CC_STAGING_GLOB,
    build_cfg_env,
    cc_output_path,
    cc_staging_path,
    owner_is_dead,
    run_in_process_group,
    staging_owner,
)

# Import builders package so the registry is populated with all registered
# build system backends before ``detect_build_system()`` is called.
from . import builders  # noqa: F401 — side-effect import
from .build_layout import COMPILE_COMMANDS_NAME, BuildLayout
from .builders import output_compile_commands
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
        target: Mbed OS target board name (e.g. ``"BOARD_V2_BOARD"``).
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
    # A retired key.  Each build goes to .fw-context/build/<variant>/out (see
    # build_layout), thus the value has no effect.  It stays parsed only so
    # that the index run can refuse it (retired_build_dir_keys) instead of
    # ignoring it without a word.
    build_dir: str | None = None

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

    # ── Staging file for the compilation database ──
    # generate_compile_commands sets this, and no user configuration does.
    # The backend then writes the compilation database to this file instead
    # of the final compile_commands.json, and the caller renames it when the
    # build ends.  A reader of the final file thus never gets a database that
    # a build still writes.
    #
    # None means the final path — see utils.cc_output_path.
    cc_output: Path | None = None

    # ── Name of the variant this config builds ──
    # build_variant_config sets this, and no user configuration does.  It
    # selects the output directory of the build,
    # .fw-context/build/<variant>/out (see build_layout), and a backend that
    # records a property of its build beside that directory needs it too.
    # "" is the build with no variants.
    variant_name: str = ""

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

    ``index_overrides`` holds ``[index]`` keys.  Those ADD to the ``[index]``
    section, they do not replace it: a user with a value in both would
    otherwise lose the shared entries without a word, and that is the same
    class of silent wrongness as a lost staleness signal.  A variant can
    therefore not NARROW the vendor set; the channels for that are
    ``project_paths``, which wins over every vendor pattern, or a fix to the
    detection.
    """

    name: str
    description: str = ""
    board: str | None = None
    build_dir: str | None = None  # retired, see BuildConfig.build_dir
    env: dict[str, str] = field(default_factory=dict)
    images: list[BuildImage] = field(default_factory=list)
    overrides: dict = field(default_factory=dict)
    # ``[index]`` keys written in this variant's table.  Kept apart from
    # ``overrides`` because build_variant_config applies BuildConfig fields
    # only: an [index] key landing there was dropped in silence, and since
    # the unknown-key warning it would be reported as a typo.
    index_overrides: dict = field(default_factory=dict)


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
    "source_dir", "sysbuild",
})
_LIST_FIELDS: frozenset[str] = frozenset({
    "extra_profiles", "defines", "include_dirs", "system_include_dirs",
    "extra_flags", "source_dirs", "extra_path",
})
_DICT_FIELDS: frozenset[str] = frozenset({
    "env", "make_vars", "extra_env",
})


def retired_build_dir_keys(cfg: BuildConfig) -> list[str]:
    """Return where the config still sets the retired ``build_dir``.

    Each build goes to ``.fw-context/build/<variant>/out`` (see
    ``build_layout``), never to a directory of the user, thus ``build_dir``
    has no effect.  The index run refuses such a config with the list that
    this function gives: a key that the config shows and the build ignores
    lets the user think that the build goes somewhere else.
    """
    found: list[str] = []
    if cfg.build_dir is not None:
        found.append("[build] build_dir")
    found.extend(
        f"[[build.variants]] {variant.name!r} build_dir"
        for variant in cfg.variants
        if variant.build_dir is not None
    )
    return found


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

    # The explicit variant field board is a scalar override; env is a dict
    # merge.  images/default_* are not part of the effective build, and
    # neither is the retired build_dir (see retired_build_dir_keys).
    cfg.variant_name = variant.name
    if variant.board is not None:
        cfg.board = variant.board
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
    log.info("Running pre-build hook: %s", cfg.pre_build)
    import shlex
    # build_env, not the raw inherited environment: a hook the harness put in
    # BASH_ENV hijacks any `bash -c` the user configures here — see utils.
    # build_cfg_env adds [build] env, extra_path and extra_env, as for every
    # builder command.
    # In its own process group, as each build command — see
    # utils.run_in_process_group.  The timeout is a RuntimeError, because
    # each caller of generate_compile_commands catches only that.
    try:
        result = run_in_process_group(
            shlex.split(cfg.pre_build), cwd=cwd, env=build_cfg_env(cfg),
            timeout=cfg.timeout, capture_output=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Pre-build command timed out after {cfg.timeout}s") from None
    if result.returncode != 0:
        raise RuntimeError(f"Pre-build command failed with exit code {result.returncode}")


def clear_dead_staging_files(staging: Path) -> None:
    """Remove the staging files that stopped builds left in the directory.

    A build that a signal stops leaves its staging file behind, and that file
    holds a whole compilation database — megabytes on a large project.  The
    name carries the owner token of the process that writes it (see
    ``utils.owner_token``), thus a file whose process no longer runs is
    garbage.  A file of a process that still runs stays: that build writes
    it.  A file from a different PID namespace stays too, because this
    process cannot see if its owner runs.

    Best-effort.  A file that cannot be deleted costs disk space only, thus
    no failure here may stop the build.  The unlink is thus outside the
    ownership check, with its own handler: an exception in an ``except``
    clause does not go to the next clause of the same ``try``.
    """
    for candidate in staging.parent.glob(CC_STAGING_GLOB):
        if candidate == staging:
            continue
        token = staging_owner(candidate)
        if token is None or not owner_is_dead(token):
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("Cannot remove the dead staging file %s: %s", candidate, exc)


def generate_compile_commands(project_root: Path, cfg: BuildConfig) -> Path:
    """Run the build of the variant that *cfg* names, and return its compilation database.

    The build writes into ``.fw-context/build/<variant>/out`` (see
    ``build_layout``), the variant from ``cfg.variant_name``.  The result is
    the database that the build system wrote there, or
    ``out/compile_commands.json`` for a tool that fw-context gives the
    output path to.

    Such a tool writes a staging file, and one atomic rename then gives
    ``out/compile_commands.json`` the new content.  WHY: on 2026-09-23 two
    builds of one project wrote one database at the same time and each
    destroyed the output of the other.  The index lock in ``cli/_index``
    keeps two builds apart, and this rename keeps a reader from getting a
    database that a build still writes.

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
    layout = BuildLayout(root)
    final = layout.out_dir(cfg.variant_name) / COMPILE_COMMANDS_NAME
    # The staging file sits in the directory of the variant, beside out/ and
    # not in it: `mbed compile --clean`, `pio run` after a change of
    # project.checksum and a pristine build remove out/ while the build runs,
    # and bear keeps its intermediate file beside its output.  The rename
    # into out/ stays in one file system, thus it stays atomic.
    staging = cc_staging_path(layout.variant_dir(cfg.variant_name))
    clear_dead_staging_files(staging)
    # A file with the name of this run can already exist: a run that SIGKILL
    # stopped left it, and this run got the same PID again.  A backend that
    # reports success and writes nothing would then pass the exists() check
    # below, and the rename would publish that partial file.
    staging.unlink(missing_ok=True)
    try:
        produced = _generate_into(root, replace(cfg, cc_output=staging))
        if produced != staging:
            # The database that the build system wrote in the output
            # directory (CMake, Zephyr, ESP-IDF, Arduino).  It stays where it
            # is: it is the input of the index, and its directory holds the
            # build.ninja that the linker pass reads.
            return produced
        if not staging.exists():
            # A backend that returns its output path without writing the file.
            # os.replace would raise FileNotFoundError here, and no caller
            # catches that — every one of them catches RuntimeError.
            raise RuntimeError(
                f"The build reported success and wrote no compilation database to {staging}"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, final)
        return final
    finally:
        # A failed build leaves a partial staging file.  It must not stay —
        # the file is large and no later run can use it.
        staging.unlink(missing_ok=True)


def _generate_into(root: Path, cfg: BuildConfig) -> Path:
    """Run the generation path that *cfg* selects, and return what it wrote.

    *root* must be resolved.  ``cfg.cc_output`` names the file that each
    backend writes through :func:`cc_output_path` — see
    :func:`generate_compile_commands`, the only caller.
    """
    # Every path below writes into the build tree of fw-context, and a
    # builder creates its directory itself, so the rule that keeps it out of
    # git has to be in place before any of them runs.  One point covers the
    # custom command, convert, generate and build alike.
    BuildLayout(root).ensure_ignored()

    # Full command override — highest priority
    if cfg.command:
        _run_pre_build(cfg, root)
        log.info("Running custom build command: %s", cfg.command)
        import shlex
        # Same reason as the pre-build hook: `command = "bash -c ..."` is a
        # documented override, and BASH_ENV would hijack it.
        # In its own process group — see utils.run_in_process_group.
        result = run_in_process_group(
            shlex.split(cfg.command), cwd=root, env=build_cfg_env(cfg),
            timeout=None, capture_output=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Build command failed with exit code {result.returncode}")
        # A custom command runs an arbitrary build tool in the project root;
        # its native output lands at ``compile_commands.json``.  Copy it to
        # the output directory of the variant, where every other build puts
        # its database.
        native_cc = root / "compile_commands.json"
        if not native_cc.exists():
            raise RuntimeError("compile_commands.json was not generated")
        cc_path = cc_output_path(root, cfg)
        shutil.copy2(native_cc, cc_path)
        # A custom command records no link.  The PlatformIO backend keys its
        # record by the hash of the database, and a change of the link alone
        # keeps that hash, thus an old record would describe another link.
        from .builders._platformio_link import forget, sidecar_path
        forget(sidecar_path(BuildLayout(root).variant_dir(cfg.variant_name)))
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


def can_run_build(cfg: BuildConfig, system: str | None) -> bool:
    """Say if :func:`generate_compile_commands` can make compile_commands.json for *system*.

    A ``[build] command`` runs in all cases.  Otherwise the builder of
    *system* decides: a builder that only detects a project (a stub, such
    as STM32CubeIDE) sets ``can_build = False``, because its ``build()``
    only raises with instructions.  Such a build must run outside of
    fw-context, and the caller tells the user or the LLM so.
    """
    if cfg.command:
        return True
    builder_cls = _builder_registry.get(system) if system else None
    if builder_cls is None:
        return False
    return bool(getattr(builder_cls, "can_build", True))


def checked_compile_commands(project_root: Path, cfg, indexed: Path | None) -> Path | None:
    """Give the compile_commands.json whose build an index run checks, or None.

    It is the file that a run without an explicit file reads
    (:func:`default_compile_commands`), and only when the index came
    from it.  For the build of fw-context, "came from it" means that the
    index read a database in the output directory of the build.
    *indexed* is the file of the active index, or None when there is no
    index yet.  WHY the second condition: an index of another file (an
    explicit file of the user, or a file that an earlier config named) is
    not the build of this file.  A check of it would replace an explicit
    file with a build, or start a build that never repairs what it checks.

    None for a project with ``[[build.variants]]``: each variant has a file
    of its own, and this check does not cover them.

    The CLI and ``get_active_build`` both ask here, thus the two cannot
    disagree about which build is missing.
    """
    if cfg.build.variants:
        return None
    explicit = explicit_compile_commands(project_root, cfg)
    if explicit is not None:
        if indexed is not None and explicit != indexed.resolve():
            return None
        return explicit
    if indexed is None:
        return build_compile_commands(project_root, cfg.build, "")
    # The database of the build is where the build system put it in the
    # output directory: out/compile_commands.json, or deeper, as
    # out/zephyr/compile_commands.json.  When the build is gone, the path of
    # the index is the only record of where it was, thus the test is "in the
    # output directory", and not one fixed path that a removed build cannot
    # give any more.
    out_dir = BuildLayout(project_root.resolve()).out_dir("")
    if not indexed.resolve().is_relative_to(out_dir.resolve()):
        return None
    return indexed


# The values of ``[index] compile_commands`` that name no file of the user:
# the project-root file that ``fw-context init`` wrote before 2026-08, and the
# copy in .fw-context/build/ that fw-context kept before the build layout.
# For a project that fw-context builds, each of the two means "the database
# of the build", which is now in the output directory of the build.
_BUILD_DATABASE_VALUES: tuple[Path, ...] = (
    Path("compile_commands.json"),
    Path(".fw-context") / "build" / "compile_commands.json",
)


def explicit_compile_commands(project_root: Path, cfg) -> Path | None:
    """Return the compile_commands.json that ``[index] compile_commands`` names, or None.

    None means that the index reads the database of the build that
    fw-context runs (:func:`build_compile_commands`).  That is the case
    when the value is one of ``_BUILD_DATABASE_VALUES`` and fw-context can
    run the build of the project.

    A project whose build fw-context cannot run (a stub such as
    STM32CubeIDE, see :func:`can_run_build`) gets the configured file in all
    cases: the user makes that file outside of fw-context, often in the
    project root.

    The result is resolved, an absolute value too: a caller that compares it
    must not see /var and /private/var.
    """
    root = project_root.resolve()
    configured = (root / cfg.index.compile_commands).resolve()
    if not can_run_build(cfg.build, cfg.build.system or detect_build_system(root)):
        return configured
    if any(configured == (root / value).resolve() for value in _BUILD_DATABASE_VALUES):
        return None
    return configured


def default_compile_commands(project_root: Path, cfg) -> Path:
    """Return the compile_commands.json that a run without an explicit file reads.

    The file that ``[index] compile_commands`` names when it names a file of
    the user (:func:`explicit_compile_commands`), else the database of the
    build without variants (:func:`build_compile_commands`).  The file can
    be missing: the caller then runs the build.
    """
    explicit = explicit_compile_commands(project_root, cfg)
    if explicit is not None:
        return explicit
    return build_compile_commands(project_root, cfg.build, "")


def build_compile_commands(project_root: Path, build_cfg: BuildConfig, variant: str) -> Path:
    """Return the database of the only program that the build of *variant* makes.

    The backend says where its build system put the database in the output
    directory (``builders.output_compile_commands``).  When the build is not
    there, the answer is ``out/compile_commands.json``, the file that the
    build writes, thus the caller sees a missing file and runs the build.
    """
    root = project_root.resolve()
    out_dir = BuildLayout(root).out_dir(variant)
    system = build_cfg.system or detect_build_system(root)
    builder_cls = _builder_registry.get(system) if system else None
    found = output_compile_commands(builder_cls() if builder_cls else None, out_dir, build_cfg)
    if "" in found:
        return found[""]
    if len(found) == 1:
        return next(iter(found.values()))
    return out_dir / COMPILE_COMMANDS_NAME


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
