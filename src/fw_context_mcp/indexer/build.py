"""Build system detection and compile_commands.json generation.

Supports Mbed OS, Zephyr, and PlatformIO.  Auto-detects the build system
from project markers and runs the appropriate command to generate a fresh,
complete compile_commands.json.
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


@dataclass
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


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_build_system(project_root: Path) -> str | None:
    """Detect the build system from project markers.

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


def generate_compile_commands(
    project_root: Path,
    cfg: BuildConfig,
) -> Path:
    """Generate a fresh compile_commands.json and return its path.

    Auto-detects the build system when ``cfg.system`` is ``None``.
    Delegates to the registered ``BuildSystem`` implementation via
    ``BuildSystemRegistry``.
    Raises ``RuntimeError`` when detection or build fails.
    """
    root = project_root.resolve()

    # Full command override
    if cfg.command:
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
    return builder.build(root, cfg)
