"""ESP-IDF build system — detection, build, and validation."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import cc_output_path, resolve_real_binary, run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class ESPIDFBuildSystem:
    """ESP-IDF build system (``idf.py build``).

    Detected by the presence of ``sdkconfig`` alongside a CMakeLists.txt
    that references ``idf_build``.  Registered AFTER the Mbed/Zephyr/PlatformIO
    builders so those take precedence in ambiguous setups.

    WHY ``idf.py build`` (not raw CMake): ESP-IDF uses CMake internally
    but the project structure is non-standard — CMakeLists.txt in an
    IDF-managed directory hierarchy.  ``idf.py build`` invokes CMake with
    ESP-IDF-specific toolchain paths and component discovery that a raw
    ``cmake`` call would miss.  It also handles the two-phase build
    (bootloader + app) and the ``sdkconfig`` configuration system.
    """

    name: str = "ESP-IDF"
    config_key: str = "esp-idf"
    markers: list[str] = ["sdkconfig"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        root = project_root.resolve()
        # Primary marker: sdkconfig in root (created by idf.py set-target)
        if not (root / "sdkconfig").exists():
            return False
        # Secondary: CMakeLists.txt references idf_build (distinguishes from generic CMake)
        cmake_file = root / "CMakeLists.txt"
        if cmake_file.exists():
            try:
                content = cmake_file.read_text(encoding="utf-8")
                if "idf_build" in content or "IDF" in content:
                    return True
            except OSError:
                pass
        # Fallback: sdkconfig alone is suggestive enough
        return True

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via ``idf.py build``."""
        idf_py = shutil.which("idf.py")
        if not idf_py:
            raise RuntimeError(
                "idf.py is required for ESP-IDF builds.  Install the ESP-IDF framework:\n"
                "  git clone --recursive https://github.com/espressif/esp-idf.git\n"
                "  cd esp-idf && ./install.sh && source export.sh"
            )

        cmd: list[str] = [idf_py, "build"]

        if cfg.clean:
            # idf.py fullclean removes all build artifacts
            clean_cmd = [idf_py, "fullclean"]
            log.info("esp-idf clean: %s", " ".join(clean_cmd))
            try:
                run_build_command(clean_cmd, cwd=project_root, description="idf.py fullclean", build_cfg=cfg)
            except RuntimeError:
                pass  # clean is best-effort — build dir may not exist yet

        # Ninja deletes .d depfiles after reading them by default.
        # Create a wrapper that adds -d keepdepfile so .d files persist
        # for incremental re-indexing.
        #
        # Resolve the REAL ninja binary, not a pyenv/asdf shim: the shim
        # re-execs `ninja` by name, and since the wrapper dir is prepended
        # to PATH below, that re-resolution would find the wrapper again
        # and recurse (appending -d keepdepfile each cycle).
        ninja = resolve_real_binary("ninja")
        if ninja is None:
            raise RuntimeError("ninja is required for ESP-IDF builds")
        wrapper_dir = project_root / ".fw-context"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        ninja_wrapper = wrapper_dir / "ninja"
        expected = f'#!/bin/sh\nexec "{ninja}" -d keepdepfile "$@"\n'
        if not ninja_wrapper.exists() or ninja_wrapper.read_text(encoding="utf-8") != expected:
            tmp_wrapper = wrapper_dir / ".ninja.tmp"
            tmp_wrapper.write_text(expected, encoding="utf-8")
            tmp_wrapper.chmod(0o755)
            tmp_wrapper.rename(ninja_wrapper)

        env = dict(os.environ)
        if cfg.idf_path:
            env["IDF_PATH"] = cfg.idf_path

        # CMake 3.30+ misparses file(TO_CMAKE_PATH $ENV{ESP_ROM_ELF_DIR} ...)
        # when ESP_ROM_ELF_DIR is unset or empty — it treats the first argument
        # as a path instead of a subcommand keyword.  Set a non-empty default
        # so ESP-IDF's gdbinit.cmake does not crash.
        if not os.environ.get("ESP_ROM_ELF_DIR"):
            env["ESP_ROM_ELF_DIR"] = "/dev/null"

        # Prepend our ninja wrapper directory to PATH so Ninja keeps .d files.
        # Also enable ccache depend_mode so .d files are regenerated on cache hits.
        env["PATH"] = f"{wrapper_dir}{os.pathsep}{env['PATH']}"
        env["CCACHE_DEPEND"] = "1"

        # Inject .d dependency tracking via EXTRA_CFLAGS / EXTRA_CXXFLAGS.
        # These environment variables are respected by the ESP-IDF build system
        # and appended to compiler flags without overriding the toolchain.
        env.pop("EXTRA_CFLAGS", None)  # Don't inherit from parent env
        env["EXTRA_CFLAGS"] = ""
        env.pop("EXTRA_CXXFLAGS", None)  # Don't inherit from parent env
        env["EXTRA_CXXFLAGS"] = ""
        if "-MMD" not in env["EXTRA_CFLAGS"]:
            env["EXTRA_CFLAGS"] += " -MMD" if env["EXTRA_CFLAGS"] else "-MMD"
        if "-MMD" not in env["EXTRA_CXXFLAGS"]:
            env["EXTRA_CXXFLAGS"] += " -MMD" if env["EXTRA_CXXFLAGS"] else "-MMD"

        # Ensure the target is configured — idf.py build without a prior
        # set-target fails when the cmake cache is missing (e.g. after fullclean
        # or a fresh clone).  set-target reads the target from sdkconfig.
        build_dir = project_root / "build"
        if not build_dir.exists():
            target = "esp32"
            sdkconfig = project_root / "sdkconfig"
            if sdkconfig.exists():
                import re
                try:
                    content = sdkconfig.read_text(encoding="utf-8")
                    m = re.search(r'CONFIG_IDF_TARGET="(\w+)"', content)
                    if m:
                        target = m.group(1)
                except OSError:
                    pass
            if target == "esp32":
                log.warning(
                    "Could not detect ESP-IDF target from sdkconfig — defaulting to esp32. "
                    "Set target explicitly with 'idf.py set-target <chip>' if using a different variant."
                )
            set_target_cmd = [idf_py, "set-target", target]
            log.info("esp-idf set-target: %s", " ".join(set_target_cmd))
            try:
                run_build_command(set_target_cmd, cwd=project_root, description="idf.py set-target", build_cfg=cfg)
            except RuntimeError:
                log.warning("idf.py set-target failed — build may use wrong target")

        log.info("esp-idf build: %s", " ".join(cmd))
        run_build_command(cmd, cwd=project_root, description="idf.py build", env=env, build_cfg=cfg)

        # ESP-IDF puts compile_commands.json in the build directory
        cc_in_build = project_root / "build" / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                "compile_commands.json not found in build/ directory. "
                "Ensure the ESP-IDF project was configured correctly."
            )

        # Copy to the gitignored fw-context build dir for a stable location
        target_cc = cc_output_path(project_root)
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        issues: list[BuildIssue] = []
        build_dir = project_root / "build"
        if not build_dir.exists():
            issues.append(
                BuildIssue(
                    severity="error",
                    category="missing_build_dir",
                    message="ESP-IDF build/ directory not found",
                    auto_fixable=False,
                    fix_hint="Run 'fw-context index --build' to compile the project.",
                )
            )
        return issues

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["idf.py"]

    # ── Environment auto-detection ──

    @classmethod
    def detect_environment(cls, project_root: Path) -> dict[str, str | None]:
        # EIM (Espressif Installation Manager) activation scripts take
        # priority.  They set IDF_PATH / IDF_TOOLS_PATH / the toolchain
        # PATH directly without running idf_tools.py's full install check,
        # which fails on target-only installs (e.g. ``eim install -t esp32``
        # without the RISC-V toolchain).  ``export.sh`` by contrast runs
        # that check and aborts the build when any declared tool is absent.
        tools_dir = Path.home() / ".espressif" / "tools"
        eim_scripts = sorted(tools_dir.glob("activate_idf_*.sh"), reverse=True)
        if eim_scripts:
            return {"python": None, "activate": str(eim_scripts[0])}

        idf_path = os.environ.get("IDF_PATH")
        if idf_path:
            export_sh = Path(idf_path) / "export.sh"
            if export_sh.exists():
                return {"python": None, "activate": str(export_sh)}

        for candidate in [
            Path.home() / "esp" / "esp-idf" / "export.sh",
            Path.home() / "esp-idf" / "export.sh",
        ]:
            if candidate.exists():
                return {"python": None, "activate": str(candidate)}

        if shutil.which("idf.py"):
            return {"python": None, "activate": None}

        return {"python": None, "activate": None}

    @classmethod
    def environment_help(cls) -> str:
        return (
            "ESP-IDF builds require an activated environment.\n"
            "Source the export script:\n"
            "  source ~/esp/esp-idf/export.sh\n"
            "Or set in .fw-context/local.toml:\n"
            '  [build]\n  activate = "~/esp/esp-idf/export.sh"'
        )


# Register
registry.register(ESPIDFBuildSystem)
