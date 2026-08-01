"""Generic CMake build system — detection, build, and validation.

Used for any CMake-based project that isn't Zephyr or ESP-IDF.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from fw_context_mcp.utils import run_build_command

from . import registry
from .protocol import BuildIssue

if TYPE_CHECKING:
    from ..build import BuildConfig

log = logging.getLogger(__name__)


class GenericCMakeBuildSystem:
    """Generic CMake build system (``cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON``).

    Registered AFTER Zephyr and ESP-IDF so those specific builders match first.
    """

    name: str = "CMake (generic)"
    config_key: str = "cmake"
    markers: list[str] = ["CMakeLists.txt"]

    # ── Detection ──

    @classmethod
    def detect(cls, project_root: Path) -> bool:
        cmake_file = project_root.resolve() / "CMakeLists.txt"
        if not cmake_file.exists():
            return False
        # Exclude ESP-IDF and Zephyr — those are detected by their own builders
        try:
            content = cmake_file.read_text(encoding="utf-8")
            if "idf_build" in content or "IDF" in content:
                # Check for sdkconfig — if present, it's ESP-IDF territory
                if (project_root / "sdkconfig").exists():
                    return False
            if "find_package(Zephyr" in content or "zephyr" in content.lower():
                return False
        except OSError:
            pass
        return True

    # ── Build ──

    def build(self, project_root: Path, cfg: BuildConfig) -> Path:
        """Generate compile_commands.json via CMake configure + build."""
        if not shutil.which("cmake"):
            raise RuntimeError("cmake is required.  Install it:  sudo pacman -S cmake")

        build_dir = project_root / "build"

        # Configure
        configure_cmd: list[str] = [
            "cmake",
            "-B",
            str(build_dir),
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ]
        if cfg.cmake_generator:
            configure_cmd += ["-G", cfg.cmake_generator]

        if cfg.clean and build_dir.exists():
            shutil.rmtree(build_dir)

        log.info("cmake configure: %s", " ".join(configure_cmd))
        run_build_command(configure_cmd, cwd=project_root, description="cmake configure")

        # Build
        build_cmd: list[str] = ["cmake", "--build", str(build_dir)]
        log.info("cmake build: %s", " ".join(build_cmd))
        run_build_command(build_cmd, cwd=project_root, description="cmake build")

        cc_in_build = build_dir / "compile_commands.json"
        if not cc_in_build.exists():
            raise RuntimeError(
                "compile_commands.json not found in build/ directory. Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled."
            )

        # Copy to project root for consistency
        target_cc = project_root / "compile_commands.json"
        shutil.copy2(cc_in_build, target_cc)
        log.info("Copied %s → %s", cc_in_build, target_cc)

        return target_cc

    # ── Build dir patterns ──

    def get_build_dir_patterns(self, project_root: Path) -> list[str]:
        """Return build-output directory patterns for staleness filtering."""
        return ["build/", "cmake-build-"]

    # ── Validation ──

    def validate_artifacts(self, compile_commands: Path, project_root: Path) -> list[BuildIssue]:
        return []

    # ── Auto-fix ──

    def auto_fix(self, issue: BuildIssue, project_root: Path) -> bool:
        return False

    # ── Tools ──

    def required_tools(self) -> list[str]:
        return ["cmake"]


# Register
registry.register(GenericCMakeBuildSystem)
