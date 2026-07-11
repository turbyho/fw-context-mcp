"""Build system detection and compile_commands.json generation.

Supports Mbed OS, Zephyr, and PlatformIO.  Auto-detects the build system
from project markers and runs the appropriate command to generate a fresh,
complete compile_commands.json.
"""

from __future__ import annotations

import logging
import shutil
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
    """Parse ``.mbed`` into a dict of KEY=VALUE pairs."""
    dotfile = project_root / ".mbed"
    result: dict[str, str] = {}
    if not dotfile.exists():
        return result
    for line in dotfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _mbed_target_from_custom_targets(project_root: Path) -> str | None:
    """Extract the first board name from custom_targets.json."""
    import json

    ct = project_root / "custom_targets.json"
    if not ct.exists():
        return None
    try:
        data = json.loads(ct.read_text(encoding="utf-8"))
        for key in data:
            if isinstance(data[key], dict) and "inherits" in data[key]:
                return key
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _resolve_mbed_extra_profiles(project_root: Path, extra: list[str]) -> list[str]:
    """Resolve extra profile paths — prefixes ``mbed-os/tools/profiles/extensions/``
    for bare filenames so users can write ``lto.json`` instead of the full path."""
    resolved: list[str] = []
    for p in extra:
        if "/" in p or p.startswith("."):
            resolved.append(p)
        else:
            candidate = f"mbed-os/tools/profiles/extensions/{p}"
            if (project_root / candidate).exists():
                resolved.append(candidate)
            else:
                resolved.append(p)  # pass through, let mbed CLI fail if wrong
    return resolved


def _build_mbed_os(project_root: Path, cfg: BuildConfig) -> Path:
    """Generate compile_commands.json via ``bear -- mbed compile --clean``."""
    if not shutil.which("bear"):
        raise RuntimeError(
            "bear is required to generate compile_commands.json. "
            "Install it:  sudo pacman -S bear   (or your distro's equivalent)"
        )

    dot = _parse_mbed_dotfile(project_root)
    target = cfg.target or dot.get("TARGET") or _mbed_target_from_custom_targets(project_root)
    toolchain = cfg.toolchain or dot.get("TOOLCHAIN") or "GCC_ARM"

    if not target:
        raise RuntimeError(
            "Cannot determine target board.  Set it in .mbed (mbed config target <BOARD>), "
            "in custom_targets.json, or in .fw-context/config.toml [build] target = \"...\""
        )

    cmd: list[str] = [
        "bear",
        "--output", "compile_commands.json",
        "--",
        "mbed", "compile",
        "-t", toolchain,
        "-m", target,
        "--profile", cfg.profile,
        "--app-config", cfg.app_config,
    ]

    for ep in _resolve_mbed_extra_profiles(project_root, cfg.extra_profiles):
        cmd += ["--profile", ep]

    for d in cfg.defines:
        cmd += ["-D", d]

    if cfg.clean:
        cmd.append("--clean")

    log.info("mbed-os build: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode != 0:
        raise RuntimeError(f"mbed compile failed with exit code {result.returncode}")

    cc_path = project_root / "compile_commands.json"
    if not cc_path.exists():
        raise RuntimeError("compile_commands.json was not generated — bear may have failed silently")

    return cc_path


# ---------------------------------------------------------------------------
# Zephyr helpers
# ---------------------------------------------------------------------------


def _build_zephyr(project_root: Path, cfg: BuildConfig) -> Path:
    """Generate compile_commands.json via ``west build``."""
    if not shutil.which("west"):
        raise RuntimeError(
            "west is required for Zephyr builds.  Install the Zephyr SDK and west tool."
        )

    if not cfg.board:
        raise RuntimeError(
            "Zephyr requires a board name.  Set it in .fw-context/config.toml:\n"
            "  [build]\n  board = \"your_board\""
        )

    build_dir = project_root / "build"

    cmd: list[str] = [
        "west", "build",
        "-b", cfg.board,
        "-d", str(build_dir),
    ]

    if cfg.clean:
        cmd.append("--pristine")
    cmd.append("--")
    cmd.append("-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")

    log.info("zephyr build: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode != 0:
        raise RuntimeError(f"west build failed with exit code {result.returncode}")

    cc_in_build = build_dir / "compile_commands.json"
    if not cc_in_build.exists():
        raise RuntimeError(
            "compile_commands.json not found in build directory. "
            "Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled."
        )

    # Copy to project root for consistency
    target_cc = project_root / "compile_commands.json"
    shutil.copy2(cc_in_build, target_cc)
    log.info("Copied %s → %s", cc_in_build, target_cc)

    return target_cc


# ---------------------------------------------------------------------------
# PlatformIO helpers
# ---------------------------------------------------------------------------


def _build_platformio(project_root: Path, cfg: BuildConfig) -> Path:
    """Generate compile_commands.json via ``pio run --target compiledb``."""
    if not shutil.which("pio") and not shutil.which("platformio"):
        raise RuntimeError(
            "PlatformIO CLI is required.  Install it:  pip install platformio"
        )

    pio_bin = "pio" if shutil.which("pio") else "platformio"

    cmd: list[str] = [pio_bin, "run", "--project-dir", str(project_root), "--target", "compiledb"]

    if cfg.clean:
        # pio run --target clean && pio run --target compiledb
        clean_cmd = [pio_bin, "run", "--project-dir", str(project_root), "--target", "clean"]
        log.info("platformio clean: %s", " ".join(clean_cmd))
        subprocess.run(clean_cmd, cwd=project_root)

    log.info("platformio build: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode != 0:
        raise RuntimeError(f"pio run --target compiledb failed with exit code {result.returncode}")

    cc_path = project_root / "compile_commands.json"
    if not cc_path.exists():
        raise RuntimeError("compile_commands.json was not generated — pio run may have failed silently")

    return cc_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_BUILDERS = {
    "mbed-os": _build_mbed_os,
    "zephyr": _build_zephyr,
    "platformio": _build_platformio,
}


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

    builder = _BUILDERS.get(system)
    if not builder:
        raise RuntimeError(
            f"Unknown build system '{system}'.  Supported: {', '.join(sorted(_BUILDERS))}"
        )

    log.info("Detected build system: %s (clean=%s)", system, cfg.clean)
    return builder(root, cfg)
